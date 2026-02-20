"""Automatic device discovery for audio-modem transport."""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Callable
import uuid

from .audio_io_ffmpeg import AudioDuplexIO, AudioIOError, _list_pulse_devices, build_audio_duplex_io
from .audio_modem import AudioFrameCodec

_PING_KIND = "ping"
_PONG_KIND = "pong"
_FOUND_KIND = "found"
_FOUND_ACK_KIND = "found_ack"


@dataclass(frozen=True)
class AudioDiscoveryConfig:
    ffmpeg_bin: str = "ffmpeg"
    audio_backend: str = "auto"
    sample_rate: int = 48000
    read_timeout: float = 0.01
    write_timeout: float = 0.05
    ping_interval: float = 0.25
    timeout: float = 45.0
    idle_sleep: float = 0.01
    byte_repeat: int = 3
    marker_run: int = 16


@dataclass(frozen=True)
class DiscoveredAudioDevices:
    input_device: str
    output_device: str
    peer_input_device: str | None = None
    peer_output_device: str | None = None


@dataclass
class _WriterChannel:
    output_device: str
    io_obj: AudioDuplexIO
    codec: AudioFrameCodec
    next_ping_at: float = 0.0


@dataclass
class _ListenerChannel:
    input_device: str
    io_obj: AudioDuplexIO
    codec: AudioFrameCodec


def list_pulse_audio_devices() -> tuple[list[str], list[str]]:
    """Return Pulse/PipeWire capture sources and playback sinks."""

    inputs, _input_raw = _list_pulse_devices("source")
    outputs, _output_raw = _list_pulse_devices("sink")
    return _dedupe(inputs), _dedupe(outputs)


