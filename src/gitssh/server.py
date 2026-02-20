"""Git transport SSH server daemon."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass, field
import getpass
import socket
import sys
import threading
import time
from typing import Any
import uuid

from sshcore.pty_shell import PtyShellError, PtyShellSession
from sshcore.session import EndpointState
from sshcore.shell import resolve_shell

from .git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
)
from .google_drive_transport import (
    DEFAULT_DRIVE_LOG_C2S,
    DEFAULT_DRIVE_LOG_S2C,
    GoogleDriveTransportBackend,
    GoogleDriveTransportConfig,
)
from .audio_io_ffmpeg import PulseCliAudioDuplexIO
from .audio_modem_transport import AudioModemTransportBackend, AudioModemTransportConfig
from .audio_pulse_runtime import PulseRuntimeError, resolve_server_default_paths
from .protocol import Message, build_message
from .transport import TransportBackend, TransportError
from .usb_serial_transport import USBSerialTransportBackend, USBSerialTransportConfig


@dataclass
class ServerConfig:
    poll_interval: float = 0.1
    max_output_chunk: int = 4096
    preferred_shell: str = "tcsh"
    command_timeout: float = 120.0
    io_flush_interval: float = 0.02
    fetch_interval: float = 0.1
    push_interval: float = 0.1
    verbose: bool = False


@dataclass
class ActiveSession:
    state: EndpointState
    shell: PtyShellSession
    stream_id: str
    pending_output: bytearray = field(default_factory=bytearray)
    last_flush_at: float = field(default_factory=time.monotonic)


class GitSSHServer:
    def __init__(self, backend: TransportBackend, config: ServerConfig) -> None:
        self.backend = backend
        self.config = config
        self._active: ActiveSession | None = None
        self._server_seq = 0
        self._cursor: str | None = None
        self._sync_stop: threading.Event | None = None
        self._sync_thread: threading.Thread | None = None

    def _log(self, text: str) -> None:
        if self.config.verbose:
            print(f"[sshgd] {text}", file=sys.stderr)

    def _next_seq(self) -> int:
        self._server_seq += 1
        return self._server_seq

    def _start_sync_worker(self) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return

        self._sync_stop = threading.Event()
        self._sync_thread = threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="gitssh-server-sync",
        )
        self._sync_thread.start()

    def _stop_sync_worker(self) -> None:
        stop_event = self._sync_stop
        thread = self._sync_thread
        self._sync_stop = None
        self._sync_thread = None

        if stop_event is not None:
            stop_event.set()
        if thread is not None:
            thread.join(timeout=1.0)

    def _sync_loop(self) -> None:
        stop_event = self._sync_stop
        if stop_event is None:
            return

        next_fetch = 0.0
        next_push = 0.0

        while not stop_event.is_set():
            now = time.monotonic()
            did_work = False

            if now >= next_fetch:
                try:
                    self.backend.fetch_inbound()
                except TransportError as exc:
                    self._log(f"fetch failed: {exc}")
                next_fetch = now + self.config.fetch_interval
                did_work = True

            if now >= next_push:
                try:
                    self.backend.push_outbound()
                except TransportError as exc:
                    self._log(f"push failed: {exc}")
                next_push = now + self.config.push_interval
                did_work = True

            if did_work:
                continue

            wait_fetch = max(next_fetch - now, 0.0)
            wait_push = max(next_push - now, 0.0)
            wait_time = min(wait_fetch, wait_push, 0.1)
            stop_event.wait(wait_time)

    def _read_messages(self) -> list[Message]:
        try:
            messages, self._cursor = self.backend.read_inbound_messages(self._cursor)
        except TransportError as exc:
            self._log(f"transport read failed: {exc}")
            return []
        return messages

    def _write_message(self, message: Message) -> None:
        try:
            self.backend.write_outbound_message(message)
        except TransportError as exc:
            self._log(f"transport write failed: {exc}")

    def _make_message(self, *, kind: str, session_id: str, body: Any = None) -> Message:
        return build_message(
            kind=kind,
            session_id=session_id,
            source="server",
            target="client",
            seq=self._next_seq(),
            body=body,
        )

    def _server_hostname(self) -> str | None:
        try:
            hostname = socket.gethostname()
            return hostname or None
        except OSError:
            return None

    def _collect_prompt_context(self) -> dict[str, str | None]:
        return {
            "user": getpass.getuser(),
            "cwd": None,
            "host": self._server_hostname(),
        }

    def _close_active_session(self) -> None:
        if self._active is None:
            return

        self._log(f"closing session {self._active.state.session_id}")
        self._active.shell.close()
        self._active = None

    def _term_size_from_connect(self, message: Message) -> tuple[int, int]:
        cols = 80
        rows = 24

        body = message.body if isinstance(message.body, dict) else {}
        pty = body.get("pty")
        if isinstance(pty, dict):
            raw_cols = pty.get("cols")
            raw_rows = pty.get("rows")
            if isinstance(raw_cols, int):
                cols = max(raw_cols, 1)
            if isinstance(raw_rows, int):
                rows = max(raw_rows, 1)

        return cols, rows

    def _session_error(self, *, session_id: str, text: str) -> None:
        self._write_message(
            self._make_message(
                kind="error",
                session_id=session_id,
                body={"error": text},
            )
        )

    def _connect_ack_body(self, *, stream_id: str, shell_path: str) -> dict[str, Any]:
        return {
            "shell": shell_path,
            "backend": self.backend.name(),
            "stream_id": stream_id,
            "prompt": self._collect_prompt_context(),
        }

    def _handle_connect(self, message: Message) -> None:
        if self._active is not None:
            if message.session_id == self._active.state.session_id:
                self._log(f"re-acknowledging session {message.session_id}")
                self._write_message(
                    self._make_message(
                        kind="connect_ack",
                        session_id=message.session_id,
                        body=self._connect_ack_body(
                            stream_id=self._active.stream_id,
                            shell_path=self._active.shell.shell_path,
                        ),
                    )
                )
                return

            self._log(f"rejecting session {message.session_id}: busy")
            self._write_message(
                self._make_message(
                    kind="busy",
                    session_id=message.session_id,
                    body={"reason": "server has an active session"},
                )
            )
            return

        cols, rows = self._term_size_from_connect(message)

        try:
            shell_path, _shell_flavor = resolve_shell(self.config.preferred_shell)
            shell = PtyShellSession(shell_path=shell_path, cols=cols, rows=rows)
        except Exception as exc:
            self._session_error(
                session_id=message.session_id,
                text=f"failed to start pty shell: {exc}",
            )
            return

        stream_id = str(uuid.uuid4())
        self._active = ActiveSession(
            state=EndpointState(session_id=message.session_id),
            shell=shell,
            stream_id=stream_id,
        )

        self._log(
            f"accepted session {message.session_id} using {shell_path} (stream_id={stream_id})"
        )
        self._write_message(
            self._make_message(
                kind="connect_ack",
                session_id=message.session_id,
                body=self._connect_ack_body(stream_id=stream_id, shell_path=shell_path),
            )
        )

    def _emit_pty_output(self, session: ActiveSession, payload: bytes) -> None:
        encoded = base64.b64encode(payload).decode("ascii")
        self._write_message(
            self._make_message(
                kind="pty_output",
                session_id=session.state.session_id,
                body={"stream_id": session.stream_id, "data_b64": encoded},
            )
        )

    def _flush_pending_output(self, *, force: bool = False) -> None:
        session = self._active
        if session is None or not session.pending_output:
            return

        chunk_size = max(self.config.max_output_chunk, 1)

        def should_flush() -> bool:
            if force:
                return True
            if len(session.pending_output) >= chunk_size:
                return True
            return (time.monotonic() - session.last_flush_at) >= max(self.config.io_flush_interval, 0.0)

        while session.pending_output and should_flush():
            data = bytes(session.pending_output[:chunk_size])
            del session.pending_output[:chunk_size]
            self._emit_pty_output(session, data)
            session.last_flush_at = time.monotonic()
            if not force and len(session.pending_output) < chunk_size:
                break

    def _drain_pty_output(self) -> None:
        session = self._active
        if session is None:
            return

        chunk_size = max(self.config.max_output_chunk, 1)
        while True:
            try:
                data = session.shell.read_output(timeout=0.0, max_bytes=chunk_size)
            except PtyShellError as exc:
                self._log(f"pty output read failed: {exc}")
                break
            if not data:
                break
            session.pending_output.extend(data)
            self._flush_pending_output(force=False)

        self._flush_pending_output(force=False)

    def _handle_pty_input(self, message: Message) -> None:
        session = self._active
        if session is None:
            return

        body = message.body if isinstance(message.body, dict) else {}
        stream_id = body.get("stream_id")
        data_b64 = body.get("data_b64")
        if stream_id != session.stream_id:
            self._session_error(
                session_id=message.session_id,
                text="pty_input stream_id does not match active stream",
            )
            return
        if not isinstance(data_b64, str):
            self._session_error(
                session_id=message.session_id,
                text="pty_input payload must contain string field 'data_b64'",
            )
            return

        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error):
            self._session_error(session_id=message.session_id, text="pty_input contains invalid base64 data")
            return

        if not data:
            return

        try:
            session.shell.write_input(data)
        except PtyShellError as exc:
            self._session_error(session_id=message.session_id, text=f"failed to write PTY input: {exc}")

    def _handle_pty_resize(self, message: Message) -> None:
        session = self._active
        if session is None:
            return

        body = message.body if isinstance(message.body, dict) else {}
        stream_id = body.get("stream_id")
        cols = body.get("cols")
        rows = body.get("rows")

        if stream_id != session.stream_id:
            self._session_error(
                session_id=message.session_id,
                text="pty_resize stream_id does not match active stream",
            )
            return
        if not isinstance(cols, int) or not isinstance(rows, int):
            self._session_error(
                session_id=message.session_id,
                text="pty_resize payload must contain integer fields 'cols' and 'rows'",
            )
            return

        try:
            session.shell.resize(cols=max(cols, 1), rows=max(rows, 1))
        except PtyShellError as exc:
            self._session_error(session_id=message.session_id, text=f"failed to resize PTY: {exc}")

    def _handle_pty_signal(self, message: Message) -> None:
        session = self._active
        if session is None:
            return

        body = message.body if isinstance(message.body, dict) else {}
        stream_id = body.get("stream_id")
        signal_name = body.get("signal")

        if stream_id != session.stream_id:
            self._session_error(
                session_id=message.session_id,
                text="pty_signal stream_id does not match active stream",
            )
            return
        if not isinstance(signal_name, str):
            self._session_error(
                session_id=message.session_id,
                text="pty_signal payload must contain string field 'signal'",
            )
            return

        try:
            session.shell.send_signal(signal_name)
        except PtyShellError as exc:
            self._session_error(session_id=message.session_id, text=f"failed to send signal to PTY: {exc}")

    def _handle_disconnect(self, message: Message) -> None:
        self._log(f"disconnect requested for session {message.session_id}")
        self._close_active_session()

    def _handle_session_message(self, message: Message) -> None:
        if self._active is None:
            return
        if message.session_id != self._active.state.session_id:
            return

        is_new = self._active.state.incoming_seen.mark(message.msg_id)
        if not is_new:
            return

        if message.kind == "pty_input":
            self._handle_pty_input(message)
            return

        if message.kind == "pty_resize":
            self._handle_pty_resize(message)
            return

        if message.kind == "pty_signal":
            self._handle_pty_signal(message)
            return

        if message.kind == "disconnect":
            self._handle_disconnect(message)
            return

        self._session_error(
            session_id=message.session_id,
            text=f"Unsupported session message kind: {message.kind}",
        )

    def _handle_message(self, message: Message) -> None:
        if message.target != "server":
            return

        if message.kind == "connect_req":
            self._handle_connect(message)
            return

        if self._active is None:
            return

        self._handle_session_message(message)

    def _check_for_shell_exit(self) -> None:
        session = self._active
        if session is None:
            return
        if session.shell.is_alive():
            return

        self._flush_pending_output(force=True)

        exit_code = session.shell.wait_exit(timeout=0.0)
        if exit_code is None:
            exit_code = 1

        self._write_message(
            self._make_message(
                kind="pty_closed",
                session_id=session.state.session_id,
                body={"stream_id": session.stream_id, "exit_code": exit_code},
            )
        )
        self._close_active_session()

    def serve_forever(self, stop_event: threading.Event | None = None) -> None:
        self._log(f"server started with backend={self.backend.name()}")
        self._start_sync_worker()

        try:
            self.backend.fetch_inbound()
            self._cursor = self.backend.snapshot_inbound_cursor()

            while True:
                if stop_event is not None and stop_event.is_set():
                    return

                for message in self._read_messages():
                    self._handle_message(message)

                self._drain_pty_output()
                self._check_for_shell_exit()

                time.sleep(self.config.poll_interval)
        finally:
            self._stop_sync_worker()
            self._close_active_session()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshgd", description="Git transport SSH server daemon")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    parser.add_argument(
        "--transport",
        choices=["git", "usb-serial", "audio-modem", "google-drive"],
        default="git",
        help="Transport backend",
    )
    parser.add_argument(
        "--local-repo",
        default="/tmp/gitssh-server.git",
        help="Path to this server's local bare mirror repository",
    )
    parser.add_argument(
        "--upstream-url",
        default="/tmp/gitssh-upstream.git",
        help="Shared upstream git bare repository URL or path",
    )
    parser.add_argument(
        "--branch-c2s",
        default=DEFAULT_BRANCH_C2S,
        help="Branch used for client-to-server frames",
    )
    parser.add_argument(
        "--branch-s2c",
        default=DEFAULT_BRANCH_S2C,
        help="Branch used for server-to-client frames",
    )
    parser.add_argument(
        "--drive-client-secrets",
        default=None,
        help="Path to Google OAuth client-secrets JSON used by --transport google-drive",
    )
    parser.add_argument(
        "--drive-token-path",
        default="~/.config/clipssh/drive-token.json",
        help="Path to cached Google OAuth token JSON used by --transport google-drive",
    )
    parser.add_argument(
        "--drive-c2s-file-name",
        default=DEFAULT_DRIVE_LOG_C2S,
        help="Google Drive appData file name used for client-to-server frames",
    )
    parser.add_argument(
        "--drive-s2c-file-name",
        default=DEFAULT_DRIVE_LOG_S2C,
        help="Google Drive appData file name used for server-to-client frames",
    )
    parser.add_argument(
        "--drive-poll-page-size",
        type=int,
        default=200,
        help="Page size for Google Drive file lookup queries",
    )
    parser.add_argument(
        "--serial-port",
        default="/dev/ttyACM0",
        help="Serial device path used by --transport usb-serial",
    )
    parser.add_argument(
        "--serial-baud",
        type=int,
        default=3000000,
        help="Requested serial baud rate for --transport usb-serial",
    )
    parser.add_argument(
        "--serial-read-timeout-ms",
        type=int,
        default=5,
        help="Serial read timeout in milliseconds for --transport usb-serial",
    )
    parser.add_argument(
        "--serial-write-timeout-ms",
        type=int,
        default=20,
        help="Serial write timeout in milliseconds for --transport usb-serial",
    )
    parser.add_argument(
        "--serial-frame-max-bytes",
        type=int,
        default=65536,
        help="Maximum encoded message bytes per serial frame",
    )
    parser.add_argument(
        "--serial-ack-timeout-ms",
        type=int,
        default=150,
        help="Retransmission timeout in milliseconds for serial data frames",
    )
    parser.add_argument(
        "--serial-max-retries",
        type=int,
        default=20,
        help="Maximum serial retransmissions before failing the session",
    )
    parser.add_argument(
        "--serial-no-configure-tty",
        action="store_true",
        help="Do not apply raw termios settings to serial fd (debug/testing)",
    )
    parser.add_argument(
        "--audio-sample-rate",
        type=int,
        default=48000,
        help="PCM sample rate for --transport audio-modem",
    )
    parser.add_argument(
        "--audio-read-timeout-ms",
        type=int,
        default=10,
        help="Audio read timeout in milliseconds for --transport audio-modem",
    )
    parser.add_argument(
        "--audio-write-timeout-ms",
        type=int,
        default=50,
        help="Audio write timeout in milliseconds for --transport audio-modem",
    )
    parser.add_argument(
        "--audio-frame-max-bytes",
        type=int,
        default=65536,
        help="Maximum encoded message bytes per audio link frame",
    )
    parser.add_argument(
        "--audio-ack-timeout-ms",
        type=int,
        default=200,
        help="Retransmission timeout in milliseconds for audio data frames",
    )
    parser.add_argument(
        "--audio-max-retries",
        type=int,
        default=32,
        help="Maximum audio retransmissions before failing the session",
    )
    parser.add_argument(
        "--audio-byte-repeat",
        type=int,
        default=3,
        help="Simple forward-error-correction repeat factor for audio bytes",
    )
    parser.add_argument(
        "--audio-marker-run",
        type=int,
        default=16,
        help="Number of marker samples used to delimit audio frames",
    )
    parser.add_argument(
        "--audio-modulation",
        default="auto",
        choices=["auto", "legacy", "robust-v1"],
        help="Audio modulation profile for --transport audio-modem",
    )
    parser.add_argument(
        "--audio-backend",
        default="pulse-cli",
        help="Audio backend for --transport audio-modem (must be pulse-cli)",
    )
    parser.add_argument(
        "--shell",
        default="tcsh",
        help="Preferred shell executable name or path (default: tcsh)",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=100,
        help="Polling interval in milliseconds",
    )
    parser.add_argument(
        "--max-output-chunk",
        type=int,
        default=4096,
        help="Maximum size of each pty_output payload in bytes before base64 encoding",
    )
    parser.add_argument(
        "--io-flush-interval",
        type=float,
        default=0.02,
        help="Maximum seconds to hold buffered PTY output before emitting a frame",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=120.0,
        help="Deprecated compatibility option (unused in PTY mode)",
    )
    parser.add_argument(
        "--fetch-interval",
        type=float,
        default=0.1,
        help="Seconds between background fetch operations",
    )
    parser.add_argument(
        "--push-interval",
        type=float,
        default=0.1,
        help="Seconds between background push operations",
    )
    return parser


def _build_backend(args: argparse.Namespace) -> TransportBackend:
    if args.transport == "usb-serial":
        return USBSerialTransportBackend(
            USBSerialTransportConfig(
                serial_port=args.serial_port,
                baud_rate=max(args.serial_baud, 1),
                read_timeout=max(args.serial_read_timeout_ms / 1000.0, 0.0),
                write_timeout=max(args.serial_write_timeout_ms / 1000.0, 0.001),
                frame_max_bytes=max(args.serial_frame_max_bytes, 1024),
                ack_timeout=max(args.serial_ack_timeout_ms / 1000.0, 0.01),
                max_retries=max(args.serial_max_retries, 1),
                configure_tty=not args.serial_no_configure_tty,
            )
        )

    if args.transport == "audio-modem":
        requested_backend = (args.audio_backend or "").strip().lower()
        if requested_backend != "pulse-cli":
            raise TransportError(
                f"--transport audio-modem supports only --audio-backend pulse-cli in this build "
                f"(received {args.audio_backend!r})."
            )

        try:
            input_device, output_device = resolve_server_default_paths()
        except PulseRuntimeError as exc:
            raise TransportError(f"Failed to resolve server default microphone/speakers: {exc}") from exc

        if args.verbose:
            print(
                f"[sshgd] using default Pulse devices input={input_device} output={output_device}",
                file=sys.stderr,
            )

        return AudioModemTransportBackend(
            AudioModemTransportConfig(
                input_device=input_device,
                output_device=output_device,
                sample_rate=max(args.audio_sample_rate, 8000),
                read_timeout=max(args.audio_read_timeout_ms / 1000.0, 0.0),
                write_timeout=max(args.audio_write_timeout_ms / 1000.0, 0.001),
                frame_max_bytes=max(args.audio_frame_max_bytes, 1024),
                ack_timeout=max(args.audio_ack_timeout_ms / 1000.0, 0.01),
                max_retries=max(args.audio_max_retries, 1),
                byte_repeat=max(args.audio_byte_repeat, 1),
                marker_run=max(args.audio_marker_run, 4),
                audio_modulation=args.audio_modulation,
                audio_backend="pulse-cli",
                verbose=args.verbose,
                io_factory=lambda config: PulseCliAudioDuplexIO(
                    input_device=input_device,
                    output_device=output_device,
                    sample_rate=max(config.sample_rate, 8000),
                    read_timeout=max(config.read_timeout, 0.0),
                    write_timeout=max(config.write_timeout, 0.001),
                ),
            )
        )

    if args.transport == "google-drive":
        client_secrets = (args.drive_client_secrets or "").strip()
        if not client_secrets:
            raise TransportError(
                "For --transport google-drive, pass --drive-client-secrets /path/to/client_secrets.json"
            )

        return GoogleDriveTransportBackend(
            GoogleDriveTransportConfig(
                client_secrets_path=client_secrets,
                token_path=args.drive_token_path,
                inbound_file_name=args.drive_c2s_file_name,
                outbound_file_name=args.drive_s2c_file_name,
                poll_page_size=max(args.drive_poll_page_size, 1),
            )
        )

    return GitTransportBackend(
        local_repo_path=args.local_repo,
        upstream_url=args.upstream_url,
        inbound_branch=args.branch_c2s,
        outbound_branch=args.branch_s2c,
        auto_init_local=True,
    )
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = _build_backend(args)
    except TransportError as exc:
        print(f"sshgd: {exc}", file=sys.stderr)
        return 2

    config = ServerConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        max_output_chunk=max(args.max_output_chunk, 1),
        preferred_shell=args.shell,
        command_timeout=max(args.command_timeout, 1.0),
        io_flush_interval=max(args.io_flush_interval, 0.0),
        fetch_interval=max(args.fetch_interval, 0.02),
        push_interval=max(args.push_interval, 0.02),
        verbose=args.verbose,
    )

    server = GitSSHServer(backend=backend, config=config)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
