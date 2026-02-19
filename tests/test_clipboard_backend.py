from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.clipboard import (
    ClipboardError,
    CommandClipboardBackend,
    _candidate_backends,
    detect_backend,
    detect_session_type,
)


class CommandClipboardBackendTests(unittest.TestCase):
    def test_read_uses_configured_timeout(self) -> None:
        backend = CommandClipboardBackend(
            read_cmd=["cmd-read"],
            write_cmd=["cmd-write"],
            backend_name="test",
            read_timeout=3.5,
        )

        with mock.patch("clipssh.clipboard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["cmd-read"],
                returncode=0,
                stdout="value",
                stderr="",
            )
            text = backend.read_text()

        self.assertEqual(text, "value")
        self.assertEqual(run.call_args.kwargs["timeout"], 3.5)

    def test_write_uses_configured_timeout(self) -> None:
        backend = CommandClipboardBackend(
            read_cmd=["cmd-read"],
            write_cmd=["cmd-write"],
            backend_name="test",
            write_timeout=8.0,
        )

        with mock.patch("clipssh.clipboard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["cmd-write"],
                returncode=0,
                stdout="",
                stderr="",
            )
            backend.write_text("payload")

        self.assertEqual(run.call_args.kwargs["timeout"], 8.0)

    def test_probe_roundtrip_uses_probe_timeouts(self) -> None:
        backend = CommandClipboardBackend(
            read_cmd=["cmd-read"],
            write_cmd=["cmd-write"],
            backend_name="test",
            read_timeout=0.2,
            write_timeout=0.4,
            probe_read_timeout=3.0,
            probe_write_timeout=4.0,
        )

        with mock.patch("clipssh.clipboard.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    args=["cmd-write"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    args=["cmd-read"],
                    returncode=0,
                    stdout="probe",
                    stderr="",
                ),
            ]
            text = backend.probe_roundtrip("payload")

        self.assertEqual(text, "probe")
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 4.0)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 3.0)


class SessionDetectionTests(unittest.TestCase):
    def test_detect_session_type_prefers_xdg_session_type(self) -> None:
        self.assertEqual(
            detect_session_type(
                {
                    "XDG_SESSION_TYPE": "wayland",
                    "WAYLAND_DISPLAY": "",
                    "DISPLAY": ":1",
                }
            ),
            "wayland",
        )

    def test_detect_session_type_uses_display_fallback(self) -> None:
        self.assertEqual(
            detect_session_type(
                {
                    "XDG_SESSION_TYPE": "",
                    "WAYLAND_DISPLAY": "",
                    "DISPLAY": ":1",
                }
            ),
            "x11",
        )

    def test_detect_session_type_unknown_when_no_signals(self) -> None:
        self.assertEqual(
            detect_session_type(
                {
                    "XDG_SESSION_TYPE": "",
                    "WAYLAND_DISPLAY": "",
                    "DISPLAY": "",
                }
            ),
            "unknown",
        )


class CandidateBackendTests(unittest.TestCase):
    def test_auto_order_for_x11_prefers_xsel_then_xclip(self) -> None:
        candidates = _candidate_backends(
            session_type="x11",
            backend_preference="auto",
            read_timeout=1.5,
            write_timeout=2.5,
            probe_read_timeout=3.5,
            probe_write_timeout=4.5,
            availability={"wayland": False, "xclip": True, "xsel": True},
        )
        self.assertEqual([candidate.backend_name for candidate in candidates], ["xsel", "xclip"])
        self.assertEqual(candidates[0].read_timeout, 1.5)
        self.assertEqual(candidates[0].write_timeout, 2.5)
        self.assertEqual(candidates[0].probe_read_timeout, 3.5)
        self.assertEqual(candidates[0].probe_write_timeout, 4.5)

    def test_auto_wayland_does_not_include_x11_fallbacks(self) -> None:
        candidates = _candidate_backends(
            session_type="wayland",
            backend_preference="auto",
            read_timeout=1.0,
            write_timeout=2.0,
            probe_read_timeout=3.0,
            probe_write_timeout=4.0,
            availability={"wayland": True, "xclip": True, "xsel": True},
        )
        self.assertEqual([candidate.backend_name for candidate in candidates], ["wayland-wl-clipboard"])

    def test_explicit_backend_override_is_respected(self) -> None:
        candidates = _candidate_backends(
            session_type="wayland",
            backend_preference="xclip",
            read_timeout=1.0,
            write_timeout=2.0,
            probe_read_timeout=3.0,
            probe_write_timeout=4.0,
            availability={"wayland": True, "xclip": True, "xsel": True},
        )
        self.assertEqual([candidate.backend_name for candidate in candidates], ["xclip"])
        xclip_backend = candidates[0]
        self.assertEqual(
            xclip_backend.write_cmd,
            ["xclip", "-selection", "clipboard", "-in", "-silent"],
        )


