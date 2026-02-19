"""Clipboard backend implementations."""

from __future__ import annotations

from dataclasses import dataclass
import shutil
import subprocess
import threading
from typing import Protocol


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

    def name(self) -> str:
        return self.backend_name

    def read_text(self) -> str | None:
        try:
            result = subprocess.run(
                self.read_cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"Clipboard read failed for {self.backend_name}: {exc}") from exc

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise ClipboardError(
                f"Clipboard read failed for {self.backend_name} (code {result.returncode}): {stderr}"
            )
        return result.stdout

    def write_text(self, text: str) -> None:
        try:
            subprocess.run(
                self.write_cmd,
                input=text,
                text=True,
                capture_output=True,
                check=True,
                timeout=2,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"Clipboard write failed for {self.backend_name}: {exc}") from exc


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



def _candidate_backends() -> list[CommandClipboardBackend]:
    candidates: list[CommandClipboardBackend] = []

    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        candidates.append(
            CommandClipboardBackend(
                read_cmd=["wl-paste", "--no-newline"],
                write_cmd=["wl-copy"],
                backend_name="wayland-wl-clipboard",
            )
        )

    if shutil.which("xclip"):
        candidates.append(
            CommandClipboardBackend(
                read_cmd=["xclip", "-selection", "clipboard", "-o"],
                write_cmd=["xclip", "-selection", "clipboard"],
                backend_name="xclip",
            )
        )

    if shutil.which("xsel"):
        candidates.append(
            CommandClipboardBackend(
                read_cmd=["xsel", "--clipboard", "--output"],
                write_cmd=["xsel", "--clipboard", "--input"],
                backend_name="xsel",
            )
        )

    return candidates



def detect_backend() -> ClipboardBackend:
    candidates = _candidate_backends()
    if not candidates:
        raise ClipboardError(
            "No clipboard tools found. Install wl-clipboard, xclip, or xsel."
        )
    return candidates[0]
