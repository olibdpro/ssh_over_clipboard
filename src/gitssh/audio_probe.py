"""Audio path probe utility for audio-modem transport."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

from .audio_device_names import (
    AudioDeviceNameError,
    resolve_input_device_name,
    resolve_output_device_name,
)
from .audio_io_ffmpeg import (
    AudioIOError,
    _ffmpeg_format_capabilities,
    _format_duplex_backends,
    build_audio_duplex_io,
)
from .audio_pipewire_runtime import build_client_pipewire_preflight_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg-audio-probe", description="Probe ffmpeg audio capture/playback paths")
    parser.add_argument(
        "--pipewire-preflight",
        action="store_true",
        help="Run PipeWire client preflight checks and exit",
    )
    parser.add_argument(
        "--pw-capture-node-id",
        type=int,
        default=None,
        help="Optional capture node id to validate during PipeWire preflight",
    )
    parser.add_argument(
        "--pw-write-node-id",
        type=int,
        default=None,
        help="Optional write node id to validate during PipeWire preflight",
    )
    parser.add_argument(
        "--input-device",
        default="default",
        help="Capture device name (role alias or concrete backend device name)",
    )
    parser.add_argument(
        "--output-device",
        default="default",
        help="Playback device name (role alias or concrete backend device name)",
    )
    parser.add_argument("--sample-rate", type=int, default=48000, help="PCM sample rate")
    parser.add_argument("--duration", type=float, default=5.0, help="Probe duration in seconds")
    parser.add_argument("--tx", action="store_true", help="Emit probe tone to playback path")
    parser.add_argument("--rx", action="store_true", help="Capture and report input RMS/peak")
    parser.add_argument("--tone-hz", type=float, default=1040.0, help="Probe tone frequency")
    parser.add_argument(
        "--audio-backend",
        default="auto",
        help="FFmpeg input/output backend (default: auto-detect)",
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg executable")
    parser.add_argument(
        "--list-backends",
        action="store_true",
        help="List detected ffmpeg duplex audio backends and exit",
    )
    return parser


def _tone_chunk(*, sample_rate: int, frequency_hz: float, frames: int, phase0: float) -> tuple[bytes, float]:
    values = []
    phase = phase0
    step = (2.0 * math.pi * frequency_hz) / float(sample_rate)
    for _ in range(frames):
        sample = int(math.sin(phase) * 12000)
        values.append(sample)
        phase += step
        if phase > 2.0 * math.pi:
            phase -= 2.0 * math.pi
    return struct.pack("<" + "h" * len(values), *values), phase


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.pipewire_preflight:
        report = build_client_pipewire_preflight_report(
            capture_node_id=args.pw_capture_node_id,
            write_node_id=args.pw_write_node_id,
        )
        rendered = report.render()
        if report.ok:
            print(rendered)
            return 0
        print(f"sshg-audio-probe: {rendered}", file=sys.stderr)
        return 2

    if args.list_backends:
        try:
            caps = _ffmpeg_format_capabilities(args.ffmpeg_bin)
        except AudioIOError as exc:
            print(f"sshg-audio-probe: {exc}", file=sys.stderr)
            return 2
        print(f"Available duplex backends: {_format_duplex_backends(caps)}")
        return 0

    do_tx = args.tx or (not args.tx and not args.rx)
    do_rx = args.rx or (not args.tx and not args.rx)

    try:
        resolved_input = resolve_input_device_name(
            requested=args.input_device,
            backend=args.audio_backend,
        )
        resolved_output = resolve_output_device_name(
            requested=args.output_device,
            backend=args.audio_backend,
        )
    except AudioDeviceNameError as exc:
        print(f"sshg-audio-probe: {exc}", file=sys.stderr)
        return 2

    try:
        io_obj = build_audio_duplex_io(
            ffmpeg_bin=args.ffmpeg_bin,
            backend=args.audio_backend,
            input_device=resolved_input,
            output_device=resolved_output,
            sample_rate=max(args.sample_rate, 8000),
            read_timeout=0.01,
            write_timeout=0.1,
        )
    except AudioIOError as exc:
        print(f"sshg-audio-probe: {exc}", file=sys.stderr)
        return 2

    peak = 0
    rms_accum = 0.0
    rms_count = 0
    read_bytes = 0
    phase = 0.0

    deadline = time.monotonic() + max(args.duration, 0.1)
    try:
        while time.monotonic() < deadline:
            if do_tx:
                pcm, phase = _tone_chunk(
                    sample_rate=max(args.sample_rate, 8000),
                    frequency_hz=max(args.tone_hz, 10.0),
                    frames=480,
                    phase0=phase,
                )
                io_obj.write(pcm)

            if do_rx:
                chunk = io_obj.read(4096)
                if chunk:
                    read_bytes += len(chunk)
                    if len(chunk) % 2 == 1:
                        chunk = chunk[:-1]
                    if chunk:
                        samples = struct.unpack("<" + "h" * (len(chunk) // 2), chunk)
                        for sample in samples:
                            abs_s = abs(sample)
                            if abs_s > peak:
                                peak = abs_s
                            rms_accum += float(sample) * float(sample)
                            rms_count += 1

            time.sleep(0.01)
    except AudioIOError as exc:
        print(f"sshg-audio-probe: {exc}", file=sys.stderr)
        return 1
    finally:
        io_obj.close()

    print("Audio probe completed")
    print(f"- tx_enabled={do_tx}")
    print(f"- rx_enabled={do_rx}")
    if do_rx:
        rms = math.sqrt(rms_accum / rms_count) if rms_count else 0.0
        print(f"- captured_bytes={read_bytes}")
        print(f"- peak={peak}")
        print(f"- rms={rms:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
