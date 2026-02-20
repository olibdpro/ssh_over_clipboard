from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_io_ffmpeg import AudioIOError, build_audio_duplex_io


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


if __name__ == "__main__":
    unittest.main()
