# clipssh

`clipssh` is a local client/server SSH emulator with four transports:

- clipboard transport (`sshc` + `sshcd`)
- git transport (`sshg` + `sshgd`)
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

Client host setup:

```bash
sshg-audio-setup create-client-devices
sshg-audio-setup status
```

Optional VM/server-side setup:

```bash
sshg-audio-setup create-server-devices
sshg-audio-setup status
```

Probe audio capture/playback:

```bash
sshg-audio-probe --duration 5 --tx --rx
sshg-audio-probe --list-backends
```

Probe explicit virtual devices created by `sshg-audio-setup`:

```bash
sshg-audio-probe --tx --rx --duration 5 \
  --input-device sshg_vm_mic \
  --output-device sshg_vm_sink
```

Run server in VM:

```bash
sshgd -v \
  --transport audio-modem
```

Run client on host:

```bash
sshg localhost \
  --transport audio-modem
```

If `--audio-input-device` and `--audio-output-device` are both omitted, `sshg` and `sshgd` run an auto-discovery sequence:
- warns you to lower speaker volume,
- asks for confirmation before probes start,
- sends/listens discovery pings on all detected input/output devices,
- prints the selected `--audio-input-device` / `--audio-output-device` flags to reuse next time.
- requires an interactive terminal for confirmation (non-interactive runs should pass both device flags explicitly).

If you prefer fixed routing, pass both flags explicitly.

Troubleshooting:
- If `sshg-audio-probe` reports ffmpeg capture/playback exit, rerun with explicit `--input-device` and `--output-device`.
- Run `pactl list short sources` / `pactl list short sinks` and select concrete device names.
- Backend auto mode prefers `pulse-cli` (`parec`/`pacat`) and falls back to ffmpeg if pulse-cli is unavailable.
- You can force a backend: `--audio-backend pulse-cli` or an ffmpeg format backend such as `--audio-backend alsa` (if available).

Useful reliability knobs:
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
- USB serial transport requires both peers to access the same forwarded serial channel.
- USB gadget emulation requires root and Linux gadget-capable hardware on the emulating side.
- Audio-modem transport requires ffmpeg and PulseAudio/PipeWire routing support.
- Audio DSP (AGC/noise suppression/echo cancellation) can reduce reliability; tune remoting audio settings when possible.

## Testing

Run unit and integration tests:

```bash
python -m unittest discover -s tests -v
```
