"""Message protocol for git transport."""

from __future__ import annotations

from typing import Any

from sshcore.protocol import (
    Message,
    build_message as _build_message,
    decode_message as _decode_message,
    encode_message as _encode_message,
)

PROTOCOL_NAME = "gitssh/2"
WIRE_PREFIX = ""
VALID_KINDS = {
    "connect_req",
    "connect_ack",
    "pty_input",
    "pty_output",
    "pty_resize",
    "pty_signal",
    "pty_closed",
    "disconnect",
    "error",
    "busy",
}



def build_message(
    *,
    kind: str,
    session_id: str,
    source: str,
    target: str,
    seq: int,
    body: Any = None,
    msg_id: str | None = None,
    ts: str | None = None,
) -> Message:
    return _build_message(
        kind=kind,
        session_id=session_id,
        source=source,
        target=target,
        seq=seq,
        body=body,
        msg_id=msg_id,
        ts=ts,
        protocol_name=PROTOCOL_NAME,
        valid_kinds=VALID_KINDS,
    )



def encode_message(message: Message) -> str:
    return _encode_message(message, wire_prefix=WIRE_PREFIX)



def decode_message(text: str | None) -> Message | None:
    return _decode_message(
        text,
        protocol_name=PROTOCOL_NAME,
        wire_prefix=WIRE_PREFIX,
        valid_kinds=VALID_KINDS,
    )
