from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.client import ClipboardSSHClient, ClientConfig
from clipssh.clipboard import MemoryClipboardBackend
from clipssh.server import ClipboardSSHServer, ServerConfig


class ClientServerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clipboard = MemoryClipboardBackend()
        self.server = ClipboardSSHServer(
            backend=self.clipboard,
            config=ServerConfig(
                poll_interval=0.01,
                max_output_chunk=8,
                preferred_shell="sh",
                command_timeout=5.0,
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
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.stop_event.set()
        self.server_thread.join(timeout=2.0)

    def _make_client(self) -> ClipboardSSHClient:
        return ClipboardSSHClient(
            backend=self.clipboard,
            config=ClientConfig(
                poll_interval=0.01,
                connect_timeout=2.0,
                session_timeout=4.0,
                retry_interval=0.05,
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

    def test_tolerates_non_protocol_clipboard_data(self) -> None:
        client = self._make_client()
        client.connect("localhost")

        def inject_noise() -> None:
            for _ in range(3):
                time.sleep(0.03)
                self.clipboard.write_text("this is normal copy/paste data")

        noise_thread = threading.Thread(target=inject_noise, daemon=True)
        noise_thread.start()
        try:
            result = client.execute("echo noisy")
        finally:
            client.disconnect()
            noise_thread.join(timeout=1.0)

        self.assertIn("noisy", result.stdout)
        self.assertEqual(result.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
