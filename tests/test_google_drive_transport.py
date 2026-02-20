from __future__ import annotations

import pathlib
from types import SimpleNamespace
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.google_drive_transport import (
    DEFAULT_DRIVE_LOG_C2S,
    DEFAULT_DRIVE_LOG_S2C,
    GoogleDriveTransportBackend,
    GoogleDriveTransportConfig,
    GoogleDriveTransportError,
)
from gitssh.protocol import build_message, encode_message


class _FakeHttpError(Exception):
    def __init__(self, status: int, text: str = "") -> None:
        super().__init__(text or f"http {status}")
        self.resp = SimpleNamespace(status=status)


class _FakeRequest:
    def __init__(self, callback):
        self._callback = callback

    def execute(self):
        return self._callback()


class _FakeDriveFilesResource:
    def __init__(self) -> None:
        self._next_id = 1
        self._files: dict[str, dict[str, str]] = {}
        self.update_failures_remaining = 0

    def files_for_name(self, name: str) -> list[dict[str, str]]:
        return [
            {"id": file_id, "name": file_info["name"]}
            for file_id, file_info in self._files.items()
            if file_info["name"] == name
        ]

    def file_content_by_name(self, name: str) -> str:
        matches = self.files_for_name(name)
        if not matches:
            raise KeyError(name)
        return self._files[matches[0]["id"]]["content"]

    def set_file_content_by_name(self, name: str, content: str) -> None:
        matches = self.files_for_name(name)
        if not matches:
            file_id = f"file-{self._next_id}"
            self._next_id += 1
            self._files[file_id] = {"name": name, "content": content}
            return
        self._files[matches[0]["id"]]["content"] = content

    def list(self, *, q: str, spaces: str, fields: str, pageSize: int):
        _ = (spaces, fields)

        def _execute():
            name = _extract_name_from_query(q)
            if name is None:
                listed = [{"id": file_id, "name": info["name"]} for file_id, info in self._files.items()]
            else:
                listed = self.files_for_name(name)
            return {"files": listed[:pageSize]}

        return _FakeRequest(_execute)

    def create(self, *, body: dict, media_body, fields: str):
        _ = fields

        def _execute():
            file_id = f"file-{self._next_id}"
            self._next_id += 1
            self._files[file_id] = {
                "name": str(body.get("name", "")),
                "content": _media_to_text(media_body),
            }
            return {"id": file_id}

        return _FakeRequest(_execute)

    def get_media(self, *, fileId: str):
        def _execute():
            return self._files[fileId]["content"].encode("utf-8")

        return _FakeRequest(_execute)

    def update(self, *, fileId: str, media_body):
        def _execute():
            if self.update_failures_remaining > 0:
                self.update_failures_remaining -= 1
                raise _FakeHttpError(503, "backend error")
            self._files[fileId]["content"] = _media_to_text(media_body)
            return {"id": fileId}

        return _FakeRequest(_execute)


class _FakeDriveService:
    def __init__(self) -> None:
        self._files_resource = _FakeDriveFilesResource()

    def files(self) -> _FakeDriveFilesResource:
        return self._files_resource


def _extract_name_from_query(query: str) -> str | None:
    marker = "name = '"
    start = query.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = query.find("'", start)
    if end < 0:
        return None
    return query[start:end].replace("\\'", "'")


def _media_to_text(media_body) -> str:
    if isinstance(media_body, bytes):
        return media_body.decode("utf-8", errors="ignore")
    if isinstance(media_body, bytearray):
        return bytes(media_body).decode("utf-8", errors="ignore")
    if isinstance(media_body, str):
        return media_body
    raise TypeError(f"Unsupported fake media body type: {type(media_body).__name__}")


