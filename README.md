# clipssh

`clipssh` is a local client/server SSH emulator that uses the system clipboard as its transport.

- Client command: `sshc`
- Server daemon: `sshcd`
- Transport: clipboard polling + tagged protocol messages
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

This exposes `sshc` and `sshcd` commands.

## Quick Start

Terminal 1:

```bash
sshcd -v
```

Terminal 2:

```bash
sshc localhost
```

Then type commands at the `sshc> ` prompt.

## Protocol Notes

Clipboard messages use this wire prefix:

- `CLIPSSH/1 `

Payload is JSON containing:

- `kind`: `connect_req`, `connect_ack`, `cmd`, `stdout`, `stderr`, `exit`, `disconnect`, `busy`, `error`
- `session_id`, `msg_id`, `seq`, `ts`
- `source` / `target`
- `body`

Non-protocol clipboard entries are ignored.

## Limitations

- Single active server session.
- No encryption, authentication, or login checks.
- Command/response mode, not full TTY emulation.
- Clipboard is shared with normal copy/paste, so user clipboard activity can interfere.
- Best-effort reliability via retries + de-duplication.

## Testing

Run unit and integration tests:

```bash
python -m unittest discover -s tests -v
```
