from __future__ import annotations

import pathlib
import sys
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sshcore.session import EndpointState

from gitssh.protocol import Message, build_message
from gitssh.server import (
    _CONNECT_ACK_BURST_GAP_SEC,
    DIAG_IDLE_SESSION_ID,
    ActiveSession,
    GitSSHServer,
    ServerConfig,
)


class _CaptureBackend:
    def __init__(self, *, backend_name: str = "capture-backend") -> None:
        self.outbound_messages: list[Message] = []
        self._backend_name = backend_name

    def name(self) -> str:
        return self._backend_name

    def snapshot_inbound_cursor(self) -> str | None:
        return "0"

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        return [], cursor

    def fetch_inbound(self) -> None:
        return

    def write_outbound_message(self, message: Message) -> str:
        self.outbound_messages.append(message)
        return "ok"

    def push_outbound(self) -> None:
        return

    def close(self) -> None:
        return


class GitServerDiagTests(unittest.TestCase):
    class _FakeShell:
        def __init__(self, shell_path: str = "/bin/sh") -> None:
            self.shell_path = shell_path

        def close(self) -> None:
            return

    def _build_server(
        self,
        *,
        diag: bool = True,
        diag_interval: float = 1.0,
        backend_name: str = "capture-backend",
        connect_ack_burst: int = 5,
    ) -> tuple[GitSSHServer, _CaptureBackend]:
        backend = _CaptureBackend(backend_name=backend_name)
        server = GitSSHServer(
            backend=backend,
            config=ServerConfig(
                diag=diag,
                diag_interval=diag_interval,
                connect_ack_burst=connect_ack_burst,
                verbose=False,
            ),
        )
        return server, backend

    def _build_connect_req(self, session_id: str) -> Message:
        return build_message(
            kind="connect_req",
            session_id=session_id,
            source="client",
            target="server",
            seq=1,
            body={"host": "localhost", "pty": {"cols": 80, "rows": 24}},
        )

    def test_diag_emits_idle_heartbeat_without_active_session(self) -> None:
        server, backend = self._build_server(diag=True, diag_interval=1.0)

        with mock.patch("gitssh.server.time.monotonic", return_value=10.0):
            server._maybe_emit_diag_ping()

        self.assertEqual(len(backend.outbound_messages), 1)
        message = backend.outbound_messages[0]
        self.assertEqual(message.kind, "diag_ping")
        self.assertEqual(message.session_id, DIAG_IDLE_SESSION_ID)

        body = message.body if isinstance(message.body, dict) else {}
        self.assertEqual(body.get("phase"), "idle_heartbeat")
        self.assertEqual(body.get("diag_counter"), 1)
        self.assertEqual(body.get("active_session"), False)
        self.assertNotIn("stream_id", body)
        self.assertEqual(server._next_diag_at, 11.0)

    def test_diag_idle_heartbeat_respects_interval(self) -> None:
        server, backend = self._build_server(diag=True, diag_interval=1.0)

        with mock.patch("gitssh.server.time.monotonic", side_effect=[10.0, 10.2, 11.2]):
            server._maybe_emit_diag_ping()
            server._maybe_emit_diag_ping()
            server._maybe_emit_diag_ping()

        self.assertEqual(len(backend.outbound_messages), 2)
        first_body = backend.outbound_messages[0].body
        second_body = backend.outbound_messages[1].body
        assert isinstance(first_body, dict)
        assert isinstance(second_body, dict)
        self.assertEqual(first_body.get("diag_counter"), 1)
        self.assertEqual(second_body.get("diag_counter"), 2)
        self.assertEqual(first_body.get("phase"), "idle_heartbeat")
        self.assertEqual(second_body.get("phase"), "idle_heartbeat")

    def test_diag_emits_active_heartbeat_with_stream_id(self) -> None:
        server, backend = self._build_server(diag=True, diag_interval=1.0)
        session_id = str(uuid.uuid4())
        server._active = ActiveSession(
            state=EndpointState(session_id=session_id),
            shell=object(),
            stream_id="stream-123",
        )

        with mock.patch("gitssh.server.time.monotonic", return_value=5.0):
            server._maybe_emit_diag_ping()

        self.assertEqual(len(backend.outbound_messages), 1)
        message = backend.outbound_messages[0]
        self.assertEqual(message.session_id, session_id)
        body = message.body if isinstance(message.body, dict) else {}
        self.assertEqual(body.get("phase"), "active_heartbeat")
        self.assertEqual(body.get("stream_id"), "stream-123")
        self.assertEqual(body.get("active_session"), True)

    def test_non_diag_mode_does_not_emit_heartbeat(self) -> None:
        server, backend = self._build_server(diag=False, diag_interval=1.0)

        with mock.patch("gitssh.server.time.monotonic", return_value=4.0):
            server._maybe_emit_diag_ping()

        self.assertEqual(backend.outbound_messages, [])

    def test_connect_ack_bursts_for_audio_modem_backend(self) -> None:
        session_id = str(uuid.uuid4())
        server, backend = self._build_server(
            diag=False,
            backend_name="audio-modem:pulse-cli:robust-v1:in=mic.default,out=speaker.default",
            connect_ack_burst=5,
        )

        with (
            mock.patch("gitssh.server.resolve_shell", return_value=("/bin/sh", "sh")),
            mock.patch("gitssh.server.PtyShellSession", return_value=self._FakeShell("/bin/sh")),
            mock.patch("gitssh.server.time.sleep") as sleep_mock,
        ):
            server._handle_connect(self._build_connect_req(session_id))

        acks = [msg for msg in backend.outbound_messages if msg.kind == "connect_ack"]
        self.assertEqual(len(acks), 5)
        self.assertEqual({msg.session_id for msg in acks}, {session_id})
        stream_ids = {
            msg.body.get("stream_id")
            for msg in acks
            if isinstance(msg.body, dict)
        }
        self.assertEqual(len(stream_ids), 1)
        self.assertEqual(sleep_mock.call_count, 4)
        self.assertTrue(all(call.args == (_CONNECT_ACK_BURST_GAP_SEC,) for call in sleep_mock.call_args_list))

    def test_connect_ack_is_single_for_non_audio_backend(self) -> None:
        session_id = str(uuid.uuid4())
        server, backend = self._build_server(
            diag=False,
            backend_name="capture-backend",
            connect_ack_burst=5,
        )

        with (
            mock.patch("gitssh.server.resolve_shell", return_value=("/bin/sh", "sh")),
            mock.patch("gitssh.server.PtyShellSession", return_value=self._FakeShell("/bin/sh")),
            mock.patch("gitssh.server.time.sleep") as sleep_mock,
        ):
            server._handle_connect(self._build_connect_req(session_id))

        acks = [msg for msg in backend.outbound_messages if msg.kind == "connect_ack"]
        self.assertEqual(len(acks), 1)
        sleep_mock.assert_not_called()

    def test_connect_reack_bursts_for_same_session_on_audio_modem(self) -> None:
        session_id = str(uuid.uuid4())
        server, backend = self._build_server(
            diag=False,
            backend_name="audio-modem:pulse-cli:robust-v1:in=mic.default,out=speaker.default",
            connect_ack_burst=5,
        )
        server._active = ActiveSession(
            state=EndpointState(session_id=session_id),
            shell=self._FakeShell("/bin/sh"),
            stream_id="stream-123",
        )

        with mock.patch("gitssh.server.time.sleep") as sleep_mock:
            server._handle_connect(self._build_connect_req(session_id))

        acks = [msg for msg in backend.outbound_messages if msg.kind == "connect_ack"]
        self.assertEqual(len(acks), 5)
        self.assertTrue(
            all(
                isinstance(msg.body, dict) and msg.body.get("stream_id") == "stream-123"
                for msg in acks
            )
        )
        self.assertEqual(sleep_mock.call_count, 4)

    def test_connect_busy_path_remains_single_message(self) -> None:
        active_session_id = str(uuid.uuid4())
        incoming_session_id = str(uuid.uuid4())
        server, backend = self._build_server(
            diag=False,
            backend_name="audio-modem:pulse-cli:robust-v1:in=mic.default,out=speaker.default",
            connect_ack_burst=5,
        )
        server._active = ActiveSession(
            state=EndpointState(session_id=active_session_id),
            shell=self._FakeShell("/bin/sh"),
            stream_id="stream-123",
        )

        with mock.patch("gitssh.server.time.sleep") as sleep_mock:
            server._handle_connect(self._build_connect_req(incoming_session_id))

        busy = [msg for msg in backend.outbound_messages if msg.kind == "busy"]
        acks = [msg for msg in backend.outbound_messages if msg.kind == "connect_ack"]
        self.assertEqual(len(busy), 1)
        self.assertEqual(len(acks), 0)
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
