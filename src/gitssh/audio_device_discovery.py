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
    ping_interval: float = 0.12
    found_interval: float = 0.12
    timeout: float = 90.0
    candidate_grace: float = 20.0
    max_silent_seconds: float = 10.0
    progress_log_interval: float = 2.0
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
    input_device: str
    output_device: str
    io_obj: AudioDuplexIO
    codec: AudioFrameCodec
    next_ping_at: float = 0.0
    last_activity: float = 0.0


@dataclass
class _ListenerChannel:
    input_device: str
    output_device: str
    io_obj: AudioDuplexIO
    codec: AudioFrameCodec
    last_activity: float = 0.0


@dataclass
class _DiscoveryStats:
    pings_sent: int = 0
    pongs_rx: int = 0
    found_sent: int = 0
    found_rx: int = 0
    found_ack_rx: int = 0
    frames_rx: int = 0


@dataclass
class _ListenerEvent:
    candidate: DiscoveredAudioDevices | None = None
    candidate_peer_id: str | None = None
    peer_found_ack: bool = False
    pongs_rx: int = 0
    found_rx: int = 0
    found_ack_rx: int = 0
    frames_rx: int = 0


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
        f"{len(input_devices)} input(s), {len(output_devices)} output(s) "
        f"(timeout={max(config.timeout, 1.0):.1f}s, ping_interval={max(config.ping_interval, 0.01):.3f}s)"
    )

    open_errors: list[str] = []
    disabled_channels: list[str] = []

    writers = _create_writer_channels(
        config=config,
        input_devices=input_devices,
        output_devices=output_devices,
        io_factory=io_factory,
        open_errors=open_errors,
    )
    listeners = _create_listener_channels(
        config=config,
        input_devices=input_devices,
        output_devices=output_devices,
        io_factory=io_factory,
        open_errors=open_errors,
    )
    target_writer_channels = len(output_devices)
    target_listener_channels = len(input_devices)
    log(
        "audio discovery channels opened: "
        f"writer_channels={len(writers)}/{target_writer_channels}, "
        f"listener_channels={len(listeners)}/{target_listener_channels}"
    )

    try:
        if not writers:
            details = "\n".join(f"- {line}" for line in open_errors) if open_errors else "- unknown error"
            raise AudioIOError(f"Audio discovery could not open any writer channel:\n{details}")
        if not listeners:
            details = "\n".join(f"- {line}" for line in open_errors) if open_errors else "- unknown error"
            raise AudioIOError(f"Audio discovery could not open any listener channel:\n{details}")

        local_id = uuid.uuid4().hex
        pending_pings: dict[str, tuple[str, float]] = {}
        stats = _DiscoveryStats()
        selected_devices: DiscoveredAudioDevices | None = None
        selected_peer_id: str | None = None
        selected_ack_received = False
        selected_at: float | None = None
        next_found_at = 0.0

        deadline = time.monotonic() + max(config.timeout, 1.0)
        candidate_deadline = deadline
        last_progress_log = 0.0

        def disable_writer(channel: _WriterChannel, reason: str) -> None:
            if channel not in writers:
                return
            writers.remove(channel)
            _safe_close(channel.io_obj)
            detail = (
                f"writer channel out='{channel.output_device}' anchor_in='{channel.input_device}' "
                f"disabled: {reason}"
            )
            disabled_channels.append(detail)
            log(f"audio discovery {detail}")

        def disable_listener(channel: _ListenerChannel, reason: str) -> None:
            if channel not in listeners:
                return
            listeners.remove(channel)
            _safe_close(channel.io_obj)
            detail = (
                f"listener channel in='{channel.input_device}' anchor_out='{channel.output_device}' "
                f"disabled: {reason}"
            )
            disabled_channels.append(detail)
            log(f"audio discovery {detail}")

        while True:
            now = time.monotonic()
            if now >= deadline and (
                selected_devices is None or selected_ack_received or now >= candidate_deadline
            ):
                raise AudioIOError(
                    _format_timeout_error(
                        input_devices=input_devices,
                        output_devices=output_devices,
                        stats=stats,
                        open_errors=open_errors,
                        disabled_channels=disabled_channels,
                        pending_pings=pending_pings,
                        writers=writers,
                        listeners=listeners,
                        selected_devices=selected_devices,
                        selected_peer_id=selected_peer_id,
                        selected_at=selected_at,
                        now=now,
                    )
                )

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

                send_error = _send_discovery_frame(writer, ping)
                if send_error is not None:
                    disable_writer(writer, send_error)
                    continue

                stats.pings_sent += 1
                writer.last_activity = now
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
                    send_error = _send_discovery_frame(writer, found)
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    stats.found_sent += 1
                    writer.last_activity = now
                next_found_at = now + max(config.found_interval, 0.01)

            for listener in list(listeners):
                try:
                    event = _read_and_process_listener(
                        listener=listener,
                        writers=writers,
                        local_id=local_id,
                        pending_pings=pending_pings,
                        selected_devices=selected_devices,
                        selected_peer_id=selected_peer_id,
                        now=now,
                        disable_writer=disable_writer,
                    )
                except AudioIOError as exc:
                    disable_listener(listener, str(exc))
                    continue

                stats.frames_rx += event.frames_rx
                stats.pongs_rx += event.pongs_rx
                stats.found_rx += event.found_rx
                stats.found_ack_rx += event.found_ack_rx

                if event.candidate is not None and selected_devices is None:
                    selected_devices = event.candidate
                    selected_peer_id = event.candidate_peer_id
                    selected_at = now
                    candidate_deadline = max(deadline, now + max(config.candidate_grace, 0.0))
                    next_found_at = 0.0
                    log(
                        "audio discovery candidate selected: "
                        f"in={selected_devices.input_device}, out={selected_devices.output_device}, "
                        f"peer={selected_peer_id}"
                    )

                if event.peer_found_ack:
                    selected_ack_received = True
                    log("audio discovery confirmed by peer acknowledgement")
                    break

            if not listeners:
                raise AudioIOError("Audio discovery lost all usable input devices while probing.")

            if selected_devices is not None and selected_ack_received:
                return selected_devices

            ttl = max(config.max_silent_seconds, 1.0)
            cutoff = now - ttl
            for nonce in [n for n, (_output, ts) in pending_pings.items() if ts < cutoff]:
                pending_pings.pop(nonce, None)

            if (now - last_progress_log) >= max(config.progress_log_interval, 0.5):
                last_progress_log = now
                log(
                    "audio discovery progress: "
                    f"active_listeners={len(listeners)} active_writers={len(writers)} "
                    f"pings_sent={stats.pings_sent} pongs_rx={stats.pongs_rx} "
                    f"found_sent={stats.found_sent} found_ack_rx={stats.found_ack_rx} "
                    f"pending_pings={len(pending_pings)}"
                )

            time.sleep(max(config.idle_sleep, 0.0))
    finally:
        for writer in writers:
            _safe_close(writer.io_obj)
        for listener in listeners:
            _safe_close(listener.io_obj)


