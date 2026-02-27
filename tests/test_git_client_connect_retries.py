from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.client import ClientConfig, GitSSHClient
from gitssh.protocol import Message
from gitssh.transport import TransportBackend


class _RetryProbeBackend(TransportBackend):
    def __init__(self) -> None:
        self.writes: list[Message] = []

    def name(self) -> str:
        return "retry-probe"

    def snapshot_inbound_cursor(self) -> str | None:
        return "0"

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        return [], cursor

    def fetch_inbound(self) -> None:
        return

    def write_outbound_message(self, message: Message) -> str:
        self.writes.append(message)
        return message.msg_id

    def push_outbound(self) -> None:
        return

    def close(self) -> None:
        return


class GitClientConnectRetryTests(unittest.TestCase):
    def test_connect_retries_use_unique_connect_req_message_ids(self) -> None:
        backend = _RetryProbeBackend()
        client = GitSSHClient(
            backend=backend,
            config=ClientConfig(
                poll_interval=0.01,
                connect_timeout=0.2,
                retry_interval=0.03,
                fetch_interval=0.01,
                push_interval=0.01,
                no_raw=True,
            ),
        )

        with self.assertRaises(TimeoutError):
            client.connect("localhost")

        connect_reqs = [msg for msg in backend.writes if msg.kind == "connect_req"]
        self.assertGreaterEqual(len(connect_reqs), 2)

        msg_ids = {msg.msg_id for msg in connect_reqs}
        seqs = [msg.seq for msg in connect_reqs]
        self.assertEqual(len(msg_ids), len(connect_reqs))
        self.assertEqual(seqs, sorted(seqs))
        self.assertGreater(seqs[-1], seqs[0])


if __name__ == "__main__":
    unittest.main()
