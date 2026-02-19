"""FFmpeg-backed duplex PCM I/O for audio transport."""

from __future__ import annotations

import errno
import os
import select
import subprocess
from typing import Protocol


class AudioIOError(RuntimeError):
    """Raised when audio I/O setup or streaming fails."""


class AudioDuplexIO(Protocol):
    """Minimal duplex PCM interface consumed by audio transport."""

    def read(self, max_bytes: int) -> bytes:
        ...

    def write(self, data: bytes) -> None:
        ...

    def close(self) -> None:
        ...


def _build_ffmpeg_capture_cmd(
    *,
    ffmpeg_bin: str,
    backend: str,
    input_device: str,
    sample_rate: int,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        backend,
        "-i",
        input_device,
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]


def _build_ffmpeg_playback_cmd(
    *,
    ffmpeg_bin: str,
    backend: str,
    output_device: str,
    sample_rate: int,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-i",
        "pipe:0",
        "-f",
        backend,
        output_device,
    ]


class FFmpegAudioDuplexIO:
    """Duplex PCM I/O using two ffmpeg subprocesses (capture + playback)."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        backend: str,
        input_device: str,
        output_device: str,
        sample_rate: int,
        read_timeout: float,
        write_timeout: float,
    ) -> None:
        self.read_timeout = max(read_timeout, 0.0)
        self.write_timeout = max(write_timeout, 0.0)

        capture_cmd = _build_ffmpeg_capture_cmd(
            ffmpeg_bin=ffmpeg_bin,
            backend=backend,
            input_device=input_device,
            sample_rate=sample_rate,
        )
        playback_cmd = _build_ffmpeg_playback_cmd(
            ffmpeg_bin=ffmpeg_bin,
            backend=backend,
            output_device=output_device,
            sample_rate=sample_rate,
        )

        try:
            self._capture = subprocess.Popen(
                capture_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
            self._playback = subprocess.Popen(
                playback_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            raise AudioIOError(f"ffmpeg executable not found: {ffmpeg_bin}") from exc
        except Exception as exc:
            raise AudioIOError(f"Failed to start ffmpeg audio pipelines: {exc}") from exc

        if self._capture.stdout is None or self._playback.stdin is None:
            self.close()
            raise AudioIOError("Failed to initialize ffmpeg pipes")

        self._rx_fd = self._capture.stdout.fileno()
        self._tx_fd = self._playback.stdin.fileno()
        os.set_blocking(self._rx_fd, False)
        os.set_blocking(self._tx_fd, False)
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        if self._capture.poll() is not None:
            raise AudioIOError("ffmpeg capture process exited unexpectedly")
        if max_bytes < 1:
            return b""

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
            raise AudioIOError(f"ffmpeg capture read failed: {exc}") from exc

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        if self._playback.poll() is not None:
            raise AudioIOError("ffmpeg playback process exited unexpectedly")

        view = memoryview(data)
        deadline = self.write_timeout
        while view:
            _, writable, _ = select.select([], [self._tx_fd], [], deadline)
            if not writable:
                raise AudioIOError("Timed out writing PCM data to ffmpeg playback process")
            try:
                written = os.write(self._tx_fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    continue
                raise AudioIOError(f"ffmpeg playback write failed: {exc}") from exc
            if written <= 0:
                raise AudioIOError("Zero-byte write to ffmpeg playback pipeline")
            view = view[written:]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

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

