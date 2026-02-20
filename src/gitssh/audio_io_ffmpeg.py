"""FFmpeg-backed duplex PCM I/O for audio transport."""

from __future__ import annotations

import errno
import os
import select
import subprocess
import time
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


def _run_pactl(args: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioIOError(
            "pactl executable not found; install pulseaudio-utils or pass explicit Pulse device names"
        ) from exc
    except Exception as exc:
        raise AudioIOError(f"Failed to execute pactl {' '.join(args)}: {exc}") from exc
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _parse_pactl_short_devices(output: str) -> list[str]:
    names: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        names.append(fields[1])
    return names


def _list_pulse_devices(kind: str) -> tuple[list[str], str]:
    plural = "sources" if kind == "source" else "sinks"
    code, stdout, stderr = _run_pactl(["list", "short", plural])
    if code != 0:
        detail = stderr or stdout or "unknown error"
        raise AudioIOError(f"pactl list short {plural} failed: {detail}")
    return _parse_pactl_short_devices(stdout), stdout


def _default_device_from_pactl_info(kind: str) -> str:
    code, stdout, stderr = _run_pactl(["info"])
    if code != 0:
        detail = stderr or stdout or "unknown error"
        raise AudioIOError(f"pactl info failed: {detail}")

    prefix = "Default Source:" if kind == "source" else "Default Sink:"
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        value = line.split(":", 1)[1].strip()
        if value:
            return value
        break
    raise AudioIOError(f"pactl info did not report {prefix.lower()}")


def _format_device_listing(kind: str, names: list[str], raw_output: str) -> str:
    plural = "sources" if kind == "source" else "sinks"
    if names:
        listed = "\n".join(f"- {name}" for name in names)
        return f"Available Pulse {plural}:\n{listed}"

    trimmed = raw_output.strip()
    if trimmed:
        return f"Available Pulse {plural} (raw):\n{trimmed}"
    return f"No Pulse {plural} were reported by pactl."


def _resolve_default_pulse_device(kind: str) -> str:
    subcmd = "get-default-source" if kind == "source" else "get-default-sink"

    code, stdout, stderr = _run_pactl([subcmd])
    if code == 0 and stdout:
        return stdout
    get_default_detail = stderr or stdout or "unknown error"

    try:
        return _default_device_from_pactl_info(kind)
    except AudioIOError as info_exc:
        names: list[str] = []
        raw_devices = ""
        list_error = ""
        try:
            names, raw_devices = _list_pulse_devices(kind)
        except AudioIOError as list_exc:
            list_error = str(list_exc)

        lines = [
            f"Unable to resolve default Pulse {kind}.",
            f"`pactl {subcmd}` failed: {get_default_detail}",
            f"`pactl info` fallback failed: {info_exc}",
        ]
        if list_error:
            lines.append(f"Could not list available Pulse devices: {list_error}")
        else:
            lines.append(_format_device_listing(kind, names, raw_devices))
        raise AudioIOError("\n".join(lines)) from info_exc


def _resolve_pulse_device_name(*, kind: str, requested: str, arg_name: str) -> str:
    value = (requested or "").strip()
    if not value or value == "default":
        resolved = _resolve_default_pulse_device(kind)
    else:
        resolved = value

    names: list[str] = []
    raw_devices = ""
    try:
        names, raw_devices = _list_pulse_devices(kind)
    except AudioIOError:
        return resolved

    if names and resolved not in names:
        lines = [
            f"Pulse {kind} '{resolved}' was not found for `{arg_name}`.",
            _format_device_listing(kind, names, raw_devices),
        ]
        if value in {"", "default"}:
            lines.append(f"`{arg_name}` resolved to '{resolved}' from the Pulse default device.")
        raise AudioIOError("\n".join(lines))

    return resolved


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
        self._wait_for_startup_stability()

    def _wait_for_startup_stability(self) -> None:
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if self._capture.poll() is not None:
                raise AudioIOError(_process_stderr(self._capture, "ffmpeg capture"))
            if self._playback.poll() is not None:
                raise AudioIOError(_process_stderr(self._playback, "ffmpeg playback"))
            time.sleep(0.01)

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

        resolved_input = _resolve_pulse_device_name(
            kind="source",
            requested=input_device,
            arg_name="--audio-input-device",
        )
        resolved_output = _resolve_pulse_device_name(
            kind="sink",
            requested=output_device,
            arg_name="--audio-output-device",
        )

        capture_cmd = [
            parec_bin,
            "--raw",
            "--channels=1",
            f"--rate={sample_rate}",
            "--format=s16le",
            "--latency-msec=30",
            f"--device={resolved_input}",
        ]
        playback_cmd = [
            pacat_bin,
            "--raw",
            "--playback",
            "--channels=1",
            f"--rate={sample_rate}",
            "--format=s16le",
            "--latency-msec=30",
            f"--device={resolved_output}",
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
        pulse_error: str | None = None
        try:
            return PulseCliAudioDuplexIO(
                input_device=input_device,
                output_device=output_device,
                sample_rate=sample_rate,
                read_timeout=read_timeout,
                write_timeout=write_timeout,
            )
        except AudioIOError as exc:
            pulse_error = str(exc)

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
        except AudioIOError as ffmpeg_exc:
            raise AudioIOError(
                "No usable audio backend found. "
                f"pulse-cli attempt failed: {pulse_error}; ffmpeg fallback failed: {ffmpeg_exc}"
            ) from ffmpeg_exc

    return FFmpegAudioDuplexIO(
        ffmpeg_bin=ffmpeg_bin,
        backend=requested,
        input_device=input_device,
        output_device=output_device,
        sample_rate=sample_rate,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
    )
