from __future__ import annotations

import pathlib
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.protocol import (
    PROTOCOL_NAME,
    VALID_KINDS,
    WIRE_PREFIX,
    build_message,
    decode_message,
    encode_message,
)


class ProtocolTests(unittest.TestCase):
    def test_round_trip_encode_decode(self) -> None:
        session_id = str(uuid.uuid4())
        message = build_message(
            kind="cmd",
            session_id=session_id,
            source="client",
            target="server",
            seq=1,
            body={"command": "echo hi", "cmd_id": str(uuid.uuid4())},
        )

        wire = encode_message(message)
        parsed = decode_message(wire)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.protocol, PROTOCOL_NAME)
        self.assertEqual(parsed.kind, "cmd")
        self.assertEqual(parsed.session_id, session_id)
        self.assertEqual(parsed.seq, 1)
        self.assertEqual(parsed.body["command"], "echo hi")

    def test_decode_ignores_non_protocol_content(self) -> None:
        self.assertIsNone(decode_message("hello world"))
        self.assertIsNone(decode_message(None))

    def test_decode_rejects_invalid_payload(self) -> None:
        invalid = f"{WIRE_PREFIX}" + '{"protocol":"clipssh/1","kind":"bad-kind"}'
        self.assertIsNone(decode_message(invalid))

    def test_build_message_validates_kind(self) -> None:
        session_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            build_message(
                kind="not-a-kind",
                session_id=session_id,
                source="client",
                target="server",
                seq=1,
                body={},
            )

    def test_all_expected_kinds_available(self) -> None:
        self.assertIn("connect_req", VALID_KINDS)
        self.assertIn("connect_ack", VALID_KINDS)
        self.assertIn("cmd", VALID_KINDS)
        self.assertIn("stdout", VALID_KINDS)
        self.assertIn("stderr", VALID_KINDS)
        self.assertIn("exit", VALID_KINDS)
        self.assertIn("disconnect", VALID_KINDS)
        self.assertIn("busy", VALID_KINDS)
        self.assertIn("error", VALID_KINDS)


if __name__ == "__main__":
    unittest.main()
