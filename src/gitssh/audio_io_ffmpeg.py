"""FFmpeg-backed duplex PCM I/O for audio transport."""

from __future__ import annotations

import errno
import os
import select
import subprocess
from typing import Dict
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


def _process_stderr(proc: subprocess.Popen[bytes] | subprocess.Popen[str], label: str) -> str:
    stderr_text = ""
    try:
        if proc.stderr is not None:
            raw = proc.stderr.read()
            if isinstance(raw, bytes):
                stderr_text = raw.decode("utf-8", errors="ignore")
            else:
                stderr_text = raw or ""
    except Exception:
        stderr_text = ""

    cleaned = (stderr_text or "").strip()
    if cleaned:
        return f"{label} process exited unexpectedly: {cleaned}"
    return f"{label} process exited unexpectedly"


def _ffmpeg_format_capabilities(ffmpeg_bin: str) -> dict[str, tuple[bool, bool]]:
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-formats"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioIOError(f"ffmpeg executable not found: {ffmpeg_bin}") from exc
    except Exception as exc:
        raise AudioIOError(f"Failed to query ffmpeg formats: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise AudioIOError(f"ffmpeg -formats failed: {stderr or 'unknown error'}")

    caps: dict[str, tuple[bool, bool]] = {}
    for raw in (result.stdout or "").splitlines():
        line = raw.rstrip()
        if len(line) < 6:
            continue
        if line.startswith("File formats:"):
            continue
        if not line.startswith(" "):
            continue
        flag_d = line[1] == "D"
        flag_e = line[2] == "E"
        if line[3] != " ":
            continue
        rest = line[4:].strip()
        if not rest:
            continue
        token = rest.split()[0]
        # token may contain comma-separated aliases.
        for name in token.split(","):
            if not name:
                continue
            prev = caps.get(name, (False, False))
            caps[name] = (prev[0] or flag_d, prev[1] or flag_e)
    return caps


def _format_duplex_backends(caps: dict[str, tuple[bool, bool]]) -> str:
    names = sorted(name for name, (can_in, can_out) in caps.items() if can_in and can_out)
    return ", ".join(names) if names else "<none>"


def resolve_audio_backend(ffmpeg_bin: str, requested_backend: str) -> str:
    caps = _ffmpeg_format_capabilities(ffmpeg_bin)
    requested = (requested_backend or "").strip().lower()

    if requested in {"", "auto"}:
        for candidate in ("pulse", "pipewire", "alsa", "jack", "sndio", "oss"):
            can_in, can_out = caps.get(candidate, (False, False))
            if can_in and can_out:
                return candidate
        raise AudioIOError(
            "No duplex ffmpeg audio backend found. "
            f"Available duplex backends: {_format_duplex_backends(caps)}"
        )

    can_in, can_out = caps.get(requested, (False, False))
    if not can_in or not can_out:
        raise AudioIOError(
            f"ffmpeg backend '{requested_backend}' is not available for both input and output. "
            f"Available duplex backends: {_format_duplex_backends(caps)}"
        )
    return requested


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
        self.backend = resolve_audio_backend(ffmpeg_bin, backend)

        capture_cmd = _build_ffmpeg_capture_cmd(
            ffmpeg_bin=ffmpeg_bin,
            backend=self.backend,
            input_device=input_device,
            sample_rate=sample_rate,
        )
        playback_cmd = _build_ffmpeg_playback_cmd(
            ffmpeg_bin=ffmpeg_bin,
            backend=self.backend,
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

        # Fail fast when ffmpeg exits immediately due to bad device/backend config.
        if self._capture.poll() is not None:
            raise AudioIOError(_process_stderr(self._capture, "ffmpeg capture"))
        if self._playback.poll() is not None:
            raise AudioIOError(_process_stderr(self._playback, "ffmpeg playback"))

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        if self._capture.poll() is not None:
            raise AudioIOError(_process_stderr(self._capture, "ffmpeg capture"))
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
            raise AudioIOError(_process_stderr(self._playback, "ffmpeg playback"))

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


class PulseCliAudioDuplexIO:
    """Duplex PCM I/O using `parec` and `pacat` utilities."""

    def __init__(
        self,
        *,
        input_device: str,
        output_device: str,
        sample_rate: int,
        read_timeout: float,
        write_timeout: float,
        parec_bin: str = "parec",
        pacat_bin: str = "pacat",
    ) -> None:
        self.read_timeout = max(read_timeout, 0.0)
        self.write_timeout = max(write_timeout, 0.0)

        capture_cmd = [
            parec_bin,
            "--raw",
            "--channels=1",
            f"--rate={sample_rate}",
            "--format=s16le",
            f"--device={input_device}",
        ]
        playback_cmd = [
            pacat_bin,
            "--raw",
            "--playback",
            "--channels=1",
            f"--rate={sample_rate}",
            "--format=s16le",
            f"--device={output_device}",
        ]

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
            raise AudioIOError(
                "Pulse CLI tools are not available. Install pulseaudio-utils (parec/pacat)."
            ) from exc
        except Exception as exc:
            raise AudioIOError(f"Failed to start parec/pacat pipelines: {exc}") from exc

        if self._capture.stdout is None or self._playback.stdin is None:
            self.close()
            raise AudioIOError("Failed to initialize parec/pacat pipes")

        self._rx_fd = self._capture.stdout.fileno()
        self._tx_fd = self._playback.stdin.fileno()
        os.set_blocking(self._rx_fd, False)
        os.set_blocking(self._tx_fd, False)
        self._closed = False

        if self._capture.poll() is not None:
            raise AudioIOError(_process_stderr(self._capture, "parec capture"))
        if self._playback.poll() is not None:
            raise AudioIOError(_process_stderr(self._playback, "pacat playback"))

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        if self._capture.poll() is not None:
            raise AudioIOError(_process_stderr(self._capture, "parec capture"))
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
            raise AudioIOError(f"parec read failed: {exc}") from exc

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        if self._playback.poll() is not None:
            raise AudioIOError(_process_stderr(self._playback, "pacat playback"))

        view = memoryview(data)
        deadline = self.write_timeout
        while view:
            _, writable, _ = select.select([], [self._tx_fd], [], deadline)
            if not writable:
                raise AudioIOError("Timed out writing PCM data to pacat")
            try:
                written = os.write(self._tx_fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    continue
                raise AudioIOError(f"pacat write failed: {exc}") from exc
            if written <= 0:
                raise AudioIOError("Zero-byte write to pacat")
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


def build_audio_duplex_io(
    *,
    ffmpeg_bin: str,
    backend: str,
    input_device: str,
    output_device: str,
    sample_rate: int,
    read_timeout: float,
    write_timeout: float,
) -> AudioDuplexIO:
    requested = (backend or "").strip().lower()

    if requested == "pulse-cli":
        return PulseCliAudioDuplexIO(
            input_device=input_device,
            output_device=output_device,
            sample_rate=sample_rate,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
        )

    if requested == "auto":
        ffmpeg_error: str | None = None
        try:
            return FFmpegAudioDuplexIO(
                ffmpeg_bin=ffmpeg_bin,
                backend="auto",
                input_device=input_device,
                output_device=output_device,
                sample_rate=sample_rate,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
            )
        except AudioIOError as exc:
            ffmpeg_error = str(exc)

        try:
            return PulseCliAudioDuplexIO(
                input_device=input_device,
                output_device=output_device,
                sample_rate=sample_rate,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
            )
        except AudioIOError as pulse_exc:
            raise AudioIOError(
                "No usable audio backend found. "
                f"ffmpeg attempt failed: {ffmpeg_error}; pulse-cli fallback failed: {pulse_exc}"
            ) from pulse_exc

    return FFmpegAudioDuplexIO(
        ffmpeg_bin=ffmpeg_bin,
        backend=requested,
        input_device=input_device,
        output_device=output_device,
        sample_rate=sample_rate,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
    )
