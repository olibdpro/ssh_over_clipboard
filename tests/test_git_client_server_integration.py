from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

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
                max_output_chunk=512,
                preferred_shell="sh",
                command_timeout=5.0,
                io_flush_interval=0.01,
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
                stdin_batch_interval=0.01,
                input_chunk_bytes=256,
                resize_debounce=0.01,
                no_raw=True,
                verbose=False,
            ),
        )

    def _read_stream_until(
        self,
        client: GitSSHClient,
        *,
        expected_text: str | None = None,
        wait_close: bool = False,
        timeout: float = 5.0,
    ) -> tuple[str, int | None]:
        state = client._ensure_state()
        stream_id = client._ensure_stream_id()

        captured = bytearray()
        closed_code: int | None = None
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            for incoming in client._read_messages():
                if incoming.target != "client" or incoming.source != "server":
                    continue
                if incoming.session_id != state.session_id:
                    continue
                if not state.incoming_seen.mark(incoming.msg_id):
                    continue

                body = incoming.body if isinstance(incoming.body, dict) else {}

                if incoming.kind == "pty_output" and body.get("stream_id") == stream_id:
                    data_b64 = body.get("data_b64")
                    if isinstance(data_b64, str):
                        try:
                            captured.extend(base64.b64decode(data_b64, validate=True))
                        except Exception:
                            continue

                elif incoming.kind == "pty_closed" and body.get("stream_id") == stream_id:
                    raw = body.get("exit_code", 1)
                    try:
                        closed_code = int(raw)
                    except (TypeError, ValueError):
                        closed_code = 1
                    if wait_close:
                        return captured.decode(errors="ignore"), closed_code

                elif incoming.kind == "error":
                    message = body.get("error", "unknown server error")
                    raise AssertionError(f"server reported error: {message}")

            decoded = captured.decode(errors="ignore")
            if expected_text and expected_text in decoded:
                return decoded, closed_code
            if wait_close and closed_code is not None:
                return decoded, closed_code

            time.sleep(0.02)

        return captured.decode(errors="ignore"), closed_code

    def test_connect_and_stream_io(self) -> None:
        client = self._make_client()
        client.connect("localhost")
        try:
            self.assertTrue(client.is_connected)
            self.assertIsInstance(client._stream_id, str)
            self.assertTrue(bool(client._stream_id))

            marker = f"PTY-{uuid.uuid4().hex}"
            client._send_pty_input(f"echo {marker}\n".encode())
            output, _closed_code = self._read_stream_until(client, expected_text=marker)
            self.assertIn(marker, output)
        finally:
            client.disconnect()

    def test_busy_when_second_client_connects(self) -> None:
        client1 = self._make_client()
        client2 = self._make_client()

        client1.connect("localhost")
        try:
            with self.assertRaises(RuntimeError):
                client2.connect("localhost")
        finally:
            client1.disconnect()

    def test_server_emits_pty_closed_on_shell_exit(self) -> None:
        client = self._make_client()
        client.connect("localhost")
        try:
            client._send_pty_input(b"exit 7\n")
            _output, closed_code = self._read_stream_until(client, wait_close=True)
            self.assertEqual(closed_code, 7)
        finally:
            client.disconnect()


if __name__ == "__main__":
    unittest.main()
