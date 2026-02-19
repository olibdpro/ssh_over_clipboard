"""USB/serial framed transport backend."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import select
import struct
import termios
import threading
import time
from typing import Deque
import zlib

from .protocol import Message, decode_message, encode_message
from .transport import TransportError

_MAGIC = b"USBS"
_VERSION = 1
_TYPE_DATA = 1
_TYPE_ACK = 2
_HEADER = struct.Struct("!4sBBIII")


@dataclass
class USBSerialTransportConfig:
    serial_port: str | None = None
    serial_fd: int | None = None
    baud_rate: int = 3000000
    read_timeout: float = 0.005
    write_timeout: float = 0.02
    frame_max_bytes: int = 65536
    ack_timeout: float = 0.15
    max_retries: int = 20
    seen_seq_window: int = 4096
    configure_tty: bool = True


class USBSerialTransportError(TransportError):
    """Raised when the serial transport fails."""


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
    offset: int = 0


class USBSerialTransportBackend:
    """Framed full-duplex message transport over a serial file descriptor."""

    def __init__(self, config: USBSerialTransportConfig) -> None:
        if not config.serial_port and config.serial_fd is None:
            raise USBSerialTransportError("Either serial_port or serial_fd must be provided")
        if config.serial_port and config.serial_fd is not None:
            raise USBSerialTransportError("Provide serial_port or serial_fd, not both")

        self.config = config
        self._fd: int | None = None
        self._lock = threading.RLock()
        self._closed = False

        self._rx_buffer = bytearray()
        self._incoming_messages: list[Message] = []
        self._inbound_cursor = 0

        self._next_out_seq = 1
        self._pending: dict[int, _PendingFrame] = {}
        self._ack_frames: Deque[bytes] = deque()
        self._tx_queue: Deque[_TxItem] = deque()
        self._active_tx: _TxItem | None = None

        self._seen_inbound: set[int] = set()
        self._seen_order: Deque[int] = deque()

    def name(self) -> str:
        if self.config.serial_port:
            return f"usb-serial:{self.config.serial_port}"
        return "usb-serial:fd"

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
            raise USBSerialTransportError(
                f"Serialized message exceeds frame_max_bytes ({len(payload)} > {self.config.frame_max_bytes})"
            )

        with self._lock:
            self._ensure_open_locked()
            seq = self._next_out_seq
            self._next_out_seq += 1
            frame = self._build_frame(frame_type=_TYPE_DATA, seq=seq, payload=payload)
            self._pending[seq] = _PendingFrame(seq=seq, frame=frame)
            return message.msg_id

    def push_outbound(self) -> None:
        with self._lock:
            self._ensure_open_locked()
            self._read_available_locked()

            now = time.monotonic()
            self._enqueue_due_frames_locked(now)
            self._drain_tx_locked(deadline=now + max(self.config.write_timeout, 0.001))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fd = self._fd
            self._fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise USBSerialTransportError("Transport is closed")
        if self._fd is not None:
            return

        if self.config.serial_fd is not None:
            fd = os.dup(self.config.serial_fd)
        else:
            serial_path = Path(self.config.serial_port or "").expanduser()
            try:
                fd = os.open(
                    str(serial_path),
                    os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK,
                )
            except OSError as exc:
                raise USBSerialTransportError(f"Failed to open serial port {serial_path}: {exc}") from exc

        try:
            os.set_blocking(fd, False)
        except OSError as exc:
            os.close(fd)
            raise USBSerialTransportError(f"Failed to set nonblocking mode on serial fd: {exc}") from exc

        if self.config.configure_tty:
            self._configure_tty(fd)

        self._fd = fd

    def _configure_tty(self, fd: int) -> None:
        try:
            attrs = termios.tcgetattr(fd)
        except termios.error:
            # Some fd types in tests are non-tty streams.
            return

        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = attrs[2] | termios.CLOCAL | termios.CREAD
        attrs[2] = attrs[2] & ~termios.PARENB
        attrs[2] = attrs[2] & ~termios.CSTOPB
        attrs[2] = attrs[2] & ~termios.CSIZE
        attrs[2] = attrs[2] | termios.CS8
        attrs[3] = 0

        speed = self._termios_speed(self.config.baud_rate)
        if speed is not None:
            attrs[4] = speed
            attrs[5] = speed

        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0

        try:
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error as exc:
            raise USBSerialTransportError(f"Failed to configure serial TTY attributes: {exc}") from exc

    def _termios_speed(self, baud_rate: int) -> int | None:
        candidates = [
            f"B{baud_rate}",
            "B3000000",
            "B2000000",
            "B1000000",
            "B921600",
            "B460800",
            "B230400",
            "B115200",
        ]
        for name in candidates:
            value = getattr(termios, name, None)
            if value is not None:
                return int(value)
        return None

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
                    raise USBSerialTransportError(
                        f"Frame seq={seq} was not acknowledged after {pending.attempts} retransmissions"
                    )

            pending.queued = True
            pending.next_retry_at = now + max(self.config.ack_timeout, 0.01)
            self._tx_queue.append(_TxItem(seq=pending.seq, frame=pending.frame))

    def _drain_tx_locked(self, *, deadline: float) -> None:
        fd = self._fd
        if fd is None:
            return

        while time.monotonic() < deadline:
            if self._active_tx is None:
                if not self._tx_queue:
                    return
                self._active_tx = self._tx_queue.popleft()

            item = self._active_tx
            assert item is not None

            remaining = item.frame[item.offset :]
            if not remaining:
                self._mark_tx_item_complete_locked(item)
                self._active_tx = None
                continue

            try:
                written = os.write(fd, remaining)
            except BlockingIOError:
                wait = max(deadline - time.monotonic(), 0.0)
                if wait <= 0:
                    return
                select.select([], [fd], [], wait)
                continue
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    wait = max(deadline - time.monotonic(), 0.0)
                    if wait <= 0:
                        return
                    select.select([], [fd], [], wait)
                    continue
                raise USBSerialTransportError(f"Serial write failed: {exc}") from exc

            if written <= 0:
                return
            item.offset += written
            if item.offset >= len(item.frame):
                self._mark_tx_item_complete_locked(item)
                self._active_tx = None

    def _mark_tx_item_complete_locked(self, item: _TxItem) -> None:
        if item.seq is None:
            return
        pending = self._pending.get(item.seq)
        if pending is not None:
            pending.queued = False

    def _read_available_locked(self) -> None:
        fd = self._fd
        if fd is None:
            return

        timeout = max(self.config.read_timeout, 0.0)
        if timeout > 0.0:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return

        # Bound a single poll call to keep runtime predictable under heavy streams.
        for _ in range(32):
            try:
                chunk = os.read(fd, max(self.config.frame_max_bytes, 1024))
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    break
                raise USBSerialTransportError(f"Serial read failed: {exc}") from exc

            if not chunk:
                break

            self._rx_buffer.extend(chunk)
            self._parse_rx_buffer_locked()

    def _parse_rx_buffer_locked(self) -> None:
        while len(self._rx_buffer) >= _HEADER.size:
            if self._rx_buffer[:4] != _MAGIC:
                marker = self._rx_buffer.find(_MAGIC, 1)
                if marker < 0:
                    del self._rx_buffer[:-3]
                    return
                del self._rx_buffer[:marker]
                continue

            header = bytes(self._rx_buffer[: _HEADER.size])
            magic, version, frame_type, seq, payload_len, payload_crc = _HEADER.unpack(header)
            if magic != _MAGIC or version != _VERSION:
                del self._rx_buffer[0]
                continue
            if payload_len > max(self.config.frame_max_bytes, 1):
                del self._rx_buffer[0]
                continue

            frame_size = _HEADER.size + payload_len
            if len(self._rx_buffer) < frame_size:
                return

            payload = bytes(self._rx_buffer[_HEADER.size : frame_size])
            del self._rx_buffer[:frame_size]

            if frame_type == _TYPE_ACK:
                self._pending.pop(seq, None)
                continue

            if frame_type != _TYPE_DATA:
                continue

            if (zlib.crc32(payload) & 0xFFFFFFFF) != payload_crc:
                continue

            self._ack_frames.append(self._build_frame(frame_type=_TYPE_ACK, seq=seq, payload=b""))

            if self._seen_seq(seq):
                continue

            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue

            message = decode_message(text)
            if message is None:
                continue
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

    def _build_frame(self, *, frame_type: int, seq: int, payload: bytes) -> bytes:
        payload_crc = zlib.crc32(payload) & 0xFFFFFFFF if frame_type == _TYPE_DATA else 0
        header = _HEADER.pack(
            _MAGIC,
            _VERSION,
            frame_type,
            seq,
            len(payload),
            payload_crc,
        )
        return header + payload
