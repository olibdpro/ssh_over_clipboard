from __future__ import annotations

import pathlib
import socket
import sys
import time
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.protocol import build_message
from gitssh.usb_serial_transport import (
    USBSerialTransportBackend,
    USBSerialTransportConfig,
    USBSerialTransportError,
)


class USBSerialTransportTests(unittest.TestCase):
    def _make_pair(
        self,
        *,
        ack_timeout: float = 0.05,
        max_retries: int = 10,
    ) -> tuple[USBSerialTransportBackend, USBSerialTransportBackend, socket.socket, socket.socket]:
        sock_a, sock_b = socket.socketpair()
        backend_a = USBSerialTransportBackend(
            USBSerialTransportConfig(
                serial_fd=sock_a.fileno(),
                configure_tty=False,
                ack_timeout=ack_timeout,
                max_retries=max_retries,
            )
        )
        backend_b = USBSerialTransportBackend(
            USBSerialTransportConfig(
                serial_fd=sock_b.fileno(),
                configure_tty=False,
                ack_timeout=ack_timeout,
                max_retries=max_retries,
            )
        )
        return backend_a, backend_b, sock_a, sock_b

    def test_round_trip_message_delivery(self) -> None:
        backend_a, backend_b, sock_a, sock_b = self._make_pair()
        try:
            cursor = backend_b.snapshot_inbound_cursor()
            message = build_message(
                kind="connect_req",
                session_id=str(uuid.uuid4()),
                source="client",
                target="server",
                seq=1,
                body={"host": "localhost"},
            )
            backend_a.write_outbound_message(message)

            received: list = []
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not received:
                backend_a.push_outbound()
                backend_b.fetch_inbound()
                backend_b.push_outbound()
                backend_a.fetch_inbound()
                messages, cursor = backend_b.read_inbound_messages(cursor)
                if messages:
                    received.extend(messages)
                time.sleep(0.005)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].msg_id, message.msg_id)
        finally:
            backend_a.close()
            backend_b.close()
            sock_a.close()
            sock_b.close()

    def test_duplicate_data_frames_are_deduplicated_when_acks_are_missing(self) -> None:
        backend_a, backend_b, sock_a, sock_b = self._make_pair(ack_timeout=0.01, max_retries=2)
        try:
            cursor = backend_b.snapshot_inbound_cursor()
            message = build_message(
                kind="connect_req",
                session_id=str(uuid.uuid4()),
                source="client",
                target="server",
                seq=1,
                body={"host": "localhost"},
            )
            backend_a.write_outbound_message(message)

            received: list = []
            failed = False
            for _ in range(60):
                try:
                    backend_a.push_outbound()
                except USBSerialTransportError:
                    failed = True
                    break
                backend_b.fetch_inbound()
                messages, cursor = backend_b.read_inbound_messages(cursor)
                if messages:
                    received.extend(messages)
                # Intentionally skip backend_b.push_outbound() to suppress ACK delivery.
                time.sleep(0.01)

            self.assertTrue(failed)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].msg_id, message.msg_id)
        finally:
            backend_a.close()
            backend_b.close()
            sock_a.close()
            sock_b.close()

    def test_rejects_oversized_frame_payload(self) -> None:
        sock_a, sock_b = socket.socketpair()
        backend = USBSerialTransportBackend(
            USBSerialTransportConfig(
                serial_fd=sock_a.fileno(),
                configure_tty=False,
                frame_max_bytes=64,
            )
        )
        try:
            message = build_message(
                kind="connect_req",
                session_id=str(uuid.uuid4()),
                source="client",
                target="server",
                seq=1,
                body={"host": "x" * 500},
            )
            with self.assertRaises(USBSerialTransportError):
                backend.write_outbound_message(message)
        finally:
            backend.close()
            sock_a.close()
            sock_b.close()


if __name__ == "__main__":
    unittest.main()
