"""Git commit-based transport backend with upstream synchronization."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Iterator

from .protocol import Message, decode_message, encode_message

DEFAULT_BRANCH_C2S = "gitssh-c2s"
DEFAULT_BRANCH_S2C = "gitssh-s2c"


class GitTransportError(RuntimeError):
    """Raised when git transport operations fail."""


class GitTransportBackend:
    """Stores protocol messages in a local bare repo and syncs with upstream."""

    def __init__(
        self,
        local_repo_path: str,
        *,
        upstream_url: str,
        inbound_branch: str,
        outbound_branch: str,
        auto_init_local: bool = True,
        push_retries: int = 6,
        conflict_retry_delay: float = 0.05,
    ) -> None:
        self.local_repo_path = Path(local_repo_path).expanduser()
        self.upstream_url = upstream_url
        self.inbound_branch = inbound_branch
        self.outbound_branch = outbound_branch
        self.inbound_ref = f"refs/heads/{inbound_branch}"
        self.outbound_ref = f"refs/heads/{outbound_branch}"
        self.auto_init_local = auto_init_local
        self.push_retries = max(push_retries, 1)
        self.conflict_retry_delay = max(conflict_retry_delay, 0.01)

        self._lock_path = self.local_repo_path / "gitssh.lock"

        self.ensure_initialized()

    def name(self) -> str:
        return (
            "git:"
            f"{self.local_repo_path}"
            f" (upstream={self.upstream_url}, in={self.inbound_branch}, out={self.outbound_branch})"
        )

    def ensure_initialized(self) -> None:
        if shutil.which("git") is None:
            raise GitTransportError("git executable is not available in PATH")

        if not self.local_repo_path.exists():
            if not self.auto_init_local:
                raise GitTransportError(
                    f"Local mirror repo does not exist: {self.local_repo_path}"
                )

            self.local_repo_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "init", "--bare", str(self.local_repo_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise GitTransportError(
                    f"Failed to initialize local bare repo: {stderr}"
                )

        is_bare = self._run_git(["rev-parse", "--is-bare-repository"]).strip()
        if is_bare != "true":
            raise GitTransportError(
                f"Local mirror repo is not bare: {self.local_repo_path}"
            )

        self._lock_path.touch(exist_ok=True)
        self._ensure_origin_remote()

    def snapshot_inbound_cursor(self) -> str | None:
        return self._resolve_ref(self.inbound_ref)

    def read_inbound_messages(self, cursor: str | None) -> tuple[list[Message], str | None]:
        head = self._resolve_ref(self.inbound_ref)
        if head is None:
            return [], cursor
        if cursor == head:
            return [], cursor

        commit_ids = self._list_commits(cursor=cursor, head=head)
        messages: list[Message] = []

        for commit_id in commit_ids:
            frame_path = self._frame_path_for_commit(commit_id)
            if frame_path is None:
                continue

            content = self._show_file(commit_id, frame_path)
            message = decode_message(content)
            if message is None:
                continue
            messages.append(message)

        next_cursor = commit_ids[-1] if commit_ids else cursor
        return messages, next_cursor

    def fetch_inbound(self) -> None:
        with self._repo_lock():
            self._fetch_branch_to_local(
                branch=self.inbound_branch,
                local_ref=self.inbound_ref,
                allow_missing=True,
            )

    def push_outbound(self) -> None:
        with self._repo_lock():
            if self._resolve_ref(self.outbound_ref) is None:
                return

            push_ok, push_error = self._push_outbound_once()
            if push_ok:
                return

            if self._is_non_fast_forward_error(push_error):
                # Refresh local mirror of outbound branch so next write starts from upstream tip.
                self._fetch_branch_to_local(
                    branch=self.outbound_branch,
                    local_ref=self.outbound_ref,
                    allow_missing=True,
                )
                return

            raise GitTransportError(push_error)

    def sync_once(self) -> None:
        self.fetch_inbound()
        self.push_outbound()

    def write_outbound_message(self, message: Message) -> str:
        payload = encode_message(message)

        with self._repo_lock():
            delay = self.conflict_retry_delay

            for attempt in range(self.push_retries):
                commit_id = self._commit_frame_on_outbound(
                    message=message,
                    payload=payload,
                )

                push_ok, push_error = self._push_outbound_once()
                if push_ok:
                    return commit_id

                if self._is_non_fast_forward_error(push_error):
                    if attempt + 1 < self.push_retries:
                        self._fetch_branch_to_local(
                            branch=self.outbound_branch,
                            local_ref=self.outbound_ref,
                            allow_missing=True,
                        )
                        time.sleep(delay)
                        delay = min(delay * 2.0, 0.5)
                        continue

                    raise GitTransportError(
                        "Failed to push outbound branch after retries due to repeated "
                        f"non-fast-forward conflicts: {push_error}"
                    )

                raise GitTransportError(push_error)

            raise GitTransportError("Failed to push outbound message")

    def _ensure_origin_remote(self) -> None:
        result = self._run_git_result(["remote", "get-url", "origin"])
        if result.returncode != 0:
            self._run_git(["remote", "add", "origin", self.upstream_url])
            return

        existing = (result.stdout or "").strip()
        if existing != self.upstream_url:
            self._run_git(["remote", "set-url", "origin", self.upstream_url])

    def _commit_frame_on_outbound(self, *, message: Message, payload: str) -> str:
        parent = self._resolve_ref(self.outbound_ref)

        blob = self._run_git(["hash-object", "-w", "--stdin"], input_text=payload).strip()

        frames_entry = f"100644 blob {blob}\t{message.msg_id}.json\n"
        frames_tree = self._run_git(["mktree"], input_text=frames_entry).strip()

        root_entry = f"040000 tree {frames_tree}\tframes\n"
        root_tree = self._run_git(["mktree"], input_text=root_entry).strip()

        commit_subject = (
            f"gitssh:{message.kind}:{message.session_id}:{message.seq}:{message.msg_id}"
        )
        commit_args = ["commit-tree", root_tree]
        if parent:
            commit_args.extend(["-p", parent])
        commit_id = self._run_git(commit_args, input_text=f"{commit_subject}\n").strip()

        update_args = ["update-ref", self.outbound_ref, commit_id]
        if parent:
            update_args.append(parent)
        self._run_git(update_args)

        return commit_id

    def _push_outbound_once(self) -> tuple[bool, str]:
        result = self._run_git_result(
            ["push", "origin", f"{self.outbound_ref}:{self.outbound_ref}"]
        )
        if result.returncode == 0:
            return True, ""

        combined = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
        return False, combined or "unknown push error"

    def _fetch_branch_to_local(self, *, branch: str, local_ref: str, allow_missing: bool) -> bool:
        result = self._run_git_result(
            ["fetch", "--prune", "origin", f"+refs/heads/{branch}:{local_ref}"]
        )

        if result.returncode == 0:
            return True

        stderr = (result.stderr or "").lower()
        stdout = (result.stdout or "").lower()
        combined = f"{stdout}\n{stderr}"

        missing_patterns = (
            "couldn't find remote ref",
            "no such ref was fetched",
            "fatal: couldn't find remote ref",
        )

        if allow_missing and any(pattern in combined for pattern in missing_patterns):
            return False

        raise GitTransportError(
            "Git fetch failed "
            f"(branch={branch}, ref={local_ref}): {(result.stderr or '').strip() or (result.stdout or '').strip()}"
        )

    def _list_commits(self, *, cursor: str | None, head: str) -> list[str]:
        if cursor is None:
            args = ["rev-list", "--reverse", head]
        else:
            args = ["rev-list", "--reverse", f"{cursor}..{head}"]

        try:
            output = self._run_git(args)
        except GitTransportError:
            # Cursor can become invalid if history is rewritten.
            output = self._run_git(["rev-list", "--reverse", head])

        return [line.strip() for line in output.splitlines() if line.strip()]

    def _frame_path_for_commit(self, commit_id: str) -> str | None:
        output = self._run_git(["ls-tree", "--name-only", "-r", commit_id, "frames"])
        for line in output.splitlines():
            candidate = line.strip()
            if candidate.endswith(".json"):
                return candidate
        return None

    def _show_file(self, commit_id: str, path: str) -> str:
        return self._run_git(["show", f"{commit_id}:{path}"])

    def _resolve_ref(self, ref: str) -> str | None:
        result = self._run_git_result(["rev-parse", "--verify", "-q", ref])
        if result.returncode != 0:
            return None

        value = (result.stdout or "").strip()
        return value or None

    def _is_non_fast_forward_error(self, message: str) -> bool:
        lowered = message.lower()
        patterns = (
            "non-fast-forward",
            "fetch first",
            "rejected",
            "failed to push some refs",
        )
        return any(pattern in lowered for pattern in patterns)

    def _run_git(self, args: list[str], input_text: str | None = None) -> str:
        result = self._run_git_result(args, input_text=input_text)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise GitTransportError(
                f"Git command failed ({self._format_cmd(args)}): {stderr or 'unknown error'}"
            )

        return result.stdout

    def _run_git_result(
        self,
        args: list[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "--git-dir", str(self.local_repo_path), *args]
        return subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def _format_cmd(self, args: list[str]) -> str:
        return " ".join(["git", "--git-dir", str(self.local_repo_path), *args])

    @contextmanager
    def _repo_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
