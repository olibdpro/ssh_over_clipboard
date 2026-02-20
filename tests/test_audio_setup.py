from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh import audio_setup


class AudioSetupClientDevicesTests(unittest.TestCase):
    def test_create_client_devices_creates_virtual_mic_when_missing(self) -> None:
        state: dict[str, object] = {"modules": []}
        with (
            mock.patch.object(audio_setup, "_read_state", return_value=state),
            mock.patch.object(audio_setup, "_write_state") as write_state,
            mock.patch.object(audio_setup, "_list_short_device_names", return_value=set()) as list_names,
            mock.patch.object(audio_setup, "_load_module", side_effect=[11, 12, 13]) as load_module,
            mock.patch("builtins.print") as print_mock,
        ):
            audio_setup.create_client_devices()

        list_names.assert_called_once_with("sources")
        self.assertEqual(load_module.call_count, 3)
        self.assertEqual(load_module.call_args_list[2].args[0], "module-remap-source")
        remap_args = load_module.call_args_list[2].args[1]
        self.assertIn("master=client_response_sender_pulse.monitor", remap_args)
        self.assertIn(f"source_name={audio_setup.CLIENT_VIRTUAL_MIC_SOURCE}", remap_args)
        self.assertIn(
            f"source_properties=device.description={audio_setup.CLIENT_VIRTUAL_MIC_DESCRIPTION}",
            remap_args,
        )

        write_state.assert_called_once_with(state)
        modules = state["modules"]
        assert isinstance(modules, list)
        self.assertEqual(
            [item["module_name"] for item in modules],
            ["module-null-sink", "module-null-sink", "module-remap-source"],
        )
        print_mock.assert_any_call(
            f"- source (UI-selectable mic): {audio_setup.CLIENT_VIRTUAL_MIC_SOURCE} "
            f"(description target: {audio_setup.CLIENT_VIRTUAL_MIC_DESCRIPTION}, created)"
        )
        print_mock.assert_any_call(f"- pactl set-default-source {audio_setup.CLIENT_VIRTUAL_MIC_SOURCE}")

    def test_create_client_devices_reuses_virtual_mic_when_already_present(self) -> None:
        state: dict[str, object] = {"modules": []}
        with (
            mock.patch.object(audio_setup, "_read_state", return_value=state),
            mock.patch.object(audio_setup, "_write_state") as write_state,
            mock.patch.object(
                audio_setup,
                "_list_short_device_names",
                return_value={audio_setup.CLIENT_VIRTUAL_MIC_SOURCE},
            ),
            mock.patch.object(audio_setup, "_load_module", side_effect=[21, 22]) as load_module,
            mock.patch("builtins.print") as print_mock,
        ):
            audio_setup.create_client_devices()

        self.assertEqual(load_module.call_count, 2)
        module_names = [call.args[0] for call in load_module.call_args_list]
        self.assertEqual(module_names, ["module-null-sink", "module-null-sink"])

        write_state.assert_called_once_with(state)
        modules = state["modules"]
        assert isinstance(modules, list)
        self.assertEqual(
            [item["module_name"] for item in modules],
            ["module-null-sink", "module-null-sink"],
        )
        print_mock.assert_any_call(
            f"- source (UI-selectable mic): {audio_setup.CLIENT_VIRTUAL_MIC_SOURCE} "
            f"(description target: {audio_setup.CLIENT_VIRTUAL_MIC_DESCRIPTION}, reused)"
        )

    def test_create_client_devices_falls_back_when_spaced_description_is_rejected(self) -> None:
        state: dict[str, object] = {"modules": []}
        with (
            mock.patch.object(audio_setup, "_read_state", return_value=state),
            mock.patch.object(audio_setup, "_write_state"),
            mock.patch.object(audio_setup, "_list_short_device_names", return_value=set()),
            mock.patch.object(
                audio_setup,
                "_load_module",
                side_effect=[11, 12, audio_setup.AudioSetupError("bad properties"), 13],
            ) as load_module,
            mock.patch("builtins.print") as print_mock,
        ):
            audio_setup.create_client_devices()

        self.assertEqual(load_module.call_count, 4)
        self.assertIn(
            f"source_properties=device.description={audio_setup.CLIENT_VIRTUAL_MIC_DESCRIPTION}",
            load_module.call_args_list[2].args[1],
        )
        self.assertIn(
            "source_properties=device.description=Client_Response_Sender",
            load_module.call_args_list[3].args[1],
        )
        print_mock.assert_any_call("- note: Pulse accepted description 'Client_Response_Sender' on this host")


class AudioSetupStatusMatchingTests(unittest.TestCase):
    def test_managed_line_matches_virtual_mic_source(self) -> None:
        line = (
            f"81\t{audio_setup.CLIENT_VIRTUAL_MIC_SOURCE}\tmodule-remap-source.c\t"
            "s16le 2ch 44100Hz\tRUNNING"
        )
        self.assertTrue(audio_setup._is_managed_pulse_line(line))

    def test_unmanaged_line_is_not_matched(self) -> None:
        line = "42\tunrelated_source\tmodule-null-sink.c\ts16le 2ch 44100Hz\tIDLE"
        self.assertFalse(audio_setup._is_managed_pulse_line(line))


if __name__ == "__main__":
    unittest.main()
