from __future__ import annotations

import io
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_pipewire_runtime import (  # noqa: E402
    PipeWireLinkAudioDuplexIO,
    PipeWireNode,
    PipeWirePreflightReport,
    PipeWireRuntimeError,
    _ports_for_node,
    build_client_pipewire_preflight_report,
    describe_node,
    ensure_client_pipewire_preflight,
    list_capture_nodes,
    list_nodes,
    list_write_nodes,
    resolve_client_capture_node_id,
    resolve_client_write_node_id,
)


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = None
        self.stderr = io.BytesIO()
        self._terminated = False
        self._killed = False
        self._returncode = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self._killed = True
        self._returncode = -9


class PipeWireRuntimeTests(unittest.TestCase):
    def test_list_nodes_parses_pw_cli_output(self) -> None:
        payload = """
\tid 42, type PipeWire:Interface:Node/3
 \t\tapplication.name = \"Firefox\"
 \t\tnode.description = \"Firefox Output\"
 \t\tnode.name = \"firefox.output\"
 \t\tmedia.class = \"Stream/Output/Audio\"
\tid 44, type PipeWire:Interface:Node/3
 \t\tapplication.name = \"PCoIP\"
 \t\tnode.description = \"PCoIP Recording\"
 \t\tnode.name = \"pcoip.input\"
 \t\tmedia.class = \"Stream/Input/Audio\"
"""
        with mock.patch("gitssh.audio_pipewire_runtime._run_pw_cli", return_value=payload):
            nodes = list_nodes()

        self.assertEqual([node.node_id for node in nodes], [44, 42])
        self.assertEqual(nodes[1].node_name, "firefox.output")
        self.assertEqual(nodes[0].media_class, "Stream/Input/Audio")

    def test_describe_node_contains_key_fields(self) -> None:
        node = PipeWireNode(
            node_id=7,
            node_name="n7",
            node_description="desc",
            app_name="app",
            media_class="Stream/Output/Audio",
        )
        text = describe_node(node)
        self.assertIn("id=7", text)
        self.assertIn("name=n7", text)
        self.assertIn("class=Stream/Output/Audio", text)

    def test_list_capture_nodes_filters_media_class(self) -> None:
        nodes = [
            PipeWireNode(1, "sink", "sink", "a", "Audio/Sink"),
            PipeWireNode(2, "out", "out", "b", "Stream/Output/Audio"),
            PipeWireNode(3, "in", "in", "c", "Stream/Input/Audio"),
        ]
        with mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes):
            capture_nodes = list_capture_nodes()

        self.assertEqual([node.node_id for node in capture_nodes], [1, 2])

    def test_list_write_nodes_filters_media_class(self) -> None:
        nodes = [
            PipeWireNode(1, "sink", "sink", "a", "Audio/Sink"),
            PipeWireNode(2, "src", "src", "b", "Audio/Source"),
            PipeWireNode(3, "in", "in", "c", "Stream/Input/Audio"),
            PipeWireNode(4, "out", "out", "d", "Stream/Output/Audio"),
        ]
        with mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes):
            write_nodes = list_write_nodes()

        self.assertEqual([node.node_id for node in write_nodes], [2, 3])

    def test_resolve_capture_node_by_id(self) -> None:
        nodes = [
            PipeWireNode(8, "cap8", "d", "app", "Stream/Output/Audio"),
            PipeWireNode(9, "cap9", "d", "app", "Stream/Output/Audio"),
        ]
        with mock.patch("gitssh.audio_pipewire_runtime.list_capture_nodes", return_value=nodes):
            selected = resolve_client_capture_node_id(node_id=9, node_match=None, interactive=False)

        self.assertEqual(selected, 9)

    def test_resolve_capture_node_requires_selector_noninteractive(self) -> None:
        with mock.patch("gitssh.audio_pipewire_runtime.list_capture_nodes", return_value=[]):
            with self.assertRaises(PipeWireRuntimeError):
                resolve_client_capture_node_id(node_id=None, node_match=None, interactive=False)

    def test_resolve_write_node_by_regex(self) -> None:
        nodes = [
            PipeWireNode(10, "pcoip-record", "desc", "pcoip", "Stream/Input/Audio"),
            PipeWireNode(11, "other", "desc", "other", "Stream/Input/Audio"),
        ]
        with mock.patch("gitssh.audio_pipewire_runtime.list_write_nodes", return_value=nodes):
            selected = resolve_client_write_node_id(node_id=None, node_match="pcoip", interactive=False)

        self.assertEqual(selected, 10)

    def test_resolve_write_node_rejects_ambiguous_regex(self) -> None:
        nodes = [
            PipeWireNode(10, "node-a", "desc", "app", "Stream/Input/Audio"),
            PipeWireNode(11, "node-b", "desc", "app", "Stream/Input/Audio"),
        ]
        with mock.patch("gitssh.audio_pipewire_runtime.list_write_nodes", return_value=nodes):
            with self.assertRaises(PipeWireRuntimeError):
                resolve_client_write_node_id(node_id=None, node_match="node", interactive=False)

    def test_resolve_capture_node_interactive_prompt_selects_choice(self) -> None:
        nodes = [
            PipeWireNode(2, "node-a", "desc", "app", "Stream/Output/Audio"),
            PipeWireNode(3, "node-b", "desc", "app", "Stream/Output/Audio"),
        ]
        stdin = io.StringIO("2\n")
        stderr = io.StringIO()
        with mock.patch("gitssh.audio_pipewire_runtime.list_capture_nodes", return_value=nodes):
            selected = resolve_client_capture_node_id(
                node_id=None,
                node_match=None,
                interactive=True,
                input_stream=stdin,
                output_stream=stderr,
            )
        self.assertEqual(selected, 3)

    def test_ports_for_node_matches_name_prefix(self) -> None:
        listing = "\n".join(
            [
                "capture.node:monitor_FL",
                "capture.node:monitor_FR",
                "other.node:monitor_FL",
            ]
        )
        with mock.patch("gitssh.audio_pipewire_runtime._run_pw_link", return_value=listing):
            ports, raw = _ports_for_node(node_name="capture.node", node_id=49, direction="output")

        self.assertEqual(ports, ["capture.node:monitor_FL", "capture.node:monitor_FR"])
        self.assertEqual(raw, listing)

    def test_ports_for_node_matches_numeric_id_prefix(self) -> None:
        listing = "\n".join(
            [
                "49:monitor_FL",
                "49:monitor_FR",
                "51:monitor_FL",
            ]
        )
        with mock.patch("gitssh.audio_pipewire_runtime._run_pw_link", return_value=listing):
            ports, _raw = _ports_for_node(node_name="capture.node", node_id=49, direction="output")

        self.assertEqual(ports, ["49:monitor_FL", "49:monitor_FR"])

    def test_ports_for_node_matches_alias_candidates(self) -> None:
        listing = "\n".join(
            [
                "pcoip-client-context-:input_FL",
                "other.node:input_FL",
            ]
        )
        with mock.patch("gitssh.audio_pipewire_runtime._run_pw_link", return_value=listing):
            ports, _raw = _ports_for_node(
                node_name="sshg_capture_generated",
                node_id=None,
                direction="input",
                alias_candidates=["pcoip-client-context-"],
            )

        self.assertEqual(ports, ["pcoip-client-context-:input_FL"])

    def test_build_pipewire_preflight_report_ok(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch(
                "gitssh.audio_pipewire_runtime._run_pw_cli",
                return_value='id 999, type PipeWire:Interface:Port/3\n\tport.name = "p0"\n',
            ),
            mock.patch(
                "gitssh.audio_pipewire_runtime._systemctl_user_unit_state",
                side_effect=["active", "inactive"],
            ),
        ):
            report = build_client_pipewire_preflight_report(capture_node_id=49, write_node_id=44)

        self.assertTrue(report.ok)
        self.assertIn("PipeWire client preflight OK", report.render())

    def test_build_pipewire_preflight_report_failure_includes_remediation(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime._run_pw_cli", return_value=""),
            mock.patch(
                "gitssh.audio_pipewire_runtime._systemctl_user_unit_state",
                side_effect=["failed", "inactive"],
            ),
            mock.patch("gitssh.audio_pipewire_runtime._wireplumber_has_no_space_issue", return_value=True),
        ):
            report = build_client_pipewire_preflight_report(capture_node_id=49, write_node_id=44)

        self.assertFalse(report.ok)
        rendered = report.render()
        self.assertIn("PipeWire client preflight failed:", rendered)
        self.assertIn("PipeWire exposes no visible Port objects", rendered)
        self.assertIn("No active PipeWire session manager detected.", rendered)
        self.assertIn("No space left on device", rendered)
        self.assertIn("Remediation:", rendered)

    def test_ensure_pipewire_preflight_raises_on_failure(self) -> None:
        report = PipeWirePreflightReport(
            ok=False,
            issues=("bad preflight",),
            notes=(),
            remediation=("fix me",),
        )
        with mock.patch(
            "gitssh.audio_pipewire_runtime.build_client_pipewire_preflight_report",
            return_value=report,
        ):
            with self.assertRaises(PipeWireRuntimeError) as ctx:
                ensure_client_pipewire_preflight()
        self.assertIn("bad preflight", str(ctx.exception))

    def test_duplex_uses_pw_record_and_pw_play_with_fifo_paths(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo") as mkfifo,
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink") as unlink,
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ) as popen,
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    (["capture.node:monitor_FL"], "capture.node:monitor_FL"),
                    (["write.node:input_FL"], "write.node:input_FL"),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_ensure_links_ready", return_value=None),
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            capture_file = io_obj._capture_file_path
            playback_fifo = io_obj._playback_fifo_path
            io_obj.close()

        self.assertIsNotNone(capture_file)
        self.assertIsNotNone(playback_fifo)
        self.assertTrue(capture_file.endswith(".raw"))
        self.assertTrue(playback_fifo.endswith(".raw.fifo"))
        mkfifo.assert_any_call(playback_fifo, 0o600)

        capture_cmd = popen.call_args_list[0].args[0]
        playback_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(capture_cmd[0], "pw-record")
        self.assertEqual(playback_cmd[0], "pw-play")
        self.assertEqual(capture_cmd[capture_cmd.index("--target") + 1], "49")
        self.assertEqual(playback_cmd[playback_cmd.index("--target") + 1], "44")
        self.assertEqual(capture_cmd[-1], capture_file)
        self.assertEqual(playback_cmd[-1], playback_fifo)

        unlink.assert_any_call(capture_file)
        unlink.assert_any_call(playback_fifo)

    def test_duplex_continues_with_direct_targets_when_link_setup_fails(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink") as unlink,
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ) as popen,
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    (["capture.node:monitor_FL"], "capture.node:monitor_FL"),
                    (["write.node:input_FL"], "write.node:input_FL"),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_ensure_links_ready",
                side_effect=PipeWireRuntimeError("link setup failed"),
            ),
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            self.assertEqual(io_obj._routing_mode, "direct_target_fallback")
            self.assertIn("explicit link setup failed", io_obj._routing_note)
            io_obj.close()

        capture_cmd = popen.call_args_list[0].args[0]
        playback_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(capture_cmd[capture_cmd.index("--target") + 1], "49")
        self.assertEqual(playback_cmd[playback_cmd.index("--target") + 1], "44")
        self.assertTrue(capture_proc._terminated)
        self.assertTrue(playback_proc._terminated)
        self.assertGreaterEqual(unlink.call_count, 2)

    def test_duplex_late_link_failure_keeps_existing_direct_targets(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
            PipeWireNode(42, "sink.node", "sink", "app", "Audio/Sink"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ) as popen,
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    (["capture.node:monitor_FL"], "capture.node:monitor_FL"),
                    (["write.node:input_FL"], "write.node:input_FL"),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_ensure_links_ready",
                side_effect=PipeWireRuntimeError(
                    "Failed to find output ports for capture link source node 'capture.node' (id=49)."
                ),
            ),
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            self.assertEqual(io_obj._routing_mode, "direct_target_fallback")
            self.assertIn("explicit link setup failed", io_obj._routing_note)
            io_obj.close()

        capture_cmd = popen.call_args_list[0].args[0]
        playback_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(capture_cmd[capture_cmd.index("--target") + 1], "49")
        self.assertEqual(playback_cmd[playback_cmd.index("--target") + 1], "44")

    def test_duplex_uses_pw_play_and_pw_record_aliases_when_dynamic_names_do_not_resolve(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        def _fake_ports_for_node(
            *,
            node_name: str,
            node_id: int | None,
            direction: str,
            alias_candidates: list[str] | None = None,
        ) -> tuple[list[str], str]:
            if direction == "output":
                if node_name == "capture.node" or node_id == 49:
                    return (["capture.node:monitor_FL"], "")
                if alias_candidates and "pw-play" in alias_candidates:
                    return (["pw-play:output_FL"], "")
            if direction == "input":
                if node_name == "write.node" or node_id == 44:
                    return (["write.node:input_FL"], "")
                if alias_candidates and "pw-record" in alias_candidates:
                    return (["pw-record:input_FL"], "")
            return ([], "")

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ),
            mock.patch("gitssh.audio_pipewire_runtime._ports_for_node", side_effect=_fake_ports_for_node),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch("gitssh.audio_pipewire_runtime._run_pw_link", return_value="") as run_pw_link,
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            self.assertEqual(io_obj._routing_mode, "explicit_link")
            io_obj.close()

        link_args = [call.args[0] for call in run_pw_link.call_args_list]
        self.assertIn(["pw-play:output_FL", "write.node:input_FL"], link_args)
        self.assertIn(["capture.node:monitor_FL", "pw-record:input_FL"], link_args)

    def test_link_direction_error_includes_prefixes_and_port_sample(self) -> None:
        io_obj = PipeWireLinkAudioDuplexIO.__new__(PipeWireLinkAudioDuplexIO)

        with mock.patch(
            "gitssh.audio_pipewire_runtime._ports_for_node",
            return_value=([], "xnode:out1\nxnode:out2"),
        ):
            with self.assertRaises(PipeWireRuntimeError) as ctx:
                io_obj._link_direction(
                    output_node_name="pcoip-client-context-",
                    output_node_id=49,
                    input_node_name="sshg_capture_abc",
                    input_node_id=None,
                    label="capture",
                )

        message = str(ctx.exception)
        self.assertIn("Tried prefixes: pcoip-client-context-:, 49:", message)
        self.assertIn("Available output ports sample:", message)

    def test_duplex_falls_back_to_direct_targets_when_probe_has_no_ports(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    ([], ""),
                    ([], ""),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ) as popen,
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_ensure_links_ready") as ensure_links_ready,
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            io_obj.close()

        capture_cmd = popen.call_args_list[0].args[0]
        playback_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(capture_cmd[capture_cmd.index("--target") + 1], "49")
        self.assertEqual(playback_cmd[playback_cmd.index("--target") + 1], "44")
        ensure_links_ready.assert_not_called()

    def test_duplex_write_prepends_wav_header_once(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()
        writes: list[bytes] = []

        def _fake_write(_fd: int, chunk: bytes | memoryview) -> int:
            blob = bytes(chunk)
            writes.append(blob)
            return len(blob)

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    (["capture.node:monitor_FL"], "capture.node:monitor_FL"),
                    (["write.node:input_FL"], "write.node:input_FL"),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_ensure_links_ready", return_value=None),
            mock.patch("gitssh.audio_pipewire_runtime.select.select", return_value=([], [103], [])),
            mock.patch("gitssh.audio_pipewire_runtime.os.write", side_effect=_fake_write),
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            io_obj.write(b"\x01\x02\x03\x04")
            io_obj.write(b"\x05\x06")
            io_obj.close()

        self.assertGreaterEqual(len(writes), 3)
        self.assertEqual(writes[0][:4], b"RIFF")
        self.assertEqual(writes[0][8:12], b"WAVE")
        self.assertEqual(writes[1], b"\x01\x02\x03\x04")
        self.assertEqual(writes[2], b"\x05\x06")

    def test_duplex_fallback_retargets_stream_nodes_to_sink_when_available(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
            PipeWireNode(42, "sink.node", "sink", "app", "Audio/Sink"),
        ]
        capture_proc = _FakeProc()
        playback_proc = _FakeProc()

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    ([], ""),
                    ([], ""),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=True,
            ),
            mock.patch(
                "gitssh.audio_pipewire_runtime.subprocess.Popen",
                side_effect=[capture_proc, playback_proc],
            ) as popen,
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_wait_for_process_stability", return_value=None),
            mock.patch.object(PipeWireLinkAudioDuplexIO, "_ensure_links_ready") as ensure_links_ready,
        ):
            io_obj = PipeWireLinkAudioDuplexIO(
                capture_node_id=49,
                write_node_id=44,
                sample_rate=48000,
                read_timeout=0.01,
                write_timeout=0.05,
            )
            io_obj.close()

        capture_cmd = popen.call_args_list[0].args[0]
        playback_cmd = popen.call_args_list[1].args[0]
        self.assertEqual(capture_cmd[capture_cmd.index("--target") + 1], "42")
        self.assertEqual(playback_cmd[playback_cmd.index("--target") + 1], "42")
        ensure_links_ready.assert_not_called()

    def test_duplex_fallback_raises_when_no_pipewire_ports_are_visible(self) -> None:
        nodes = [
            PipeWireNode(49, "capture.node", "capture", "app", "Stream/Output/Audio"),
            PipeWireNode(44, "write.node", "write", "app", "Stream/Input/Audio"),
        ]

        with (
            mock.patch("gitssh.audio_pipewire_runtime.list_nodes", return_value=nodes),
            mock.patch("gitssh.audio_pipewire_runtime.os.mkfifo"),
            mock.patch("gitssh.audio_pipewire_runtime.os.open", side_effect=[100, 101, 102, 103]),
            mock.patch("gitssh.audio_pipewire_runtime.os.set_blocking"),
            mock.patch("gitssh.audio_pipewire_runtime.os.close"),
            mock.patch("gitssh.audio_pipewire_runtime.os.unlink"),
            mock.patch(
                "gitssh.audio_pipewire_runtime._ports_for_node",
                side_effect=[
                    ([], ""),
                    ([], ""),
                ],
            ),
            mock.patch.object(
                PipeWireLinkAudioDuplexIO,
                "_pipewire_has_visible_ports",
                return_value=False,
            ),
        ):
            with self.assertRaises(PipeWireRuntimeError) as ctx:
                PipeWireLinkAudioDuplexIO(
                    capture_node_id=49,
                    write_node_id=44,
                    sample_rate=48000,
                    read_timeout=0.01,
                    write_timeout=0.05,
                )

        self.assertIn("no visible Port objects", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