class GoogleDriveTransportTests(unittest.TestCase):
    def _build_backend_pair(self) -> tuple[GoogleDriveTransportBackend, GoogleDriveTransportBackend, _FakeDriveService]:
        service = _FakeDriveService()

        client_backend = GoogleDriveTransportBackend(
            GoogleDriveTransportConfig(
                client_secrets_path="unused-in-tests.json",
                inbound_file_name=DEFAULT_DRIVE_LOG_S2C,
                outbound_file_name=DEFAULT_DRIVE_LOG_C2S,
                drive_service=service,
                media_upload_factory=lambda data: data,
            )
        )
        server_backend = GoogleDriveTransportBackend(
            GoogleDriveTransportConfig(
                client_secrets_path="unused-in-tests.json",
                inbound_file_name=DEFAULT_DRIVE_LOG_C2S,
                outbound_file_name=DEFAULT_DRIVE_LOG_S2C,
                drive_service=service,
                media_upload_factory=lambda data: data,
            )
        )

        return client_backend, server_backend, service

    def test_round_trip_and_cursor_progression(self) -> None:
        client_backend, server_backend, _service = self._build_backend_pair()
        cursor = server_backend.snapshot_inbound_cursor()

        message = build_message(
            kind="connect_req",
            session_id=str(uuid.uuid4()),
            source="client",
            target="server",
            seq=1,
            body={"host": "localhost"},
        )
        client_backend.write_outbound_message(message)

        server_backend.fetch_inbound()
        messages, cursor = server_backend.read_inbound_messages(cursor)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].msg_id, message.msg_id)

        again, cursor_again = server_backend.read_inbound_messages(cursor)
        self.assertEqual(again, [])
        self.assertEqual(cursor_again, cursor)

    def test_ignores_malformed_log_lines(self) -> None:
        _client_backend, server_backend, service = self._build_backend_pair()

        valid_message = build_message(
            kind="connect_req",
            session_id=str(uuid.uuid4()),
            source="client",
            target="server",
            seq=1,
            body={"host": "localhost"},
        )

        payload = "\n".join(
            [
                "not-json",
                encode_message(valid_message),
                "{}",
            ]
        )
        service.files().set_file_content_by_name(DEFAULT_DRIVE_LOG_C2S, f"{payload}\n")

        cursor = "0"
        server_backend.fetch_inbound()
        messages, _next = server_backend.read_inbound_messages(cursor)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].msg_id, valid_message.msg_id)

    def test_auto_creates_missing_log_files(self) -> None:
        service = _FakeDriveService()

        backend = GoogleDriveTransportBackend(
            GoogleDriveTransportConfig(
                client_secrets_path="unused-in-tests.json",
                inbound_file_name="inbound-custom.log",
                outbound_file_name="outbound-custom.log",
                drive_service=service,
                media_upload_factory=lambda data: data,
            )
        )

        self.assertIsNotNone(backend)
        self.assertEqual(len(service.files().files_for_name("inbound-custom.log")), 1)
        self.assertEqual(len(service.files().files_for_name("outbound-custom.log")), 1)

    def test_retries_transient_update_failures(self) -> None:
        service = _FakeDriveService()
        backend = GoogleDriveTransportBackend(
            GoogleDriveTransportConfig(
                client_secrets_path="unused-in-tests.json",
                inbound_file_name=DEFAULT_DRIVE_LOG_S2C,
                outbound_file_name=DEFAULT_DRIVE_LOG_C2S,
                drive_service=service,
                media_upload_factory=lambda data: data,
                max_retries=4,
                retry_base_delay=0.0,
            )
        )

        service.files().update_failures_remaining = 2

        message = build_message(
            kind="connect_req",
            session_id=str(uuid.uuid4()),
            source="client",
            target="server",
            seq=1,
            body={"host": "localhost"},
        )
        backend.write_outbound_message(message)

        content = service.files().file_content_by_name(DEFAULT_DRIVE_LOG_C2S)
        self.assertIn(message.msg_id, content)
        self.assertEqual(service.files().update_failures_remaining, 0)

    def test_wraps_auth_failures(self) -> None:
        def _fail_auth(_cfg: GoogleDriveTransportConfig):
            raise RuntimeError("auth failed")

        with self.assertRaises(GoogleDriveTransportError) as ctx:
            GoogleDriveTransportBackend(
                GoogleDriveTransportConfig(
                    client_secrets_path="unused-in-tests.json",
                    auth_factory=_fail_auth,
                    drive_service_factory=lambda _creds: _FakeDriveService(),
                    media_upload_factory=lambda data: data,
                )
            )

        self.assertIn("auth failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
