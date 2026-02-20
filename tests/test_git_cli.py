from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.client import _build_backend as build_client_backend
from gitssh.client import _build_parser as build_client_parser
from gitssh.server import _build_backend as build_server_backend
from gitssh.server import _build_parser as build_server_parser
from gitssh.transport import TransportError


class GitClientCliTests(unittest.TestCase):
    def test_defaults_include_git_transport(self) -> None:
        args = build_client_parser().parse_args(["localhost"])
        self.assertEqual(args.transport, "git")
        self.assertEqual(args.serial_port, "/dev/ttyACM0")
        self.assertEqual(args.serial_baud, 3000000)
        self.assertIsNone(args.audio_input_device)
        self.assertIsNone(args.audio_output_device)
        self.assertEqual(args.audio_discovery_timeout, 90.0)
        self.assertEqual(args.audio_discovery_ping_interval_ms, 120)
        self.assertEqual(args.audio_discovery_found_interval_ms, 120)
        self.assertEqual(args.audio_discovery_candidate_grace, 20.0)
        self.assertEqual(args.audio_discovery_max_silent_seconds, 10.0)
        self.assertEqual(args.audio_modulation, "auto")

    def test_supports_usb_serial_transport_options(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "usb-serial",
                "--serial-port",
                "/dev/ttyACM9",
                "--serial-baud",
                "115200",
                "--serial-frame-max-bytes",
                "8192",
            ]
        )
        self.assertEqual(args.transport, "usb-serial")
        self.assertEqual(args.serial_port, "/dev/ttyACM9")
        self.assertEqual(args.serial_baud, 115200)
        self.assertEqual(args.serial_frame_max_bytes, 8192)

    def test_supports_audio_modem_transport_options(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "audio-modem",
                "--audio-input-device",
                "server_output_receiver",
                "--audio-output-device",
                "client_response_sender",
                "--audio-sample-rate",
                "44100",
                "--audio-byte-repeat",
                "5",
                "--audio-modulation",
                "robust-v1",
                "--audio-discovery-timeout",
                "30",
                "--audio-discovery-ping-interval-ms",
                "80",
                "--audio-discovery-found-interval-ms",
                "90",
                "--audio-discovery-candidate-grace",
                "6",
                "--audio-discovery-max-silent-seconds",
                "4",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.audio_input_device, "server_output_receiver")
        self.assertEqual(args.audio_output_device, "client_response_sender")
        self.assertEqual(args.audio_sample_rate, 44100)
        self.assertEqual(args.audio_byte_repeat, 5)
        self.assertEqual(args.audio_modulation, "robust-v1")
        self.assertEqual(args.audio_discovery_timeout, 30.0)
        self.assertEqual(args.audio_discovery_ping_interval_ms, 80)
        self.assertEqual(args.audio_discovery_found_interval_ms, 90)
        self.assertEqual(args.audio_discovery_candidate_grace, 6.0)
        self.assertEqual(args.audio_discovery_max_silent_seconds, 4.0)

    def test_supports_google_drive_transport_options(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "google-drive",
                "--drive-client-secrets",
                "/tmp/client-secrets.json",
                "--drive-token-path",
                "/tmp/drive-token.json",
                "--drive-c2s-file-name",
                "custom-c2s.log",
                "--drive-s2c-file-name",
                "custom-s2c.log",
                "--drive-poll-page-size",
                "250",
            ]
        )
        self.assertEqual(args.transport, "google-drive")
        self.assertEqual(args.drive_client_secrets, "/tmp/client-secrets.json")
        self.assertEqual(args.drive_token_path, "/tmp/drive-token.json")
        self.assertEqual(args.drive_c2s_file_name, "custom-c2s.log")
        self.assertEqual(args.drive_s2c_file_name, "custom-s2c.log")
        self.assertEqual(args.drive_poll_page_size, 250)

    def test_google_drive_transport_requires_client_secrets(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "google-drive",
            ]
        )
        with self.assertRaises(TransportError):
            build_client_backend(args)


class GitServerCliTests(unittest.TestCase):
    def test_defaults_include_git_transport(self) -> None:
        args = build_server_parser().parse_args([])
        self.assertEqual(args.transport, "git")
        self.assertEqual(args.serial_port, "/dev/ttyACM0")
        self.assertEqual(args.serial_baud, 3000000)
        self.assertIsNone(args.audio_input_device)
        self.assertIsNone(args.audio_output_device)
        self.assertEqual(args.audio_discovery_timeout, 90.0)
        self.assertEqual(args.audio_discovery_ping_interval_ms, 120)
        self.assertEqual(args.audio_discovery_found_interval_ms, 120)
        self.assertEqual(args.audio_discovery_candidate_grace, 20.0)
        self.assertEqual(args.audio_discovery_max_silent_seconds, 10.0)
        self.assertEqual(args.audio_modulation, "auto")

    def test_supports_usb_serial_transport_options(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--transport",
                "usb-serial",
                "--serial-port",
                "/dev/ttyACM1",
                "--serial-baud",
                "460800",
                "--serial-max-retries",
                "4",
            ]
        )
        self.assertEqual(args.transport, "usb-serial")
        self.assertEqual(args.serial_port, "/dev/ttyACM1")
        self.assertEqual(args.serial_baud, 460800)
        self.assertEqual(args.serial_max_retries, 4)

    def test_supports_audio_modem_transport_options(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--transport",
                "audio-modem",
                "--audio-input-device",
                "client_output_receiver",
                "--audio-output-device",
                "server_response_sender",
                "--audio-marker-run",
                "24",
                "--audio-modulation",
                "legacy",
                "--audio-discovery-timeout",
                "35",
                "--audio-discovery-ping-interval-ms",
                "100",
                "--audio-discovery-found-interval-ms",
                "110",
                "--audio-discovery-candidate-grace",
                "7.5",
                "--audio-discovery-max-silent-seconds",
                "5.5",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.audio_input_device, "client_output_receiver")
        self.assertEqual(args.audio_output_device, "server_response_sender")
        self.assertEqual(args.audio_marker_run, 24)
        self.assertEqual(args.audio_modulation, "legacy")
        self.assertEqual(args.audio_discovery_timeout, 35.0)
        self.assertEqual(args.audio_discovery_ping_interval_ms, 100)
        self.assertEqual(args.audio_discovery_found_interval_ms, 110)
        self.assertEqual(args.audio_discovery_candidate_grace, 7.5)
        self.assertEqual(args.audio_discovery_max_silent_seconds, 5.5)

    def test_supports_google_drive_transport_options(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--transport",
                "google-drive",
                "--drive-client-secrets",
                "/tmp/client-secrets.json",
                "--drive-token-path",
                "/tmp/drive-token.json",
                "--drive-c2s-file-name",
                "custom-c2s.log",
                "--drive-s2c-file-name",
                "custom-s2c.log",
                "--drive-poll-page-size",
                "300",
            ]
        )
        self.assertEqual(args.transport, "google-drive")
        self.assertEqual(args.drive_client_secrets, "/tmp/client-secrets.json")
        self.assertEqual(args.drive_token_path, "/tmp/drive-token.json")
        self.assertEqual(args.drive_c2s_file_name, "custom-c2s.log")
        self.assertEqual(args.drive_s2c_file_name, "custom-s2c.log")
        self.assertEqual(args.drive_poll_page_size, 300)

    def test_google_drive_transport_requires_client_secrets(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--transport",
                "google-drive",
            ]
        )
        with self.assertRaises(TransportError):
            build_server_backend(args)


if __name__ == "__main__":
    unittest.main()
