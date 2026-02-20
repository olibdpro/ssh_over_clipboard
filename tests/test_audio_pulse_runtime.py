from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_pulse_runtime import (  # noqa: E402
    CLIENT_VIRTUAL_MIC_SINK,
    CLIENT_VIRTUAL_MIC_SOURCE,
    ClientVirtualMicManager,
    PulsePlaybackStream,
    PulseRuntimeError,
    describe_stream,
    list_active_playback_streams,
    resolve_client_capture_stream_index,
    resolve_server_default_paths,
)


class PulseStreamDiscoveryTests(unittest.TestCase):
    def test_list_active_playback_streams_filters_and_sorts(self) -> None:
        payload = """
[
  {
    "index": 5,
    "sink": 1,
    "state": "RUNNING",
    "corked": false,
    "properties": {
      "application.name": "Firefox",
      "media.name": "Video",
      "application.process.binary": "firefox",
      "application.process.id": "1234"
    }
  },
  {
    "index": 7,
    "sink": 1,
    "state": "IDLE",
    "corked": false,
    "properties": {
      "application.name": "Music",
      "media.name": "Track",
      "application.process.binary": "mpv"
    }
  },
  {
    "index": 9,
    "sink": 2,
    "state": "RUNNING",
    "corked": false,
    "properties": {
      "application.name": "Chrome",
      "media.name": "Tab Audio",
      "application.process.binary": "chrome"
    }
  }
]
"""
        with mock.patch("gitssh.audio_pulse_runtime._run_pactl", return_value=payload) as run_pactl:
            streams = list_active_playback_streams()

        self.assertEqual([stream.index for stream in streams], [9, 5])
        self.assertEqual(streams[0].app_name, "Chrome")
        self.assertEqual(streams[1].process_id, 1234)
        run_pactl.assert_called_once_with(["-f", "json", "list", "sink-inputs"])

    def test_resolve_stream_by_index(self) -> None:
        streams = [
            PulsePlaybackStream(
                index=3,
                app_name="A",
                media_name="A",
                process_binary="a",
                process_id=11,
                sink="1",
                state="RUNNING",
                corked=False,
            ),
            PulsePlaybackStream(
                index=8,
                app_name="B",
                media_name="B",
                process_binary="b",
                process_id=22,
                sink="2",
                state="RUNNING",
                corked=False,
            ),
        ]
        with mock.patch("gitssh.audio_pulse_runtime.list_active_playback_streams", return_value=streams):
            selected = resolve_client_capture_stream_index(
                stream_index=8,
                stream_match=None,
                interactive=False,
            )
        self.assertEqual(selected, 8)

    def test_resolve_stream_requires_interactive_without_selector(self) -> None:
        with mock.patch("gitssh.audio_pulse_runtime.list_active_playback_streams", return_value=[]):
            with self.assertRaises(PulseRuntimeError):
                resolve_client_capture_stream_index(
                    stream_index=None,
                    stream_match=None,
                    interactive=False,
                )

    def test_resolve_stream_by_regex(self) -> None:
        streams = [
            PulsePlaybackStream(
                index=1,
                app_name="Firefox",
                media_name="Music",
                process_binary="firefox",
                process_id=100,
                sink="1",
                state="RUNNING",
                corked=False,
            ),
            PulsePlaybackStream(
                index=2,
                app_name="Chrome",
                media_name="Call",
                process_binary="chrome",
                process_id=200,
                sink="1",
                state="RUNNING",
                corked=False,
            ),
        ]
        with mock.patch("gitssh.audio_pulse_runtime.list_active_playback_streams", return_value=streams):
            selected = resolve_client_capture_stream_index(
                stream_index=None,
                stream_match="chrome",
                interactive=False,
            )
        self.assertEqual(selected, 2)

    def test_resolve_stream_by_regex_rejects_ambiguous_matches(self) -> None:
        streams = [
            PulsePlaybackStream(
                index=1,
                app_name="Chrome",
                media_name="A",
                process_binary="chrome",
                process_id=None,
                sink="1",
                state="RUNNING",
                corked=False,
            ),
            PulsePlaybackStream(
                index=2,
                app_name="Chromium",
                media_name="B",
                process_binary="chromium",
                process_id=None,
                sink="2",
                state="RUNNING",
                corked=False,
            ),
        ]
        with mock.patch("gitssh.audio_pulse_runtime.list_active_playback_streams", return_value=streams):
            with self.assertRaises(PulseRuntimeError):
                resolve_client_capture_stream_index(
                    stream_index=None,
                    stream_match="chrom",
                    interactive=False,
                )

    def test_describe_stream_contains_key_fields(self) -> None:
        stream = PulsePlaybackStream(
            index=3,
            app_name="App",
            media_name="Media",
            process_binary="bin",
            process_id=42,
            sink="9",
            state="RUNNING",
            corked=False,
        )
        text = describe_stream(stream)
        self.assertIn("idx=3", text)
        self.assertIn("app=App", text)
        self.assertIn("pid=42", text)


class ClientVirtualMicManagerTests(unittest.TestCase):
    def test_ensure_ready_creates_modules_and_restores_on_close(self) -> None:
        manager = ClientVirtualMicManager()
        with (
            mock.patch("gitssh.audio_pulse_runtime._default_device", return_value="old.source"),
            mock.patch("gitssh.audio_pulse_runtime._list_short_names", side_effect=[[], []]),
            mock.patch("gitssh.audio_pulse_runtime._load_module", side_effect=[101, 202]) as load_module,
            mock.patch("gitssh.audio_pulse_runtime._run_pactl") as run_pactl,
            mock.patch("gitssh.audio_pulse_runtime._unload_module") as unload_module,
        ):
            route = manager.ensure_ready()
            self.assertEqual(route.sink_name, CLIENT_VIRTUAL_MIC_SINK)
            self.assertEqual(route.source_name, CLIENT_VIRTUAL_MIC_SOURCE)
            manager.close()

        self.assertEqual(load_module.call_count, 2)
        run_pactl.assert_any_call(["set-default-source", CLIENT_VIRTUAL_MIC_SOURCE])
        run_pactl.assert_any_call(["set-default-source", "old.source"])
        unload_module.assert_has_calls([mock.call(202), mock.call(101)])

    def test_ensure_ready_reuses_existing_devices(self) -> None:
        manager = ClientVirtualMicManager()
        with (
            mock.patch("gitssh.audio_pulse_runtime._default_device", return_value="old.source"),
            mock.patch(
                "gitssh.audio_pulse_runtime._list_short_names",
                side_effect=[[CLIENT_VIRTUAL_MIC_SINK], [CLIENT_VIRTUAL_MIC_SOURCE]],
            ),
            mock.patch("gitssh.audio_pulse_runtime._load_module") as load_module,
            mock.patch("gitssh.audio_pulse_runtime._run_pactl") as run_pactl,
            mock.patch("gitssh.audio_pulse_runtime._unload_module") as unload_module,
        ):
            manager.ensure_ready()
            manager.close()

        load_module.assert_not_called()
        unload_module.assert_not_called()
        run_pactl.assert_any_call(["set-default-source", CLIENT_VIRTUAL_MIC_SOURCE])
        run_pactl.assert_any_call(["set-default-source", "old.source"])

    def test_resolve_server_default_paths_reads_source_and_sink(self) -> None:
        with mock.patch("gitssh.audio_pulse_runtime._default_device", side_effect=["source.default", "sink.default"]):
            input_device, output_device = resolve_server_default_paths()
        self.assertEqual(input_device, "source.default")
        self.assertEqual(output_device, "sink.default")


if __name__ == "__main__":
    unittest.main()

