"""Automatic device discovery for audio-modem transport."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import time
from typing import Callable
import uuid

from .audio_io_ffmpeg import AudioDuplexIO, AudioIOError, _list_pulse_devices, build_audio_duplex_io
from .audio_modem import (
    MODULATION_LEGACY,
    MODULATION_ROBUST_V1,
    AudioModulationCodec,
    create_audio_frame_codec,
    normalize_audio_modulation,
)

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
    max_pending_pings_per_output: int = 2
    byte_repeat: int = 3
    marker_run: int = 16
    audio_modulation: str = "auto"


@dataclass(frozen=True)
class DiscoveredAudioDevices:
    input_device: str
    output_device: str
    modulation: str = MODULATION_LEGACY
    peer_input_device: str | None = None
    peer_output_device: str | None = None


@dataclass
class _WriterChannel:
    input_device: str
    output_device: str
    io_obj: AudioDuplexIO
    codec: AudioModulationCodec
    next_ping_at: float = 0.0
    next_tx_at: float = 0.0
    last_activity: float = 0.0


@dataclass
class _ListenerChannel:
    input_device: str
    output_device: str
    io_obj: AudioDuplexIO
    codec: AudioModulationCodec
    last_activity: float = 0.0


@dataclass
class _DiscoveryStats:
    pings_sent: int = 0
    pongs_rx: int = 0
    found_sent: int = 0
    found_rx: int = 0
    found_ack_rx: int = 0
    frames_rx: int = 0
    codec_frames_decoded: int = 0
    codec_crc_failures: int = 0
    codec_sync_hits: int = 0
    codec_decode_failures: int = 0


@dataclass
class _ListenerEvent:
    candidate: DiscoveredAudioDevices | None = None
    candidate_peer_id: str | None = None
    peer_found_ack: bool = False
    pongs_rx: int = 0
    found_rx: int = 0
    found_ack_rx: int = 0
    frames_rx: int = 0
    codec_frames_decoded: int = 0
    codec_crc_failures: int = 0
    codec_sync_hits: int = 0
    codec_decode_failures: int = 0


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
    try:
        requested_modulation = normalize_audio_modulation(config.audio_modulation, allow_auto=True)
    except Exception as exc:
        raise AudioIOError(f"Invalid audio modulation setting for discovery: {exc}") from exc
    if requested_modulation != "auto":
        return _discover_audio_devices_once(
            config=config,
            modulation=requested_modulation,
            input_devices=input_devices,
            output_devices=output_devices,
            io_factory=io_factory,
            logger=log,
        )

    total_timeout = max(config.timeout, 1.0)
    robust_timeout = max(total_timeout * 0.7, 1.0)
    legacy_timeout = max(total_timeout - robust_timeout, 1.0)

    log(
        "audio discovery auto modulation: "
        f"trying {MODULATION_ROBUST_V1} for {robust_timeout:.1f}s, "
        f"then {MODULATION_LEGACY} for {legacy_timeout:.1f}s if needed"
    )

    robust_config = replace(
        config,
        timeout=robust_timeout,
        audio_modulation=MODULATION_ROBUST_V1,
    )
    try:
        return _discover_audio_devices_once(
            config=robust_config,
            modulation=MODULATION_ROBUST_V1,
            input_devices=input_devices,
            output_devices=output_devices,
            io_factory=io_factory,
            logger=log,
        )
    except AudioIOError as robust_exc:
        log(f"audio discovery robust-v1 failed; falling back to legacy: {robust_exc}")
        legacy_config = replace(
            config,
            timeout=legacy_timeout,
            audio_modulation=MODULATION_LEGACY,
        )
        try:
            return _discover_audio_devices_once(
                config=legacy_config,
                modulation=MODULATION_LEGACY,
                input_devices=input_devices,
                output_devices=output_devices,
                io_factory=io_factory,
                logger=log,
            )
        except AudioIOError as legacy_exc:
            raise AudioIOError(
                "Audio discovery failed in both modulation modes.\n"
                f"- {MODULATION_ROBUST_V1}: {robust_exc}\n"
                f"- {MODULATION_LEGACY}: {legacy_exc}"
            ) from legacy_exc


def _discover_audio_devices_once(
    *,
    config: AudioDiscoveryConfig,
    modulation: str,
    input_devices: list[str],
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    logger: Callable[[str], None],
) -> DiscoveredAudioDevices:
    log = logger
    log(
        "audio discovery starting with "
        f"{len(input_devices)} input(s), {len(output_devices)} output(s), "
        f"modulation={modulation} "
        f"(timeout={max(config.timeout, 1.0):.1f}s, ping_interval={max(config.ping_interval, 0.01):.3f}s)"
    )

    open_errors: list[str] = []
    disabled_channels: list[str] = []

    writers = _create_writer_channels(
        config=config,
        modulation=modulation,
        input_devices=input_devices,
        output_devices=output_devices,
        io_factory=io_factory,
        open_errors=open_errors,
    )
    listeners = _create_listener_channels(
        config=config,
        modulation=modulation,
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

        local_id = uuid.uuid4().hex[:12]
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

            if selected_devices is None:
                for writer in list(writers):
                    if now < writer.next_ping_at:
                        continue
                    if now < writer.next_tx_at:
                        continue
                    if (
                        _count_pending_for_output(pending_pings, writer.output_device)
                        >= max(config.max_pending_pings_per_output, 1)
                    ):
                        continue

                    nonce = uuid.uuid4().hex[:16]
                    ping = {
                        "kind": _PING_KIND,
                        "sender": local_id,
                        "nonce": nonce,
                        "modulation": modulation,
                    }

                    sent, send_error = _send_discovery_frame(
                        writer,
                        ping,
                        now=now,
                        sample_rate=config.sample_rate,
                        respect_backpressure=True,
                    )
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    if not sent:
                        continue

                    pending_pings[nonce] = (writer.output_device, now)
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
                    "modulation": modulation,
                }
                for writer in list(writers):
                    sent, send_error = _send_discovery_frame(
                        writer,
                        found,
                        now=now,
                        sample_rate=config.sample_rate,
                        respect_backpressure=True,
                    )
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    if not sent:
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
                        modulation=modulation,
                        sample_rate=config.sample_rate,
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
                stats.codec_frames_decoded += event.codec_frames_decoded
                stats.codec_crc_failures += event.codec_crc_failures
                stats.codec_sync_hits += event.codec_sync_hits
                stats.codec_decode_failures += event.codec_decode_failures

                if event.candidate is not None and selected_devices is None:
                    selected_devices = event.candidate
                    selected_peer_id = event.candidate_peer_id
                    selected_at = now
                    candidate_deadline = max(deadline, now + max(config.candidate_grace, 0.0))
                    next_found_at = 0.0
                    log(
                        "audio discovery candidate selected: "
                        f"in={selected_devices.input_device}, out={selected_devices.output_device}, "
                        f"modulation={selected_devices.modulation}, "
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
                    f"pending_pings={len(pending_pings)} "
                    f"codec_frames_decoded={stats.codec_frames_decoded} "
                    f"codec_sync_hits={stats.codec_sync_hits} "
                    f"codec_crc_failures={stats.codec_crc_failures} "
                    f"pending_cap_per_output={max(config.max_pending_pings_per_output, 1)}"
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
    modulation: str,
    sample_rate: int,
    pending_pings: dict[str, tuple[str, float]],
    selected_devices: DiscoveredAudioDevices | None,
    selected_peer_id: str | None,
    now: float,
    disable_writer: Callable[[_WriterChannel, str], None],
) -> _ListenerEvent:
    event = _ListenerEvent()
    codec_before = _snapshot_codec_stats(listener.codec)

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
            peer_modulation = message.get("modulation")
            if not isinstance(kind, str) or not isinstance(sender, str):
                continue
            if isinstance(peer_modulation, str):
                normalized_peer_modulation = peer_modulation
            elif modulation == MODULATION_LEGACY:
                normalized_peer_modulation = MODULATION_LEGACY
            else:
                continue
            if normalized_peer_modulation != modulation:
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
                        "modulation": modulation,
                    }
                    sent, send_error = _send_discovery_frame(
                        writer,
                        pong,
                        now=now,
                        sample_rate=sample_rate,
                        respect_backpressure=False,
                    )
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    if not sent:
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
                    "modulation": modulation,
                }
                for writer in list(writers):
                    sent, send_error = _send_discovery_frame(
                        writer,
                        found_ack,
                        now=now,
                        sample_rate=sample_rate,
                        respect_backpressure=False,
                    )
                    if send_error is not None:
                        disable_writer(writer, send_error)
                        continue
                    if not sent:
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
                _apply_codec_stat_delta(event, codec_before, listener.codec)
                return _ListenerEvent(
                    candidate=event.candidate,
                    candidate_peer_id=event.candidate_peer_id,
                    peer_found_ack=True,
                    pongs_rx=event.pongs_rx,
                    found_rx=event.found_rx,
                    found_ack_rx=event.found_ack_rx,
                    frames_rx=event.frames_rx,
                    codec_frames_decoded=event.codec_frames_decoded,
                    codec_crc_failures=event.codec_crc_failures,
                    codec_sync_hits=event.codec_sync_hits,
                    codec_decode_failures=event.codec_decode_failures,
                )

            if kind == _PONG_KIND:
                event.pongs_rx += 1
                target = message.get("target")
                echo_nonce = message.get("echo_nonce")
                if target != local_id:
                    continue
                if not isinstance(echo_nonce, str) or not echo_nonce:
                    continue

                local = pending_pings.pop(echo_nonce, None)
                if local is None:
                    continue

                if event.candidate is None:
                    local_output = local[0]
                    event = _ListenerEvent(
                        candidate=DiscoveredAudioDevices(
                            input_device=listener.input_device,
                            output_device=local_output,
                            modulation=modulation,
                        ),
                        candidate_peer_id=sender,
                        peer_found_ack=event.peer_found_ack,
                        pongs_rx=event.pongs_rx,
                        found_rx=event.found_rx,
                        found_ack_rx=event.found_ack_rx,
                        frames_rx=event.frames_rx,
                        codec_frames_decoded=event.codec_frames_decoded,
                        codec_crc_failures=event.codec_crc_failures,
                        codec_sync_hits=event.codec_sync_hits,
                        codec_decode_failures=event.codec_decode_failures,
                    )

    _apply_codec_stat_delta(event, codec_before, listener.codec)
    return event


def _create_writer_channels(
    *,
    config: AudioDiscoveryConfig,
    modulation: str,
    input_devices: list[str],
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> list[_WriterChannel]:
    channels: list[_WriterChannel] = []
    for output_device in output_devices:
        channel = _try_open_writer_channel(
            config=config,
            modulation=modulation,
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
    modulation: str,
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
            codec=_build_codec(config=config, modulation=modulation),
            next_ping_at=0.0,
            last_activity=time.monotonic(),
        )

    return None


def _create_listener_channels(
    *,
    config: AudioDiscoveryConfig,
    modulation: str,
    input_devices: list[str],
    output_devices: list[str],
    io_factory: Callable[[str, str], AudioDuplexIO] | None,
    open_errors: list[str],
) -> list[_ListenerChannel]:
    channels: list[_ListenerChannel] = []
    for input_device in input_devices:
        channel = _try_open_listener_channel(
            config=config,
            modulation=modulation,
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
    modulation: str,
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
            codec=_build_codec(config=config, modulation=modulation),
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
        "Codec stats: "
        f"frames_decoded={stats.codec_frames_decoded}, sync_hits={stats.codec_sync_hits}, "
        f"crc_failures={stats.codec_crc_failures}, decode_failures={stats.codec_decode_failures}",
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


def _build_codec(*, config: AudioDiscoveryConfig, modulation: str) -> AudioModulationCodec:
    return create_audio_frame_codec(
        modulation=modulation,
        sample_rate=max(config.sample_rate, 8000),
        byte_repeat=max(config.byte_repeat, 1),
        marker_run=max(config.marker_run, 4),
    )


def _snapshot_codec_stats(codec: AudioModulationCodec) -> dict[str, int]:
    try:
        snap = codec.snapshot_stats()
    except Exception:
        return {
            "frames_decoded": 0,
            "crc_failures": 0,
            "sync_hits": 0,
            "decode_failures": 0,
        }

    return {
        "frames_decoded": int(snap.get("frames_decoded", 0)),
        "crc_failures": int(snap.get("crc_failures", 0)),
        "sync_hits": int(snap.get("sync_hits", 0)),
        "decode_failures": int(snap.get("decode_failures", 0)),
    }


def _apply_codec_stat_delta(
    event: _ListenerEvent,
    before: dict[str, int],
    codec: AudioModulationCodec,
) -> None:
    after = _snapshot_codec_stats(codec)
    event.codec_frames_decoded += max(after["frames_decoded"] - before["frames_decoded"], 0)
    event.codec_crc_failures += max(after["crc_failures"] - before["crc_failures"], 0)
    event.codec_sync_hits += max(after["sync_hits"] - before["sync_hits"], 0)
    event.codec_decode_failures += max(after["decode_failures"] - before["decode_failures"], 0)


def _send_discovery_frame(
    writer: _WriterChannel,
    payload: dict[str, str],
    *,
    now: float,
    sample_rate: int,
    respect_backpressure: bool,
) -> tuple[bool, str | None]:
    if respect_backpressure and now < writer.next_tx_at:
        return False, None

    encoded = _encode_discovery_payload(payload)
    if encoded is None:
        return False, "Failed to encode discovery payload"
    pcm = writer.codec.encode_frame(encoded)
    try:
        writer.io_obj.write(pcm)
    except AudioIOError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Unexpected audio write failure: {exc}"

    frame_seconds = (len(pcm) / 2.0) / float(max(sample_rate, 8000))
    writer.next_tx_at = now + max(frame_seconds, 0.01)
    return True, None


def _count_pending_for_output(
    pending_pings: dict[str, tuple[str, float]],
    output_device: str,
) -> int:
    count = 0
    for local_output, _timestamp in pending_pings.values():
        if local_output == output_device:
            count += 1
    return count


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
