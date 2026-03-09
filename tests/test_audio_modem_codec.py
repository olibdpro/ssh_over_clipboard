from __future__ import annotations

import pathlib
import random
import shutil
import struct
import subprocess
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
        self.assertEqual(codec.bit_repeat, 3)
        self.assertEqual(codec.channels, 2)
        self.assertEqual(codec._freqs, (600.0, 900.0, 1200.0, 1800.0))

    def test_pcoip_safe_codec_round_trip_frame(self) -> None:
        codec = create_audio_frame_codec(
            modulation="pcoip-safe",
            sample_rate=48000,
            byte_repeat=3,
            marker_run=16,
        )
        payload = b"pcoip-ack-payload"
        pcm = codec.encode_frame(payload)
        frames = codec.feed_pcm(pcm)
        self.assertEqual(frames, [payload])


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class PCoIPCompressionSurvivalTests(unittest.TestCase):
    """Verify the pcoip-safe modem survives PCoIP audio codec compression.

    PCoIP uses two codecs depending on direction and client generation:

    OPUS  (host→client, modern clients — Tera2 FW 5.x, Software Client 1.4+)
    -----------------------------------------------------------------------
    Network BW   | Bitrate    | Quality
    > 10 Mbps    | 256 kbps   | Stereo high-quality
    125k–10 Mbps | 48–255 kbps| Stereo FM/AM quality
    ~125 kbps    | 32–47 kbps | Mono phone quality (minimum viable)

    ADPCM  (client→host microphone path; also host→client on legacy clients)
    -----------------------------------------------------------------------
    Network BW   | Sample rate | Channels | Codec BW
    8 Mbps       | 48 kHz      | stereo   | 1500 kbps  (16-bit PCM tier)
    2 Mbps       | 48 kHz      | stereo   | 400 kbps   (4-bit ADPCM)
    700 kbps     | 16 kHz      | mono     | 90 kbps
    125 kbps     | 8 kHz       | mono     | 60 kbps    (minimum viable)

    Each test encodes a frame → compresses through the codec → decompresses →
    feeds the PCM to a fresh decoder, verifying the frame is recovered.
    """

    _PAYLOAD = b"pcoip-integration-test-payload"

    def _make_codec(self) -> RobustFskFrameCodec:
        return create_audio_frame_codec(
            modulation="pcoip-safe",
            sample_rate=48000,
            byte_repeat=3,
            marker_run=16,
        )

    def _opus_roundtrip(self, pcm: bytes, bitrate_bps: int, channels: int = 1) -> bytes:
        """Encode s16le 48 kHz → OGG/OPUS → s16le 48 kHz.

        Uses application=audio (CELT mode) to preserve tonal content rather
        than the voice-optimised SILK mode that PCoIP would likely select for
        mono microphone audio.
        """
        enc = subprocess.run(
            [
                "ffmpeg", "-f", "s16le", "-ar", "48000", "-ac", str(channels),
                "-i", "pipe:0",
                "-c:a", "libopus", "-b:a", str(bitrate_bps),
                "-application", "audio",
                "-f", "ogg", "pipe:1",
            ],
            input=pcm,
            capture_output=True,
            check=True,
        )
        dec = subprocess.run(
            [
                "ffmpeg", "-f", "ogg", "-i", "pipe:0",
                "-f", "s16le", "-ar", "48000", "-ac", str(channels), "pipe:1",
            ],
            input=enc.stdout,
            capture_output=True,
            check=True,
        )
        return dec.stdout

    def _adpcm_roundtrip(
        self,
        pcm: bytes,
        sample_rate: int,
        adpcm_channels: int,
        *,
        pcm_channels: int = 1,
    ) -> bytes:
        """Encode s16le 48 kHz → IMA ADPCM WAV → s16le 48 kHz.

        adpcm_channels — number of channels in the compressed ADPCM stream,
            matching the PCoIP bandwidth tier (2 for ≥2 Mbps, 1 for ≤700 kbps).
        pcm_channels — number of channels in the input/output PCM, matching the
            codec.  When adpcm_channels < pcm_channels PCoIP downmixes on encode
            and upmixes on decode (both L and R become identical after the trip).

        ffmpeg resamples to the target sample_rate before encoding and back to
        48 kHz after decoding, simulating the PCoIP DSP pipeline.  IMA ADPCM
        (adpcm_ima_wav) uses 4 bits/sample — matching the PCoIP ADPCM table.
        """
        enc = subprocess.run(
            [
                "ffmpeg", "-f", "s16le", "-ar", "48000", "-ac", str(pcm_channels),
                "-i", "pipe:0",
                "-ar", str(sample_rate), "-ac", str(adpcm_channels),
                "-c:a", "adpcm_ima_wav",
                "-f", "wav", "pipe:1",
            ],
            input=pcm,
            capture_output=True,
            check=True,
        )
        dec = subprocess.run(
            [
                "ffmpeg", "-i", "pipe:0",
                "-f", "s16le", "-ar", "48000", "-ac", str(pcm_channels), "pipe:1",
            ],
            input=enc.stdout,
            capture_output=True,
            check=True,
        )
        return dec.stdout

    # ── OPUS tests ──────────────────────────────────────────────────────────

    def test_pcoip_safe_survives_opus_256kbps(self) -> None:
        """OPUS 256 kbps — high-quality tier, available on >10 Mbps links."""
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(self._opus_roundtrip(pcm, 256_000, channels=codec.channels))
        self.assertEqual(frames, [self._PAYLOAD])

    def test_pcoip_safe_survives_opus_48kbps(self) -> None:
        """OPUS 48 kbps — AM-radio quality, typical on 125 kbps–10 Mbps links."""
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(self._opus_roundtrip(pcm, 48_000, channels=codec.channels))
        self.assertEqual(frames, [self._PAYLOAD])

    def test_pcoip_safe_survives_opus_32kbps(self) -> None:
        """OPUS 32 kbps — phone quality, minimum viable PCoIP audio tier."""
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(self._opus_roundtrip(pcm, 32_000, channels=codec.channels))
        self.assertEqual(frames, [self._PAYLOAD])

    # ── ADPCM tests ─────────────────────────────────────────────────────────

    def test_pcoip_safe_survives_adpcm_48khz_stereo(self) -> None:
        """ADPCM 48 kHz stereo 4-bit — 2 Mbps network tier (400 kbps audio)."""
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(
            self._adpcm_roundtrip(pcm, 48000, adpcm_channels=codec.channels, pcm_channels=codec.channels)
        )
        self.assertEqual(frames, [self._PAYLOAD])

    def test_pcoip_safe_survives_adpcm_16khz_mono(self) -> None:
        """ADPCM 16 kHz mono 4-bit — 700 kbps network tier (90 kbps audio).

        PCoIP downmixes stereo to mono at this tier; the receive side upmixes
        back to stereo (L=R), which the stereo decoder can still decode.
        """
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(
            self._adpcm_roundtrip(pcm, 16000, adpcm_channels=1, pcm_channels=codec.channels)
        )
        self.assertEqual(frames, [self._PAYLOAD])

    def test_pcoip_safe_survives_adpcm_8khz_mono(self) -> None:
        """ADPCM 8 kHz mono 4-bit — 125 kbps network tier (60 kbps audio, minimum viable).

        PCoIP downmixes stereo to mono at this tier; the receive side upmixes
        back to stereo (L=R), which the stereo decoder can still decode.
        """
        codec = self._make_codec()
        pcm = codec.encode_frame(self._PAYLOAD)
        dec_codec = self._make_codec()
        frames = dec_codec.feed_pcm(
            self._adpcm_roundtrip(pcm, 8000, adpcm_channels=1, pcm_channels=codec.channels)
        )
        self.assertEqual(frames, [self._PAYLOAD])


if __name__ == "__main__":
    unittest.main()
