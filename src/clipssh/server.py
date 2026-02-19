"""Clipboard SSH server daemon."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import socket
import sys
import threading
import time
from typing import Any

from .clipboard import ClipboardBackend, ClipboardError, detect_backend
from .protocol import Message, build_message, decode_message, encode_message
from .session import EndpointState
from .shell import ShellExecutionError, ShellSession, resolve_shell


@dataclass
class ServerConfig:
    poll_interval: float = 0.1
    max_output_chunk: int = 32768
    preferred_shell: str = "tcsh"
    command_timeout: float = 120.0
    verbose: bool = False


@dataclass
class ActiveSession:
    state: EndpointState
    shell: ShellSession
    command_cache: dict[str, list[Message]] = field(default_factory=dict)


class ClipboardSSHServer:
    def __init__(self, backend: ClipboardBackend, config: ServerConfig) -> None:
        self.backend = backend
        self.config = config
        self._active: ActiveSession | None = None
        self._server_seq = 0

    def _log(self, text: str) -> None:
        if self.config.verbose:
            print(f"[sshcd] {text}", file=sys.stderr)

    def _next_seq(self) -> int:
        self._server_seq += 1
        return self._server_seq

    def _read_message(self) -> Message | None:
        try:
            text = self.backend.read_text()
        except ClipboardError as exc:
            self._log(f"clipboard read failed: {exc}")
            return None
        return decode_message(text)

    def _write_message(self, message: Message) -> None:
        payload = encode_message(message)
        try:
            self.backend.write_text(payload)
        except ClipboardError as exc:
            self._log(f"clipboard write failed: {exc}")
            return

        # Give the peer a chance to observe this frame before writing the next one.
        time.sleep(max(self.config.poll_interval * 2.0, 0.02))

    def _make_message(self, *, kind: str, session_id: str, body: Any = None) -> Message:
        return build_message(
            kind=kind,
            session_id=session_id,
            source="server",
            target="client",
            seq=self._next_seq(),
            body=body,
        )

    def _chunk_text(self, text: str) -> list[str]:
        if not text:
            return []
        size = max(self.config.max_output_chunk, 1)
        return [text[i : i + size] for i in range(0, len(text), size)]

    def _server_hostname(self) -> str | None:
        try:
            hostname = socket.gethostname()
            return hostname or None
        except OSError:
            return None

    def _collect_prompt_context(self) -> dict[str, str | None]:
        host = self._server_hostname()
        if self._active is None:
            return {"user": None, "cwd": None, "host": host}

        try:
            user, cwd = self._active.shell.read_prompt_context(
                timeout=min(self.config.command_timeout, 10.0),
            )
            return {"user": user, "cwd": cwd, "host": host}
        except ShellExecutionError as exc:
            self._log(f"failed to read prompt context: {exc}")
            return {"user": None, "cwd": None, "host": host}

    def _close_active_session(self) -> None:
        if self._active is None:
            return

        self._log(f"closing session {self._active.state.session_id}")
        self._active.shell.close()
        self._active = None

    def _handle_connect(self, message: Message) -> None:
        if self._active is not None:
            if message.session_id == self._active.state.session_id:
                self._log(f"re-acknowledging session {message.session_id}")
                prompt_context = self._collect_prompt_context()
                self._write_message(
                    self._make_message(
                        kind="connect_ack",
                        session_id=message.session_id,
                        body={"backend": self.backend.name(), "prompt": prompt_context},
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

        try:
            shell_path, shell_flavor = resolve_shell(self.config.preferred_shell)
            shell = ShellSession(shell_path=shell_path, shell_flavor=shell_flavor)
        except Exception as exc:
            self._write_message(
                self._make_message(
                    kind="error",
                    session_id=message.session_id,
                    body={"error": f"failed to start shell: {exc}"},
                )
            )
            return

        self._active = ActiveSession(state=EndpointState(session_id=message.session_id), shell=shell)
        self._log(
            f"accepted session {message.session_id} using {shell_path} ({shell_flavor})"
        )
        prompt_context = self._collect_prompt_context()
        self._write_message(
            self._make_message(
                kind="connect_ack",
                session_id=message.session_id,
                body={"shell": shell_path, "backend": self.backend.name(), "prompt": prompt_context},
            )
        )

    def _emit_command_messages(self, outgoing: list[Message]) -> None:
        for frame in outgoing:
            self._write_message(frame)

    def _handle_command(self, message: Message, is_new: bool) -> None:
        if self._active is None:
            return

        cached = self._active.command_cache.get(message.msg_id)
        if not is_new:
            if cached:
                self._log(f"replaying cached response for command msg {message.msg_id}")
                self._emit_command_messages(cached)
            return

        body = message.body if isinstance(message.body, dict) else {}
        command = body.get("command") if isinstance(body, dict) else None
        cmd_id = body.get("cmd_id") if isinstance(body, dict) else None
        if not isinstance(command, str) or not isinstance(cmd_id, str):
            error_frame = self._make_message(
                kind="error",
                session_id=message.session_id,
                body={"error": "cmd payload must contain string fields 'command' and 'cmd_id'"},
            )
            self._active.command_cache[message.msg_id] = [error_frame]
            self._write_message(error_frame)
            return

        self._log(f"executing command for session {message.session_id}: {command!r}")
        try:
            stdout, stderr, code = self._active.shell.execute(
                command,
                timeout=self.config.command_timeout,
            )
        except ShellExecutionError as exc:
            stdout = ""
            stderr = f"{exc}\n"
            code = 1

        outgoing: list[Message] = []

        for chunk in self._chunk_text(stdout):
            outgoing.append(
                self._make_message(
                    kind="stdout",
                    session_id=message.session_id,
                    body={"cmd_id": cmd_id, "data": chunk},
                )
            )

        for chunk in self._chunk_text(stderr):
            outgoing.append(
                self._make_message(
                    kind="stderr",
                    session_id=message.session_id,
                    body={"cmd_id": cmd_id, "data": chunk},
                )
            )

        prompt_context = self._collect_prompt_context()
        outgoing.append(
            self._make_message(
                kind="exit",
                session_id=message.session_id,
                body={"cmd_id": cmd_id, "exit_code": code, "prompt": prompt_context},
            )
        )

        self._active.command_cache[message.msg_id] = outgoing
        self._emit_command_messages(outgoing)

    def _handle_disconnect(self, message: Message) -> None:
        self._log(f"disconnect requested for session {message.session_id}")
        self._close_active_session()

    def _handle_session_message(self, message: Message) -> None:
        if self._active is None:
            return
        if message.session_id != self._active.state.session_id:
            return

        is_new = self._active.state.incoming_seen.mark(message.msg_id)

        if message.kind == "cmd":
            self._handle_command(message, is_new=is_new)
            return

        if not is_new:
            return

        if message.kind == "disconnect":
            self._handle_disconnect(message)

    def _handle_message(self, message: Message) -> None:
        if message.target != "server":
            return

        if message.kind == "connect_req":
            self._handle_connect(message)
            return

        if self._active is None:
            return

        self._handle_session_message(message)

    def serve_forever(self, stop_event: threading.Event | None = None) -> None:
        self._log(f"server started with backend={self.backend.name()}")

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return

                message = self._read_message()
                if message is not None:
                    self._handle_message(message)

                time.sleep(self.config.poll_interval)
        finally:
            self._close_active_session()



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshcd", description="Clipboard SSH server daemon")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    parser.add_argument(
        "--shell",
        default="tcsh",
        help="Preferred shell executable name or path (default: tcsh)",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=100,
        help="Clipboard polling interval in milliseconds",
    )
    parser.add_argument(
        "--max-output-chunk",
        type=int,
        default=32768,
        help="Maximum size of each stdout/stderr message chunk",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for a command to finish",
    )
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = detect_backend()
    except ClipboardError as exc:
        print(f"sshcd: {exc}", file=sys.stderr)
        return 2

    config = ServerConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        max_output_chunk=max(args.max_output_chunk, 1),
        preferred_shell=args.shell,
        command_timeout=max(args.command_timeout, 1.0),
        verbose=args.verbose,
    )

    server = ClipboardSSHServer(backend=backend, config=config)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
