from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_io_ffmpeg import AudioDuplexIO
from gitssh.audio_modem_transport import (
    AudioModemTransportBackend,
    AudioModemTransportConfig,
    AudioModemTransportError,
)
from gitssh.protocol import build_message


class _LoopAudioEndpoint(AudioDuplexIO):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._peer: _LoopAudioEndpoint | None = None
        self._closed = False

    def connect(self, peer: "_LoopAudioEndpoint") -> None:
        self._peer = peer

    def read(self, max_bytes: int) -> bytes:
        with self._lock:
            if self._closed or max_bytes < 1 or not self._buffer:
                return b""
            count = min(max_bytes, len(self._buffer))
            out = bytes(self._buffer[:count])
            del self._buffer[:count]
            return out

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        peer = self._peer
        if peer is None:
            return
        with peer._lock:
            if peer._closed:
                return
            peer._buffer.extend(data)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._buffer.clear()


def _pair_endpoints() -> tuple[_LoopAudioEndpoint, _LoopAudioEndpoint]:
    a = _LoopAudioEndpoint()
    b = _LoopAudioEndpoint()
    a.connect(b)
    b.connect(a)
    return a, b


class AudioModemTransportTests(unittest.TestCase):
    def _make_pair(
        self,
        *,
        ack_timeout: float = 0.05,
        max_retries: int = 10,
    ) -> tuple[AudioModemTransportBackend, AudioModemTransportBackend]:
        endpoint_a, endpoint_b = _pair_endpoints()

        backend_a = AudioModemTransportBackend(
            AudioModemTransportConfig(
                input_device="test",
                output_device="test",
                ack_timeout=ack_timeout,
                max_retries=max_retries,
                byte_repeat=3,
                marker_run=8,
                io_factory=lambda _cfg: endpoint_a,
            )
        )
        backend_b = AudioModemTransportBackend(
            AudioModemTransportConfig(
                input_device="test",
                output_device="test",
                ack_timeout=ack_timeout,
                max_retries=max_retries,
                byte_repeat=3,
                marker_run=8,
                io_factory=lambda _cfg: endpoint_b,
            )
        )
        return backend_a, backend_b

    def test_round_trip_message_delivery(self) -> None:
        backend_a, backend_b = self._make_pair()
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

            received = []
            deadline = time.monotonic() + 2.0
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

    def test_duplicate_frames_are_deduplicated_when_acks_are_not_sent(self) -> None:
        backend_a, backend_b = self._make_pair(ack_timeout=0.01, max_retries=2)
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

            received = []
            failed = False
            for _ in range(80):
                try:
                    backend_a.push_outbound()
                except AudioModemTransportError:
                    failed = True
                    break
                backend_b.fetch_inbound()
                messages, cursor = backend_b.read_inbound_messages(cursor)
                if messages:
                    received.extend(messages)
                # Intentionally do not push outbound on backend_b to withhold ACKs.
                time.sleep(0.005)

            self.assertTrue(failed)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].msg_id, message.msg_id)
        finally:
            backend_a.close()
            backend_b.close()


if __name__ == "__main__":
    unittest.main()
