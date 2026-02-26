from __future__ import annotations

import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.client import ClientConfig, GitSSHClient
from gitssh.protocol import Message
from gitssh.server import GitSSHServer, ServerConfig
from gitssh.transport import TransportError


class _FakeBackend:
    def __init__(self, *, fail_writes: bool = False) -> None:
        self.fail_writes = fail_writes

    def name(self) -> str:
        return "fake-backend"

    def snapshot_inbound_cursor(self) -> str | None:
        return "0"

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        return [], cursor

    def fetch_inbound(self) -> None:
        return

    def write_outbound_message(self, message: Message) -> str:
        del message
        if self.fail_writes:
            raise TransportError("write failed")
        return "ok"

    def push_outbound(self) -> None:
        return

    def close(self) -> None:
        return


class GitSyncWorkerLifecycleTests(unittest.TestCase):
    def test_client_sync_worker_non_daemon_and_disconnect_stops_it(self) -> None:
        client = GitSSHClient(
            backend=_FakeBackend(),
            config=ClientConfig(
                poll_interval=0.01,
                connect_timeout=0.3,
                session_timeout=0.3,
                retry_interval=0.01,
                fetch_interval=0.01,
                push_interval=0.01,
                no_raw=True,
                verbose=False,
            ),
        )

        client._start_sync_worker()
        time.sleep(0.02)
        thread = client._sync_thread
        self.assertIsNotNone(thread)
        assert thread is not None
        self.assertFalse(thread.daemon)

        client.disconnect()
        self.assertIsNone(client._sync_thread)
        self.assertIsNone(client._sync_stop)
        self.assertFalse(thread.is_alive())

    def test_client_connect_failure_stops_sync_worker(self) -> None:
        client = GitSSHClient(
            backend=_FakeBackend(fail_writes=True),
            config=ClientConfig(
                poll_interval=0.01,
                connect_timeout=0.3,
                session_timeout=0.3,
                retry_interval=0.01,
                fetch_interval=0.01,
                push_interval=0.01,
                no_raw=True,
                verbose=False,
            ),
        )

        with self.assertRaises(RuntimeError):
            client.connect("localhost")

        self.assertIsNone(client._sync_thread)
        self.assertIsNone(client._sync_stop)

    def test_server_sync_worker_non_daemon_and_stop_stops_it(self) -> None:
        server = GitSSHServer(
            backend=_FakeBackend(),
            config=ServerConfig(
                poll_interval=0.01,
                fetch_interval=0.01,
                push_interval=0.01,
                verbose=False,
            ),
        )

        server._start_sync_worker()
        time.sleep(0.02)
        thread = server._sync_thread
        self.assertIsNotNone(thread)
        assert thread is not None
        self.assertFalse(thread.daemon)

        server._stop_sync_worker()
        self.assertIsNone(server._sync_thread)
        self.assertIsNone(server._sync_stop)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