class DetectBackendTests(unittest.TestCase):
    def test_detect_backend_prefers_xsel_on_x11(self) -> None:
        def fake_which(command: str) -> str | None:
            return f"/usr/bin/{command}" if command in {"xsel", "xclip"} else None

        with mock.patch("clipssh.clipboard.detect_session_type", return_value="x11"), mock.patch(
            "clipssh.clipboard.shutil.which",
            side_effect=fake_which,
        ), mock.patch("clipssh.clipboard._probe_backend") as probe:
            backend = detect_backend(
                read_timeout=0.25,
                write_timeout=1.0,
                probe_read_timeout=2.0,
                probe_write_timeout=2.0,
            )

        self.assertEqual(backend.name(), "xsel")
        probe.assert_called_once()
        self.assertEqual(backend.read_timeout, 0.25)
        self.assertEqual(backend.write_timeout, 1.0)
        self.assertEqual(backend.probe_read_timeout, 2.0)
        self.assertEqual(backend.probe_write_timeout, 2.0)

    def test_detect_backend_falls_back_when_first_probe_fails(self) -> None:
        def fake_which(command: str) -> str | None:
            return f"/usr/bin/{command}" if command in {"xsel", "xclip"} else None

        def fake_probe(backend: CommandClipboardBackend) -> None:
            if backend.name() == "xsel":
                raise ClipboardError("probe failure")

        with mock.patch("clipssh.clipboard.detect_session_type", return_value="x11"), mock.patch(
            "clipssh.clipboard.shutil.which",
            side_effect=fake_which,
        ), mock.patch("clipssh.clipboard._probe_backend", side_effect=fake_probe) as probe:
            backend = detect_backend()

        self.assertEqual(backend.name(), "xclip")
        self.assertEqual(probe.call_count, 2)

    def test_detect_backend_reports_missing_tools_with_guidance(self) -> None:
        with mock.patch("clipssh.clipboard.detect_session_type", return_value="wayland"), mock.patch(
            "clipssh.clipboard.shutil.which",
            return_value=None,
        ):
            with self.assertRaises(ClipboardError) as ctx:
                detect_backend()

        text = str(ctx.exception)
        self.assertIn("No viable clipboard backend found.", text)
        self.assertIn("Session: wayland", text)
        self.assertIn("Conda: conda install -c conda-forge", text)
        self.assertIn("pip note", text)

    def test_detect_backend_reports_probe_failures(self) -> None:
        def fake_which(command: str) -> str | None:
            return "/usr/bin/xsel" if command == "xsel" else None

        with mock.patch("clipssh.clipboard.detect_session_type", return_value="x11"), mock.patch(
            "clipssh.clipboard.shutil.which",
            side_effect=fake_which,
        ), mock.patch(
            "clipssh.clipboard._probe_backend",
            side_effect=ClipboardError("timed out"),
        ):
            with self.assertRaises(ClipboardError) as ctx:
                detect_backend()

        text = str(ctx.exception)
        self.assertIn("Attempted backends:", text)
        self.assertIn("- xsel: timed out", text)


if __name__ == "__main__":
    unittest.main()
