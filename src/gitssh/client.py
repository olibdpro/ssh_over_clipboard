"""Git transport SSH client."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import os
import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty
from typing import Any
import uuid

from sshcore.session import EndpointState

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
from .audio_modem_transport import AudioModemTransportBackend, AudioModemTransportConfig
from .audio_pipewire_runtime import (
    PipeWireLinkAudioDuplexIO,
    PipeWireRuntimeError,
    ensure_client_pipewire_preflight,
    resolve_client_capture_node_id,
    resolve_client_write_node_id,
)
from .protocol import Message, build_message
from .transport import TransportBackend, TransportError
from .usb_serial_transport import USBSerialTransportBackend, USBSerialTransportConfig


@dataclass
class ClientConfig:
    poll_interval: float = 0.1
    connect_timeout: float = 10.0
    session_timeout: float = 300.0
    retry_interval: float = 0.5
    fetch_interval: float = 0.1
    push_interval: float = 0.1
    stdin_batch_interval: float = 0.02
    input_chunk_bytes: int = 4096
    resize_debounce: float = 0.1
    no_raw: bool = False
    verbose: bool = False


class GitSSHClient:
    def __init__(self, backend: TransportBackend, config: ClientConfig) -> None:
        self.backend = backend
        self.config = config
        self._state: EndpointState | None = None
        self._cursor: str | None = None
        self._stream_id: str | None = None
        self._prompt_user: str | None = None
        self._prompt_cwd: str | None = None
        self._prompt_host: str | None = None
        self._sync_stop: threading.Event | None = None
        self._sync_thread: threading.Thread | None = None

    def _log(self, text: str) -> None:
        if self.config.verbose:
            print(f"[sshg] {text}", file=sys.stderr)

    @property
    def is_connected(self) -> bool:
        return self._state is not None and self._stream_id is not None

    def _ensure_state(self) -> EndpointState:
        if self._state is None:
            raise RuntimeError("Not connected")
        return self._state

    def _ensure_stream_id(self) -> str:
        if self._stream_id is None:
            raise RuntimeError("PTY stream is not established")
        return self._stream_id

    def _start_sync_worker(self) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return

        self._sync_stop = threading.Event()
        self._sync_thread = threading.Thread(
            target=self._sync_loop,
            name="gitssh-client-sync",
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
            thread.join()

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
            raise RuntimeError(f"Failed to write transport message: {exc}") from exc

    def _update_prompt_context(self, body: dict[str, Any]) -> None:
        prompt = body.get("prompt")
        if not isinstance(prompt, dict):
            return

        user = prompt.get("user")
        cwd = prompt.get("cwd")
        host = prompt.get("host")
        if isinstance(user, str) and user:
            self._prompt_user = user
        if isinstance(cwd, str) and cwd:
            self._prompt_cwd = cwd
        if isinstance(host, str) and host:
            self._prompt_host = host

    def _terminal_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size(fallback=(80, 24))
        return max(size.columns, 1), max(size.lines, 1)

    def connect(self, host: str) -> None:
        if self._state is not None:
            raise RuntimeError("Already connected")

        self._prompt_user = None
        self._prompt_cwd = None
        self._prompt_host = None
        self._stream_id = None

        self._start_sync_worker()

        try:
            self.backend.fetch_inbound()
            self._cursor = self.backend.snapshot_inbound_cursor()

            session_id = str(uuid.uuid4())
            state = EndpointState(session_id=session_id)
            cols, rows = self._terminal_size()
            connect_message = build_message(
                kind="connect_req",
                session_id=session_id,
                source="client",
                target="server",
                seq=state.outgoing_seq.next(),
                body={"host": host, "pty": {"cols": cols, "rows": rows}},
            )

            deadline = time.monotonic() + self.config.connect_timeout
            next_send = 0.0
            diag_pings_received = 0
            last_diag_phase: str | None = None

            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send:
                    self._write_message(connect_message)
                    next_send = now + self.config.retry_interval
                    self._log(f"sent connect_req for session {session_id}")

                for incoming in self._read_messages():
                    if incoming.target != "client" or incoming.source != "server":
                        continue

                    if incoming.session_id != session_id:
                        continue

                    if not state.incoming_seen.mark(incoming.msg_id):
                        continue

                    if incoming.kind == "diag_ping":
                        body = incoming.body if isinstance(incoming.body, dict) else {}
                        phase = body.get("phase")
                        if isinstance(phase, str) and phase:
                            last_diag_phase = phase
                        diag_pings_received += 1
                        self._log(
                            "received diag_ping during connect "
                            f"(count={diag_pings_received}, phase={last_diag_phase or 'unknown'}, seq={incoming.seq})"
                        )
                        continue

                    if incoming.kind == "connect_ack":
                        body = incoming.body if isinstance(incoming.body, dict) else {}
                        self._update_prompt_context(body)
                        stream_id = body.get("stream_id")
                        if not isinstance(stream_id, str) or not stream_id:
                            raise RuntimeError("Server connect_ack did not include stream_id")
                        self._stream_id = stream_id
                        self._state = state
                        self._log(f"connected with session {session_id}, stream_id={stream_id}")
                        return

                    if incoming.kind == "busy":
                        raise RuntimeError("Server is busy with another active session")

                    if incoming.kind == "error":
                        message = (
                            incoming.body.get("error")
                            if isinstance(incoming.body, dict)
                            else "unknown error"
                        )
                        raise RuntimeError(f"Server rejected connection: {message}")

                time.sleep(self.config.poll_interval)

            if diag_pings_received > 0:
                suffix = (
                    f" (diag_pings_received={diag_pings_received}, "
                    f"last_diag_phase={last_diag_phase or 'unknown'})"
                )
            else:
                suffix = ""
            raise TimeoutError(f"Timed out waiting for server connect_ack{suffix}")
        except Exception:
            self._state = None
            self._stream_id = None
            self._stop_sync_worker()
            raise

    def _write_pty_message(self, *, kind: str, body: dict[str, Any]) -> None:
        state = self._ensure_state()
        message = build_message(
            kind=kind,
            session_id=state.session_id,
            source="client",
            target="server",
            seq=state.outgoing_seq.next(),
            body=body,
        )
        self._write_message(message)

    def _send_pty_input(self, data: bytes) -> None:
        if not data:
            return
        stream_id = self._ensure_stream_id()
        encoded = base64.b64encode(data).decode("ascii")
        self._write_pty_message(
            kind="pty_input",
            body={"stream_id": stream_id, "data_b64": encoded},
        )

    def _send_pty_resize(self, cols: int, rows: int) -> None:
        stream_id = self._ensure_stream_id()
        self._write_pty_message(
            kind="pty_resize",
            body={"stream_id": stream_id, "cols": max(cols, 1), "rows": max(rows, 1)},
        )

    def _send_pty_signal(self, signal_name: str) -> None:
        stream_id = self._ensure_stream_id()
        self._write_pty_message(
            kind="pty_signal",
            body={"stream_id": stream_id, "signal": signal_name},
        )

    def disconnect(self) -> None:
        try:
            if self._state is None:
                return

            message = build_message(
                kind="disconnect",
                session_id=self._state.session_id,
                source="client",
                target="server",
                seq=self._state.outgoing_seq.next(),
                body={},
            )

            self._write_message(message)
        finally:
            self._state = None
            self._stream_id = None
            self._prompt_user = None
            self._prompt_cwd = None
            self._prompt_host = None
            self._stop_sync_worker()

    def _handle_incoming_message(
        self,
        incoming: Message,
        *,
        on_output: Callable[[bytes], None],
    ) -> int | None:
        state = self._ensure_state()
        stream_id = self._ensure_stream_id()

        if incoming.target != "client" or incoming.source != "server":
            return None
        if incoming.session_id != state.session_id:
            return None
        if not state.incoming_seen.mark(incoming.msg_id):
            return None

        body = incoming.body if isinstance(incoming.body, dict) else {}

        if incoming.kind == "pty_output":
            if body.get("stream_id") != stream_id:
                return None
            data_b64 = body.get("data_b64")
            if not isinstance(data_b64, str):
                return None
            try:
                payload = base64.b64decode(data_b64, validate=True)
            except (ValueError, binascii.Error):
                return None
            if payload:
                on_output(payload)
            return None

        if incoming.kind == "pty_closed":
            if body.get("stream_id") != stream_id:
                return None
            raw_code = body.get("exit_code", 1)
            try:
                return int(raw_code)
            except (TypeError, ValueError):
                return 1

        if incoming.kind == "error":
            message = body.get("error", "unknown server error")
            raise RuntimeError(str(message))

        if incoming.kind == "diag_ping":
            phase = body.get("phase")
            counter = body.get("diag_counter")
            if isinstance(phase, str):
                if isinstance(counter, int):
                    self._log(f"diag_ping phase={phase} counter={counter}")
                else:
                    self._log(f"diag_ping phase={phase}")
            else:
                self._log("diag_ping received")
            return None

        return None

    def run_interactive(self, host: str) -> int:
        self.connect(host)

        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()

        raw_enabled = os.isatty(stdin_fd) and not self.config.no_raw
        original_tty: list[Any] | None = None
        previous_winch_handler = signal.getsignal(signal.SIGWINCH)

        resize_pending = False

        def on_sigwinch(_signum: int, _frame: Any) -> None:
            nonlocal resize_pending
            resize_pending = True

        def emit_output(data: bytes) -> None:
            if not data:
                return
            try:
                os.write(stdout_fd, data)
            except OSError:
                pass

        exit_code = 0

        try:
            if raw_enabled:
                original_tty = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)

            signal.signal(signal.SIGWINCH, on_sigwinch)
            resize_pending = True

            input_buffer = bytearray()
            last_input_flush = time.monotonic()
            next_resize_send = 0.0
            last_activity = time.monotonic()

            while True:
                now = time.monotonic()

                if resize_pending and now >= next_resize_send:
                    cols, rows = self._terminal_size()
                    self._send_pty_resize(cols, rows)
                    resize_pending = False
                    next_resize_send = now + self.config.resize_debounce

                for incoming in self._read_messages():
                    maybe_exit = self._handle_incoming_message(incoming, on_output=emit_output)
                    if maybe_exit is not None:
                        exit_code = maybe_exit
                        return exit_code
                    last_activity = now

                if input_buffer and (
                    len(input_buffer) >= self.config.input_chunk_bytes
                    or (now - last_input_flush) >= self.config.stdin_batch_interval
                ):
                    self._send_pty_input(bytes(input_buffer))
                    input_buffer.clear()
                    last_input_flush = now
                    last_activity = now

                ready, _, _ = select.select([stdin_fd], [], [], self.config.poll_interval)
                if ready:
                    try:
                        data = os.read(stdin_fd, self.config.input_chunk_bytes)
                    except KeyboardInterrupt:
                        self._send_pty_signal("INT")
                        continue

                    if not data:
                        return exit_code

                    input_buffer.extend(data)
                    if len(input_buffer) >= self.config.input_chunk_bytes:
                        self._send_pty_input(bytes(input_buffer))
                        input_buffer.clear()
                        last_input_flush = now
                        last_activity = now

                if (time.monotonic() - last_activity) > self.config.session_timeout:
                    raise TimeoutError("Timed out waiting for PTY stream activity")

        finally:
            try:
                signal.signal(signal.SIGWINCH, previous_winch_handler)
            except Exception:
                pass

            if original_tty is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_tty)
                except termios.error:
                    pass

            self.disconnect()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg", description="Git transport SSH client")
    parser.add_argument("host", help="ssh-style target host (informational in this local emulator)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    parser.add_argument(
        "--transport",
        choices=["git", "usb-serial", "audio-modem", "google-drive"],
        default="git",
        help="Transport backend",
    )
    parser.add_argument(
        "--local-repo",
        default="/tmp/gitssh-client.git",
        help="Path to this client's local bare mirror repository",
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
        "--pw-capture-node-id",
        type=int,
        default=None,
        help=(
            "PipeWire node id to capture on the client. "
            "If omitted, sshg prompts to choose from active capture candidates."
        ),
    )
    parser.add_argument(
        "--pw-capture-match",
        default=None,
        help=(
            "Regex used to auto-select one active client capture node "
            "(matched against id/name/description/app/class fields)."
        ),
    )
    parser.add_argument(
        "--pw-write-node-id",
        type=int,
        default=None,
        help=(
            "PipeWire node id to route server audio into on the client. "
            "If omitted, sshg prompts to choose from active write candidates."
        ),
    )
    parser.add_argument(
        "--pw-write-match",
        default=None,
        help=(
            "Regex used to auto-select one active client write node "
            "(matched against id/name/description/app/class fields)."
        ),
    )
    parser.add_argument(
        "--skip-pw-preflight",
        action="store_true",
        help="Skip PipeWire client preflight checks before audio-modem startup (debug only).",
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
        default=500,
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
        "--poll-interval-ms",
        type=int,
        default=100,
        help="Polling interval in milliseconds",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=10.0,
        help="Maximum seconds to wait for server handshake",
    )
    parser.add_argument(
        "--session-timeout",
        type=float,
        default=300.0,
        help="Maximum idle seconds waiting for stream activity",
    )
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=0.5,
        help="Seconds between retransmitting connect requests",
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
    parser.add_argument(
        "--stdin-batch-ms",
        type=int,
        default=20,
        help="Milliseconds to batch stdin bytes before sending a pty_input frame",
    )
    parser.add_argument(
        "--input-chunk-bytes",
        type=int,
        default=4096,
        help="Maximum bytes in each pty_input payload before base64 encoding",
    )
    parser.add_argument(
        "--resize-debounce-ms",
        type=int,
        default=100,
        help="Minimum milliseconds between emitted pty_resize messages",
    )
    parser.add_argument(
        "--no-raw",
        action="store_true",
        help="Do not enable local raw terminal mode (debug/fallback)",
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
        if not args.skip_pw_preflight:
            try:
                ensure_client_pipewire_preflight(
                    capture_node_id=args.pw_capture_node_id,
                    write_node_id=args.pw_write_node_id,
                )
            except PipeWireRuntimeError as exc:
                raise TransportError(str(exc)) from exc
            if args.verbose:
                print("[sshg] PipeWire preflight passed", file=sys.stderr)

        try:
            capture_node_id = resolve_client_capture_node_id(
                node_id=args.pw_capture_node_id,
                node_match=args.pw_capture_match,
                interactive=sys.stdin.isatty(),
                input_stream=sys.stdin,
                output_stream=sys.stderr,
            )
            write_node_id = resolve_client_write_node_id(
                node_id=args.pw_write_node_id,
                node_match=args.pw_write_match,
                interactive=sys.stdin.isatty(),
                input_stream=sys.stdin,
                output_stream=sys.stderr,
            )
        except PipeWireRuntimeError as exc:
            raise TransportError(str(exc)) from exc

        if args.verbose:
            print(f"[sshg] selected PipeWire capture node id={capture_node_id}", file=sys.stderr)
            print(f"[sshg] selected PipeWire write node id={write_node_id}", file=sys.stderr)
            print(
                "[sshg] using PipeWire link routing for client audio-modem I/O "
                f"(capture-node={capture_node_id}, write-node={write_node_id})",
                file=sys.stderr,
            )

        return AudioModemTransportBackend(
            AudioModemTransportConfig(
                input_device=f"pw-node:{capture_node_id}",
                output_device=f"pw-node:{write_node_id}",
                sample_rate=max(args.audio_sample_rate, 8000),
                read_timeout=max(args.audio_read_timeout_ms / 1000.0, 0.0),
                write_timeout=max(args.audio_write_timeout_ms / 1000.0, 0.001),
                frame_max_bytes=max(args.audio_frame_max_bytes, 1024),
                ack_timeout=max(args.audio_ack_timeout_ms / 1000.0, 0.01),
                max_retries=max(args.audio_max_retries, 1),
                byte_repeat=max(args.audio_byte_repeat, 1),
                marker_run=max(args.audio_marker_run, 4),
                audio_modulation=args.audio_modulation,
                audio_backend="pipewire-link",
                verbose=args.verbose,
                io_factory=lambda config: PipeWireLinkAudioDuplexIO(
                    capture_node_id=capture_node_id,
                    write_node_id=write_node_id,
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
                inbound_file_name=args.drive_s2c_file_name,
                outbound_file_name=args.drive_c2s_file_name,
                poll_page_size=max(args.drive_poll_page_size, 1),
            )
        )

    return GitTransportBackend(
        local_repo_path=args.local_repo,
        upstream_url=args.upstream_url,
        inbound_branch=args.branch_s2c,
        outbound_branch=args.branch_c2s,
        auto_init_local=True,
    )
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = _build_backend(args)
    except TransportError as exc:
        print(f"sshg: {exc}", file=sys.stderr)
        return 2

    config = ClientConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        connect_timeout=max(args.connect_timeout, 1.0),
        session_timeout=max(args.session_timeout, 1.0),
        retry_interval=max(args.retry_interval, 0.05),
        fetch_interval=max(args.fetch_interval, 0.02),
        push_interval=max(args.push_interval, 0.02),
        stdin_batch_interval=max(args.stdin_batch_ms / 1000.0, 0.001),
        input_chunk_bytes=max(args.input_chunk_bytes, 1),
        resize_debounce=max(args.resize_debounce_ms / 1000.0, 0.0),
        no_raw=args.no_raw,
        verbose=args.verbose,
    )

    client = GitSSHClient(backend=backend, config=config)

    try:
        return client.run_interactive(args.host)
    except KeyboardInterrupt:
        client.disconnect()
        return 130
    except (RuntimeError, TimeoutError) as exc:
        print(f"sshg: {exc}", file=sys.stderr)
        client.disconnect()
        return 1
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
