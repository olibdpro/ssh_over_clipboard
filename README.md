# clipssh

`clipssh` is a local client/server SSH emulator with two transports:

- clipboard transport (`sshc` + `sshcd`)
- git transport (`sshg` + `sshgd`)

- Clipboard client/server: `sshc` / `sshcd`
- Git client/server: `sshg` / `sshgd`
- Security: none (intended for local experimentation only)
- Git transport now uses a PTY byte stream (`gitssh/2`) for interactive shells.

## Requirements

- Python 3.10+
- Linux
- At least one clipboard tool:
  - Wayland: `wl-copy` + `wl-paste` (`wl-clipboard` package)
  - X11: `xclip`
  - X11 fallback: `xsel`

Server shell behavior:
- Prefer `tcsh` when available
- Fallback to `/bin/sh` automatically

## Install

From this repository:

```bash
python -m pip install -e .
```

This exposes `sshc`, `sshcd`, `sshg`, and `sshgd` commands.

## Quick Start

### Clipboard transport

Terminal 1:

```bash
sshcd -v
```

Terminal 2:

```bash
sshc localhost
```

Then type commands at the prompt.
The first prompt is initialized from server handshake metadata as `user@host:cwd$`.

Clipboard backend controls are available on both `sshc` and `sshcd`:

```bash
sshc localhost --clipboard-backend auto --clipboard-read-timeout 2 --clipboard-write-timeout 2
sshcd --clipboard-backend auto --clipboard-read-timeout 2 --clipboard-write-timeout 2
```

You can tune startup probing separately from runtime polling/writes:

```bash
sshc localhost \
  --clipboard-read-timeout 0.25 \
  --clipboard-write-timeout 1.0 \
  --clipboard-probe-read-timeout 2.0 \
  --clipboard-probe-write-timeout 2.0
```

`--clipboard-backend` choices: `auto`, `wayland`, `xclip`, `xsel`.
In `auto` mode, session detection is used:
- Wayland session: `wl-copy`/`wl-paste` only.
- X11 session: prefer `xsel`, then `xclip`.

### Git transport (shared upstream)

Initialize one upstream bare repo that both peers can access:

```bash
git init --bare /tmp/gitssh-upstream.git
```

Terminal 1:

```bash
sshgd -v \
  --upstream-url /tmp/gitssh-upstream.git \
  --local-repo /tmp/gitssh-server.git
```

Terminal 2:

```bash
sshg localhost \
  --upstream-url /tmp/gitssh-upstream.git \
  --local-repo /tmp/gitssh-client.git
```

Then type commands at the prompt.
`sshg` now opens a raw interactive PTY stream (no local `input()` prompt wrapper).

## Protocol Notes

Clipboard messages use this wire prefix:

- `CLIPSSH/1 `

Payload is JSON containing:

- `kind`: `connect_req`, `connect_ack`, `cmd`, `stdout`, `stderr`, `exit`, `disconnect`, `busy`, `error`
- `session_id`, `msg_id`, `seq`, `ts`
- `source` / `target`
- `body`

Non-protocol clipboard entries are ignored.

Git transport stores one protocol frame per commit and syncs through:
- client->server branch: `gitssh2-c2s`
- server->client branch: `gitssh2-s2c`

Each peer keeps its own local bare mirror and continuously fetches/pushes against the upstream remote.

Git transport protocol details:
- Protocol: `gitssh/2`
- Interactive PTY message kinds: `pty_input`, `pty_output`, `pty_resize`, `pty_signal`, `pty_closed`

## Limitations

- Single active server session.
- No encryption, authentication, or login checks.
- Git transport is PTY-stream based and supports interactive terminal applications, but remains latency-sensitive due to commit-based transport.
- Clipboard is shared with normal copy/paste, so user clipboard activity can interfere.
- Clipboard tools (`wl-copy`/`wl-paste`, `xsel`, `xclip`) are native system executables.
  If missing, use your distro package manager or Conda (`conda install -c conda-forge wl-clipboard xsel xclip`).
  `pip` is not a reliable way to install these binaries.
- Best-effort reliability via retries + de-duplication.
- Git transport requires a local `git` executable.
- Git transport requires both peers to have access to the same upstream bare repo URL/path.

## Testing

Run unit and integration tests:

```bash
python -m unittest discover -s tests -v
```
