"""PTY shell process management used by stream-oriented transports."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios


class PtyShellError(RuntimeError):
    """Raised when PTY shell operations fail."""


class PtyShellSession:
    """Persistent PTY-backed shell process."""

    def __init__(self, shell_path: str, *, cols: int = 80, rows: int = 24) -> None:
        self.shell_path = shell_path
        self._master_fd: int | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.start(cols=cols, rows=rows)

    def start(self, *, cols: int, rows: int) -> None:
        if self._proc is not None:
            raise PtyShellError("PTY shell is already running")

        cols = max(int(cols), 1)
        rows = max(int(rows), 1)

        master_fd, slave_fd = pty.openpty()

        def _preexec() -> None:
            os.setsid()
            try:
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except OSError:
                # Some environments already have a controlling TTY attached.
                pass

        try:
            self._set_winsize(slave_fd, cols=cols, rows=rows)
            proc = subprocess.Popen(
                [self.shell_path],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_preexec,
            )
        except Exception as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise PtyShellError(f"Failed to start PTY shell: {exc}") from exc

        os.close(slave_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._master_fd = master_fd
        self._proc = proc

    def _ensure_proc(self) -> subprocess.Popen[bytes]:
        if self._proc is None:
            raise PtyShellError("PTY shell process is not initialized")
        return self._proc

    def _ensure_master_fd(self) -> int:
        if self._master_fd is None:
            raise PtyShellError("PTY master fd is not initialized")
        return self._master_fd

    def _set_winsize(self, fd: int, *, cols: int, rows: int) -> None:
        packed = struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
        except OSError as exc:
            raise PtyShellError(f"Failed to resize PTY: {exc}") from exc

    def write_input(self, data: bytes) -> None:
        if not data:
            return

        proc = self._ensure_proc()
        if proc.poll() is not None:
            raise PtyShellError("PTY shell has already exited")

        fd = self._ensure_master_fd()
        view = memoryview(data)

        while view:
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                select.select([], [fd], [], 0.1)
                continue
            except OSError as exc:
                raise PtyShellError(f"Failed to write to PTY: {exc}") from exc

            if written <= 0:
                raise PtyShellError("Failed to write to PTY: zero-byte write")
            view = view[written:]

    def read_output(self, *, timeout: float = 0.0, max_bytes: int = 65536) -> bytes:
        fd = self._ensure_master_fd()
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")

        if timeout > 0:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return b""
        else:
            ready, _, _ = select.select([fd], [], [], 0.0)
            if not ready:
                return b""

        try:
            return os.read(fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:
                return b""
            raise PtyShellError(f"Failed to read from PTY: {exc}") from exc

    def resize(self, *, cols: int, rows: int) -> None:
        fd = self._ensure_master_fd()
        self._set_winsize(fd, cols=max(cols, 1), rows=max(rows, 1))

    def send_signal(self, signal_name: str) -> None:
        proc = self._ensure_proc()
        if proc.poll() is not None:
            return

        mapping = {
            "INT": signal.SIGINT,
            "TERM": signal.SIGTERM,
            "HUP": signal.SIGHUP,
            "QUIT": signal.SIGQUIT,
        }
        sig = mapping.get(signal_name.upper())
        if sig is None:
            raise PtyShellError(f"Unsupported signal: {signal_name}")

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, sig)
        except OSError as exc:
            raise PtyShellError(f"Failed to send signal to PTY shell: {exc}") from exc

    def is_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def wait_exit(self, *, timeout: float | None = None) -> int | None:
        proc = self._proc
        if proc is None:
            return None

        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        proc = self._proc
        self._proc = None

        master_fd = self._master_fd
        self._master_fd = None

        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=1.0)

        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
