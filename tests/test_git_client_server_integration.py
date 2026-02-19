from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.client import ClientConfig, GitSSHClient
from gitssh.git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
)
from gitssh.server import GitSSHServer, ServerConfig


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
class GitClientServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tempdir.name)
        self.upstream_repo = str(root / "upstream.git")
        self.server_local_repo = str(root / "server-local.git")
        self.client_local_repo = str(root / "client-local.git")

        _init_bare_repo(self.upstream_repo)

        self.server_backend = GitTransportBackend(
            local_repo_path=self.server_local_repo,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_C2S,
            outbound_branch=DEFAULT_BRANCH_S2C,
            auto_init_local=True,
        )
        self.server = GitSSHServer(
            backend=self.server_backend,
            config=ServerConfig(
                poll_interval=0.02,
                max_output_chunk=8,
                preferred_shell="sh",
                command_timeout=5.0,
                fetch_interval=0.02,
                push_interval=0.02,
                verbose=False,
            ),
        )
        self.stop_event = threading.Event()
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"stop_event": self.stop_event},
            daemon=True,
        )
        self.server_thread.start()
        time.sleep(0.1)

    def tearDown(self) -> None:
        self.stop_event.set()
        self.server_thread.join(timeout=2.0)
        self.tempdir.cleanup()

    def _make_client(self) -> GitSSHClient:
        return GitSSHClient(
            backend=GitTransportBackend(
                local_repo_path=self.client_local_repo,
                upstream_url=self.upstream_repo,
                inbound_branch=DEFAULT_BRANCH_S2C,
                outbound_branch=DEFAULT_BRANCH_C2S,
                auto_init_local=True,
            ),
            config=ClientConfig(
                poll_interval=0.02,
                connect_timeout=3.0,
                session_timeout=4.0,
                retry_interval=0.05,
                fetch_interval=0.02,
                push_interval=0.02,
                verbose=False,
            ),
        )

    def test_connect_execute_disconnect(self) -> None:
        client = self._make_client()
        results = client.run_commands(
            "localhost",
            ["echo hello", "printf 'err\\n' 1>&2; /bin/sh -c 'exit 3'"],
        )

        self.assertEqual(len(results), 2)
        self.assertIn("hello", results[0].stdout)
        self.assertEqual(results[0].exit_code, 0)

        self.assertIn("err", results[1].stderr)
        self.assertEqual(results[1].exit_code, 3)

    def test_busy_when_second_client_connects(self) -> None:
        client1 = self._make_client()
        client2 = self._make_client()

        client1.connect("localhost")
        try:
            with self.assertRaises(RuntimeError):
                client2.connect("localhost")
        finally:
            client1.disconnect()


if __name__ == "__main__":
    unittest.main()
