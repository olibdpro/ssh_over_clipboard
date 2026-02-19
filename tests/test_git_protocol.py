from __future__ import annotations

import pathlib
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.protocol import PROTOCOL_NAME, VALID_KINDS, build_message, decode_message, encode_message


class GitProtocolTests(unittest.TestCase):
    def test_round_trip_encode_decode(self) -> None:
        session_id = str(uuid.uuid4())
        message = build_message(
            kind="pty_input",
            session_id=session_id,
            source="client",
            target="server",
            seq=1,
            body={"stream_id": str(uuid.uuid4()), "data_b64": "YQ=="},
        )

        wire = encode_message(message)
        parsed = decode_message(wire)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.protocol, PROTOCOL_NAME)
        self.assertEqual(parsed.kind, "pty_input")
        self.assertEqual(parsed.session_id, session_id)
        self.assertEqual(parsed.seq, 1)

    def test_cmd_kind_is_rejected(self) -> None:
        session_id = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            build_message(
                kind="cmd",
                session_id=session_id,
                source="client",
                target="server",
                seq=1,
                body={"command": "echo hi"},
            )

    def test_expected_kinds_available(self) -> None:
        self.assertIn("connect_req", VALID_KINDS)
        self.assertIn("connect_ack", VALID_KINDS)
        self.assertIn("pty_input", VALID_KINDS)
        self.assertIn("pty_output", VALID_KINDS)
        self.assertIn("pty_resize", VALID_KINDS)
        self.assertIn("pty_signal", VALID_KINDS)
        self.assertIn("pty_closed", VALID_KINDS)
        self.assertNotIn("cmd", VALID_KINDS)


if __name__ == "__main__":
    unittest.main()
