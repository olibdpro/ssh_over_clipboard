from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.client import _build_parser as build_client_parser
from gitssh.server import _build_parser as build_server_parser


class GitClientCliTests(unittest.TestCase):
    def test_defaults_include_git_transport(self) -> None:
        args = build_client_parser().parse_args(["localhost"])
        self.assertEqual(args.transport, "git")
        self.assertEqual(args.serial_port, "/dev/ttyACM0")
        self.assertEqual(args.serial_baud, 3000000)

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
                "sshg_rx_sink.monitor",
                "--audio-output-device",
                "sshg_tx_sink",
                "--audio-sample-rate",
                "44100",
                "--audio-byte-repeat",
                "5",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.audio_input_device, "sshg_rx_sink.monitor")
        self.assertEqual(args.audio_output_device, "sshg_tx_sink")
        self.assertEqual(args.audio_sample_rate, 44100)
        self.assertEqual(args.audio_byte_repeat, 5)


class GitServerCliTests(unittest.TestCase):
    def test_defaults_include_git_transport(self) -> None:
        args = build_server_parser().parse_args([])
        self.assertEqual(args.transport, "git")
        self.assertEqual(args.serial_port, "/dev/ttyACM0")
        self.assertEqual(args.serial_baud, 3000000)

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
                "sshg_vm_mic",
                "--audio-output-device",
                "sshg_vm_sink",
                "--audio-marker-run",
                "24",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.audio_input_device, "sshg_vm_mic")
        self.assertEqual(args.audio_output_device, "sshg_vm_sink")
        self.assertEqual(args.audio_marker_run, 24)


if __name__ == "__main__":
    unittest.main()
