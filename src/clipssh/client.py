"""Clipboard SSH client."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
import time
import uuid

from .clipboard import ClipboardBackend, ClipboardError, detect_backend
from .protocol import Message, build_message, decode_message, encode_message
from .session import EndpointState


@dataclass
class ClientConfig:
    poll_interval: float = 0.1
    connect_timeout: float = 10.0
    session_timeout: float = 300.0
    retry_interval: float = 0.5
    verbose: bool = False


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


class ClipboardSSHClient:
    def __init__(self, backend: ClipboardBackend, config: ClientConfig) -> None:
        self.backend = backend
        self.config = config
        self._state: EndpointState | None = None

    def _log(self, text: str) -> None:
        if self.config.verbose:
            print(f"[sshc] {text}", file=sys.stderr)

    @property
    def is_connected(self) -> bool:
        return self._state is not None

    def _ensure_state(self) -> EndpointState:
        if self._state is None:
            raise RuntimeError("Not connected")
        return self._state

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
            raise RuntimeError(f"Failed to write clipboard message: {exc}") from exc

    def connect(self, host: str) -> None:
        if self._state is not None:
            raise RuntimeError("Already connected")

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

            incoming = self._read_message()
            if incoming is None:
                time.sleep(self.config.poll_interval)
                continue

            if incoming.target != "client" or incoming.source != "server":
                time.sleep(self.config.poll_interval)
                continue

            if incoming.session_id != session_id:
                time.sleep(self.config.poll_interval)
                continue

            if not state.incoming_seen.mark(incoming.msg_id):
                time.sleep(self.config.poll_interval)
                continue

            if incoming.kind == "connect_ack":
                self._state = state
                self._log(f"connected with session {session_id}")
                return

            if incoming.kind == "busy":
                raise RuntimeError("Server is busy with another active session")

            if incoming.kind == "error":
                message = incoming.body.get("error") if isinstance(incoming.body, dict) else "unknown error"
                raise RuntimeError(f"Server rejected connection: {message}")

            time.sleep(self.config.poll_interval)

        raise TimeoutError("Timed out waiting for server connect_ack")

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

            incoming = self._read_message()
            if incoming is not None:
                if incoming.target != "client" or incoming.source != "server":
                    time.sleep(self.config.poll_interval)
                    continue
                if incoming.session_id != state.session_id:
                    time.sleep(self.config.poll_interval)
                    continue
                if not state.incoming_seen.mark(incoming.msg_id):
                    time.sleep(self.config.poll_interval)
                    continue

                body = incoming.body if isinstance(incoming.body, dict) else {}

                if incoming.kind in {"stdout", "stderr", "exit"} and body.get("cmd_id") != cmd_id:
                    time.sleep(self.config.poll_interval)
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

        try:
            self._write_message(message)
        finally:
            self._state = None

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
                    line = input("sshc> ")
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
                    print(f"[sshc] exit_code={result.exit_code}", file=sys.stderr)
        finally:
            self.disconnect()

        return 0



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshc", description="Clipboard SSH client")
    parser.add_argument("host", help="ssh-style target host (informational in this local emulator)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logs")
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=100,
        help="Clipboard polling interval in milliseconds",
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
    return parser



def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        backend = detect_backend()
    except ClipboardError as exc:
        print(f"sshc: {exc}", file=sys.stderr)
        return 2

    config = ClientConfig(
        poll_interval=max(args.poll_interval_ms / 1000.0, 0.01),
        connect_timeout=max(args.connect_timeout, 1.0),
        session_timeout=max(args.session_timeout, 1.0),
        retry_interval=max(args.retry_interval, 0.05),
        verbose=args.verbose,
    )

    client = ClipboardSSHClient(backend=backend, config=config)

    try:
        return client.run_interactive(args.host)
    except KeyboardInterrupt:
        client.disconnect()
        return 130
    except (RuntimeError, TimeoutError) as exc:
        print(f"sshc: {exc}", file=sys.stderr)
        client.disconnect()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