def discover_audio_devices(
    config: AudioDiscoveryConfig,
    *,
    input_devices: list[str] | None = None,
    output_devices: list[str] | None = None,
    io_factory: Callable[[str, str], AudioDuplexIO] | None = None,
    logger: Callable[[str], None] | None = None,
) -> DiscoveredAudioDevices:
    """Probe all local audio devices and return the first confirmed bidirectional pair."""

    if input_devices is None or output_devices is None:
        listed_inputs, listed_outputs = list_pulse_audio_devices()
        if input_devices is None:
            input_devices = listed_inputs
        if output_devices is None:
            output_devices = listed_outputs

    input_devices = _dedupe(input_devices)
    output_devices = _dedupe(output_devices)

    if not input_devices:
        raise AudioIOError("Audio discovery found no input devices.")
    if not output_devices:
        raise AudioIOError("Audio discovery found no output devices.")

    log = logger if logger is not None else (lambda _text: None)
    log(
        "audio discovery starting with "
        f"{len(input_devices)} input(s) and {len(output_devices)} output(s)"
    )

    first_input = input_devices[0]
    first_output = output_devices[0]
    writers: list[_WriterChannel] = []
    listeners: list[_ListenerChannel] = []
    open_errors: list[str] = []

    try:
        for output_device in output_devices:
            try:
                io_obj = _open_discovery_io(
                    config=config,
                    input_device=first_input,
                    output_device=output_device,
                    io_factory=io_factory,
                )
            except AudioIOError as exc:
                open_errors.append(f"output '{output_device}': {exc}")
                continue

            writers.append(
                _WriterChannel(
                    output_device=output_device,
                    io_obj=io_obj,
                    codec=AudioFrameCodec(
                        byte_repeat=max(config.byte_repeat, 1),
                        marker_run=max(config.marker_run, 4),
                    ),
                )
            )

        for input_device in input_devices:
            try:
                io_obj = _open_discovery_io(
                    config=config,
                    input_device=input_device,
                    output_device=first_output,
                    io_factory=io_factory,
                )
            except AudioIOError as exc:
                open_errors.append(f"input '{input_device}': {exc}")
                continue

            listeners.append(
                _ListenerChannel(
                    input_device=input_device,
                    io_obj=io_obj,
                    codec=AudioFrameCodec(
                        byte_repeat=max(config.byte_repeat, 1),
                        marker_run=max(config.marker_run, 4),
                    ),
                )
            )

        if not writers:
            details = "\n".join(f"- {line}" for line in open_errors) if open_errors else "- unknown error"
            raise AudioIOError(f"Audio discovery could not open any output device:\n{details}")
        if not listeners:
            details = "\n".join(f"- {line}" for line in open_errors) if open_errors else "- unknown error"
            raise AudioIOError(f"Audio discovery could not open any input device:\n{details}")

        local_id = uuid.uuid4().hex
        pending_pings: dict[str, tuple[str, float]] = {}
        selected_devices: DiscoveredAudioDevices | None = None
        selected_peer_id: str | None = None
        selected_ack_received = False
        next_found_at = 0.0
        deadline = time.monotonic() + max(config.timeout, 1.0)

        while time.monotonic() < deadline:
            now = time.monotonic()

            for writer in list(writers):
                if now < writer.next_ping_at:
                    continue

                nonce = uuid.uuid4().hex
                pending_pings[nonce] = (writer.output_device, now)
                ping = {
                    "kind": _PING_KIND,
                    "sender": local_id,
                    "nonce": nonce,
                    "tx_device": writer.output_device,
                }
                if not _send_discovery_frame(writer, ping):
                    _safe_close(writer.io_obj)
                    writers.remove(writer)
                    continue
                writer.next_ping_at = now + max(config.ping_interval, 0.01)

            if not writers:
                raise AudioIOError("Audio discovery lost all usable output devices while probing.")

            if selected_devices is not None and selected_peer_id is not None and now >= next_found_at:
                found = {
                    "kind": _FOUND_KIND,
                    "sender": local_id,
                    "target": selected_peer_id,
                    "tx_device": selected_devices.output_device,
                    "rx_device": selected_devices.input_device,
                }
                for writer in list(writers):
                    if _send_discovery_frame(writer, found):
                        continue
                    _safe_close(writer.io_obj)
                    writers.remove(writer)
                next_found_at = now + max(config.ping_interval, 0.05)

            for listener in list(listeners):
                try:
                    event = _read_and_process_listener(
                        listener=listener,
                        writers=writers,
                        local_id=local_id,
                        pending_pings=pending_pings,
                        selected_devices=selected_devices,
                        selected_peer_id=selected_peer_id,
                    )
                except AudioIOError as exc:
                    _safe_close(listener.io_obj)
                    listeners.remove(listener)
                    log(f"audio discovery disabled input '{listener.input_device}': {exc}")
                    continue

                if event.candidate is not None and selected_devices is None:
                    selected_devices = event.candidate
                    selected_peer_id = event.candidate_peer_id
                    next_found_at = 0.0
                    log(
                        "audio discovery candidate selected: "
                        f"in={selected_devices.input_device}, out={selected_devices.output_device}"
                    )

                if event.peer_found_ack:
                    selected_ack_received = True

            if not listeners:
                raise AudioIOError("Audio discovery lost all usable input devices while probing.")

            if selected_devices is not None and selected_ack_received:
                return selected_devices

            ttl = max(config.timeout * 0.5, 5.0)
            cutoff = now - ttl
            for nonce in [n for n, (_output, ts) in pending_pings.items() if ts < cutoff]:
                pending_pings.pop(nonce, None)

            time.sleep(max(config.idle_sleep, 0.0))

        raise AudioIOError(
            "Timed out while probing audio devices. "
            "No bidirectional ping/pong path was found."
        )
    finally:
        for writer in writers:
            _safe_close(writer.io_obj)
        for listener in listeners:
            _safe_close(listener.io_obj)


@dataclass(frozen=True)
class _ListenerEvent:
    candidate: DiscoveredAudioDevices | None = None
    candidate_peer_id: str | None = None
    peer_found_ack: bool = False


