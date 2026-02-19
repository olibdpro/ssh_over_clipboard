"""Clipboard backend implementations."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import threading
from typing import Mapping, Protocol

BACKEND_CHOICES = ("auto", "wayland", "xclip", "xsel")


class ClipboardError(RuntimeError):
    """Raised when clipboard access fails."""


class ClipboardBackend(Protocol):
    def read_text(self) -> str | None:
        ...

    def write_text(self, text: str) -> None:
        ...

    def name(self) -> str:
        ...


@dataclass
class CommandClipboardBackend:
    """Clipboard backend driven by shell commands."""

    read_cmd: list[str]
    write_cmd: list[str]
    backend_name: str
    read_timeout: float = 2.0
    write_timeout: float = 5.0
    probe_read_timeout: float = 2.0
    probe_write_timeout: float = 2.0

    def name(self) -> str:
        return self.backend_name

    def _read_text_with_timeout(self, timeout: float) -> str | None:
        try:
            result = subprocess.run(
                self.read_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"Clipboard read failed for {self.backend_name}: {exc}") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise ClipboardError(
                f"Clipboard read failed for {self.backend_name} (code {result.returncode}): {stderr}"
            )
        return result.stdout

    def _write_text_with_timeout(self, text: str, timeout: float) -> None:
        try:
            subprocess.run(
                self.write_cmd,
                input=text,
                text=True,
                capture_output=True,
                check=True,
                timeout=timeout,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"Clipboard write failed for {self.backend_name}: {exc}") from exc

    def read_text(self) -> str | None:
        return self._read_text_with_timeout(self.read_timeout)

    def write_text(self, text: str) -> None:
        self._write_text_with_timeout(text, self.write_timeout)

    def probe_roundtrip(self, probe_text: str) -> str | None:
        self._write_text_with_timeout(probe_text, self.probe_write_timeout)
        return self._read_text_with_timeout(self.probe_read_timeout)


class MemoryClipboardBackend:
    """In-memory clipboard backend, useful for tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = ""

    def name(self) -> str:
        return "memory"

    def read_text(self) -> str | None:
        with self._lock:
            return self._value

    def write_text(self, text: str) -> None:
        with self._lock:
            self._value = text



def detect_session_type(environ: Mapping[str, str] | None = None) -> str:
    """Return one of: wayland, x11, unknown."""

    env = os.environ if environ is None else environ
    session_type = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    has_wayland = bool((env.get("WAYLAND_DISPLAY") or "").strip())
    has_x11 = bool((env.get("DISPLAY") or "").strip())

    if session_type in {"wayland", "x11"}:
        return session_type
    if has_wayland and not has_x11:
        return "wayland"
    if has_x11 and not has_wayland:
        return "x11"
    if has_wayland:
        return "wayland"
    if has_x11:
        return "x11"
    return "unknown"


def _availability() -> dict[str, bool]:
    return {
        "wayland": bool(shutil.which("wl-copy") and shutil.which("wl-paste")),
        "xclip": bool(shutil.which("xclip")),
        "xsel": bool(shutil.which("xsel")),
    }


def _ordered_backend_keys(session_type: str, backend_preference: str) -> list[str]:
    if backend_preference != "auto":
        return [backend_preference]
    if session_type == "wayland":
        return ["wayland"]
    if session_type == "x11":
        return ["xsel", "xclip"]
    return ["xsel", "wayland", "xclip"]


def _build_backend(
    backend_key: str,
    *,
    read_timeout: float,
    write_timeout: float,
    probe_read_timeout: float,
    probe_write_timeout: float,
    availability: Mapping[str, bool],
) -> CommandClipboardBackend | None:
    if backend_key == "wayland":
        if not availability["wayland"]:
            return None
        return CommandClipboardBackend(
            read_cmd=["wl-paste", "--no-newline"],
            write_cmd=["wl-copy"],
            backend_name="wayland-wl-clipboard",
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            probe_read_timeout=probe_read_timeout,
            probe_write_timeout=probe_write_timeout,
        )

    if backend_key == "xclip":
        if not availability["xclip"]:
            return None
        return CommandClipboardBackend(
            read_cmd=["xclip", "-selection", "clipboard", "-o"],
            write_cmd=["xclip", "-selection", "clipboard", "-in", "-silent"],
            backend_name="xclip",
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            probe_read_timeout=probe_read_timeout,
            probe_write_timeout=probe_write_timeout,
        )

    if backend_key == "xsel":
        if not availability["xsel"]:
            return None
        return CommandClipboardBackend(
            read_cmd=["xsel", "--clipboard", "--output"],
            write_cmd=["xsel", "--clipboard", "--input"],
            backend_name="xsel",
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            probe_read_timeout=probe_read_timeout,
            probe_write_timeout=probe_write_timeout,
        )
    return None