def _read_and_process_listener(
    *,
    listener: _ListenerChannel,
    writers: list[_WriterChannel],
    local_id: str,
    pending_pings: dict[str, tuple[str, float]],
    selected_devices: DiscoveredAudioDevices | None,
    selected_peer_id: str | None,
    now: float,
    disable_writer: Callable[[_WriterChannel, str], None],
) -> _ListenerEvent:
    event = _ListenerEvent()

    for _ in range(8):
        try:
            pcm = listener.io_obj.read(4096)
        except AudioIOError:
            raise
        except Exception as exc:
            raise AudioIOError(f"Audio discovery read failed: {exc}") from exc
        if not pcm:
            break

        listener.last_activity = now

        for raw in listener.codec.feed_pcm(pcm):
            event.frames_rx += 1
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
                    send_error = _send_discovery_frame(writer, pong)
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    writer.last_activity = now
                continue

            if kind == _FOUND_KIND:
                event.found_rx += 1
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
                    send_error = _send_discovery_frame(writer, found_ack)
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    writer.last_activity = now
                continue

            if kind == _FOUND_ACK_KIND:
                event.found_ack_rx += 1
                target = message.get("target")
                if target != local_id:
                    continue
                if selected_peer_id is None or sender != selected_peer_id:
                    continue
                return _ListenerEvent(
                    candidate=event.candidate,
                    candidate_peer_id=event.candidate_peer_id,
                    peer_found_ack=True,
                    pongs_rx=event.pongs_rx,
                    found_rx=event.found_rx,
                    found_ack_rx=event.found_ack_rx,
                    frames_rx=event.frames_rx,
                )

            if kind == _PONG_KIND:
                event.pongs_rx += 1
                target = message.get("target")
                echo_nonce = message.get("echo_nonce")
                remote_output = message.get("tx_device")
                remote_input = message.get("rx_device")
                if target != local_id:
                    continue
                if not isinstance(echo_nonce, str) or not echo_nonce:
                    continue

                local = pending_pings.pop(echo_nonce, None)
                if local is None:
                    continue

                if event.candidate is None:
                    local_output = local[0]
                    peer_output = remote_output if isinstance(remote_output, str) and remote_output else None
                    peer_input = remote_input if isinstance(remote_input, str) and remote_input else None
                    event = _ListenerEvent(
                        candidate=DiscoveredAudioDevices(
                            input_device=listener.input_device,
                            output_device=local_output,
                            peer_input_device=peer_input,
                            peer_output_device=peer_output,
                        ),
                        candidate_peer_id=sender,
                        peer_found_ack=event.peer_found_ack,
                        pongs_rx=event.pongs_rx,
                        found_rx=event.found_rx,
                        found_ack_rx=event.found_ack_rx,
                        frames_rx=event.frames_rx,
                    )

    return event


