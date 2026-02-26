from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_pipewire_runtime import PipeWireRuntimeError
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
        self.assertIsNone(args.pw_capture_node_id)
        self.assertIsNone(args.pw_capture_match)
        self.assertIsNone(args.pw_write_node_id)
        self.assertIsNone(args.pw_write_match)
        self.assertFalse(args.skip_pw_preflight)
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
                "--pw-capture-node-id",
                "77",
                "--pw-capture-match",
                "chrome|firefox",
                "--pw-write-node-id",
                "88",
                "--pw-write-match",
                "pcoip-record-stream",
                "--audio-sample-rate",
                "44100",
                "--audio-byte-repeat",
                "5",
                "--audio-modulation",
                "robust-v1",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.pw_capture_node_id, 77)
        self.assertEqual(args.pw_capture_match, "chrome|firefox")
        self.assertEqual(args.pw_write_node_id, 88)
        self.assertEqual(args.pw_write_match, "pcoip-record-stream")
        self.assertEqual(args.audio_sample_rate, 44100)
        self.assertEqual(args.audio_byte_repeat, 5)
        self.assertEqual(args.audio_modulation, "robust-v1")

    def test_audio_modem_rejects_legacy_backend_flag(self) -> None:
        with self.assertRaises(SystemExit):
            build_client_parser().parse_args(
                [
                    "localhost",
                    "--transport",
                    "audio-modem",
                    "--audio-backend",
                    "pulse-cli",
                ]
            )

    def test_audio_modem_rejects_legacy_stream_flags(self) -> None:
        with self.assertRaises(SystemExit):
            build_client_parser().parse_args(
                [
                    "localhost",
                    "--transport",
                    "audio-modem",
                    "--audio-stream-index",
                    "11",
                ]
            )

    def test_audio_modem_backend_build_with_explicit_node_ids(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "audio-modem",
                "--pw-capture-node-id",
                "11",
                "--pw-write-node-id",
                "22",
            ]
        )

        with (
            mock.patch("gitssh.client.ensure_client_pipewire_preflight") as preflight,
            mock.patch("gitssh.client.resolve_client_capture_node_id", return_value=11) as resolve_capture,
            mock.patch("gitssh.client.resolve_client_write_node_id", return_value=22) as resolve_write,
        ):
            backend = build_client_backend(args)

        self.assertEqual(
            backend.name(),
            "audio-modem:pipewire-link:robust-v1:in=pw-node:11,out=pw-node:22",
        )
        preflight.assert_called_once_with(capture_node_id=11, write_node_id=22)
        resolve_capture.assert_called_once()
        resolve_write.assert_called_once()
        backend.close()

    def test_audio_modem_propagates_pipewire_selection_errors(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "audio-modem",
            ]
        )
        with mock.patch(
            "gitssh.client.ensure_client_pipewire_preflight",
            return_value=None,
        ):
            with mock.patch(
                "gitssh.client.resolve_client_capture_node_id",
                side_effect=PipeWireRuntimeError("selection failed"),
            ):
                with self.assertRaises(TransportError):
                    build_client_backend(args)

    def test_audio_modem_propagates_pipewire_preflight_errors(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "audio-modem",
            ]
        )
        with mock.patch(
            "gitssh.client.ensure_client_pipewire_preflight",
            side_effect=PipeWireRuntimeError("preflight failed"),
        ):
            with self.assertRaises(TransportError):
                build_client_backend(args)

    def test_audio_modem_can_skip_pipewire_preflight(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--transport",
                "audio-modem",
                "--pw-capture-node-id",
                "11",
                "--pw-write-node-id",
                "22",
                "--skip-pw-preflight",
            ]
        )
        with (
            mock.patch("gitssh.client.ensure_client_pipewire_preflight") as preflight,
            mock.patch("gitssh.client.resolve_client_capture_node_id", return_value=11),
            mock.patch("gitssh.client.resolve_client_write_node_id", return_value=22),
        ):
            backend = build_client_backend(args)
        preflight.assert_not_called()
        backend.close()

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
                "--audio-marker-run",
                "24",
                "--audio-modulation",
                "legacy",
            ]
        )
        self.assertEqual(args.transport, "audio-modem")
        self.assertEqual(args.audio_marker_run, 24)
        self.assertEqual(args.audio_modulation, "legacy")

    def test_server_rejects_legacy_audio_backend_flag(self) -> None:
        with self.assertRaises(SystemExit):
            build_server_parser().parse_args(
                [
                    "--transport",
                    "audio-modem",
                    "--audio-backend",
                    "pulse-cli",
                ]
            )

    def test_audio_modem_backend_uses_server_defaults(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--transport",
                "audio-modem",
            ]
        )
        with mock.patch("gitssh.server.resolve_server_default_paths", return_value=("mic.default", "speaker.default")):
            backend = build_server_backend(args)

        self.assertEqual(
            backend.name(),
            "audio-modem:pulse-cli:robust-v1:in=mic.default,out=speaker.default",
        )
        backend.close()

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
