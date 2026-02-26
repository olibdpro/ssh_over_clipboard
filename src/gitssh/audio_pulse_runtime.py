"""PulseAudio/PipeWire runtime helpers for audio-modem routing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import sys
from typing import Any, TextIO


class PulseRuntimeError(RuntimeError):
    """Raised when Pulse runtime discovery/setup fails."""


CLIENT_VIRTUAL_MIC_SINK = "sshg_client_virtual_mic_sink"
CLIENT_VIRTUAL_MIC_SOURCE = "sshg_client_virtual_mic_source"
CLIENT_VIRTUAL_MIC_DESCRIPTION = "sshg_client_virtual_mic"


@dataclass(frozen=True)
class PulsePlaybackStream:
    index: int
    app_name: str
    media_name: str
    process_binary: str
    process_id: int | None
    sink: str | None
    state: str
    corked: bool

    @property
    def is_active(self) -> bool:
        if self.corked:
            return False
        # Some Pulse servers omit the `state` field in JSON; treat that as active.
        if not self.state:
            return True
        return self.state.upper() == "RUNNING"


@dataclass(frozen=True)
class PulseRecordStream:
    index: int
    app_name: str
    media_name: str
    process_binary: str
    process_id: int | None
    source: str | None
    corked: bool

    @property
    def is_active(self) -> bool:
        return not self.corked


@dataclass(frozen=True)
class ClientVirtualMicRoute:
    sink_name: str
    source_name: str


def _run_pactl(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PulseRuntimeError("pactl executable not found; install pulseaudio-utils.") from exc
    except Exception as exc:
        raise PulseRuntimeError(f"Failed to execute pactl {' '.join(args)}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "unknown error"
        raise PulseRuntimeError(f"pactl {' '.join(args)} failed: {detail}")
    return (result.stdout or "").strip()


def _parse_short_names(output: str) -> list[str]:
    names: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) >= 2:
            names.append(fields[1])
    return names


def _list_short_names(kind: str) -> list[str]:
    plural = "sources" if kind == "source" else "sinks"
    return _parse_short_names(_run_pactl(["list", "short", plural]))


def _default_device_from_info(kind: str) -> str:
    info = _run_pactl(["info"])
    prefix = "Default Source:" if kind == "source" else "Default Sink:"
    for raw in info.splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    raise PulseRuntimeError(f"pactl info did not report {prefix.lower()}")


def _default_device(kind: str) -> str:
    subcmd = "get-default-source" if kind == "source" else "get-default-sink"
    try:
        value = _run_pactl([subcmd]).strip()
    except PulseRuntimeError:
        value = ""
    if value:
        return value
    return _default_device_from_info(kind)


def _load_module(module_name: str, module_args: list[str]) -> int:
    output = _run_pactl(["load-module", module_name, *module_args])
    try:
        return int(output)
    except ValueError as exc:
        raise PulseRuntimeError(f"Unexpected pactl module id: {output!r}") from exc


def _unload_module(module_id: int) -> None:
    _run_pactl(["unload-module", str(module_id)])


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return bool(value)


def describe_stream(stream: PulsePlaybackStream) -> str:
    app = stream.app_name or "unknown-app"
    media = stream.media_name or "unknown-media"
    binary = stream.process_binary or "unknown-bin"
    pid = str(stream.process_id) if stream.process_id is not None else "?"
    sink = stream.sink or "?"
    return f"idx={stream.index} app={app} media={media} bin={binary} pid={pid} sink={sink}"


def describe_record_stream(stream: PulseRecordStream) -> str:
    app = stream.app_name or "unknown-app"
    media = stream.media_name or "unknown-media"
    binary = stream.process_binary or "unknown-bin"
    pid = str(stream.process_id) if stream.process_id is not None else "?"
    source = stream.source or "?"
    return f"idx={stream.index} app={app} media={media} bin={binary} pid={pid} source={source}"


def _is_utility_record_stream(stream: PulseRecordStream) -> bool:
    app = stream.app_name.strip().lower()
    media = stream.media_name.strip().lower()
    binary = stream.process_binary.strip().lower()
    if media == "peak detect":
        return True
    if app == "pulseaudio volume control":
        return True
    if binary == "pavucontrol":
        return True
    return False


def list_active_playback_streams() -> list[PulsePlaybackStream]:
    output = _run_pactl(["-f", "json", "list", "sink-inputs"])
    try:
        payload = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise PulseRuntimeError(f"Failed to parse `pactl -f json list sink-inputs`: {exc}") from exc

    if not isinstance(payload, list):
        raise PulseRuntimeError("Unexpected `pactl -f json list sink-inputs` payload shape.")

    streams: list[PulsePlaybackStream] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        index = _to_int(item.get("index"))
        if index is None:
            continue

        props = item.get("properties")
        properties = props if isinstance(props, dict) else {}
        app_name = str(properties.get("application.name", "") or "")
        media_name = str(properties.get("media.name", "") or "")
        process_binary = str(properties.get("application.process.binary", "") or "")
        process_id = _to_int(properties.get("application.process.id"))
        sink = item.get("sink")
        sink_name = str(sink) if sink is not None else None
        state = str(item.get("state", "") or "")
        corked = _to_bool(item.get("corked"))

        stream = PulsePlaybackStream(
            index=index,
            app_name=app_name,
            media_name=media_name,
            process_binary=process_binary,
            process_id=process_id,
            sink=sink_name,
            state=state,
            corked=corked,
        )
        if stream.is_active:
            streams.append(stream)

    streams.sort(key=lambda stream: stream.index, reverse=True)
    return streams


def list_active_record_streams() -> list[PulseRecordStream]:
    output = _run_pactl(["-f", "json", "list", "source-outputs"])
    try:
        payload = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise PulseRuntimeError(f"Failed to parse `pactl -f json list source-outputs`: {exc}") from exc

    if not isinstance(payload, list):
        raise PulseRuntimeError("Unexpected `pactl -f json list source-outputs` payload shape.")

    streams: list[PulseRecordStream] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        index = _to_int(item.get("index"))
        if index is None:
            continue

        props = item.get("properties")
        properties = props if isinstance(props, dict) else {}
        app_name = str(properties.get("application.name", "") or "")
        media_name = str(properties.get("media.name", "") or "")
        process_binary = str(properties.get("application.process.binary", "") or "")
        process_id = _to_int(properties.get("application.process.id"))
        source = item.get("source")
        source_name = str(source) if source is not None else None
        corked = _to_bool(item.get("corked"))

        stream = PulseRecordStream(
            index=index,
            app_name=app_name,
            media_name=media_name,
            process_binary=process_binary,
            process_id=process_id,
            source=source_name,
            corked=corked,
        )
        if stream.is_active:
            streams.append(stream)

    streams.sort(key=lambda stream: stream.index, reverse=True)
    non_utility = [stream for stream in streams if not _is_utility_record_stream(stream)]
    if non_utility:
        return non_utility
    return streams


def prompt_select_playback_stream(
    streams: list[PulsePlaybackStream],
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> PulsePlaybackStream:
    if not streams:
        raise PulseRuntimeError("No active playback streams are available to select.")

    print("sshg: select a playback stream to capture:", file=output_stream)
    for offset, stream in enumerate(streams, start=1):
        print(f"  {offset}. {describe_stream(stream)}", file=output_stream)

    while True:
        print(
            f"sshg: enter stream number [1-{len(streams)}] (or q to cancel): ",
            end="",
            file=output_stream,
            flush=True,
        )
        line = input_stream.readline()
        if line == "":
            raise PulseRuntimeError("Playback stream selection cancelled (stdin closed).")

        value = line.strip()
        if value.lower() in {"q", "quit", "exit"}:
            raise PulseRuntimeError("Playback stream selection cancelled.")

        try:
            choice = int(value)
        except ValueError:
            print("sshg: invalid selection; enter a number.", file=output_stream)
            continue

        if 1 <= choice <= len(streams):
            return streams[choice - 1]
        print(f"sshg: selection out of range; choose 1-{len(streams)}.", file=output_stream)


def prompt_select_record_stream(
    streams: list[PulseRecordStream],
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> PulseRecordStream:
    if not streams:
        raise PulseRuntimeError("No active recording streams are available to select.")

    print("sshg: select a recording stream to route server audio into:", file=output_stream)
    for offset, stream in enumerate(streams, start=1):
        print(f"  {offset}. {describe_record_stream(stream)}", file=output_stream)

    while True:
        print(
            f"sshg: enter stream number [1-{len(streams)}] (or q to cancel): ",
            end="",
            file=output_stream,
            flush=True,
        )
        line = input_stream.readline()
        if line == "":
            raise PulseRuntimeError("Recording stream selection cancelled (stdin closed).")

        value = line.strip()
        if value.lower() in {"q", "quit", "exit"}:
            raise PulseRuntimeError("Recording stream selection cancelled.")

        try:
            choice = int(value)
        except ValueError:
            print("sshg: invalid selection; enter a number.", file=output_stream)
            continue

        if 1 <= choice <= len(streams):
            return streams[choice - 1]
        print(f"sshg: selection out of range; choose 1-{len(streams)}.", file=output_stream)


def resolve_client_capture_stream_index(
    *,
    stream_index: int | None,
    stream_match: str | None,
    interactive: bool,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> int:
    streams = list_active_playback_streams()

    if stream_index is not None:
        for stream in streams:
            if stream.index == stream_index:
                return stream_index
        if streams:
            listing = "\n".join(f"- {describe_stream(stream)}" for stream in streams)
            raise PulseRuntimeError(
                f"Requested stream index {stream_index} is not active.\nActive playback streams:\n{listing}"
            )
        raise PulseRuntimeError(
            f"Requested stream index {stream_index} is not active and no active playback streams were found."
        )

    pattern_text = (stream_match or "").strip()
    if pattern_text:
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise PulseRuntimeError(f"Invalid --audio-stream-match regex {pattern_text!r}: {exc}") from exc

        matches = [stream for stream in streams if pattern.search(describe_stream(stream))]
        if len(matches) == 1:
            return matches[0].index
        if not matches:
            if streams:
                listing = "\n".join(f"- {describe_stream(stream)}" for stream in streams)
                raise PulseRuntimeError(
                    "No active playback stream matched --audio-stream-match.\n"
                    f"Pattern: {pattern_text!r}\n"
                    f"Active playback streams:\n{listing}"
                )
            raise PulseRuntimeError(
                "No active playback stream matched --audio-stream-match and no active streams were found."
            )
        listing = "\n".join(f"- {describe_stream(stream)}" for stream in matches)
        raise PulseRuntimeError(
            "Multiple active playback streams matched --audio-stream-match; be more specific.\n"
            f"Pattern: {pattern_text!r}\n"
            f"Matches:\n{listing}"
        )

    if not interactive:
        raise PulseRuntimeError(
            "Client audio capture selection requires an interactive terminal. "
            "Pass --audio-stream-index or --audio-stream-match for non-interactive runs."
        )

    while True:
        if streams:
            return prompt_select_playback_stream(
                streams,
                input_stream=input_stream,
                output_stream=output_stream,
            ).index

        print(
            "sshg: no active playback streams found. Start playback and press Enter to refresh (q to cancel): ",
            end="",
            file=output_stream,
            flush=True,
        )
        line = input_stream.readline()
        if line == "":
            raise PulseRuntimeError("Playback stream selection cancelled (stdin closed).")
        if line.strip().lower() in {"q", "quit", "exit"}:
            raise PulseRuntimeError("Playback stream selection cancelled.")
        streams = list_active_playback_streams()


def resolve_client_write_stream_index(
    *,
    stream_index: int | None,
    stream_match: str | None,
    interactive: bool,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> int:
    streams = list_active_record_streams()

    if stream_index is not None:
        for stream in streams:
            if stream.index == stream_index:
                return stream_index
        if streams:
            listing = "\n".join(f"- {describe_record_stream(stream)}" for stream in streams)
            raise PulseRuntimeError(
                f"Requested write stream index {stream_index} is not active.\nActive recording streams:\n{listing}"
            )
        raise PulseRuntimeError(
            f"Requested write stream index {stream_index} is not active and no active recording streams were found."
        )

    pattern_text = (stream_match or "").strip()
    if pattern_text:
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise PulseRuntimeError(f"Invalid --audio-write-stream-match regex {pattern_text!r}: {exc}") from exc

        matches = [stream for stream in streams if pattern.search(describe_record_stream(stream))]
        if len(matches) == 1:
            return matches[0].index
        if not matches:
            if streams:
                listing = "\n".join(f"- {describe_record_stream(stream)}" for stream in streams)
                raise PulseRuntimeError(
                    "No active recording stream matched --audio-write-stream-match.\n"
                    f"Pattern: {pattern_text!r}\n"
                    f"Active recording streams:\n{listing}"
                )
            raise PulseRuntimeError(
                "No active recording stream matched --audio-write-stream-match and no active recording streams were found."
            )
        listing = "\n".join(f"- {describe_record_stream(stream)}" for stream in matches)
        raise PulseRuntimeError(
            "Multiple active recording streams matched --audio-write-stream-match; be more specific.\n"
            f"Pattern: {pattern_text!r}\n"
            f"Matches:\n{listing}"
        )

    if not interactive:
        raise PulseRuntimeError(
            "Client audio write-stream selection requires an interactive terminal. "
            "Pass --audio-write-stream-index or --audio-write-stream-match for non-interactive runs."
        )

    while True:
        if streams:
            return prompt_select_record_stream(
                streams,
                input_stream=input_stream,
                output_stream=output_stream,
            ).index

        print(
            "sshg: no active recording streams found. Start the target app and press Enter to refresh (q to cancel): ",
            end="",
            file=output_stream,
            flush=True,
        )
        line = input_stream.readline()
        if line == "":
            raise PulseRuntimeError("Recording stream selection cancelled (stdin closed).")
        if line.strip().lower() in {"q", "quit", "exit"}:
            raise PulseRuntimeError("Recording stream selection cancelled.")
        streams = list_active_record_streams()


def move_source_output_to_source(*, source_output_index: int, source_name: str) -> None:
    if source_output_index < 0:
        raise PulseRuntimeError("Source output index must be non-negative.")
    target = source_name.strip()
    if not target:
        raise PulseRuntimeError("Source name must not be empty.")
    _run_pactl(["move-source-output", str(source_output_index), target])


def resolve_server_default_paths() -> tuple[str, str]:
    return _default_device("source"), _default_device("sink")


class ClientVirtualMicManager:
    """Creates and restores a virtual microphone source for client playback."""

    def __init__(
        self,
        *,
        sink_name: str = CLIENT_VIRTUAL_MIC_SINK,
        source_name: str = CLIENT_VIRTUAL_MIC_SOURCE,
        source_description: str = CLIENT_VIRTUAL_MIC_DESCRIPTION,
    ) -> None:
        self.sink_name = sink_name
        self.source_name = source_name
        self.source_description = source_description
        self._sink_module_id: int | None = None
        self._source_module_id: int | None = None
        self._previous_default_source: str | None = None
        self._default_source_overridden = False
        self._ready = False
        self._closed = False

    def ensure_ready(self) -> ClientVirtualMicRoute:
        if self._ready:
            return ClientVirtualMicRoute(sink_name=self.sink_name, source_name=self.source_name)

        try:
            existing_sinks = set(_list_short_names("sink"))
            if self.sink_name not in existing_sinks:
                self._sink_module_id = _load_module(
                    "module-null-sink",
                    [
                        f"sink_name={self.sink_name}",
                        f"sink_properties=device.description={self.sink_name}",
                    ],
                )

            existing_sources = set(_list_short_names("source"))
            if self.source_name not in existing_sources:
                self._source_module_id = _load_module(
                    "module-remap-source",
                    [
                        f"master={self.sink_name}.monitor",
                        f"source_name={self.source_name}",
                        f"source_properties=device.description={self.source_description}",
                    ],
                )

            self._ready = True
            return ClientVirtualMicRoute(sink_name=self.sink_name, source_name=self.source_name)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._default_source_overridden and self._previous_default_source:
            try:
                _run_pactl(["set-default-source", self._previous_default_source])
            except Exception:
                pass

        for module_id in (self._source_module_id, self._sink_module_id):
            if module_id is None:
                continue
            try:
                _unload_module(module_id)
            except Exception:
                pass

    def enable_default_source_fallback(self) -> None:
        if not self._ready:
            raise PulseRuntimeError("Virtual microphone route is not ready.")
        if self._default_source_overridden:
            return
        self._previous_default_source = _default_device("source")
        _run_pactl(["set-default-source", self.source_name])
        self._default_source_overridden = True
