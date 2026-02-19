# clipssh

`clipssh` is a local client/server SSH emulator with two transports:

- clipboard transport (`sshc` + `sshcd`)
- git transport (`sshg` + `sshgd`)

- Clipboard client/server: `sshc` / `sshcd`
- Git client/server: `sshg` / `sshgd`
- Security: none (intended for local experimentation only)

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
The first prompt is initialized from server handshake metadata as `user@host:cwd$`.

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
- client->server branch: `gitssh-c2s`
- server->client branch: `gitssh-s2c`

Each peer keeps its own local bare mirror and continuously fetches/pushes against the upstream remote.

## Limitations

- Single active server session.
- No encryption, authentication, or login checks.
- Command/response mode, not full TTY emulation.
- Clipboard is shared with normal copy/paste, so user clipboard activity can interfere.
- Best-effort reliability via retries + de-duplication.
- Git transport requires a local `git` executable.
- Git transport requires both peers to have access to the same upstream bare repo URL/path.

## Testing

Run unit and integration tests:

```bash
python -m unittest discover -s tests -v
```
