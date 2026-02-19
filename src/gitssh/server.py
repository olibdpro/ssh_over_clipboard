"""Git transport SSH server daemon."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import sys
import threading
import time
from typing import Any

from sshcore.session import EndpointState
from sshcore.shell import ShellExecutionError, ShellSession, resolve_shell

from .git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
    GitTransportError,
)
from .protocol import Message, build_message


@dataclass
class ServerConfig:
    poll_interval: float = 0.1
    max_output_chunk: int = 32768
    preferred_shell: str = "tcsh"
    command_timeout: float = 120.0
    fetch_interval: float = 0.1
    push_interval: float = 0.1
    verbose: bool = False


@dataclass
class ActiveSession:
    state: EndpointState
    shell: ShellSession
    command_cache: dict[str, list[Message]] = field(default_factory=dict)


class GitSSHServer:
    def __init__(self, backend: GitTransportBackend, config: ServerConfig) -> None:
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
                except GitTransportError as exc:
                    self._log(f"fetch failed: {exc}")
                next_fetch = now + self.config.fetch_interval
                did_work = True

            if now >= next_push:
                try:
                    self.backend.push_outbound()
                except GitTransportError as exc:
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
        except GitTransportError as exc:
            self._log(f"git read failed: {exc}")
            return []
        return messages

    def _write_message(self, message: Message) -> None:
        try:
            self.backend.write_outbound_message(message)
        except GitTransportError as exc:
            self._log(f"git write failed: {exc}")
            return

        # Give the peer a chance to observe this commit before writing the next one.
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
                self._write_message(
                    self._make_message(
                        kind="connect_ack",
                        session_id=message.session_id,
                        body={"backend": self.backend.name()},
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
        self._write_message(
            self._make_message(
                kind="connect_ack",
                session_id=message.session_id,
                body={"shell": shell_path, "backend": self.backend.name()},
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

        outgoing.append(
            self._make_message(
                kind="exit",
                session_id=message.session_id,
                body={"cmd_id": cmd_id, "exit_code": code},
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
        self._start_sync_worker()

        try:
            self.backend.fetch_inbound()
            self._cursor = self.backend.snapshot_inbound_cursor()

            while True:
                if stop_event is not None and stop_event.is_set():
                    return

                for message in self._read_messages():
                    self._handle_message(message)

                time.sleep(self.config.poll_interval)
        finally:
            self._stop_sync_worker()
            self._close_active_session()



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshgd", description="Git transport SSH server daemon")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
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
        default=32768,
        help="Maximum size of each stdout/stderr message chunk",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for a command to finish",
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



def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = GitTransportBackend(
            local_repo_path=args.local_repo,
            upstream_url=args.upstream_url,
            inbound_branch=args.branch_c2s,
            outbound_branch=args.branch_s2c,
            auto_init_local=True,
        )
    except GitTransportError as exc:
        print(f"sshgd: {exc}", file=sys.stderr)
        return 2

    config = ServerConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        max_output_chunk=max(args.max_output_chunk, 1),
        preferred_shell=args.shell,
        command_timeout=max(args.command_timeout, 1.0),
        fetch_interval=max(args.fetch_interval, 0.02),
        push_interval=max(args.push_interval, 0.02),
        verbose=args.verbose,
    )

    server = GitSSHServer(backend=backend, config=config)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
