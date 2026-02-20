from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_io_ffmpeg import AudioIOError, PulseCliAudioDuplexIO, build_audio_duplex_io


class _DummyPipe:
    def fileno(self) -> int:
        return 0

    def close(self) -> None:
        return


class _DummyProcess:
    def __init__(self, *, has_stdout: bool, has_stdin: bool) -> None:
        self.stdout = _DummyPipe() if has_stdout else None
        self.stdin = _DummyPipe() if has_stdin else None
        self.stderr = _DummyPipe()

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        return


class AudioIoFfmpegTests(unittest.TestCase):
    def test_auto_prefers_pulse_cli(self) -> None:
        pulse_obj = object()
        with (
            mock.patch("gitssh.audio_io_ffmpeg.PulseCliAudioDuplexIO", return_value=pulse_obj) as pulse_ctor,
            mock.patch("gitssh.audio_io_ffmpeg.FFmpegAudioDuplexIO") as ffmpeg_ctor,
        ):
            result = build_audio_duplex_io(
                ffmpeg_bin="ffmpeg",
                backend="auto",
                input_device="in",
                output_device="out",
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )

        self.assertIs(result, pulse_obj)
        pulse_ctor.assert_called_once()
        ffmpeg_ctor.assert_not_called()

    def test_auto_falls_back_to_ffmpeg_after_pulse_cli_error(self) -> None:
        ffmpeg_obj = object()
        with (
            mock.patch(
                "gitssh.audio_io_ffmpeg.PulseCliAudioDuplexIO",
                side_effect=AudioIOError("pulse failed"),
            ) as pulse_ctor,
            mock.patch("gitssh.audio_io_ffmpeg.FFmpegAudioDuplexIO", return_value=ffmpeg_obj) as ffmpeg_ctor,
        ):
            result = build_audio_duplex_io(
                ffmpeg_bin="ffmpeg",
                backend="auto",
                input_device="in",
                output_device="out",
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )

        self.assertIs(result, ffmpeg_obj)
        pulse_ctor.assert_called_once()
        ffmpeg_ctor.assert_called_once()
        self.assertEqual(ffmpeg_ctor.call_args.kwargs["backend"], "auto")

    def test_auto_reports_both_errors_when_no_backend_works(self) -> None:
        with (
            mock.patch(
                "gitssh.audio_io_ffmpeg.PulseCliAudioDuplexIO",
                side_effect=AudioIOError("pulse failed"),
            ) as pulse_ctor,
            mock.patch(
                "gitssh.audio_io_ffmpeg.FFmpegAudioDuplexIO",
                side_effect=AudioIOError("ffmpeg failed"),
            ) as ffmpeg_ctor,
        ):
            with self.assertRaises(AudioIOError) as raised:
                build_audio_duplex_io(
                    ffmpeg_bin="ffmpeg",
                    backend="auto",
                    input_device="in",
                    output_device="out",
                    sample_rate=48000,
                    read_timeout=0.01,
                    write_timeout=0.05,
                )

        message = str(raised.exception)
        self.assertIn("pulse-cli attempt failed", message)
        self.assertIn("pulse failed", message)
        self.assertIn("ffmpeg fallback failed", message)
        self.assertIn("ffmpeg failed", message)
        pulse_ctor.assert_called_once()
        ffmpeg_ctor.assert_called_once()

    def test_explicit_pulse_cli_uses_pulse_only(self) -> None:
        pulse_obj = object()
        with (
            mock.patch("gitssh.audio_io_ffmpeg.PulseCliAudioDuplexIO", return_value=pulse_obj) as pulse_ctor,
            mock.patch("gitssh.audio_io_ffmpeg.FFmpegAudioDuplexIO") as ffmpeg_ctor,
        ):
            result = build_audio_duplex_io(
                ffmpeg_bin="ffmpeg",
                backend="pulse-cli",
                input_device="in",
                output_device="out",
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )

        self.assertIs(result, pulse_obj)
        pulse_ctor.assert_called_once()
        ffmpeg_ctor.assert_not_called()

    def test_explicit_ffmpeg_backend_skips_pulse_cli(self) -> None:
        ffmpeg_obj = object()
        with (
            mock.patch("gitssh.audio_io_ffmpeg.PulseCliAudioDuplexIO") as pulse_ctor,
            mock.patch("gitssh.audio_io_ffmpeg.FFmpegAudioDuplexIO", return_value=ffmpeg_obj) as ffmpeg_ctor,
        ):
            result = build_audio_duplex_io(
                ffmpeg_bin="ffmpeg",
                backend="alsa",
                input_device="in",
                output_device="out",
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )

        self.assertIs(result, ffmpeg_obj)
        pulse_ctor.assert_not_called()
        ffmpeg_ctor.assert_called_once()
        self.assertEqual(ffmpeg_ctor.call_args.kwargs["backend"], "alsa")

    def test_pulse_cli_supports_monitor_stream_capture(self) -> None:
        capture_proc = _DummyProcess(has_stdout=True, has_stdin=False)
        playback_proc = _DummyProcess(has_stdout=False, has_stdin=True)
        with (
            mock.patch("gitssh.audio_io_ffmpeg._resolve_pulse_device_name", side_effect=["in_dev", "out_dev"]),
            mock.patch("gitssh.audio_io_ffmpeg.os.set_blocking"),
            mock.patch("gitssh.audio_io_ffmpeg.subprocess.Popen", side_effect=[capture_proc, playback_proc]) as popen_mock,
        ):
            io_obj = PulseCliAudioDuplexIO(
                input_device="@DEFAULT_MONITOR@",
                output_device="out_dev",
                monitor_stream_index=123,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            capture_cmd = popen_mock.call_args_list[0].args[0]
            self.assertIn("--monitor-stream=123", capture_cmd)
            io_obj.close()


if __name__ == "__main__":
    unittest.main()
