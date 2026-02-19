from __future__ import annotations

import pathlib
import socket
import sys
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.client import ClipboardSSHClient, ClientConfig
from clipssh.clipboard import MemoryClipboardBackend
from clipssh.protocol import build_message
from clipssh.server import ClipboardSSHServer, ServerConfig


class DelayedMemoryClipboardBackend(MemoryClipboardBackend):
    def __init__(self, *, read_delay: float = 0.0, write_delay: float = 0.0) -> None:
        super().__init__()
        self.read_delay = read_delay
        self.write_delay = write_delay

    def read_text(self) -> str | None:
        if self.read_delay > 0:
            time.sleep(self.read_delay)
        return super().read_text()

    def write_text(self, text: str) -> None:
        if self.write_delay > 0:
            time.sleep(self.write_delay)
        super().write_text(text)


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
            ["echo hello", "cd /tmp", "printf 'err\\n' 1>&2; /bin/sh -c 'exit 3'"],
        )

        self.assertEqual(len(results), 3)
        self.assertIn("hello", results[0].stdout)
        self.assertEqual(results[0].exit_code, 0)
        self.assertIsInstance(results[0].prompt_user, str)
        self.assertTrue(bool(results[0].prompt_user))
        self.assertIsInstance(results[0].prompt_cwd, str)
        self.assertTrue(bool(results[0].prompt_cwd))

        self.assertEqual(results[1].exit_code, 0)
        self.assertEqual(results[1].prompt_cwd, "/tmp")

        self.assertIn("err", results[2].stderr)
        self.assertEqual(results[2].exit_code, 3)
        self.assertEqual(results[2].prompt_cwd, "/tmp")

    def test_busy_when_second_client_connects(self) -> None:
        client1 = self._make_client()
        client2 = self._make_client()

        client1.connect("localhost")
        try:
            with self.assertRaises(RuntimeError):
                client2.connect("localhost")
        finally:
            client1.disconnect()

    def test_first_prompt_is_populated_on_connect_ack(self) -> None:
        client = self._make_client()
        client.connect("localhost")
        try:
            prompt = client._render_prompt("localhost")
            self.assertIn(f"@{socket.gethostname()}:", prompt)
            self.assertTrue(prompt.endswith("$ "))
            self.assertNotEqual(prompt, "sshc> ")
        finally:
            client.disconnect()

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

    def test_connect_tolerates_slow_clipboard_reads(self) -> None:
        delayed_clipboard = DelayedMemoryClipboardBackend(read_delay=0.08)
        server = ClipboardSSHServer(
            backend=delayed_clipboard,
            config=ServerConfig(
                poll_interval=0.01,
                max_output_chunk=8,
                preferred_shell="sh",
                command_timeout=5.0,
                verbose=False,
            ),
        )
        stop_event = threading.Event()
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"stop_event": stop_event},
            daemon=True,
        )
        server_thread.start()
        time.sleep(0.05)

        client = ClipboardSSHClient(
            backend=delayed_clipboard,
            config=ClientConfig(
                poll_interval=0.01,
                connect_timeout=2.0,
                session_timeout=4.0,
                retry_interval=0.05,
                verbose=False,
            ),
        )

        try:
            client.connect("localhost")
            self.assertTrue(client.is_connected)
        finally:
            client.disconnect()
            stop_event.set()
            server_thread.join(timeout=2.0)

    def test_connect_tolerates_noise_during_handshake(self) -> None:
        client = self._make_client()

        def inject_noise() -> None:
            for _ in range(3):
                time.sleep(0.02)
                self.clipboard.write_text("ordinary clipboard data")

        noise_thread = threading.Thread(target=inject_noise, daemon=True)
        noise_thread.start()
        try:
            client.connect("localhost")
            self.assertTrue(client.is_connected)
        finally:
            client.disconnect()
            noise_thread.join(timeout=1.0)

    def test_server_write_message_has_no_forced_sleep(self) -> None:
        server = ClipboardSSHServer(
            backend=MemoryClipboardBackend(),
            config=ServerConfig(poll_interval=0.5, verbose=False),
        )
        message = build_message(
            kind="connect_ack",
            session_id=str(uuid.uuid4()),
            source="server",
            target="client",
            seq=1,
            body={},
        )

        with mock.patch("clipssh.server.time.sleep") as sleep:
            wrote = server._write_message(message)

        self.assertTrue(wrote)
        sleep.assert_not_called()

    def test_server_emit_command_messages_paces_between_frames(self) -> None:
        server = ClipboardSSHServer(
            backend=MemoryClipboardBackend(),
            config=ServerConfig(inter_frame_delay=0.02, verbose=False),
        )
        session_id = str(uuid.uuid4())
        frames = [
            build_message(
                kind="stdout",
                session_id=session_id,
                source="server",
                target="client",
                seq=1,
                body={"cmd_id": "a", "data": "x"},
            ),
            build_message(
                kind="stdout",
                session_id=session_id,
                source="server",
                target="client",
                seq=2,
                body={"cmd_id": "a", "data": "y"},
            ),
            build_message(
                kind="exit",
                session_id=session_id,
                source="server",
                target="client",
                seq=3,
                body={"cmd_id": "a", "exit_code": 0},
            ),
        ]

        with mock.patch.object(server, "_write_message", return_value=True) as write_message, mock.patch(
            "clipssh.server.time.sleep"
        ) as sleep:
            server._emit_command_messages(frames)

        self.assertEqual(write_message.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.02)


if __name__ == "__main__":
    unittest.main()
