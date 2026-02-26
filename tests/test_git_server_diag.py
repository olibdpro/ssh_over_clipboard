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

from gitssh.protocol import Message
from gitssh.server import (
    DIAG_IDLE_SESSION_ID,
    ActiveSession,
    GitSSHServer,
    ServerConfig,
)


class _CaptureBackend:
    def __init__(self) -> None:
        self.outbound_messages: list[Message] = []

    def name(self) -> str:
        return "capture-backend"

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
    def _build_server(self, *, diag: bool = True, diag_interval: float = 1.0) -> tuple[GitSSHServer, _CaptureBackend]:
        backend = _CaptureBackend()
        server = GitSSHServer(
            backend=backend,
            config=ServerConfig(
                diag=diag,
                diag_interval=diag_interval,
                verbose=False,
            ),
        )
        return server, backend

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


if __name__ == "__main__":
    unittest.main()