def _candidate_backends(
    *,
    session_type: str,
    backend_preference: str,
    read_timeout: float,
    write_timeout: float,
    probe_read_timeout: float,
    probe_write_timeout: float,
    availability: Mapping[str, bool] | None = None,
) -> list[CommandClipboardBackend]:
    resolved_availability = dict(_availability() if availability is None else availability)
    candidates: list[CommandClipboardBackend] = []
    for key in _ordered_backend_keys(session_type=session_type, backend_preference=backend_preference):
        backend = _build_backend(
            key,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            probe_read_timeout=probe_read_timeout,
            probe_write_timeout=probe_write_timeout,
            availability=resolved_availability,
        )
        if backend is not None:
            candidates.append(backend)
    return candidates


def _probe_backend(backend: ClipboardBackend) -> None:
    # Strict viability check: backend must support both write and read without timing out.
    if isinstance(backend, CommandClipboardBackend):
        text = backend.probe_roundtrip("CLIPSSH/PROBE")
    else:
        backend.write_text("CLIPSSH/PROBE")
        text = backend.read_text()
    if text is None:
        raise ClipboardError(f"Clipboard read probe failed for {backend.name()}: no text returned")


def _install_hints() -> str:
    return "\n".join(
        [
            "Install clipboard tools for this environment:",
            "- Debian/Ubuntu: sudo apt install wl-clipboard xsel xclip",
            "- Fedora: sudo dnf install wl-clipboard xsel xclip",
            "- Arch: sudo pacman -S wl-clipboard xsel xclip",
            "- Conda: conda install -c conda-forge wl-clipboard xsel xclip",
            "- pip note: pip cannot reliably install these native clipboard executables.",
        ]
    )


def _format_available_tools(availability: Mapping[str, bool]) -> str:
    available = [name for name, present in availability.items() if present]
    if not available:
        return "none"
    return ", ".join(available)


def _missing_backend_lines(expected_keys: list[str], availability: Mapping[str, bool]) -> list[str]:
    lines: list[str] = []
    for key in expected_keys:
        if availability.get(key):
            continue
        if key == "wayland":
            lines.append("- wayland: requires both `wl-copy` and `wl-paste`.")
        elif key == "xclip":
            lines.append("- xclip: requires `xclip` executable.")
        elif key == "xsel":
            lines.append("- xsel: requires `xsel` executable.")
    return lines


def detect_backend(
    *,
    backend_preference: str = "auto",
    read_timeout: float = 2.0,
    write_timeout: float = 2.0,
    probe_read_timeout: float = 2.0,
    probe_write_timeout: float = 2.0,
) -> ClipboardBackend:
    if backend_preference not in BACKEND_CHOICES:
        raise ClipboardError(
            f"Unknown clipboard backend '{backend_preference}'. "
            f"Expected one of: {', '.join(BACKEND_CHOICES)}."
        )

    session_type = detect_session_type()
    availability = _availability()
    expected_keys = _ordered_backend_keys(session_type=session_type, backend_preference=backend_preference)
    candidates = _candidate_backends(
        session_type=session_type,
        backend_preference=backend_preference,
        read_timeout=max(read_timeout, 0.1),
        write_timeout=max(write_timeout, 0.1),
        probe_read_timeout=max(probe_read_timeout, 0.1),
        probe_write_timeout=max(probe_write_timeout, 0.1),
        availability=availability,
    )
    if not candidates:
        missing_lines = _missing_backend_lines(expected_keys=expected_keys, availability=availability)
        details = "\n".join(missing_lines) if missing_lines else "- No matching backend executable found."
        raise ClipboardError(
            "\n".join(
                [
                    "No viable clipboard backend found.",
                    f"Session: {session_type}",
                    f"Preference: {backend_preference}",
                    f"Available tools: {_format_available_tools(availability)}",
                    "Missing requirements:",
                    details,
                    _install_hints(),
                ]
            )
        )

    failures: list[str] = []
    for backend in candidates:
        try:
            _probe_backend(backend)
            return backend
        except ClipboardError as exc:
            failures.append(f"- {backend.name()}: {exc}")

    raise ClipboardError(
        "\n".join(
            [
                "No viable clipboard backend found.",
                f"Session: {session_type}",
                f"Preference: {backend_preference}",
                f"Available tools: {_format_available_tools(availability)}",
                "Attempted backends:",
                *failures,
                _install_hints(),
            ]
        )
    )