def _read_and_process_listener(
    *,
    listener: _ListenerChannel,
    writers: list[_WriterChannel],
    local_id: str,
    pending_pings: dict[str, tuple[str, float]],
    selected_devices: DiscoveredAudioDevices | None,
    selected_peer_id: str | None,
) -> _ListenerEvent:
    for _ in range(8):
        try:
            pcm = listener.io_obj.read(4096)
        except AudioIOError:
            raise
        except Exception as exc:
            raise AudioIOError(f"Audio discovery read failed: {exc}") from exc
        if not pcm:
            break

        for raw in listener.codec.feed_pcm(pcm):
            message = _decode_discovery_payload(raw)
            if message is None:
                continue

            kind = message.get("kind")
            sender = message.get("sender")
            if not isinstance(kind, str) or not isinstance(sender, str):
                continue
            if sender == local_id:
                continue

            if kind == _PING_KIND:
                nonce = message.get("nonce")
                if not isinstance(nonce, str) or not nonce:
                    continue
                for writer in list(writers):
                    pong = {
                        "kind": _PONG_KIND,
                        "sender": local_id,
                        "target": sender,
                        "echo_nonce": nonce,
                        "tx_device": writer.output_device,
                        "rx_device": listener.input_device,
                    }
                    if _send_discovery_frame(writer, pong):
                        continue
                    _safe_close(writer.io_obj)
                    writers.remove(writer)
                continue

            if kind == _FOUND_KIND:
                target = message.get("target")
                if target != local_id:
                    continue
                if selected_devices is None or selected_peer_id is None or sender != selected_peer_id:
                    continue
                found_ack = {
                    "kind": _FOUND_ACK_KIND,
                    "sender": local_id,
                    "target": sender,
                    "tx_device": selected_devices.output_device,
                    "rx_device": selected_devices.input_device,
                }
                for writer in list(writers):
                    if _send_discovery_frame(writer, found_ack):
                        continue
                    _safe_close(writer.io_obj)
                    writers.remove(writer)
                continue

            if kind == _FOUND_ACK_KIND:
                target = message.get("target")
                if target != local_id:
                    continue
                if selected_peer_id is None or sender != selected_peer_id:
                    continue
                return _ListenerEvent(peer_found_ack=True)

            if kind == _PONG_KIND:
                target = message.get("target")
                echo_nonce = message.get("echo_nonce")
                remote_output = message.get("tx_device")
                remote_input = message.get("rx_device")
                if target != local_id:
                    continue
                if not isinstance(echo_nonce, str) or not echo_nonce:
                    continue
                local = pending_pings.get(echo_nonce)
                if local is None:
                    continue
                local_output = local[0]
                peer_output = remote_output if isinstance(remote_output, str) and remote_output else None
                peer_input = remote_input if isinstance(remote_input, str) and remote_input else None
                return _ListenerEvent(
                    candidate=DiscoveredAudioDevices(
                        input_device=listener.input_device,
                        output_device=local_output,
                        peer_input_device=peer_input,
                        peer_output_device=peer_output,
                    ),
                    candidate_peer_id=sender,
                )
    return _ListenerEvent()


def _send_discovery_frame(writer: _WriterChannel, payload: dict[str, str]) -> bool:
    encoded = _encode_discovery_payload(payload)
    if encoded is None:
        return False
    try:
        writer.io_obj.write(writer.codec.encode_frame(encoded))
    except AudioIOError:
        return False
    return True


def _encode_discovery_payload(payload: dict[str, str]) -> bytes | None:
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _decode_discovery_payload(payload: bytes) -> dict[str, str] | None:
    if not payload:
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    clean: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            clean[key] = value
    return clean


def _open_discovery_io(
    *,
    config: AudioDiscoveryConfig,
    input_device: str,
    output_device: str,
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
) -> AudioDuplexIO:
    if io_factory is not None:
        try:
            return io_factory(input_device, output_device)
        except AudioIOError:
            raise
        except Exception as exc:
            raise AudioIOError(f"Audio discovery I/O factory failed: {exc}") from exc
    try:
        return build_audio_duplex_io(
            ffmpeg_bin=config.ffmpeg_bin,
            backend=config.audio_backend,
            input_device=input_device,
            output_device=output_device,
            sample_rate=max(config.sample_rate, 8000),
            read_timeout=max(config.read_timeout, 0.0),
            write_timeout=max(config.write_timeout, 0.001),
        )
    except AudioIOError:
        raise
    except Exception as exc:
        raise AudioIOError(f"Audio discovery failed to open duplex stream: {exc}") from exc


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = item.strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _safe_close(io_obj: AudioDuplexIO) -> None:
    try:
        io_obj.close()
    except Exception:
        pass
