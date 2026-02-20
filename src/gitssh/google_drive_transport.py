"""Google Drive appData transport backend with OAuth authentication."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, TypeVar

from .protocol import Message, decode_message, encode_message
from .transport import TransportError

DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
DEFAULT_DRIVE_LOG_C2S = "gitssh2-c2s.log"
DEFAULT_DRIVE_LOG_S2C = "gitssh2-s2c.log"

T = TypeVar("T")


class GoogleDriveTransportError(TransportError):
    """Raised when Google Drive transport operations fail."""


@dataclass
class GoogleDriveTransportConfig:
    client_secrets_path: str
    token_path: str = "~/.config/clipssh/drive-token.json"
    inbound_file_name: str = DEFAULT_DRIVE_LOG_C2S
    outbound_file_name: str = DEFAULT_DRIVE_LOG_S2C
    scope: str = DRIVE_APPDATA_SCOPE
    poll_page_size: int = 200
    max_retries: int = 5
    retry_base_delay: float = 0.2
    drive_service: Any | None = None
    auth_factory: Callable[["GoogleDriveTransportConfig"], Any] | None = None
    drive_service_factory: Callable[[Any], Any] | None = None
    media_upload_factory: Callable[[bytes], Any] | None = None


class GoogleDriveTransportBackend:
    """Stores transport messages in two shared Google Drive appData log files."""

    def __init__(self, config: GoogleDriveTransportConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._inbound_lines: list[str] = []

        self._drive = self._build_drive_service()
        self._inbound_file_id = self._ensure_appdata_file(self.config.inbound_file_name)
        self._outbound_file_id = self._ensure_appdata_file(self.config.outbound_file_name)
        self.fetch_inbound()

    def name(self) -> str:
        return (
            "google-drive:"
            f"in={self.config.inbound_file_name},"
            f"out={self.config.outbound_file_name},"
            f"scope={self.config.scope}"
        )

    def snapshot_inbound_cursor(self) -> str | None:
        with self._lock:
            return str(len(self._inbound_lines))

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        with self._lock:
            start = self._parse_cursor(cursor)
            if start >= len(self._inbound_lines):
                return [], str(len(self._inbound_lines))

            messages: list[Message] = []
            for line in self._inbound_lines[start:]:
                message = decode_message(line)
                if message is None:
                    continue
                messages.append(message)

            return messages, str(len(self._inbound_lines))

    def fetch_inbound(self) -> None:
        with self._lock:
            text = self._download_file_text(self._inbound_file_id)
            self._inbound_lines = [line.strip() for line in text.splitlines() if line.strip()]

    def write_outbound_message(self, message: Message) -> str:
        payload = encode_message(message)

        with self._lock:
            existing = self._download_file_text(self._outbound_file_id)
            if existing and not existing.endswith("\n"):
                existing += "\n"
            updated = f"{existing}{payload}\n"
            self._upload_file_text(self._outbound_file_id, updated)

        return message.msg_id

    def push_outbound(self) -> None:
        """Writes are sent immediately on each append."""
        return None

    def close(self) -> None:
        """API parity with other transports."""
        return None

    def _build_drive_service(self) -> Any:
        if self.config.drive_service is not None:
            return self.config.drive_service

        credentials = self._authorize()

        if self.config.drive_service_factory is not None:
            service = self.config.drive_service_factory(credentials)
            if service is None:
                raise GoogleDriveTransportError("Drive service factory returned no service")
            return service

        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GoogleDriveTransportError(
                "Google API client libraries are required for --transport google-drive. "
                "Install: google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2"
            ) from exc

        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _authorize(self) -> Any:
        if self.config.auth_factory is not None:
            try:
                return self.config.auth_factory(self.config)
            except Exception as exc:
                raise GoogleDriveTransportError(f"Google Drive OAuth failed: {exc}") from exc

        return self._default_authorize()

    def _default_authorize(self) -> Any:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise GoogleDriveTransportError(
                "Google API client libraries are required for --transport google-drive. "
                "Install: google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2"
            ) from exc

        client_secrets_path = Path(self.config.client_secrets_path).expanduser()
        token_path = Path(self.config.token_path).expanduser()

        if not client_secrets_path.exists():
            raise GoogleDriveTransportError(
                f"Google OAuth client secrets file does not exist: {client_secrets_path}"
            )

        scopes = [self.config.scope]
        creds: Any | None = None

        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), scopes=scopes)
            except Exception:
                creds = None

        if creds is not None and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise GoogleDriveTransportError(f"Failed to refresh Google OAuth token: {exc}") from exc
            self._write_token_json(token_path, creds.to_json())

        if creds is None or not getattr(creds, "valid", False):
            if not sys.stdin.isatty():
                raise GoogleDriveTransportError(
                    "Google OAuth token is missing or invalid and interactive login is required. "
                    "Run once in an interactive terminal to complete OAuth consent."
                )

            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(client_secrets_path),
                    scopes=scopes,
                )
                creds = flow.run_local_server(port=0)
            except Exception as exc:
                raise GoogleDriveTransportError(f"Failed to complete Google OAuth flow: {exc}") from exc

            self._write_token_json(token_path, creds.to_json())

        return creds

    def _write_token_json(self, token_path: Path, payload: str) -> None:
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(payload, encoding="utf-8")
            if os.name != "nt":
                os.chmod(token_path, 0o600)
        except OSError as exc:
            raise GoogleDriveTransportError(f"Failed to persist OAuth token at {token_path}: {exc}") from exc

    def _ensure_appdata_file(self, name: str) -> str:
        existing = self._find_file_id_by_name(name)
        if existing is not None:
            return existing

        body = {
            "name": name,
            "parents": ["appDataFolder"],
        }
        media = self._make_media_upload(b"")

        created = self._run_drive_call(
            action=f"create appData file {name}",
            func=lambda: self._drive.files()
            .create(body=body, media_body=media, fields="id")
            .execute(),
        )

        file_id = (created or {}).get("id")
        if not isinstance(file_id, str) or not file_id:
            raise GoogleDriveTransportError(
                f"Drive create returned invalid file id for {name}: {created!r}"
            )

        return file_id

    def _find_file_id_by_name(self, name: str) -> str | None:
        safe_name = name.replace("'", "\\'")
        query = f"name = '{safe_name}' and trashed = false"

        result = self._run_drive_call(
            action=f"find appData file {name}",
            func=lambda: self._drive.files()
            .list(
                q=query,
                spaces="appDataFolder",
                fields="files(id,name)",
                pageSize=max(self.config.poll_page_size, 1),
            )
            .execute(),
        )

        files = (result or {}).get("files")
        if not isinstance(files, list):
            return None

        for item in files:
            if not isinstance(item, dict):
                continue
            file_id = item.get("id")
            if isinstance(file_id, str) and file_id:
                return file_id

        return None

    def _download_file_text(self, file_id: str) -> str:
        content = self._run_drive_call(
            action=f"download file {file_id}",
            func=lambda: self._drive.files().get_media(fileId=file_id).execute(),
        )

        if content is None:
            return ""
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="ignore")
        if isinstance(content, str):
            return content

        raise GoogleDriveTransportError(
            f"Unexpected Drive download payload type: {type(content).__name__}"
        )

    def _upload_file_text(self, file_id: str, text: str) -> None:
        payload = text.encode("utf-8")
        media = self._make_media_upload(payload)

        self._run_drive_call(
            action=f"update file {file_id}",
            func=lambda: self._drive.files().update(fileId=file_id, media_body=media).execute(),
        )

    def _make_media_upload(self, payload: bytes) -> Any:
        if self.config.media_upload_factory is not None:
            return self.config.media_upload_factory(payload)

        try:
            from googleapiclient.http import MediaInMemoryUpload
        except ImportError as exc:
            raise GoogleDriveTransportError(
                "googleapiclient is required to upload Google Drive log updates"
            ) from exc

        return MediaInMemoryUpload(payload, mimetype="text/plain", resumable=False)

    def _run_drive_call(self, *, action: str, func: Callable[[], T]) -> T:
        delay = max(self.config.retry_base_delay, 0.0)
        attempts = max(self.config.max_retries, 1)

        for attempt in range(attempts):
            try:
                return func()
            except Exception as exc:
                retryable = self._is_retryable_error(exc)
                if retryable and attempt + 1 < attempts:
                    if delay > 0:
                        time.sleep(delay)
                    delay = min(delay * 2.0 if delay > 0 else 0.1, 2.0)
                    continue

                raise GoogleDriveTransportError(f"Google Drive {action} failed: {exc}") from exc

        raise GoogleDriveTransportError(f"Google Drive {action} failed after retries")

    def _is_retryable_error(self, exc: Exception) -> bool:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in {429, 500, 502, 503, 504}:
            return True

        if isinstance(status, str):
            try:
                code = int(status)
            except ValueError:
                code = 0
            if code in {429, 500, 502, 503, 504}:
                return True

        text = str(exc).lower()
        transient_patterns = (
            "rate limit",
            "backend error",
            "internal error",
            "temporarily unavailable",
            "connection reset",
            "timeout",
        )
        return any(pattern in text for pattern in transient_patterns)

    def _parse_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0

        try:
            value = int(cursor)
        except (TypeError, ValueError):
            return 0

        return max(value, 0)
