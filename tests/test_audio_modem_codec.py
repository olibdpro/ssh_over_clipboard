from __future__ import annotations

import pathlib
import random
import struct
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_modem import AudioFrameCodec, RobustFskFrameCodec, create_audio_frame_codec


class AudioFrameCodecTests(unittest.TestCase):
    def test_round_trip_frame(self) -> None:
        codec = AudioFrameCodec(byte_repeat=3, marker_run=12)
        payload = b"\x00\x01hello\x00world\xff"
        pcm = codec.encode_frame(payload)
        frames = codec.feed_pcm(pcm)
        self.assertEqual(frames, [payload])

    def test_recovers_from_single_sample_corruption_with_repeat_code(self) -> None:
        codec = AudioFrameCodec(byte_repeat=3, marker_run=12)
        payload = b"test-payload-123"
        pcm = bytearray(codec.encode_frame(payload))
        ints = list(struct.unpack("<" + "h" * (len(pcm) // 2), pcm))

        marker = 12
        # Flip one sample in each repeated triplet for first few payload bytes.
        start = marker
        for i in range(0, 9, 3):
            idx = start + i + 1
            ints[idx] = 0

        corrupted = struct.pack("<" + "h" * len(ints), *ints)
        frames = codec.feed_pcm(corrupted)
        self.assertEqual(frames, [payload])

    def test_robust_codec_round_trip_frame(self) -> None:
        codec = RobustFskFrameCodec(sample_rate=48000)
        payload = b"robust-payload-hello"
        pcm = codec.encode_frame(payload)
        frames = codec.feed_pcm(pcm)
        self.assertEqual(frames, [payload])

    def test_robust_codec_survives_small_noise(self) -> None:
        codec = RobustFskFrameCodec(sample_rate=48000)
        payload = b"robust-noise-test"
        pcm = bytearray(codec.encode_frame(payload))
        ints = list(struct.unpack("<" + "h" * (len(pcm) // 2), pcm))

        random.seed(7)
        for _ in range(150):
            idx = random.randrange(0, len(ints))
            ints[idx] = max(min(ints[idx] + random.randint(-400, 400), 32767), -32768)

        noisy_pcm = struct.pack("<" + "h" * len(ints), *ints)
        frames = codec.feed_pcm(noisy_pcm)
        self.assertEqual(frames, [payload])

    def test_create_codec_auto_uses_robust_profile(self) -> None:
        codec = create_audio_frame_codec(
            modulation="auto",
            sample_rate=48000,
            byte_repeat=3,
            marker_run=16,
        )
        self.assertIsInstance(codec, RobustFskFrameCodec)

    def test_create_codec_pcoip_safe_uses_resilient_fsk_profile(self) -> None:
        codec = create_audio_frame_codec(
            modulation="pcoip-safe",
            sample_rate=48000,
            byte_repeat=3,
            marker_run=16,
        )
        self.assertIsInstance(codec, RobustFskFrameCodec)
        self.assertEqual(codec.symbol_rate, 900)
        self.assertEqual(codec.bit_repeat, 5)


if __name__ == "__main__":
    unittest.main()
