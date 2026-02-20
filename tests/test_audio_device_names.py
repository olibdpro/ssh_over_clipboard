from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_device_names import (
    AudioDeviceNameError,
    is_managed_pulse_device_name,
    resolve_input_device_name,
    resolve_output_device_name,
)


class AudioDeviceNameResolutionTests(unittest.TestCase):
    def test_pulse_input_role_alias_resolves_to_monitor(self) -> None:
        resolved = resolve_input_device_name(
            requested="server_output_receiver",
            backend="pulse-cli",
        )
        self.assertEqual(resolved, "server_output_receiver_pulse.monitor")

    def test_pulse_output_role_alias_resolves_to_sink(self) -> None:
        resolved = resolve_output_device_name(
            requested="client_response_sender",
            backend="pipewire",
        )
        self.assertEqual(resolved, "client_response_sender_pulse")

    def test_auto_backend_resolves_role_alias_to_pulse_variant(self) -> None:
        resolved = resolve_output_device_name(
            requested="server_response_sender",
            backend="auto",
        )
        self.assertEqual(resolved, "server_response_sender_pulse")

    def test_alsa_role_alias_resolves_to_alsa_variant(self) -> None:
        resolved = resolve_input_device_name(
            requested="client_output_receiver",
            backend="alsa",
        )
        self.assertEqual(resolved, "client_output_receiver_alsa")

    def test_role_alias_with_monitor_suffix_is_rejected(self) -> None:
        with self.assertRaises(AudioDeviceNameError):
            resolve_input_device_name(
                requested="server_output_receiver.monitor",
                backend="pulse-cli",
            )

    def test_role_alias_cannot_resolve_for_unknown_backend(self) -> None:
        with self.assertRaises(AudioDeviceNameError):
            resolve_output_device_name(
                requested="server_response_sender",
                backend="jack",
            )

    def test_explicit_managed_pulse_name_remains_accepted(self) -> None:
        resolved = resolve_input_device_name(
            requested="server_output_receiver_pulse",
            backend="pulse-cli",
        )
        self.assertEqual(resolved, "server_output_receiver_pulse.monitor")

    def test_legacy_name_is_rejected(self) -> None:
        with self.assertRaises(AudioDeviceNameError):
            resolve_input_device_name(
                requested="sshg_rx_sink.monitor",
                backend="pulse-cli",
            )

    def test_unmanaged_name_passes_through_unchanged(self) -> None:
        resolved = resolve_output_device_name(
            requested="alsa_output.usb-1234",
            backend="pulse-cli",
        )
        self.assertEqual(resolved, "alsa_output.usb-1234")


class AudioManagedPulseNameTests(unittest.TestCase):
    def test_reports_managed_sink_name(self) -> None:
        self.assertTrue(is_managed_pulse_device_name("server_output_receiver_pulse"))

    def test_reports_managed_monitor_name(self) -> None:
        self.assertTrue(is_managed_pulse_device_name("server_output_receiver_pulse.monitor"))

    def test_reports_unmanaged_name_as_false(self) -> None:
        self.assertFalse(is_managed_pulse_device_name("sshg_rx_sink.monitor"))


if __name__ == "__main__":
    unittest.main()
