"""Git transport SSH client."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
import threading
import time
import uuid

from sshcore.session import EndpointState

from .git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
    GitTransportError,
)
from .protocol import Message, build_message


@dataclass
class ClientConfig:
    poll_interval: float = 0.1
    connect_timeout: float = 10.0
    session_timeout: float = 300.0
    retry_interval: float = 0.5
    fetch_interval: float = 0.1
    push_interval: float = 0.1
    verbose: bool = False


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


class GitSSHClient:
    def __init__(self, backend: GitTransportBackend, config: ClientConfig) -> None:
        self.backend = backend
        self.config = config
        self._state: EndpointState | None = None
        self._cursor: str | None = None
        self._sync_stop: threading.Event | None = None
        self._sync_thread: threading.Thread | None = None

    def _log(self, text: str) -> None:
        if self.config.verbose:
            print(f"[sshg] {text}", file=sys.stderr)

    @property
    def is_connected(self) -> bool:
        return self._state is not None

    def _ensure_state(self) -> EndpointState:
        if self._state is None:
            raise RuntimeError("Not connected")
        return self._state

    def _start_sync_worker(self) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return

        self._sync_stop = threading.Event()
        self._sync_thread = threading.Thread(
            target=self._sync_loop,
            daemon=True,
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
            raise RuntimeError(f"Failed to write git transport message: {exc}") from exc

    def connect(self, host: str) -> None:
        if self._state is not None:
            raise RuntimeError("Already connected")

        self._start_sync_worker()

        try:
            self.backend.fetch_inbound()
            self._cursor = self.backend.snapshot_inbound_cursor()

            session_id = str(uuid.uuid4())
            state = EndpointState(session_id=session_id)
            connect_message = build_message(
                kind="connect_req",
                session_id=session_id,
                source="client",
                target="server",
                seq=state.outgoing_seq.next(),
                body={"host": host},
            )

            deadline = time.monotonic() + self.config.connect_timeout
            next_send = 0.0

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

                    if incoming.kind == "connect_ack":
                        self._state = state
                        self._log(f"connected with session {session_id}")
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

            raise TimeoutError("Timed out waiting for server connect_ack")
        except Exception:
            self._state = None
            self._stop_sync_worker()
            raise

    def execute(
        self,
        command: str,
        *,
        on_stdout=None,
        on_stderr=None,
    ) -> CommandResult:
        state = self._ensure_state()

        cmd_id = str(uuid.uuid4())
        cmd_message = build_message(
            kind="cmd",
            session_id=state.session_id,
            source="client",
            target="server",
            seq=state.outgoing_seq.next(),
            body={"command": command, "cmd_id": cmd_id},
        )

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        next_send = 0.0
        last_activity = time.monotonic()

        while True:
            now = time.monotonic()
            if now >= next_send:
                self._write_message(cmd_message)
                next_send = now + self.config.retry_interval

            for incoming in self._read_messages():
                if incoming.target != "client" or incoming.source != "server":
                    continue
                if incoming.session_id != state.session_id:
                    continue
                if not state.incoming_seen.mark(incoming.msg_id):
                    continue

                body = incoming.body if isinstance(incoming.body, dict) else {}

                if incoming.kind in {"stdout", "stderr", "exit"} and body.get("cmd_id") != cmd_id:
                    continue

                if incoming.kind == "stdout":
                    chunk = body.get("data", "")
                    if isinstance(chunk, str):
                        stdout_parts.append(chunk)
                        if on_stdout is not None:
                            on_stdout(chunk)
                    last_activity = now

                elif incoming.kind == "stderr":
                    chunk = body.get("data", "")
                    if isinstance(chunk, str):
                        stderr_parts.append(chunk)
                        if on_stderr is not None:
                            on_stderr(chunk)
                    last_activity = now

                elif incoming.kind == "exit":
                    code_raw = body.get("exit_code", 1)
                    try:
                        exit_code = int(code_raw)
                    except (TypeError, ValueError):
                        exit_code = 1
                    return CommandResult(
                        stdout="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                        exit_code=exit_code,
                    )

                elif incoming.kind == "error":
                    message = body.get("error", "unknown server error")
                    raise RuntimeError(str(message))

            if now - last_activity > self.config.session_timeout:
                raise TimeoutError("Timed out waiting for command response")

            time.sleep(self.config.poll_interval)

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
            self._stop_sync_worker()

    def run_commands(self, host: str, commands: list[str]) -> list[CommandResult]:
        self.connect(host)
        results: list[CommandResult] = []
        try:
            for command in commands:
                results.append(self.execute(command))
        finally:
            self.disconnect()
        return results

    def run_interactive(self, host: str) -> int:
        self.connect(host)

        def emit_stdout(chunk: str) -> None:
            sys.stdout.write(chunk)
            sys.stdout.flush()

        def emit_stderr(chunk: str) -> None:
            sys.stderr.write(chunk)
            sys.stderr.flush()

        try:
            while True:
                try:
                    line = input("sshg> ")
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue

                if not line.strip():
                    continue

                result = self.execute(line, on_stdout=emit_stdout, on_stderr=emit_stderr)
                if self.config.verbose:
                    print(f"[sshg] exit_code={result.exit_code}", file=sys.stderr)
        finally:
            self.disconnect()

        return 0



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg", description="Git transport SSH client")
    parser.add_argument("host", help="ssh-style target host (informational in this local emulator)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
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
        help="Maximum idle seconds waiting for command output",
    )
    parser.add_argument(
        "--retry-interval",
        type=float,
        default=0.5,
        help="Seconds between retransmitting connect/cmd requests",
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
            inbound_branch=args.branch_s2c,
            outbound_branch=args.branch_c2s,
            auto_init_local=True,
        )
    except GitTransportError as exc:
        print(f"sshg: {exc}", file=sys.stderr)
        return 2

    config = ClientConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        connect_timeout=max(args.connect_timeout, 1.0),
        session_timeout=max(args.session_timeout, 1.0),
        retry_interval=max(args.retry_interval, 0.05),
        fetch_interval=max(args.fetch_interval, 0.02),
        push_interval=max(args.push_interval, 0.02),
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


if __name__ == "__main__":
    raise SystemExit(main())
