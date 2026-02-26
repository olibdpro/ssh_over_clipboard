from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh import audio_probe
from gitssh.audio_pipewire_runtime import PipeWirePreflightReport


class AudioProbeCliTests(unittest.TestCase):
    def test_pipewire_preflight_ok(self) -> None:
        report = PipeWirePreflightReport(
            ok=True,
            issues=(),
            notes=("wireplumber.service=active",),
            remediation=(),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(audio_probe, "build_client_pipewire_preflight_report", return_value=report),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = audio_probe.main(["--pipewire-preflight"])

        self.assertEqual(rc, 0)
        self.assertIn("PipeWire client preflight OK", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_pipewire_preflight_failure_returns_error(self) -> None:
        report = PipeWirePreflightReport(
            ok=False,
            issues=("No active PipeWire session manager detected.",),
            notes=(),
            remediation=("Start wireplumber",),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(audio_probe, "build_client_pipewire_preflight_report", return_value=report),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            rc = audio_probe.main(["--pipewire-preflight"])

        self.assertEqual(rc, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("sshg-audio-probe: PipeWire client preflight failed:", stderr.getvalue())
        self.assertIn("Remediation:", stderr.getvalue())

    def test_pipewire_preflight_passes_requested_node_ids(self) -> None:
        report = PipeWirePreflightReport(
            ok=True,
            issues=(),
            notes=(),
            remediation=(),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            audio_probe,
            "build_client_pipewire_preflight_report",
            return_value=report,
        ) as report_mock, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = audio_probe.main(
                [
                    "--pipewire-preflight",
                    "--pw-capture-node-id",
                    "49",
                    "--pw-write-node-id",
                    "44",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertIn("PipeWire client preflight OK", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        report_mock.assert_called_once_with(capture_node_id=49, write_node_id=44)


if __name__ == "__main__":
    unittest.main()
