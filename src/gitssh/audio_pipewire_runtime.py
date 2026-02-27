"""PipeWire runtime helpers for client audio-modem routing."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
import re
import select
import struct
import subprocess
import sys
import time
from typing import Callable, TextIO

from .audio_io_ffmpeg import AudioIOError


class PipeWireRuntimeError(AudioIOError):
    """Raised when PipeWire runtime discovery/setup fails."""


@dataclass(frozen=True)
class PipeWireNode:
    node_id: int
    node_name: str
    node_description: str
    app_name: str
    media_class: str


@dataclass(frozen=True)
class PipeWirePlaybackStream:
    stream_index: int
    node_id: int | None
    sink_id: int | None
    app_name: str
    media_name: str
    process_binary: str


@dataclass(frozen=True)
class PipeWireRecordStream:
    stream_index: int
    node_id: int | None
    source_id: int | None
    app_name: str
    media_name: str
    process_binary: str


@dataclass(frozen=True)
class PipeWireNodeCandidate:
    node: PipeWireNode
    display_text: str
    match_text: str
    is_stream_backed: bool = False


@dataclass(frozen=True)
class PipeWirePreflightReport:
    ok: bool
    issues: tuple[str, ...]
    notes: tuple[str, ...]
    remediation: tuple[str, ...]

    def render(self) -> str:
        if self.ok:
            lines = ["PipeWire client preflight OK"]
            for note in self.notes:
                lines.append(f"- {note}")
            return "\n".join(lines)

        lines = ["PipeWire client preflight failed:"]
        for issue in self.issues:
            lines.append(f"- {issue}")
        for note in self.notes:
            lines.append(f"- {note}")
        lines.append("Remediation:")
        for idx, step in enumerate(self.remediation, start=1):
            lines.append(f"{idx}. {step}")
        return "\n".join(lines)


_NODE_HEADER_RE = re.compile(r"^id\s+(\d+),\s+type\s+PipeWire:Interface:Node/\d+$")
_PORT_HEADER_RE = re.compile(r"^id\s+(\d+),\s+type\s+PipeWire:Interface:Port/\d+$")
_NODE_PROP_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*=\s*\"(.*)\"$")


def _run_command(cmd: list[str], *, friendly_name: str) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PipeWireRuntimeError(
            f"{cmd[0]} executable not found; install PipeWire CLI tools."
        ) from exc
    except Exception as exc:
        raise PipeWireRuntimeError(f"Failed to execute {' '.join(cmd)}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "unknown error"
        raise PipeWireRuntimeError(f"{friendly_name} failed: {detail}")

    return result.stdout or ""


def _run_pw_cli(args: list[str]) -> str:
    return _run_command(["pw-cli", *args], friendly_name=f"pw-cli {' '.join(args)}")


def _run_pw_link(args: list[str]) -> str:
    return _run_command(["pw-link", *args], friendly_name=f"pw-link {' '.join(args)}")


def _run_pactl(args: list[str]) -> str:
    return _run_command(["pactl", *args], friendly_name=f"pactl {' '.join(args)}")


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return bool(value)


def _systemctl_user_unit_state(unit_name: str) -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None

    text = (result.stdout or "").strip() or (result.stderr or "").strip()
    if not text:
        return "active" if result.returncode == 0 else "inactive"
    return text.splitlines()[-1].strip().lower()


def _wireplumber_has_no_space_issue() -> bool:
    try:
        result = subprocess.run(
            ["journalctl", "--user", "-u", "wireplumber.service", "-n", "80", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    blob = "\n".join((result.stdout or "", result.stderr or ""))
    return "No space left on device" in blob


def build_client_pipewire_preflight_report(
    *,
    capture_node_id: int | None = None,
    write_node_id: int | None = None,
) -> PipeWirePreflightReport:
    issues: list[str] = []
    notes: list[str] = []
    remediation: list[str] = [
        "Inspect services: systemctl --user --no-pager --full status wireplumber.service pipewire-media-session.service",
        "Start/restart WirePlumber: systemctl --user restart wireplumber.service",
        "Re-run preflight: sshg-audio-probe --pipewire-preflight",
    ]

    try:
        nodes = list_nodes()
    except PipeWireRuntimeError as exc:
        issues.append(f"Unable to query PipeWire nodes: {exc}")
        nodes = []

    if nodes:
        active_ids = {node.node_id for node in nodes}
        if capture_node_id is not None and capture_node_id not in active_ids:
            issues.append(f"Requested capture node id {capture_node_id} is not active.")
        if write_node_id is not None and write_node_id not in active_ids:
            issues.append(f"Requested write node id {write_node_id} is not active.")
    else:
        notes.append("No active PipeWire nodes were returned by pw-cli ls Node.")

    try:
        port_listing = _run_pw_cli(["ls", "Port"])
    except PipeWireRuntimeError as exc:
        issues.append(f"Unable to query PipeWire ports: {exc}")
    else:
        has_ports = any(_PORT_HEADER_RE.match(raw.strip()) for raw in port_listing.splitlines())
        if not has_ports:
            issues.append("PipeWire exposes no visible Port objects (pw-cli ls Port returned empty).")

    wireplumber_state = _systemctl_user_unit_state("wireplumber.service")
    media_session_state = _systemctl_user_unit_state("pipewire-media-session.service")
    if wireplumber_state is not None:
        notes.append(f"wireplumber.service={wireplumber_state}")
    if media_session_state is not None:
        notes.append(f"pipewire-media-session.service={media_session_state}")
    if (wireplumber_state is not None or media_session_state is not None) and (
        wireplumber_state != "active" and media_session_state != "active"
    ):
        issues.append("No active PipeWire session manager detected.")
        if wireplumber_state in {"failed", "inactive"} and _wireplumber_has_no_space_issue():
            notes.append("wireplumber journal includes: No space left on device.")
            remediation.insert(
                2,
                "If wireplumber logs show ENOSPC/No space left on device, free or raise user inotify limits, then restart.",
            )

    ok = not issues
    return PipeWirePreflightReport(
        ok=ok,
        issues=tuple(issues),
        notes=tuple(notes),
        remediation=tuple(remediation),
    )


def ensure_client_pipewire_preflight(
    *,
    capture_node_id: int | None = None,
    write_node_id: int | None = None,
) -> None:
    report = build_client_pipewire_preflight_report(
        capture_node_id=capture_node_id,
        write_node_id=write_node_id,
    )
    if report.ok:
        return
    raise PipeWireRuntimeError(report.render())


def _parse_nodes(output: str) -> list[PipeWireNode]:
    nodes: list[PipeWireNode] = []
    current_id: int | None = None
    current_props: dict[str, str] = {}

    def flush_current() -> None:
        nonlocal current_id, current_props
        if current_id is None:
            current_props = {}
            return
        nodes.append(
            PipeWireNode(
                node_id=current_id,
                node_name=current_props.get("node.name", ""),
                node_description=current_props.get("node.description", ""),
                app_name=current_props.get("application.name", ""),
                media_class=current_props.get("media.class", ""),
            )
        )
        current_id = None
        current_props = {}

    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue

        header_match = _NODE_HEADER_RE.match(line)
        if header_match:
            flush_current()
            current_id = int(header_match.group(1))
            continue

        if current_id is None:
            continue

        prop_match = _NODE_PROP_RE.match(line)
        if not prop_match:
            continue
        key = prop_match.group(1).strip()
        value = prop_match.group(2)
        current_props[key] = value

    flush_current()
    nodes.sort(key=lambda node: node.node_id, reverse=True)
    return nodes


def list_nodes() -> list[PipeWireNode]:
    return _parse_nodes(_run_pw_cli(["ls", "Node"]))


def describe_node(node: PipeWireNode) -> str:
    name = node.node_name or "unknown-node"
    desc = node.node_description or "unknown-description"
    app = node.app_name or "unknown-app"
    media_class = node.media_class or "unknown-class"
    return f"id={node.node_id} name={name} desc={desc} app={app} class={media_class}"


def _is_capture_candidate(node: PipeWireNode) -> bool:
    media_class = (node.media_class or "").strip().lower()
    if not media_class:
        return False
    if "output/audio" in media_class:
        return True
    if media_class.startswith("audio/sink"):
        return True
    return False


def _is_write_candidate(node: PipeWireNode) -> bool:
    media_class = (node.media_class or "").strip().lower()
    if not media_class:
        return False
    if "input/audio" in media_class:
        return True
    if media_class.startswith("audio/source"):
        return True
    return False


def list_capture_nodes() -> list[PipeWireNode]:
    return [node for node in list_nodes() if _is_capture_candidate(node)]


def list_write_nodes() -> list[PipeWireNode]:
    return [node for node in list_nodes() if _is_write_candidate(node)]


def _stream_meta_text(*, app_name: str, media_name: str, process_binary: str) -> str:
    app = app_name.strip() or "unknown-app"
    media = media_name.strip() or "unknown-media"
    binary = process_binary.strip() or "unknown-bin"
    return f"app={app} media={media} bin={binary}"


def _load_pactl_json(args: list[str]) -> list[dict[str, object]]:
    try:
        payload_text = _run_pactl(args)
    except PipeWireRuntimeError:
        return []

    try:
        payload = json.loads(payload_text or "[]")
    except json.JSONDecodeError:
        return []

    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def list_active_playback_streams() -> list[PipeWirePlaybackStream]:
    streams: list[PipeWirePlaybackStream] = []
    for item in _load_pactl_json(["-f", "json", "list", "sink-inputs"]):
        stream_index = _to_int(item.get("index"))
        if stream_index is None:
            continue

        properties = item.get("properties")
        props = properties if isinstance(properties, dict) else {}
        state = str(item.get("state", "") or "").strip().lower()
        corked = _to_bool(item.get("corked"))
        if corked or state == "corked":
            continue

        streams.append(
            PipeWirePlaybackStream(
                stream_index=stream_index,
                node_id=_to_int(props.get("object.id")),
                sink_id=_to_int(item.get("sink")),
                app_name=str(props.get("application.name", "") or ""),
                media_name=str(props.get("media.name", "") or ""),
                process_binary=str(props.get("application.process.binary", "") or ""),
            )
        )

    streams.sort(key=lambda stream: stream.stream_index, reverse=True)
    return streams


def _is_utility_record_stream(stream: PipeWireRecordStream) -> bool:
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


def list_active_record_streams() -> list[PipeWireRecordStream]:
    streams: list[PipeWireRecordStream] = []
    for item in _load_pactl_json(["-f", "json", "list", "source-outputs"]):
        stream_index = _to_int(item.get("index"))
        if stream_index is None:
            continue

        properties = item.get("properties")
        props = properties if isinstance(properties, dict) else {}
        corked = _to_bool(item.get("corked"))
        if corked:
            continue

        streams.append(
            PipeWireRecordStream(
                stream_index=stream_index,
                node_id=_to_int(props.get("object.id")),
                source_id=_to_int(item.get("source")),
                app_name=str(props.get("application.name", "") or ""),
                media_name=str(props.get("media.name", "") or ""),
                process_binary=str(props.get("application.process.binary", "") or ""),
            )
        )

    streams.sort(key=lambda stream: stream.stream_index, reverse=True)
    non_utility = [stream for stream in streams if not _is_utility_record_stream(stream)]
    if non_utility:
        return non_utility
    return streams


def _build_node_candidates(
    *,
    nodes: list[PipeWireNode],
    stream_metadata_by_node: dict[int, list[str]],
) -> list[PipeWireNodeCandidate]:
    stream_backed: list[PipeWireNodeCandidate] = []
    generic: list[PipeWireNodeCandidate] = []

    for node in nodes:
        base = describe_node(node)
        stream_descriptions = stream_metadata_by_node.get(node.node_id, [])
        if stream_descriptions:
            headline = stream_descriptions[0]
            if len(stream_descriptions) > 1:
                headline = f"{headline}; +{len(stream_descriptions) - 1} more stream(s)"
            display = f"{base} stream={headline}"
            match_text = f"{display} {' '.join(stream_descriptions)}"
            stream_backed.append(
                PipeWireNodeCandidate(
                    node=node,
                    display_text=display,
                    match_text=match_text,
                    is_stream_backed=True,
                )
            )
        else:
            generic.append(
                PipeWireNodeCandidate(
                    node=node,
                    display_text=base,
                    match_text=base,
                    is_stream_backed=False,
                )
            )

    return [*stream_backed, *generic]


def _capture_node_candidates() -> list[PipeWireNodeCandidate]:
    nodes = list_capture_nodes()
    stream_metadata_by_node: dict[int, list[str]] = {}
    for stream in list_active_playback_streams():
        if stream.node_id is None:
            continue
        sink = str(stream.sink_id) if stream.sink_id is not None else "?"
        description = (
            f"idx={stream.stream_index} "
            f"{_stream_meta_text(app_name=stream.app_name, media_name=stream.media_name, process_binary=stream.process_binary)} "
            f"sink={sink}"
        )
        stream_metadata_by_node.setdefault(stream.node_id, []).append(description)
    return _build_node_candidates(nodes=nodes, stream_metadata_by_node=stream_metadata_by_node)


def _write_node_candidates() -> list[PipeWireNodeCandidate]:
    nodes = list_write_nodes()
    stream_metadata_by_node: dict[int, list[str]] = {}
    for stream in list_active_record_streams():
        if stream.node_id is None:
            continue
        source = str(stream.source_id) if stream.source_id is not None else "?"
        description = (
            f"idx={stream.stream_index} "
            f"{_stream_meta_text(app_name=stream.app_name, media_name=stream.media_name, process_binary=stream.process_binary)} "
            f"source={source}"
        )
        stream_metadata_by_node.setdefault(stream.node_id, []).append(description)
    return _build_node_candidates(nodes=nodes, stream_metadata_by_node=stream_metadata_by_node)


def _resolve_node_id(
    *,
    candidate_provider: Callable[[], list[PipeWireNodeCandidate]],
    requested_id: int | None,
    requested_match: str | None,
    interactive: bool,
    label: str,
    selector_help: str,
    input_stream: TextIO,
    output_stream: TextIO,
) -> int:
    candidates = candidate_provider()

    if requested_id is not None:
        for candidate in candidates:
            if candidate.node.node_id == requested_id:
                return requested_id
        if candidates:
            listing = "\n".join(f"- {candidate.display_text}" for candidate in candidates)
            raise PipeWireRuntimeError(
                f"Requested {label} node id {requested_id} is not active.\n"
                f"Available {label} nodes:\n{listing}"
            )
        raise PipeWireRuntimeError(
            f"Requested {label} node id {requested_id} is not active and no {label} nodes were found."
        )

    pattern_text = (requested_match or "").strip()
    if pattern_text:
        try:
            pattern = re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise PipeWireRuntimeError(f"Invalid regex for {label} node match {pattern_text!r}: {exc}") from exc

        matches = [candidate for candidate in candidates if pattern.search(candidate.match_text)]
        if len(matches) == 1:
            return matches[0].node.node_id
        if not matches:
            if candidates:
                listing = "\n".join(f"- {candidate.display_text}" for candidate in candidates)
                raise PipeWireRuntimeError(
                    f"No {label} node matched the provided regex.\n"
                    f"Pattern: {pattern_text!r}\n"
                    f"Available {label} nodes:\n{listing}"
                )
            raise PipeWireRuntimeError(
                f"No {label} node matched the provided regex and no {label} nodes were found."
            )
        listing = "\n".join(f"- {candidate.display_text}" for candidate in matches)
        raise PipeWireRuntimeError(
            f"Multiple {label} nodes matched the provided regex; be more specific.\n"
            f"Pattern: {pattern_text!r}\n"
            f"Matches:\n{listing}"
        )

    stream_backed = [candidate for candidate in candidates if candidate.is_stream_backed]
    if not interactive and len(stream_backed) == 1:
        return stream_backed[0].node.node_id
    if not interactive and len(stream_backed) > 1:
        listing = "\n".join(f"- {candidate.display_text}" for candidate in stream_backed)
        raise PipeWireRuntimeError(
            f"Multiple active stream-backed {label} nodes were found; select one explicitly.\n"
            f"{selector_help}\n"
            f"Candidates:\n{listing}"
        )

    if not interactive:
        raise PipeWireRuntimeError(
            f"Client PipeWire {label} node selection requires an interactive terminal. {selector_help}"
        )

    while True:
        if candidates:
            print(f"sshg: select a PipeWire {label} node:", file=output_stream)
            for offset, candidate in enumerate(candidates, start=1):
                print(f"  {offset}. {candidate.display_text}", file=output_stream)
            print(
                f"sshg: enter node number [1-{len(candidates)}] (or q to cancel): ",
                end="",
                file=output_stream,
                flush=True,
            )
            line = input_stream.readline()
            if line == "":
                raise PipeWireRuntimeError(f"{label.title()} node selection cancelled (stdin closed).")
            value = line.strip()
            if value.lower() in {"q", "quit", "exit"}:
                raise PipeWireRuntimeError(f"{label.title()} node selection cancelled.")
            try:
                choice = int(value)
            except ValueError:
                print("sshg: invalid selection; enter a number.", file=output_stream)
                continue
            if 1 <= choice <= len(candidates):
                return candidates[choice - 1].node.node_id
            print(f"sshg: selection out of range; choose 1-{len(candidates)}.", file=output_stream)
            continue

        print(
            f"sshg: no PipeWire {label} nodes found. Start the target app and press Enter to refresh (q to cancel): ",
            end="",
            file=output_stream,
            flush=True,
        )
        line = input_stream.readline()
        if line == "":
            raise PipeWireRuntimeError(f"{label.title()} node selection cancelled (stdin closed).")
        if line.strip().lower() in {"q", "quit", "exit"}:
            raise PipeWireRuntimeError(f"{label.title()} node selection cancelled.")
        candidates = candidate_provider()


def resolve_client_capture_node_id(
    *,
    node_id: int | None,
    node_match: str | None,
    interactive: bool,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> int:
    return _resolve_node_id(
        candidate_provider=_capture_node_candidates,
        requested_id=node_id,
        requested_match=node_match,
        interactive=interactive,
        label="capture",
        selector_help="Pass --pw-capture-node-id or --pw-capture-match for non-interactive runs.",
        input_stream=input_stream,
        output_stream=output_stream,
    )


def resolve_client_write_node_id(
    *,
    node_id: int | None,
    node_match: str | None,
    interactive: bool,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
) -> int:
    return _resolve_node_id(
        candidate_provider=_write_node_candidates,
        requested_id=node_id,
        requested_match=node_match,
        interactive=interactive,
        label="write",
        selector_help="Pass --pw-write-node-id or --pw-write-match for non-interactive runs.",
        input_stream=input_stream,
        output_stream=output_stream,
    )


def _parse_pw_link_ports(output: str) -> list[str]:
    ports: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        token = ""
        for part in line.split():
            if ":" in part:
                token = part
                break
        # Some pw-link versions print "id:port" while others include "node:port" forms
        # or prefixes like "input"/"output". Keep the left-most token containing ':'.
        if token:
            ports.append(token.rstrip(","))
    return ports


def _normalize_pipewire_aliases(value: str) -> list[str]:
    stripped = (value or "").strip()
    if not stripped:
        return []

    collapsed = re.sub(r"\s+", " ", stripped)
    variants = [
        stripped,
        collapsed,
        collapsed.replace(" ", "-"),
        collapsed.replace(" ", "_"),
        collapsed.replace(" ", ""),
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in variants:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _build_port_prefixes(
    *,
    node_name: str,
    node_id: int | None,
    alias_candidates: list[str] | None = None,
) -> list[str]:
    prefixes: list[str] = []
    for alias in _normalize_pipewire_aliases(node_name):
        prefixes.append(f"{alias}:")
    if alias_candidates:
        for alias in alias_candidates:
            for normalized in _normalize_pipewire_aliases(alias):
                prefixes.append(f"{normalized}:")
    if node_id is not None:
        prefixes.append(f"{node_id}:")

    deduped: list[str] = []
    seen: set[str] = set()
    for prefix in prefixes:
        if prefix in seen:
            continue
        seen.add(prefix)
        deduped.append(prefix)
    return deduped


def _node_alias_candidates(node: PipeWireNode) -> list[str]:
    aliases: list[str] = []
    if node.node_name:
        aliases.append(node.node_name)
    if node.node_description:
        aliases.append(node.node_description)
    if node.app_name:
        aliases.append(node.app_name)
    return aliases


def _ports_for_node(
    *,
    node_name: str,
    node_id: int | None,
    direction: str,
    alias_candidates: list[str] | None = None,
) -> tuple[list[str], str]:
    args = ["-o"] if direction == "output" else ["-i"]
    listing = _run_pw_link(args)
    prefixes = _build_port_prefixes(
        node_name=node_name,
        node_id=node_id,
        alias_candidates=alias_candidates,
    )

    ports = _parse_pw_link_ports(listing)
    matches = [port for port in ports if any(port.startswith(prefix) for prefix in prefixes)]
    return sorted(matches), listing


def _process_stderr(proc: subprocess.Popen[bytes], label: str) -> str:
    stderr_text = ""
    try:
        if proc.stderr is not None:
            stderr_text = proc.stderr.read().decode("utf-8", errors="ignore")
    except Exception:
        stderr_text = ""
    cleaned = (stderr_text or "").strip()
    if cleaned:
        return f"{label} process exited unexpectedly: {cleaned}"
    return f"{label} process exited unexpectedly"


def _build_pipewire_props(node_name: str, node_description: str) -> str:
    return json.dumps(
        {
            "node.name": node_name,
            "node.description": node_description,
            "media.type": "Audio",
            "media.role": "Communication",
        },
        separators=(",", ":"),
    )


def _build_streaming_wav_header(
    *,
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
) -> bytes:
    if sample_rate < 1:
        raise PipeWireRuntimeError(f"Invalid sample rate for WAV stream header: {sample_rate}")
    if channels < 1:
        raise PipeWireRuntimeError(f"Invalid channel count for WAV stream header: {channels}")
    if bits_per_sample < 1 or bits_per_sample % 8 != 0:
        raise PipeWireRuntimeError(
            f"Invalid bits-per-sample for WAV stream header: {bits_per_sample}"
        )

    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    if block_align < 1 or byte_rate < 1:
        raise PipeWireRuntimeError("Computed invalid WAV stream header alignment/rate values.")

    # Use a large declared data chunk size so the stream can remain open for long sessions.
    data_size = 0x7FFFFFFF
    riff_size = (36 + data_size) & 0xFFFFFFFF
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVEfmt "
        + struct.pack(
            "<IHHIIHH",
            16,  # PCM fmt chunk size
            1,  # PCM format id
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
        )
        + b"data"
        + struct.pack("<I", data_size)
    )


def _node_name_for_id(node_id: int) -> str:
    node = _node_for_id(node_id)
    if node.node_name:
        return node.node_name
    raise PipeWireRuntimeError(f"PipeWire node id {node_id} does not expose node.name.")


def _node_for_id(node_id: int) -> PipeWireNode:
    for node in list_nodes():
        if node.node_id == node_id:
            return node
    raise PipeWireRuntimeError(f"PipeWire node id {node_id} was not found.")


class PipeWireLinkAudioDuplexIO:
    """Duplex PCM I/O using pw-record/pw-play with explicit pw-link routing."""

    def __init__(
        self,
        *,
        capture_node_id: int,
        write_node_id: int,
        sample_rate: int,
        read_timeout: float,
        write_timeout: float,
        pw_record_bin: str = "pw-record",
        pw_play_bin: str = "pw-play",
    ) -> None:
        self.read_timeout = max(read_timeout, 0.0)
        self.write_timeout = max(write_timeout, 0.0)
        self._linked_pairs: list[tuple[str, str]] = []
        self._capture: subprocess.Popen[bytes] | None = None
        self._playback: subprocess.Popen[bytes] | None = None
        self._rx_fd: int | None = None
        self._tx_fd: int | None = None
        self._closed = False

        capture_node = _node_for_id(capture_node_id)
        self._capture_source_name = capture_node.node_name
        self._capture_source_id = capture_node_id
        self._capture_source_media_class = (capture_node.media_class or "").strip()
        write_node = _node_for_id(write_node_id)
        self._write_target_name = write_node.node_name
        self._write_target_id = write_node_id
        self._write_target_media_class = (write_node.media_class or "").strip()
        self._routing_mode = "explicit_link"
        self._routing_note = ""
        self._capture_target_id = capture_node_id
        self._playback_target_id = write_node_id
        self._playback_stream_header = _build_streaming_wav_header(
            sample_rate=sample_rate,
            channels=1,
            bits_per_sample=16,
        )
        self._playback_header_sent = False

        unique = f"{os.getpid()}_{int(time.monotonic() * 1000)}"
        self._capture_node_name = f"sshg_capture_{unique}"
        self._playback_node_name = f"sshg_playback_{unique}"
        self._capture_link_target_name = self._capture_node_name
        self._capture_link_target_id: int | None = None
        self._capture_link_target_aliases: list[str] = ["pw-record"]
        self._playback_link_source_name = self._playback_node_name
        self._playback_link_source_id: int | None = None
        self._playback_link_source_aliases: list[str] = ["pw-play"]
        self._known_node_ids = self._snapshot_node_ids()

        capture_env = os.environ.copy()
        capture_env["PIPEWIRE_PROPS"] = _build_pipewire_props(
            self._capture_node_name,
            "sshg_capture",
        )
        playback_env = os.environ.copy()
        playback_env["PIPEWIRE_PROPS"] = _build_pipewire_props(
            self._playback_node_name,
            "sshg_playback",
        )

        try:
            self._choose_routing_mode()
        except Exception:
            self.close()
            raise
        capture_target = str(self._capture_target_id)
        playback_target = str(self._playback_target_id)

        capture_cmd = [
            pw_record_bin,
            "--target",
            capture_target,
            "--rate",
            str(sample_rate),
            "--channels",
            "1",
            "--format",
            "s16",
            "--latency",
            "30ms",
            "-",
        ]
        playback_cmd = [
            pw_play_bin,
            "--target",
            playback_target,
            "--rate",
            str(sample_rate),
            "--channels",
            "1",
            "--format",
            "s16",
            "--latency",
            "30ms",
            "-",
        ]

        try:
            self._capture = subprocess.Popen(
                capture_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                env=capture_env,
            )
            self._playback = subprocess.Popen(
                playback_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
                env=playback_env,
            )
        except FileNotFoundError as exc:
            raise PipeWireRuntimeError(
                "pw-record/pw-play executable not found; install PipeWire CLI tools."
            ) from exc
        except Exception as exc:
            raise PipeWireRuntimeError(f"Failed to start pw-record/pw-play pipelines: {exc}") from exc

        try:
            if self._capture.poll() is not None:
                raise PipeWireRuntimeError(_process_stderr(self._capture, "pw-record capture"))
            if self._playback.poll() is not None:
                raise PipeWireRuntimeError(_process_stderr(self._playback, "pw-play playback"))
            if self._capture.stdout is None or self._playback.stdin is None:
                raise PipeWireRuntimeError("Failed to initialize pw-record/pw-play pipes.")
            self._rx_fd = self._capture.stdout.fileno()
            self._tx_fd = self._playback.stdin.fileno()
            os.set_blocking(self._rx_fd, False)
            os.set_blocking(self._tx_fd, False)

            self._wait_for_process_stability()
            if self._routing_mode == "explicit_link":
                self._refresh_dynamic_link_nodes()
                try:
                    self._ensure_links_ready()
                except PipeWireRuntimeError as exc:
                    detail = str(exc).splitlines()[0] if str(exc).strip() else str(exc)
                    self._routing_mode = "direct_target_fallback"
                    self._routing_note = f"explicit link setup failed: {detail}"
                    print(
                        "[sshg] warning: PipeWire explicit link setup failed; continuing with direct "
                        f"targets (capture target={self._capture_target_id}, "
                        f"write target={self._playback_target_id}): {detail}",
                        file=sys.stderr,
                    )
        except Exception:
            self.close()
            raise

    def _choose_routing_mode(self) -> None:
        self._capture_target_id = self._capture_source_id
        self._playback_target_id = self._write_target_id
        output_prefixes = _build_port_prefixes(
            node_name=self._capture_source_name,
            node_id=self._capture_source_id,
        )
        input_prefixes = _build_port_prefixes(
            node_name=self._write_target_name,
            node_id=self._write_target_id,
        )
        try:
            capture_ports, capture_listing = _ports_for_node(
                node_name=self._capture_source_name,
                node_id=self._capture_source_id,
                direction="output",
            )
            write_ports, write_listing = _ports_for_node(
                node_name=self._write_target_name,
                node_id=self._write_target_id,
                direction="input",
            )
        except PipeWireRuntimeError as exc:
            self._routing_mode = "direct_target_fallback"
            self._routing_note = f"pw-link probe failed: {exc}"
            reasons = self._retarget_stream_nodes_for_direct_fallback()
            reason_suffix = f" ({'; '.join(reasons)})" if reasons else ""
            print(
                "[sshg] warning: PipeWire link probing failed; using direct target fallback "
                f"(capture target={self._capture_target_id}, write target={self._playback_target_id})"
                f"{reason_suffix}",
                file=sys.stderr,
            )
            return

        if capture_ports and write_ports:
            self._routing_mode = "explicit_link"
            return

        if not self._pipewire_has_visible_ports():
            raise PipeWireRuntimeError(
                "PipeWire exposes no visible Port objects (pw-cli ls Port returned empty), "
                "so pw-link/direct routing cannot be established. "
                "Ensure a PipeWire session manager is running (wireplumber or pipewire-media-session)."
            )

        self._routing_mode = "direct_target_fallback"
        reasons = self._retarget_stream_nodes_for_direct_fallback()
        capture_sample = "\n".join(capture_listing.splitlines()[:8]) or "<none>"
        write_sample = "\n".join(write_listing.splitlines()[:8]) or "<none>"
        reason_suffix = f" ({'; '.join(reasons)})" if reasons else ""
        self._routing_note = (
            "no linkable ports available "
            f"(capture prefixes={','.join(output_prefixes)}, write prefixes={','.join(input_prefixes)})"
        )
        print(
            "[sshg] warning: no linkable PipeWire ports found for selected nodes; "
            f"using direct target fallback (capture target={self._capture_target_id}, "
            f"write target={self._playback_target_id}){reason_suffix}. "
            f"capture sample={capture_sample!r} write sample={write_sample!r}",
            file=sys.stderr,
        )

    def _retarget_capture_stream_for_direct_fallback(self) -> list[str]:
        capture_media = self._capture_source_media_class.lower()
        if "stream/output/audio" not in capture_media:
            return []

        stream_sink_id = self._sink_for_playback_stream_node(self._capture_source_id)
        if stream_sink_id is not None:
            self._capture_target_id = stream_sink_id
            return [
                f"capture stream node {self._capture_source_id} maps to active sink node {stream_sink_id}"
            ]

        sink_id = self._first_sink_node_id()
        if sink_id is None:
            return []

        self._capture_target_id = sink_id
        return [
            f"capture stream node {self._capture_source_id} uses fallback sink node {sink_id} for direct capture"
        ]

    def _snapshot_node_ids(self) -> set[int]:
        try:
            return {node.node_id for node in list_nodes()}
        except PipeWireRuntimeError:
            return set()

    def _select_stream_link_node(
        self,
        *,
        nodes: list[PipeWireNode],
        new_nodes: list[PipeWireNode],
        preferred_name: str,
        expected_media_class: str,
    ) -> PipeWireNode | None:
        if preferred_name:
            for node in nodes:
                if node.node_name == preferred_name:
                    return node

        lowered_expected = expected_media_class.lower()
        media_matches = [
            node
            for node in new_nodes
            if lowered_expected in (node.media_class or "").strip().lower()
        ]
        if len(media_matches) == 1:
            return media_matches[0]

        prefixed = [node for node in media_matches if (node.node_name or "").startswith("sshg_")]
        if prefixed:
            return max(prefixed, key=lambda node: node.node_id)
        if media_matches:
            return max(media_matches, key=lambda node: node.node_id)
        return None

    def _refresh_dynamic_link_nodes(self) -> None:
        try:
            nodes = list_nodes()
        except PipeWireRuntimeError:
            return

        new_nodes = [node for node in nodes if node.node_id not in self._known_node_ids]

        capture_target = self._select_stream_link_node(
            nodes=nodes,
            new_nodes=new_nodes,
            preferred_name=self._capture_node_name,
            expected_media_class="stream/input/audio",
        )
        if capture_target is not None:
            if capture_target.node_name:
                self._capture_link_target_name = capture_target.node_name
            self._capture_link_target_id = capture_target.node_id
            self._capture_link_target_aliases = _node_alias_candidates(capture_target)

        playback_source = self._select_stream_link_node(
            nodes=nodes,
            new_nodes=new_nodes,
            preferred_name=self._playback_node_name,
            expected_media_class="stream/output/audio",
        )
        if playback_source is not None:
            if playback_source.node_name:
                self._playback_link_source_name = playback_source.node_name
            self._playback_link_source_id = playback_source.node_id
            self._playback_link_source_aliases = _node_alias_candidates(playback_source)

        self._known_node_ids = {node.node_id for node in nodes}

    def _retarget_stream_nodes_for_direct_fallback(self) -> list[str]:
        reasons: list[str] = []
        sink_id: int | None = None
        capture_media = self._capture_source_media_class.lower()
        write_media = self._write_target_media_class.lower()

        if "stream/output/audio" in capture_media:
            reasons.extend(self._retarget_capture_stream_for_direct_fallback())
            if self._capture_target_id != self._capture_source_id:
                sink_id = self._capture_target_id

        if "stream/input/audio" in write_media:
            if sink_id is None:
                sink_id = self._first_sink_node_id()
            if sink_id is not None:
                self._playback_target_id = sink_id
                reasons.append(
                    f"write stream node {self._write_target_id} has no exposed ports; "
                    f"routing playback via sink node {sink_id}"
                )

        return reasons

    def _sink_for_playback_stream_node(self, node_id: int) -> int | None:
        for stream in list_active_playback_streams():
            if stream.node_id != node_id:
                continue
            sink_id = stream.sink_id
            if sink_id is None:
                continue
            if self._is_sink_node_id(sink_id):
                return sink_id
        return None

    def _is_sink_node_id(self, node_id: int) -> bool:
        try:
            nodes = list_nodes()
        except PipeWireRuntimeError:
            return False
        for node in nodes:
            if node.node_id != node_id:
                continue
            media_class = (node.media_class or "").strip().lower()
            return media_class.startswith("audio/sink")
        return False

    def _first_sink_node_id(self) -> int | None:
        try:
            nodes = list_nodes()
        except PipeWireRuntimeError:
            return None
        for node in nodes:
            media_class = (node.media_class or "").strip().lower()
            if media_class.startswith("audio/sink"):
                return node.node_id
        return None

    def _pipewire_has_visible_ports(self) -> bool:
        try:
            listing = _run_pw_cli(["ls", "Port"])
        except PipeWireRuntimeError:
            # If we cannot probe ports here, avoid hard-failing on diagnostics alone.
            return True
        for raw in listing.splitlines():
            if _PORT_HEADER_RE.match(raw.strip()):
                return True
        return False

    def _close_fd(self, attr_name: str) -> None:
        fd = getattr(self, attr_name, None)
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        setattr(self, attr_name, None)

    def _wait_for_process_stability(self) -> None:
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if self._capture.poll() is not None:
                raise PipeWireRuntimeError(_process_stderr(self._capture, "pw-record capture"))
            if self._playback.poll() is not None:
                raise PipeWireRuntimeError(_process_stderr(self._playback, "pw-play playback"))
            time.sleep(0.01)

    def _ensure_links_ready(self) -> None:
        self._link_direction(
            output_node_name=self._capture_source_name,
            output_node_id=self._capture_source_id,
            input_node_name=self._capture_link_target_name,
            input_node_id=self._capture_link_target_id,
            input_aliases=self._capture_link_target_aliases,
            label="capture",
        )
        self._link_direction(
            output_node_name=self._playback_link_source_name,
            output_node_id=self._playback_link_source_id,
            output_aliases=self._playback_link_source_aliases,
            input_node_name=self._write_target_name,
            input_node_id=self._write_target_id,
            label="write",
        )

    def _link_direction(
        self,
        *,
        output_node_name: str,
        output_node_id: int | None,
        input_node_name: str,
        input_node_id: int | None,
        label: str,
        output_aliases: list[str] | None = None,
        input_aliases: list[str] | None = None,
    ) -> None:
        deadline = time.monotonic() + 3.0
        output_ports: list[str] = []
        input_ports: list[str] = []
        output_listing = ""
        input_listing = ""

        while time.monotonic() < deadline:
            output_ports, output_listing = _ports_for_node(
                node_name=output_node_name,
                node_id=output_node_id,
                direction="output",
                alias_candidates=output_aliases,
            )
            input_ports, input_listing = _ports_for_node(
                node_name=input_node_name,
                node_id=input_node_id,
                direction="input",
                alias_candidates=input_aliases,
            )
            if output_ports and input_ports:
                break
            time.sleep(0.05)

        if not output_ports:
            output_prefixes = _build_port_prefixes(
                node_name=output_node_name,
                node_id=output_node_id,
                alias_candidates=output_aliases,
            )
            output_sample = "\n".join(output_listing.splitlines()[:12]) or "<none>"
            raise PipeWireRuntimeError(
                f"Failed to find output ports for {label} link source node "
                f"'{output_node_name}' (id={output_node_id}). "
                f"Tried prefixes: {', '.join(output_prefixes)}.\n"
                f"Available output ports sample:\n{output_sample}"
            )
        if not input_ports:
            input_prefixes = _build_port_prefixes(
                node_name=input_node_name,
                node_id=input_node_id,
                alias_candidates=input_aliases,
            )
            input_sample = "\n".join(input_listing.splitlines()[:12]) or "<none>"
            raise PipeWireRuntimeError(
                f"Failed to find input ports for {label} link target node "
                f"'{input_node_name}' (id={input_node_id}). "
                f"Tried prefixes: {', '.join(input_prefixes)}.\n"
                f"Available input ports sample:\n{input_sample}"
            )

        pair_count = min(len(output_ports), len(input_ports))
        if pair_count < 1:
            raise PipeWireRuntimeError(f"No compatible ports available to establish {label} PipeWire links.")

        for out_port, in_port in zip(output_ports[:pair_count], input_ports[:pair_count]):
            try:
                _run_pw_link([out_port, in_port])
            except PipeWireRuntimeError as exc:
                if "File exists" not in str(exc):
                    raise
            self._linked_pairs.append((out_port, in_port))

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        if self._capture.poll() is not None:
            raise PipeWireRuntimeError(_process_stderr(self._capture, "pw-record capture"))
        if max_bytes < 1:
            return b""
        if self._rx_fd is None:
            raise PipeWireRuntimeError("Capture pipe read fd is not initialized.")

        ready, _, _ = select.select([self._rx_fd], [], [], self.read_timeout)
        if not ready:
            return b""
        try:
            return os.read(self._rx_fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return b""
            raise PipeWireRuntimeError(f"pw-record capture read failed: {exc}") from exc

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        if self._playback.poll() is not None:
            raise PipeWireRuntimeError(_process_stderr(self._playback, "pw-play playback"))
        if self._tx_fd is None:
            raise PipeWireRuntimeError("Playback pipe write fd is not initialized.")

        if not self._playback_header_sent:
            self._write_to_playback(self._playback_stream_header)
            self._playback_header_sent = True

        self._write_to_playback(data)

    def _write_to_playback(self, payload: bytes) -> None:
        if not payload:
            return
        if self._tx_fd is None:
            raise PipeWireRuntimeError("Playback pipe write fd is not initialized.")

        view = memoryview(payload)
        deadline = self.write_timeout
        while view:
            _, writable, _ = select.select([], [self._tx_fd], [], deadline)
            if not writable:
                raise PipeWireRuntimeError("Timed out writing PCM data to pw-play playback process")
            try:
                written = os.write(self._tx_fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    continue
                raise PipeWireRuntimeError(f"pw-play playback write failed: {exc}") from exc
            if written <= 0:
                raise PipeWireRuntimeError("Zero-byte write to pw-play playback pipeline")
            view = view[written:]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for out_port, in_port in reversed(self._linked_pairs):
            try:
                _run_pw_link(["-d", out_port, in_port])
            except Exception:
                pass
        self._linked_pairs = []

        for proc, close_stdin in (
            (getattr(self, "_playback", None), True),
            (getattr(self, "_capture", None), False),
        ):
            if proc is None:
                continue
            try:
                if close_stdin and proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)

        self._close_fd("_rx_fd")
        self._close_fd("_tx_fd")
