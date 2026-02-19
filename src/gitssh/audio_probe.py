"""Audio path probe utility for audio-modem transport."""

from __future__ import annotations

import argparse
import math
import struct
import sys
import time

from .audio_io_ffmpeg import AudioIOError, FFmpegAudioDuplexIO


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg-audio-probe", description="Probe ffmpeg audio capture/playback paths")
    parser.add_argument("--input-device", default="default", help="Capture device name")
    parser.add_argument("--output-device", default="default", help="Playback device name")
    parser.add_argument("--sample-rate", type=int, default=48000, help="PCM sample rate")
    parser.add_argument("--duration", type=float, default=5.0, help="Probe duration in seconds")
    parser.add_argument("--tx", action="store_true", help="Emit probe tone to playback path")
    parser.add_argument("--rx", action="store_true", help="Capture and report input RMS/peak")
    parser.add_argument("--tone-hz", type=float, default=1040.0, help="Probe tone frequency")
    parser.add_argument("--audio-backend", default="pulse", help="FFmpeg input/output backend")
    parser.add_argument("--ffmpeg-bin", default="ffmpeg", help="ffmpeg executable")
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

    do_tx = args.tx or (not args.tx and not args.rx)
    do_rx = args.rx or (not args.tx and not args.rx)

    try:
        io_obj = FFmpegAudioDuplexIO(
            ffmpeg_bin=args.ffmpeg_bin,
            backend=args.audio_backend,
            input_device=args.input_device,
            output_device=args.output_device,
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
