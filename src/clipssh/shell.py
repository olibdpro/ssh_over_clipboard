"""Shell process management for the clipboard server."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from typing import IO
from typing import Literal
import uuid

MARKER_PREFIX = "__CLIPSSH_DONE__"


class ShellUnavailableError(RuntimeError):
    """Raised when no supported shell is available."""


class ShellExecutionError(RuntimeError):
    """Raised when command execution fails unexpectedly."""



def resolve_shell(preferred: str = "tcsh") -> tuple[str, Literal["tcsh", "sh"]]:
    preferred_path = shutil.which(preferred)
    if preferred_path:
        flavor: Literal["tcsh", "sh"] = "tcsh" if os.path.basename(preferred_path) == "tcsh" else "sh"
        return preferred_path, flavor

    fallback = shutil.which("sh") or "/bin/sh"
    if os.path.exists(fallback):
        return fallback, "sh"

    raise ShellUnavailableError(
        f"Could not find preferred shell '{preferred}' or fallback '/bin/sh'."
    )


class ShellSession:
    """Persistent shell process receiving commands on stdin."""

    def __init__(self, shell_path: str, shell_flavor: Literal["tcsh", "sh"]) -> None:
        self.shell_path = shell_path
        self.shell_flavor = shell_flavor
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._start()

    def _start(self) -> None:
        self._proc = subprocess.Popen(
            [self.shell_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        if self._proc.stdin is None or self._proc.stdout is None or self._proc.stderr is None:
            raise ShellExecutionError("Failed to initialize shell stdio streams")

        self._stdout_thread = threading.Thread(
            target=self._read_stream,
            args=("stdout", self._proc.stdout),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stream,
            args=("stderr", self._proc.stderr),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stream(self, stream_name: str, stream: IO[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._queue.put((stream_name, line))
        finally:
            self._queue.put((stream_name, None))

    def _ensure_running(self) -> subprocess.Popen[str]:
        if self._proc is None:
            raise ShellExecutionError("Shell process is not initialized")
        if self._proc.poll() is not None:
            raise ShellExecutionError("Shell process has already exited")
        return self._proc

    def _build_script(self, command: str, marker_id: str) -> str:
        if self.shell_flavor == "tcsh":
            return (
                f"{command}\n"
                "set __clipssh_rc=$status\n"
                f"echo \"{MARKER_PREFIX}{marker_id}:$__clipssh_rc\"\n"
            )

        return (
            f"{command}\n"
            "__clipssh_rc=$?\n"
            f"echo \"{MARKER_PREFIX}{marker_id}:$__clipssh_rc\"\n"
        )

    def execute(self, command: str, timeout: float = 60.0) -> tuple[str, str, int]:
        proc = self._ensure_running()
        if proc.stdin is None:
            raise ShellExecutionError("Shell stdin is not available")

        marker_id = uuid.uuid4().hex
        marker_prefix = f"{MARKER_PREFIX}{marker_id}:"
        script = self._build_script(command, marker_id)

        try:
            proc.stdin.write(script)
            proc.stdin.flush()
        except OSError as exc:
            raise ShellExecutionError(f"Failed to write command to shell: {exc}") from exc

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code: int | None = None
        deadline = time.monotonic() + timeout

        while exit_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShellExecutionError(f"Timed out waiting for command completion: {command!r}")

            try:
                stream_name, line = self._queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue

            if line is None:
                raise ShellExecutionError("Shell stream closed unexpectedly")

            if stream_name == "stdout":
                stripped = line.rstrip("\n")
                if stripped.startswith(marker_prefix):
                    raw_code = stripped[len(marker_prefix) :].strip()
                    try:
                        exit_code = int(raw_code)
                    except ValueError:
                        exit_code = 1
                    continue
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)

        # Drain any already-buffered output that was emitted before completion.
        drain_until = time.monotonic() + 0.05
        while time.monotonic() < drain_until:
            try:
                stream_name, line = self._queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue

            if stream_name == "stdout":
                stripped = line.rstrip("\n")
                if stripped.startswith(marker_prefix):
                    continue
                stdout_parts.append(line)
            else:
                stderr_parts.append(line)

        return "".join(stdout_parts), "".join(stderr_parts), exit_code

    def close(self) -> None:
        if self._proc is None:
            return

        proc = self._proc
        self._proc = None

        if proc.poll() is None and proc.stdin is not None:
            try:
                proc.stdin.write("exit\n")
                proc.stdin.flush()
            except OSError:
                pass

        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
