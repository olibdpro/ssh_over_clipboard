from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
)
from gitssh.protocol import build_message


GIT_AVAILABLE = shutil.which("git") is not None



def _init_bare_repo(path: str) -> None:
    result = subprocess.run(
        ["git", "init", "--bare", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git init --bare failed: {result.stderr.strip()}")


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class GitSyncConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tempdir.name)

        self.upstream_repo = str(root / "upstream.git")
        self.writer_a_local = str(root / "writer-a.git")
        self.writer_b_local = str(root / "writer-b.git")
        self.reader_local = str(root / "reader.git")

        _init_bare_repo(self.upstream_repo)

        self.writer_a = GitTransportBackend(
            local_repo_path=self.writer_a_local,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_S2C,
            outbound_branch=DEFAULT_BRANCH_C2S,
            auto_init_local=True,
        )
        self.writer_b = GitTransportBackend(
            local_repo_path=self.writer_b_local,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_S2C,
            outbound_branch=DEFAULT_BRANCH_C2S,
            auto_init_local=True,
        )
        self.reader = GitTransportBackend(
            local_repo_path=self.reader_local,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_C2S,
            outbound_branch=DEFAULT_BRANCH_S2C,
            auto_init_local=True,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_auto_retry_on_push_conflicts_across_local_mirrors(self) -> None:
        start_cursor = self.reader.snapshot_inbound_cursor()
        per_writer = 8
        written_ids: set[str] = set()
        written_ids_lock = threading.Lock()

        def write_batch(backend: GitTransportBackend, prefix: str) -> None:
            for i in range(per_writer):
                message = build_message(
                    kind="pty_input",
                    session_id=str(uuid.uuid4()),
                    source="client",
                    target="server",
                    seq=i + 1,
                    body={
                        "stream_id": str(uuid.uuid4()),
                        "data_b64": base64.b64encode(f"{prefix}-{i}\n".encode()).decode("ascii"),
                    },
                )
                backend.write_outbound_message(message)
                with written_ids_lock:
                    written_ids.add(message.msg_id)

        thread_a = threading.Thread(target=write_batch, args=(self.writer_a, "a"))
        thread_b = threading.Thread(target=write_batch, args=(self.writer_b, "b"))

        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5.0)
        thread_b.join(timeout=5.0)

        self.reader.fetch_inbound()
        messages, _ = self.reader.read_inbound_messages(start_cursor)
        received_ids = {msg.msg_id for msg in messages}

        self.assertEqual(len(messages), per_writer * 2)
        self.assertEqual(len(received_ids), per_writer * 2)
        self.assertTrue(written_ids.issubset(received_ids))


if __name__ == "__main__":
    unittest.main()
