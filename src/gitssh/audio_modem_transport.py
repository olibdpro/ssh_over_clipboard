"""Audio modem transport backend."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import struct
import sys
import threading
import time
from typing import Callable, Deque
import zlib

from .audio_io_ffmpeg import AudioDuplexIO, AudioIOError, build_audio_duplex_io
from .audio_modem import (
    MODULATION_ROBUST_V1,
    AudioModulationCodec,
    create_audio_frame_codec,
    normalize_audio_modulation,
)
from .protocol import Message, decode_message, encode_message
from .transport import TransportError

_LINK_MAGIC = b"AUDM"
_LINK_VERSION = 1
_LINK_TYPE_DATA = 1
_LINK_TYPE_ACK = 2
_LINK_HEADER = struct.Struct("!4sBBIII")


class AudioModemTransportError(TransportError):
    """Raised when audio-modem transport operations fail."""


@dataclass
class AudioModemTransportConfig:
    input_device: str
    output_device: str
    sample_rate: int = 48000
    read_timeout: float = 0.01
    write_timeout: float = 0.05
    frame_max_bytes: int = 65536
    ack_timeout: float = 0.2
    max_retries: int = 32
    seen_seq_window: int = 4096
    byte_repeat: int = 3
    marker_run: int = 16
    audio_modulation: str = "auto"
    ffmpeg_bin: str = "ffmpeg"
    audio_backend: str = "auto"
    verbose: bool = False
    io_factory: Callable[["AudioModemTransportConfig"], AudioDuplexIO] | None = None


@dataclass
class _PendingFrame:
    seq: int
    frame: bytes
    queued: bool = False
    attempts: int = 0
    next_retry_at: float = 0.0


@dataclass
class _TxItem:
    seq: int | None
    frame: bytes


class AudioModemTransportBackend:
    """Reliable message transport over audio modem framing."""

    def __init__(self, config: AudioModemTransportConfig) -> None:
        self.config = config
        self._io: AudioDuplexIO | None = None
        normalized_modulation = normalize_audio_modulation(config.audio_modulation, allow_auto=True)
        self._effective_modulation = (
            MODULATION_ROBUST_V1 if normalized_modulation == "auto" else normalized_modulation
        )
        self._codec: AudioModulationCodec = create_audio_frame_codec(
            modulation=self._effective_modulation,
            sample_rate=max(config.sample_rate, 8000),
            byte_repeat=max(config.byte_repeat, 1),
            marker_run=max(config.marker_run, 4),
        )
        self._last_codec_log_at = 0.0
        self._lock = threading.RLock()
        self._closed = False

        self._incoming_messages: list[Message] = []
        self._inbound_cursor = 0

        self._next_out_seq = 1
        self._pending: dict[int, _PendingFrame] = {}
        self._ack_frames: Deque[bytes] = deque()
        self._tx_queue: Deque[_TxItem] = deque()

        self._seen_inbound: set[int] = set()
        self._seen_order: Deque[int] = deque()

    def name(self) -> str:
        return (
            f"audio-modem:{self.config.audio_backend}:{self._effective_modulation}:"
            f"in={self.config.input_device},out={self.config.output_device}"
        )

    def snapshot_inbound_cursor(self) -> str | None:
        with self._lock:
            return str(self._inbound_cursor)

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        del cursor
        with self._lock:
            self._ensure_open_locked()
            self._read_available_locked()
            messages = self._incoming_messages
            self._incoming_messages = []
            self._inbound_cursor += len(messages)
            return messages, str(self._inbound_cursor)

    def fetch_inbound(self) -> None:
        with self._lock:
            self._ensure_open_locked()
            self._read_available_locked()

    def write_outbound_message(self, message: Message) -> str:
        payload = encode_message(message).encode("utf-8")
        if len(payload) > self.config.frame_max_bytes:
            raise AudioModemTransportError(
                f"Serialized message exceeds frame_max_bytes ({len(payload)} > {self.config.frame_max_bytes})"
            )

        with self._lock:
            self._ensure_open_locked()
            seq = self._next_out_seq
            self._next_out_seq += 1
            frame = self._build_link_frame(frame_type=_LINK_TYPE_DATA, seq=seq, payload=payload)
            self._pending[seq] = _PendingFrame(seq=seq, frame=frame)
            return message.msg_id

    def push_outbound(self) -> None:
        with self._lock:
            self._ensure_open_locked()
            self._read_available_locked()

            now = time.monotonic()
            self._enqueue_due_frames_locked(now)
            self._write_due_frames_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            io_obj = self._io
            self._io = None
            if io_obj is not None:
                io_obj.close()

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise AudioModemTransportError("Transport is closed")
        if self._io is not None:
            return
        try:
            if self.config.io_factory is not None:
                self._io = self.config.io_factory(self.config)
            else:
                self._io = build_audio_duplex_io(
                    ffmpeg_bin=self.config.ffmpeg_bin,
                    backend=self.config.audio_backend,
                    input_device=self.config.input_device,
                    output_device=self.config.output_device,
                    sample_rate=max(self.config.sample_rate, 8000),
                    read_timeout=max(self.config.read_timeout, 0.0),
                    write_timeout=max(self.config.write_timeout, 0.001),
                )
        except AudioIOError as exc:
            raise AudioModemTransportError(str(exc)) from exc

    def _read_available_locked(self) -> None:
        io_obj = self._io
        if io_obj is None:
            return

        # Bound work per cycle to keep client/server loops responsive.
        for _ in range(32):
            try:
                pcm = io_obj.read(4096)
            except AudioIOError as exc:
                raise AudioModemTransportError(f"Audio read failed: {exc}") from exc
            if not pcm:
                break
            for raw in self._codec.feed_pcm(pcm):
                if raw:
                    self._handle_link_frame(raw)

        self._maybe_log_codec_stats()

    def _enqueue_due_frames_locked(self, now: float) -> None:
        while self._ack_frames:
            self._tx_queue.append(_TxItem(seq=None, frame=self._ack_frames.popleft()))

        for seq in sorted(self._pending):
            pending = self._pending.get(seq)
            if pending is None or pending.queued:
                continue
            if now < pending.next_retry_at:
                continue

            if pending.next_retry_at > 0.0:
                pending.attempts += 1
                if pending.attempts > max(self.config.max_retries, 1):
                    raise AudioModemTransportError(
                        f"Frame seq={seq} was not acknowledged after {pending.attempts} retransmissions"
                    )

            pending.queued = True
            pending.next_retry_at = now + max(self.config.ack_timeout, 0.01)
            self._tx_queue.append(_TxItem(seq=pending.seq, frame=pending.frame))

    def _write_due_frames_locked(self) -> None:
        io_obj = self._io
        if io_obj is None:
            return
        while self._tx_queue:
            item = self._tx_queue.popleft()
            pcm = self._codec.encode_frame(item.frame)
            try:
                io_obj.write(pcm)
            except AudioIOError as exc:
                if item.seq is None:
                    # Keep ACK frames queued so a transient sink stall does not lose them.
                    self._tx_queue.appendleft(item)
                else:
                    pending = self._pending.get(item.seq)
                    if pending is not None:
                        # Allow retransmission after transient write failures.
                        pending.queued = False
                raise AudioModemTransportError(f"Audio write failed: {exc}") from exc
            if item.seq is not None:
                pending = self._pending.get(item.seq)
                if pending is not None:
                    pending.queued = False

    def _handle_link_frame(self, frame: bytes) -> None:
        if len(frame) < _LINK_HEADER.size:
            return
        header = frame[: _LINK_HEADER.size]
        magic, version, frame_type, seq, payload_len, payload_crc = _LINK_HEADER.unpack(header)
        if magic != _LINK_MAGIC or version != _LINK_VERSION:
            return
        if payload_len > self.config.frame_max_bytes:
            return
        if len(frame) != _LINK_HEADER.size + payload_len:
            return
        payload = frame[_LINK_HEADER.size :]

        if frame_type == _LINK_TYPE_ACK:
            self._pending.pop(seq, None)
            return

        if frame_type != _LINK_TYPE_DATA:
            return

        if (zlib.crc32(payload) & 0xFFFFFFFF) != payload_crc:
            return

        self._ack_frames.append(self._build_link_frame(frame_type=_LINK_TYPE_ACK, seq=seq, payload=b""))

        if self._seen_seq(seq):
            return

        text = payload.decode("utf-8", errors="ignore")
        message = decode_message(text)
        if message is None:
            return
        self._incoming_messages.append(message)

    def _seen_seq(self, seq: int) -> bool:
        if seq in self._seen_inbound:
            return True

        self._seen_inbound.add(seq)
        self._seen_order.append(seq)

        max_seen = max(self.config.seen_seq_window, 1)
        while len(self._seen_order) > max_seen:
            evicted = self._seen_order.popleft()
            self._seen_inbound.discard(evicted)
        return False

    def _build_link_frame(self, *, frame_type: int, seq: int, payload: bytes) -> bytes:
        payload_crc = zlib.crc32(payload) & 0xFFFFFFFF if frame_type == _LINK_TYPE_DATA else 0
        header = _LINK_HEADER.pack(
            _LINK_MAGIC,
            _LINK_VERSION,
            frame_type,
            seq,
            len(payload),
            payload_crc,
        )
        return header + payload

    def _maybe_log_codec_stats(self) -> None:
        if not self.config.verbose:
            return

        now = time.monotonic()
        if (now - self._last_codec_log_at) < 2.0:
            return

        self._last_codec_log_at = now
        stats = self._codec.snapshot_stats()
        print(
            "[audio-modem] "
            f"modulation={self._effective_modulation} "
            f"frames_decoded={stats.get('frames_decoded', 0)} "
            f"sync_hits={stats.get('sync_hits', 0)} "
            f"crc_failures={stats.get('crc_failures', 0)} "
            f"decode_failures={stats.get('decode_failures', 0)}",
            file=sys.stderr,
        )