def _create_writer_channels(
    *,
    config: AudioDiscoveryConfig,
    input_devices: list[str],
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> list[_WriterChannel]:
    channels: list[_WriterChannel] = []
    for output_device in output_devices:
        channel = _try_open_writer_channel(
            config=config,
            input_devices=input_devices,
            output_device=output_device,
            io_factory=io_factory,
            open_errors=open_errors,
        )
        if channel is not None:
            channels.append(channel)
    return channels


def _try_open_writer_channel(
    *,
    config: AudioDiscoveryConfig,
    input_devices: list[str],
    output_device: str,
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> _WriterChannel | None:
    for anchor_input in input_devices:
        try:
            io_obj = _open_discovery_io(
                config=config,
                input_device=anchor_input,
                output_device=output_device,
                io_factory=io_factory,
            )
        except AudioIOError as exc:
            open_errors.append(
                f"writer channel out='{output_device}' anchor_in='{anchor_input}' failed: {exc}"
            )
            continue

        return _WriterChannel(
            input_device=anchor_input,
            output_device=output_device,
            io_obj=io_obj,
            codec=_build_codec(config),
            next_ping_at=0.0,
            last_activity=time.monotonic(),
        )

    return None


def _create_listener_channels(
    *,
    config: AudioDiscoveryConfig,
    input_devices: list[str],
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> list[_ListenerChannel]:
    channels: list[_ListenerChannel] = []
    for input_device in input_devices:
        channel = _try_open_listener_channel(
            config=config,
            input_device=input_device,
            output_devices=output_devices,
            io_factory=io_factory,
            open_errors=open_errors,
        )
        if channel is not None:
            channels.append(channel)
    return channels


def _try_open_listener_channel(
    *,
    config: AudioDiscoveryConfig,
    input_device: str,
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> _ListenerChannel | None:
    for anchor_output in output_devices:
        try:
            io_obj = _open_discovery_io(
                config=config,
                input_device=input_device,
                output_device=anchor_output,
                io_factory=io_factory,
            )
        except AudioIOError as exc:
            open_errors.append(
                f"listener channel in='{input_device}' anchor_out='{anchor_output}' failed: {exc}"
            )
            continue

        return _ListenerChannel(
            input_device=input_device,
            output_device=anchor_output,
            io_obj=io_obj,
            codec=_build_codec(config),
            last_activity=time.monotonic(),
        )

    return None


def _format_timeout_error(
    *,
    input_devices: list[str],
    output_devices: list[str],
    stats: _DiscoveryStats,
    open_errors: list[str],
    disabled_channels: list[str],
    pending_pings: dict[str, tuple[str, float]],
    writers: list[_WriterChannel],
    listeners: list[_ListenerChannel],
    selected_devices: DiscoveredAudioDevices | None,
    selected_peer_id: str | None,
    selected_at: float | None,
    now: float,
) -> str:
    lines = [
        "Timed out while probing audio devices. No bidirectional ping/pong path was found.",
        f"Inputs attempted: {', '.join(input_devices)}",
        f"Outputs attempted: {', '.join(output_devices)}",
        "Discovery stats: "
        f"pings_sent={stats.pings_sent}, pongs_rx={stats.pongs_rx}, "
        f"found_sent={stats.found_sent}, found_rx={stats.found_rx}, "
        f"found_ack_rx={stats.found_ack_rx}, frames_rx={stats.frames_rx}",
        f"Active channels at timeout: listeners={len(listeners)}, writers={len(writers)}",
        f"Pending pings at timeout: {len(pending_pings)}",
    ]

    if selected_devices is not None:
        age = (now - selected_at) if selected_at is not None else -1.0
        lines.append(
            "Candidate selected before timeout: "
            f"in={selected_devices.input_device}, out={selected_devices.output_device}, "
            f"peer={selected_peer_id}, age={age:.1f}s"
        )

    if disabled_channels:
        lines.append("Disabled channels:")
        lines.extend(_format_limited_items(disabled_channels))

    if open_errors:
        lines.append("Open failures:")
        lines.extend(_format_limited_items(open_errors))

    return "\n".join(lines)


def _format_limited_items(items: list[str], *, limit: int = 12) -> list[str]:
    if len(items) <= limit:
        return [f"- {item}" for item in items]
    out = [f"- {item}" for item in items[:limit]]
    out.append(f"- ... {len(items) - limit} more")
    return out


def _build_codec(config: AudioDiscoveryConfig) -> AudioFrameCodec:
    return AudioFrameCodec(
        byte_repeat=max(config.byte_repeat, 1),
        marker_run=max(config.marker_run, 4),
    )


def _send_discovery_frame(writer: _WriterChannel, payload: dict[str, str]) -> str | None:
    encoded = _encode_discovery_payload(payload)
    if encoded is None:
        return "Failed to encode discovery payload"
    try:
        writer.io_obj.write(writer.codec.encode_frame(encoded))
    except AudioIOError as exc:
        return str(exc)
    except Exception as exc:
        return f"Unexpected audio write failure: {exc}"
    return None


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
