# clipssh

`clipssh` is a local client/server SSH emulator with five transports:

- clipboard transport (`sshc` + `sshcd`)
- git transport (`sshg` + `sshgd`)
- Google Drive transport (`sshg --transport google-drive` + `sshgd --transport google-drive`)
- usb-serial transport (`sshg --transport usb-serial` + `sshgd --transport usb-serial`)
- audio-modem transport (`sshg --transport audio-modem` + `sshgd --transport audio-modem`)

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

This exposes:
- `sshc`, `sshcd`
- `sshg`, `sshgd`
- `sshg-usb-probe`, `sshg-usb-gadget`
- `sshg-audio-setup`, `sshg-audio-probe`

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

### Google Drive transport (OAuth appData logs)

This transport mirrors the git branch model with two Google Drive `appDataFolder` log files:

- `gitssh2-c2s.log` for client->server frames
- `gitssh2-s2c.log` for server->client frames

Setup:
- Create a Google Cloud OAuth client ID for a Desktop app.
- Download the client-secrets JSON file.
- Use the same OAuth app and Google account on both peers.

Terminal 1 (server side):

```bash
sshgd -v \
  --transport google-drive \
  --drive-client-secrets ~/secrets/google-drive-client.json
```

Terminal 2 (client side):

```bash
sshg localhost \
  --transport google-drive \
  --drive-client-secrets ~/secrets/google-drive-client.json
```

First run opens a local browser consent flow and stores a refresh token at
`~/.config/clipssh/drive-token.json` (override with `--drive-token-path`).
For headless runs, complete OAuth once in an interactive terminal first.

### USB serial transport

Use this when you have a serial link between peers (for example a USB device forwarded through a remoting stack):

Terminal 1 (server side):

```bash
sshgd -v \
  --transport usb-serial \
  --serial-port /dev/ttyACM0
```

Terminal 2 (client side):

```bash
sshg localhost \
  --transport usb-serial \
  --serial-port /dev/ttyACM0
```

Probe local serial readiness:

```bash
sshg-usb-probe --list
sshg-usb-probe --serial-port /dev/ttyACM0
```

Linux fake USB CDC gadget helper (root required):

```bash
sudo sshg-usb-gadget create
sudo sshg-usb-gadget status
sudo sshg-usb-gadget destroy
```

Notes:
- `sshg-usb-gadget` requires a Linux host with USB gadget mode support (`/sys/class/udc` present).
- On many laptops/desktops acting as USB hosts only, gadget mode is unavailable.
- If using PCoIP USB redirection, your policy must allow forwarding the emulated class (CDC ACM).

### Audio modem transport

Audio-modem transport tunnels protocol messages through PCM audio streams.
This is intended for environments where microphone/audio channels are available (for example PCoIP).

Current routing model (Pulse-only):
- `sshgd` captures from the server default microphone and plays to the server default speakers.
- `sshg` captures one active client app playback stream and writes server audio to a client virtual microphone source.
- `--audio-backend` must be `pulse-cli`.

Run server in VM:

```bash
sshgd -v \
  --transport audio-modem \
  --audio-backend pulse-cli
```

Run client on host (interactive stream prompt):

```bash
sshg localhost \
  --transport audio-modem \
  --audio-backend pulse-cli
```

Non-interactive client runs must preselect a stream:

```bash
sshg localhost \
  --transport audio-modem \
  --audio-backend pulse-cli \
  --audio-stream-index 42
```

or by regex match:

```bash
sshg localhost \
  --transport audio-modem \
  --audio-backend pulse-cli \
  --audio-stream-match 'chrome|firefox'
```

Optional diagnostics:

```bash
sshg-audio-probe --duration 5 --tx --rx
```

Troubleshooting:
- If `sshg` reports no active playback streams, start playback in the target app and retry.
- For non-interactive usage, pass `--audio-stream-index` or `--audio-stream-match`.
- Ensure `pactl`, `parec`, and `pacat` are installed and accessible.
- Inspect streams/devices with `pactl -f json list sink-inputs`, `pactl list short sources`, and `pactl list short sinks`.

Useful reliability knobs:
- `--audio-modulation` (`auto`, `robust-v1`, `legacy`; default `auto`)
- `--audio-byte-repeat` (simple error-correction repeat factor, default `3`)
- `--audio-ack-timeout-ms` / `--audio-max-retries`
- `--audio-marker-run` (frame delimiter marker length)

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

Google Drive transport stores one protocol frame per line in two shared appData files:
- client->server file: `gitssh2-c2s.log`
- server->client file: `gitssh2-s2c.log`

Git transport protocol details:
- Protocol: `gitssh/2`
- Interactive PTY message kinds: `pty_input`, `pty_output`, `pty_resize`, `pty_signal`, `pty_closed`

USB serial transport details:
- Uses the same `gitssh/2` message schema.
- Wraps each message in a framed binary stream with CRC32 + ACK/retry.

Audio-modem transport details:
- Uses the same `gitssh/2` message schema.
- Encodes link frames into PCM audio packets with markers + COBS framing.
- Includes CRC32 integrity checks, deduplication, retransmission, and a simple repeat-code FEC.

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
- Google Drive transport requires internet access, Google OAuth credentials, and initial interactive consent.
- USB serial transport requires both peers to access the same forwarded serial channel.
- USB gadget emulation requires root and Linux gadget-capable hardware on the emulating side.
- Audio-modem transport requires PulseAudio/PipeWire routing support with `pactl`, `parec`, and `pacat`.
- Audio DSP (AGC/noise suppression/echo cancellation) can reduce reliability; tune remoting audio settings when possible.

## Testing

Run unit and integration tests:

```bash
python -m unittest discover -s tests -v
```
