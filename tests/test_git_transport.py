from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.git_transport import (
    DEFAULT_BRANCH_C2S,
    DEFAULT_BRANCH_S2C,
    GitTransportBackend,
)
from gitssh.protocol import build_message


GIT_AVAILABLE = shutil.which("git") is not None



def _run_git(repo_path: str, args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "--git-dir", repo_path, *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(args)}: {result.stderr.strip()}")
    return result



def _init_bare_repo(path: str) -> None:
    result = subprocess.run(
        ["git", "init", "--bare", path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git init --bare failed: {result.stderr.strip()}")



def _append_raw_frame(upstream_repo_path: str, branch: str, filename: str, payload: str) -> None:
    branch_ref = f"refs/heads/{branch}"
    parent_result = _run_git(upstream_repo_path, ["rev-parse", "--verify", "-q", branch_ref], check=False)
    parent = (parent_result.stdout or "").strip() or None

    blob = _run_git(upstream_repo_path, ["hash-object", "-w", "--stdin"], input_text=payload).stdout.strip()
    frames_tree = _run_git(
        upstream_repo_path,
        ["mktree"],
        input_text=f"100644 blob {blob}\t{filename}\n",
    ).stdout.strip()
    root_tree = _run_git(
        upstream_repo_path,
        ["mktree"],
        input_text=f"040000 tree {frames_tree}\tframes\n",
    ).stdout.strip()

    commit_args = ["commit-tree", root_tree]
    if parent:
        commit_args.extend(["-p", parent])
    commit_id = _run_git(upstream_repo_path, commit_args, input_text="raw-frame\n").stdout.strip()

    update_args = ["update-ref", branch_ref, commit_id]
    if parent:
        update_args.append(parent)
    _run_git(upstream_repo_path, update_args)


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required")
class GitTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tempdir.name)

        self.upstream_repo = str(root / "upstream.git")
        self.writer_local_repo = str(root / "writer-local.git")
        self.reader_local_repo = str(root / "reader-local.git")

        _init_bare_repo(self.upstream_repo)

        self.writer_backend = GitTransportBackend(
            local_repo_path=self.writer_local_repo,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_S2C,
            outbound_branch=DEFAULT_BRANCH_C2S,
            auto_init_local=True,
        )
        self.reader_backend = GitTransportBackend(
            local_repo_path=self.reader_local_repo,
            upstream_url=self.upstream_repo,
            inbound_branch=DEFAULT_BRANCH_C2S,
            outbound_branch=DEFAULT_BRANCH_S2C,
            auto_init_local=True,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_round_trip_write_read(self) -> None:
        cursor = self.reader_backend.snapshot_inbound_cursor()

        message = build_message(
            kind="connect_req",
            session_id=str(uuid.uuid4()),
            source="client",
            target="server",
            seq=1,
            body={"host": "localhost"},
        )
        self.writer_backend.write_outbound_message(message)

        self.reader_backend.fetch_inbound()
        messages, cursor = self.reader_backend.read_inbound_messages(cursor)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].kind, "connect_req")
        self.assertEqual(messages[0].body["host"], "localhost")

        messages_again, _ = self.reader_backend.read_inbound_messages(cursor)
        self.assertEqual(messages_again, [])

    def test_ignores_malformed_frames(self) -> None:
        cursor = self.reader_backend.snapshot_inbound_cursor()
        _append_raw_frame(self.upstream_repo, DEFAULT_BRANCH_C2S, "broken.json", "not-json")

        self.reader_backend.fetch_inbound()
        messages, _ = self.reader_backend.read_inbound_messages(cursor)
        self.assertEqual(messages, [])

    def test_write_lock_serializes_concurrent_writers(self) -> None:
        start_cursor = self.reader_backend.snapshot_inbound_cursor()

        total_messages = 20

        def writer(index: int) -> None:
            writer_backend = GitTransportBackend(
                local_repo_path=self.writer_local_repo,
                upstream_url=self.upstream_repo,
                inbound_branch=DEFAULT_BRANCH_S2C,
                outbound_branch=DEFAULT_BRANCH_C2S,
                auto_init_local=False,
            )
            message = build_message(
                kind="pty_input",
                session_id=str(uuid.uuid4()),
                source="client",
                target="server",
                seq=index + 1,
                body={
                    "stream_id": str(uuid.uuid4()),
                    "data_b64": base64.b64encode(f"writer-{index}\n".encode()).decode("ascii"),
                },
            )
            writer_backend.write_outbound_message(message)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(total_messages)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4.0)

        self.reader_backend.fetch_inbound()
        messages, _ = self.reader_backend.read_inbound_messages(start_cursor)
        self.assertEqual(len(messages), total_messages)
        self.assertEqual(len({msg.msg_id for msg in messages}), total_messages)


if __name__ == "__main__":
    unittest.main()
