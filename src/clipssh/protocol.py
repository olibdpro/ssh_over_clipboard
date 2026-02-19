"""Message protocol for clipboard transport."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from typing import Any
import uuid

PROTOCOL_NAME = "clipssh/1"
WIRE_PREFIX = "CLIPSSH/1 "

VALID_KINDS = {
    "connect_req",
    "connect_ack",
    "cmd",
    "stdout",
    "stderr",
    "exit",
    "heartbeat",
    "disconnect",
    "error",
    "busy",
}


@dataclass(frozen=True)
class Message:
    protocol: str
    kind: str
    session_id: str
    msg_id: str
    ts: str
    source: str
    target: str
    seq: int
    body: Any



def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")



def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True



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
    if kind not in VALID_KINDS:
        raise ValueError(f"Unsupported message kind: {kind}")
    if source not in {"client", "server"}:
        raise ValueError(f"Unsupported source: {source}")
    if target not in {"client", "server"}:
        raise ValueError(f"Unsupported target: {target}")
    if not _is_uuid(session_id):
        raise ValueError("session_id must be a UUID")
    if seq < 1:
        raise ValueError("seq must be >= 1")

    final_msg_id = msg_id or str(uuid.uuid4())
    if not _is_uuid(final_msg_id):
        raise ValueError("msg_id must be a UUID")

    return Message(
        protocol=PROTOCOL_NAME,
        kind=kind,
        session_id=session_id,
        msg_id=final_msg_id,
        ts=ts or utc_timestamp(),
        source=source,
        target=target,
        seq=seq,
        body=body,
    )



def encode_message(message: Message) -> str:
    payload = json.dumps(asdict(message), ensure_ascii=True, separators=(",", ":"))
    return f"{WIRE_PREFIX}{payload}"



def _validate_payload(payload: Any) -> Message | None:
    if not isinstance(payload, dict):
        return None

    required = {
        "protocol",
        "kind",
        "session_id",
        "msg_id",
        "ts",
        "source",
        "target",
        "seq",
        "body",
    }
    if not required.issubset(payload):
        return None

    if payload["protocol"] != PROTOCOL_NAME:
        return None
    if payload["kind"] not in VALID_KINDS:
        return None
    if payload["source"] not in {"client", "server"}:
        return None
    if payload["target"] not in {"client", "server"}:
        return None
    if not _is_uuid(payload["session_id"]) or not _is_uuid(payload["msg_id"]):
        return None
    if not isinstance(payload["seq"], int) or payload["seq"] < 1:
        return None
    if not isinstance(payload["ts"], str):
        return None

    return Message(
        protocol=payload["protocol"],
        kind=payload["kind"],
        session_id=payload["session_id"],
        msg_id=payload["msg_id"],
        ts=payload["ts"],
        source=payload["source"],
        target=payload["target"],
        seq=payload["seq"],
        body=payload["body"],
    )



def decode_message(text: str | None) -> Message | None:
    if not text or not text.startswith(WIRE_PREFIX):
        return None
    raw = text[len(WIRE_PREFIX) :]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _validate_payload(payload)
