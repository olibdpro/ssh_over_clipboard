from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.client import _build_parser as build_client_parser
from clipssh.server import _build_parser as build_server_parser


class ClipboardClientCliTests(unittest.TestCase):
    def test_client_parser_defaults(self) -> None:
        args = build_client_parser().parse_args(["localhost"])
        self.assertEqual(args.clipboard_read_timeout, 0.25)
        self.assertEqual(args.clipboard_write_timeout, 1.0)
        self.assertEqual(args.clipboard_probe_read_timeout, 2.0)
        self.assertEqual(args.clipboard_probe_write_timeout, 2.0)
        self.assertEqual(args.retry_interval, 0.2)

    def test_client_parser_accepts_probe_timeout_overrides(self) -> None:
        args = build_client_parser().parse_args(
            [
                "localhost",
                "--clipboard-read-timeout",
                "0.15",
                "--clipboard-write-timeout",
                "0.8",
                "--clipboard-probe-read-timeout",
                "3.0",
                "--clipboard-probe-write-timeout",
                "4.0",
                "--retry-interval",
                "0.3",
            ]
        )
        self.assertEqual(args.clipboard_read_timeout, 0.15)
        self.assertEqual(args.clipboard_write_timeout, 0.8)
        self.assertEqual(args.clipboard_probe_read_timeout, 3.0)
        self.assertEqual(args.clipboard_probe_write_timeout, 4.0)
        self.assertEqual(args.retry_interval, 0.3)


class ClipboardServerCliTests(unittest.TestCase):
    def test_server_parser_defaults(self) -> None:
        args = build_server_parser().parse_args([])
        self.assertEqual(args.clipboard_read_timeout, 0.25)
        self.assertEqual(args.clipboard_write_timeout, 1.0)
        self.assertEqual(args.clipboard_probe_read_timeout, 2.0)
        self.assertEqual(args.clipboard_probe_write_timeout, 2.0)

    def test_server_parser_accepts_probe_timeout_overrides(self) -> None:
        args = build_server_parser().parse_args(
            [
                "--clipboard-read-timeout",
                "0.15",
                "--clipboard-write-timeout",
                "0.8",
                "--clipboard-probe-read-timeout",
                "3.0",
                "--clipboard-probe-write-timeout",
                "4.0",
            ]
        )
        self.assertEqual(args.clipboard_read_timeout, 0.15)
        self.assertEqual(args.clipboard_write_timeout, 0.8)
        self.assertEqual(args.clipboard_probe_read_timeout, 3.0)
        self.assertEqual(args.clipboard_probe_write_timeout, 4.0)


if __name__ == "__main__":
    unittest.main()
