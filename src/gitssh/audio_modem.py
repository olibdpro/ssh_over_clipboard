"""Audio frame codec utilities for the audio-modem transport."""

from __future__ import annotations

from collections import Counter
import math
import struct
from typing import Protocol
import zlib


MODULATION_LEGACY = "legacy"
MODULATION_ROBUST_V1 = "robust-v1"
MODULATION_PCOIP_SAFE = "pcoip-safe"
MODULATION_OFDM = "ofdm"
MODULATION_AUTO = "auto"
SUPPORTED_AUDIO_MODULATIONS = (
    MODULATION_LEGACY,
    MODULATION_ROBUST_V1,
    MODULATION_PCOIP_SAFE,
    MODULATION_OFDM,
    MODULATION_AUTO,
)


class AudioCodecError(RuntimeError):
    """Raised when audio codec operations fail."""


class AudioModulationCodec(Protocol):
    """Codec interface shared by legacy and robust modulation profiles."""

    def encode_frame(self, frame: bytes) -> bytes:
        ...

    def feed_pcm(self, pcm: bytes) -> list[bytes]:
        ...

    def snapshot_stats(self) -> dict[str, int]:
        ...


def normalize_audio_modulation(value: str | None, *, allow_auto: bool = True) -> str:
    """Normalize modulation selector and validate known values."""

    cleaned = (value or "").strip().lower()
    if not cleaned:
        return MODULATION_AUTO if allow_auto else MODULATION_LEGACY
    if cleaned == MODULATION_AUTO:
        if allow_auto:
            return MODULATION_AUTO
        return MODULATION_LEGACY
    if cleaned in {MODULATION_LEGACY, MODULATION_ROBUST_V1, MODULATION_PCOIP_SAFE, MODULATION_OFDM}:
        return cleaned
    raise AudioCodecError(
        f"Unsupported audio modulation '{value}'. "
        f"Supported values: {', '.join(SUPPORTED_AUDIO_MODULATIONS)}"
    )


def create_audio_frame_codec(
    *,
    modulation: str,
    sample_rate: int,
    byte_repeat: int,
    marker_run: int,
) -> AudioModulationCodec:
    """Build a codec implementation for the requested modulation profile."""

    normalized = normalize_audio_modulation(modulation, allow_auto=True)
    effective = MODULATION_ROBUST_V1 if normalized == MODULATION_AUTO else normalized

    if effective == MODULATION_LEGACY:
        return AudioFrameCodec(
            byte_repeat=max(byte_repeat, 1),
            marker_run=max(marker_run, 4),
        )

    if effective == MODULATION_ROBUST_V1:
        return RobustFskFrameCodec(sample_rate=max(sample_rate, 8000))

    if effective == MODULATION_PCOIP_SAFE:
        # PCoIP voice channels commonly apply lossy coding and dynamic bandwidth shaping.
        # Trade throughput for resilience while keeping interactive setup latency practical.
        # Use low-frequency tone set (600–1800 Hz) to avoid PCoIP high-freq attenuation.
        # PCoIP audio path is mono-only; channels=1 avoids pw-play rejection of stereo input.
        return RobustFskFrameCodec(
            sample_rate=max(sample_rate, 8000),
            symbol_rate=900,
            bit_repeat=3,
            amplitude=13000,
            freqs=(600.0, 900.0, 1200.0, 1800.0),
            channels=1,
        )

    if effective == MODULATION_OFDM:
        # PCoIP audio path is mono-only; channels=1 avoids pw-play rejection of stereo input.
        return OfdmFrameCodec(sample_rate=48000, amplitude=13000, channels=1, bit_repeat=3)

    # Should never happen due normalization guard.
    raise AudioCodecError(f"Unsupported normalized audio modulation '{effective}'")


def _cobs_encode(data: bytes) -> bytes:
    if not data:
        return b"\x01"

    out = bytearray()
    idx = 0
    while idx < len(data):
        block_start = idx
        while idx < len(data) and data[idx] != 0 and (idx - block_start) < 254:
            idx += 1
        block_len = idx - block_start
        out.append(block_len + 1)
        out.extend(data[block_start:idx])
        if idx < len(data) and data[idx] == 0:
            idx += 1
    return bytes(out)


def _cobs_decode(data: bytes) -> bytes:
    if not data:
        raise AudioCodecError("Invalid empty COBS payload")

    out = bytearray()
    idx = 0
    while idx < len(data):
        code = data[idx]
        idx += 1
        if code == 0:
            raise AudioCodecError("Invalid COBS code 0")
        count = code - 1
        if idx + count > len(data):
            raise AudioCodecError("Truncated COBS block")
        if count:
            out.extend(data[idx : idx + count])
            idx += count
        if code < 0xFF and idx < len(data):
            out.append(0)
    return bytes(out)


class AudioFrameCodec:
    """Encodes/decodes binary link frames into a simple PCM symbol stream."""

    def __init__(
        self,
        *,
        byte_repeat: int = 3,
        marker_run: int = 16,
    ) -> None:
        self.byte_repeat = max(byte_repeat, 1)
        self.marker_run = max(marker_run, 4)
        self._samples = bytearray()

        self._start_marker = 30000
        self._end_marker = -30000

    def encode_frame(self, frame: bytes) -> bytes:
        payload = _cobs_encode(frame) + b"\x00"
        if self.byte_repeat > 1:
            repeated = bytearray()
            for value in payload:
                repeated.extend([value] * self.byte_repeat)
            payload = bytes(repeated)

        samples: list[int] = [self._start_marker] * self.marker_run
        for value in payload:
            # 256-step quantization keeps encoding/decoding simple.
            sample = (int(value) - 128) * 256
            samples.append(sample)
        samples.extend([self._end_marker] * self.marker_run)
        return struct.pack("<" + "h" * len(samples), *samples)

    def feed_pcm(self, pcm: bytes) -> list[bytes]:
        if not pcm:
            return []
        self._samples.extend(pcm)

        parsed_frames: list[bytes] = []
        while True:
            frame = self._extract_one_frame()
            if frame is None:
                break
            parsed_frames.append(frame)
        return parsed_frames

    def snapshot_stats(self) -> dict[str, int]:
        return {
            "frames_decoded": 0,
            "crc_failures": 0,
            "sync_hits": 0,
            "decode_failures": 0,
        }

    def _extract_one_frame(self) -> bytes | None:
        ints = self._samples_as_ints()
        if len(ints) < self.marker_run * 2:
            return None

        start_idx = self._find_marker(ints, self._start_marker, 0)
        if start_idx < 0:
            # Keep just enough trailing bytes so boundary-spanning markers can still match later.
            keep_samples = max(self.marker_run * 2, 8)
            if len(ints) > keep_samples:
                drop = len(ints) - keep_samples
                del self._samples[: drop * 2]
            return None

        payload_start = start_idx + self.marker_run
        end_idx = self._find_marker(ints, self._end_marker, payload_start)
        if end_idx < 0:
            # Need more audio data.
            if start_idx > 0:
                del self._samples[: start_idx * 2]
            return None

        payload_samples = ints[payload_start:end_idx]
        del self._samples[: (end_idx + self.marker_run) * 2]
        frame = self._decode_payload_samples(payload_samples)
        if frame is None:
            return b""
        return frame

    def _samples_as_ints(self) -> list[int]:
        if len(self._samples) % 2 != 0:
            # Keep a trailing byte for next call.
            self._samples.pop()
        if not self._samples:
            return []
        count = len(self._samples) // 2
        return list(struct.unpack("<" + "h" * count, self._samples))

    def _find_marker(self, samples: list[int], marker: int, start: int) -> int:
        tolerance = 2000
        run_needed = self.marker_run
        run = 0
        first_idx = -1
        for idx in range(start, len(samples)):
            if abs(samples[idx] - marker) <= tolerance:
                if run == 0:
                    first_idx = idx
                run += 1
                if run >= run_needed:
                    return first_idx
            else:
                run = 0
                first_idx = -1
        return -1

    def _decode_payload_samples(self, payload_samples: list[int]) -> bytes | None:
        if not payload_samples:
            return None

        raw_bytes = bytearray()
        for sample in payload_samples:
            value = int(round(sample / 256.0)) + 128
            if value < 0:
                value = 0
            if value > 255:
                value = 255
            raw_bytes.append(value)

        if self.byte_repeat > 1:
            decoded = bytearray()
            step = self.byte_repeat
            for idx in range(0, len(raw_bytes), step):
                group = raw_bytes[idx : idx + step]
                if len(group) < step:
                    break
                most_common = Counter(group).most_common(1)
                if not most_common:
                    continue
                decoded.append(most_common[0][0])
            raw_bytes = decoded

        try:
            terminator = raw_bytes.index(0)
        except ValueError:
            return None

        encoded = bytes(raw_bytes[:terminator])
        if not encoded:
            return None
        try:
            return _cobs_decode(encoded)
        except AudioCodecError:
            return None


class RobustFskFrameCodec:
    """More resilient voice-band 4-FSK framing for lossy/processed links."""

    _FREQS = (1200.0, 1800.0, 2400.0, 3000.0)

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        symbol_rate: int = 1200,
        bit_repeat: int = 3,
        amplitude: int = 9000,
        freqs: tuple[float, float, float, float] | None = None,
        channels: int = 1,
    ) -> None:
        self.sample_rate = max(sample_rate, 8000)
        self.symbol_rate = max(symbol_rate, 100)
        self.samples_per_symbol = max(int(round(self.sample_rate / float(self.symbol_rate))), 8)
        self.bit_repeat = max(bit_repeat, 1)
        self.amplitude = max(min(amplitude, 20000), 1000)
        self.channels = max(channels, 1)

        if freqs is not None:
            if len(freqs) != 4 or any(f <= 0 for f in freqs):
                raise AudioCodecError("freqs must be a tuple of exactly 4 positive floats.")
            self._freqs: tuple[float, ...] = tuple(float(f) for f in freqs)
        else:
            self._freqs = self._FREQS

        self._phase = 0.0
        self._sample_buffer = bytearray()
        self._symbol_buffer: list[int] = []

        self._symbol_bytes = self.samples_per_symbol * 2 * self.channels
        self._steps = tuple((2.0 * math.pi * freq) / float(self.sample_rate) for freq in self._freqs)
        self._goertzel_coeffs = tuple(2.0 * math.cos(step) for step in self._steps)

        preamble_base = [0, 3] * 32
        self._preamble = preamble_base[:64]
        self._start_sync = [0, 1, 3, 2, 0, 2, 3, 1, 1, 3, 0, 2, 2, 0, 1, 3]
        self._end_sync = [3, 2, 0, 1, 3, 1, 0, 2, 2, 0, 3, 1, 1, 3, 2, 0]
        self._start_gate = self._preamble[-16:] + self._start_sync

        self._frames_decoded = 0
        self._crc_failures = 0
        self._sync_hits = 0
        self._decode_failures = 0

    def encode_frame(self, frame: bytes) -> bytes:
        payload = frame
        header = struct.pack("!H", len(payload))
        body = header + payload
        crc = zlib.crc32(body) & 0xFFFFFFFF
        packet = body + struct.pack("!I", crc)

        bits = _bits_from_bytes(packet)
        if self.bit_repeat > 1:
            repeated: list[int] = []
            for bit in bits:
                repeated.extend([bit] * self.bit_repeat)
            bits = repeated

        if len(bits) % 2 != 0:
            bits.append(0)

        symbols: list[int] = []
        for idx in range(0, len(bits), 2):
            symbols.append((bits[idx] << 1) | bits[idx + 1])

        stream = self._preamble + self._start_sync + symbols + self._end_sync
        return self._encode_symbols_to_pcm(stream)

    def feed_pcm(self, pcm: bytes) -> list[bytes]:
        if not pcm:
            return []

        self._sample_buffer.extend(pcm)
        self._demodulate_samples_to_symbols()

        frames: list[bytes] = []
        while True:
            start_idx = _find_symbol_pattern(self._symbol_buffer, self._start_gate, start=0, max_errors=2)
            if start_idx < 0:
                keep = max(len(self._start_gate) * 2, 256)
                if len(self._symbol_buffer) > keep:
                    del self._symbol_buffer[: len(self._symbol_buffer) - keep]
                break

            data_start = start_idx + len(self._start_gate)
            end_idx = _find_symbol_pattern(
                self._symbol_buffer,
                self._end_sync,
                start=data_start,
                max_errors=1,
            )
            if end_idx < 0:
                if start_idx > 0:
                    del self._symbol_buffer[:start_idx]
                break

            self._sync_hits += 1
            data_symbols = self._symbol_buffer[data_start:end_idx]
            del self._symbol_buffer[: end_idx + len(self._end_sync)]

            frame = self._decode_frame_symbols(data_symbols)
            if frame is None:
                self._decode_failures += 1
                continue

            self._frames_decoded += 1
            frames.append(frame)

        return frames

    def snapshot_stats(self) -> dict[str, int]:
        return {
            "frames_decoded": self._frames_decoded,
            "crc_failures": self._crc_failures,
            "sync_hits": self._sync_hits,
            "decode_failures": self._decode_failures,
        }

    def _encode_symbols_to_pcm(self, symbols: list[int]) -> bytes:
        samples: list[int] = []
        phase = self._phase
        for symbol in symbols:
            step = self._steps[int(symbol) & 0x3]
            for _ in range(self.samples_per_symbol):
                s = int(self.amplitude * math.sin(phase))
                for _ch in range(self.channels):
                    samples.append(s)
                phase += step
                if phase >= 2.0 * math.pi:
                    phase -= 2.0 * math.pi
        self._phase = phase
        return struct.pack("<" + "h" * len(samples), *samples)

    def _demodulate_samples_to_symbols(self) -> None:
        while len(self._sample_buffer) >= self._symbol_bytes:
            raw = bytes(self._sample_buffer[: self._symbol_bytes])
            del self._sample_buffer[: self._symbol_bytes]
            symbol = self._detect_symbol(raw)
            self._symbol_buffer.append(symbol)

    def _detect_symbol(self, raw: bytes) -> int:
        count = len(raw) // 2
        if count <= 0:
            return 0
        samples = struct.unpack("<" + "h" * count, raw)

        best_idx = 0
        best_power = float("-inf")
        for idx, coeff in enumerate(self._goertzel_coeffs):
            power = sum(
                _goertzel_power(samples[ch :: self.channels], coeff)
                for ch in range(self.channels)
            )
            if power > best_power:
                best_power = power
                best_idx = idx
        return best_idx

    def _decode_frame_symbols(self, symbols: list[int]) -> bytes | None:
        if not symbols:
            return None

        bits: list[int] = []
        for symbol in symbols:
            value = int(symbol) & 0x3
            bits.append((value >> 1) & 0x1)
            bits.append(value & 0x1)

        if self.bit_repeat > 1:
            decoded: list[int] = []
            step = self.bit_repeat
            for idx in range(0, len(bits), step):
                group = bits[idx : idx + step]
                if len(group) < step:
                    break
                ones = sum(group)
                decoded.append(1 if ones * 2 >= len(group) else 0)
            bits = decoded

        packet = _bytes_from_bits(bits)
        if len(packet) < 6:
            return None

        payload_len = struct.unpack("!H", packet[:2])[0]
        needed = 2 + payload_len + 4
        if len(packet) < needed:
            return None

        body = packet[: 2 + payload_len]
        crc_expected = struct.unpack("!I", packet[2 + payload_len : needed])[0]
        crc_actual = zlib.crc32(body) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            self._crc_failures += 1
            return None

        return body[2:]


class OfdmFrameCodec:
    """OFDM BPSK audio modem — 3 subcarriers at 600/1200/1800 Hz, 1800 bps raw.

    Encodes data as phase (0°/180°) across three orthogonal subcarriers so that
    lossy compression (OPUS/ADPCM) preserves the signal; amplitude compression
    does not affect BPSK phase decoding via Goertzel I-component.
    """

    _SUBCARRIER_FREQS = (600.0, 1200.0, 1800.0)
    # Require the full preamble (64 symbols) so the power-based end-of-preamble
    # estimate is within ±1 symbol of reality, keeping the start-sync search small.
    _PREAMBLE_DETECT_MIN = 64
    # Minimum per-carrier per-channel Goertzel power to count a window as signal
    # (not silence/noise).  Expected signal ≈ 10^9; silence ≈ 0; noise ≈ 10^2.
    _PREAMBLE_POWER_THRESHOLD = 1e5
    # How many sample-frames to sweep when searching for start-sync.
    # Must exceed worst-case alignment error (≤ symbol_samples = 80 frames = 320 B
    # for stereo) plus the encoder preamble end position uncertainty (≤ 1 symbol).
    # Worst-case start-sync position in the trimmed buffer =
    # 2 × symbol_bytes + (signal_start % symbol_bytes) < 3 × symbol_bytes.
    _START_SYNC_SWEEP_FRAMES = 240  # = 3 × symbol_samples, guaranteed coverage
    _START_SYNC_MAX_BIT_ERRORS = 2
    _END_SYNC_MAX_BIT_ERRORS = 1

    # 16-symbol (48-bit) sync sequences — one bit per subcarrier.
    _START_SYNC_BITS = [
        0, 1, 0,  1, 0, 1,  0, 0, 1,  1, 1, 0,  0, 1, 1,  1, 0, 0,  0, 1, 0,  1, 1, 1,
        1, 0, 1,  0, 0, 0,  1, 1, 0,  0, 0, 1,  0, 1, 1,  1, 1, 1,  0, 0, 0,  1, 0, 1,
    ]
    _END_SYNC_BITS = [
        1, 0, 0,  0, 1, 0,  1, 1, 0,  0, 0, 1,  1, 0, 0,  0, 1, 1,  1, 0, 1,  0, 0, 0,
        0, 1, 1,  1, 0, 1,  0, 0, 1,  1, 1, 0,  1, 0, 0,  0, 0, 1,  1, 1, 0,  0, 1, 0,
    ]

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        amplitude: int = 13000,
        channels: int = 2,
        bit_repeat: int = 1,
    ) -> None:
        if sample_rate != 48000:
            raise AudioCodecError(
                f"OfdmFrameCodec requires sample_rate=48000, got {sample_rate}"
            )
        self.sample_rate = sample_rate
        self.amplitude = max(min(amplitude, 20000), 1000)
        self.channels = max(channels, 1)
        self.bit_repeat = max(bit_repeat, 1)

        self._n_subcarriers = len(self._SUBCARRIER_FREQS)
        self.symbol_samples = 48000 // 600  # = 80
        self._symbol_bytes = self.symbol_samples * 2 * self.channels

        self._steps = tuple(
            (2.0 * math.pi * freq) / float(self.sample_rate)
            for freq in self._SUBCARRIER_FREQS
        )
        self._goertzel_coeffs = tuple(2.0 * math.cos(step) for step in self._steps)

        self._state = "HUNT_PREAMBLE"
        self._preamble_run = 0
        self._sample_buffer = bytearray()
        self._data_bits: list[int] = []

        self._frames_decoded = 0
        self._crc_failures = 0
        self._sync_hits = 0
        self._decode_failures = 0

    def encode_frame(self, frame: bytes) -> bytes:
        header = struct.pack("!H", len(frame))
        body = header + frame
        crc = zlib.crc32(body) & 0xFFFFFFFF
        packet = body + struct.pack("!I", crc)

        bits = _bits_from_bytes(packet)
        # Repeat each bit for majority-vote error correction
        if self.bit_repeat > 1:
            bits = [b for b in bits for _ in range(self.bit_repeat)]
        # Pad to multiple of n_subcarriers
        rem = len(bits) % self._n_subcarriers
        if rem:
            bits.extend([0] * (self._n_subcarriers - rem))

        # Group into data symbols
        data_symbols = [
            bits[i : i + self._n_subcarriers]
            for i in range(0, len(bits), self._n_subcarriers)
        ]

        # Build full stream: 64 preamble + 16 start_sync + data + 16 end_sync
        preamble_symbols = [[0] * self._n_subcarriers] * 64
        start_sync_symbols = [
            self._START_SYNC_BITS[i : i + self._n_subcarriers]
            for i in range(0, len(self._START_SYNC_BITS), self._n_subcarriers)
        ]
        end_sync_symbols = [
            self._END_SYNC_BITS[i : i + self._n_subcarriers]
            for i in range(0, len(self._END_SYNC_BITS), self._n_subcarriers)
        ]

        all_symbols = preamble_symbols + start_sync_symbols + data_symbols + end_sync_symbols
        return self._encode_ofdm_symbols_to_pcm(all_symbols)

    def _encode_ofdm_symbols_to_pcm(self, symbols: list[list[int]]) -> bytes:
        scale = self.amplitude / float(self._n_subcarriers)
        samples: list[int] = []
        for sym_bits in symbols:
            for t in range(self.symbol_samples):
                s = sum(
                    (-1 if b else 1) * math.cos(step * t)
                    for b, step in zip(sym_bits, self._steps)
                )
                sample = int(scale * s)
                for _ in range(self.channels):
                    samples.append(sample)
        return struct.pack("<" + "h" * len(samples), *samples)

    def feed_pcm(self, pcm: bytes) -> list[bytes]:
        if not pcm:
            return []
        self._sample_buffer.extend(pcm)
        frames: list[bytes] = []
        self._process_buffer(frames)
        return frames

    def _process_buffer(self, frames: list[bytes]) -> None:
        while True:
            prev_state = self._state
            if self._state == "HUNT_PREAMBLE":
                self._hunt_preamble()
            elif self._state == "HUNT_START_SYNC":
                self._hunt_start_sync()
            elif self._state == "COLLECT_DATA":
                self._collect_data(frames)
            # Stop if we didn't transition (no progress) or not enough data
            if self._state == prev_state:
                break

    def _hunt_preamble(self) -> None:
        # Use POWER (alignment-independent) to detect _PREAMBLE_DETECT_MIN=64
        # consecutive signal windows.  Power is invariant to the sample-phase
        # offset so we don't need to try multiple alignments here.
        # After detecting the full preamble the start-sync follows within
        # ±symbol_bytes; _hunt_start_sync sweeps _START_SYNC_SWEEP_FRAMES exactly.
        #
        # Re-scan the entire buffer from pos=0 each call so that preamble_end is
        # computed from the correct absolute buffer position (no cross-call state).
        run = 0
        pos = 0
        while pos + self._symbol_bytes <= len(self._sample_buffer):
            raw = self._sample_buffer[pos : pos + self._symbol_bytes]
            count = len(raw) // 2
            samples = struct.unpack("<" + "h" * count, raw)
            power = sum(
                _goertzel_power(samples[ch :: self.channels], coeff)
                for ch in range(self.channels)
                for coeff in self._goertzel_coeffs
            )
            if power > self._PREAMBLE_POWER_THRESHOLD:
                run += 1
                if run >= self._PREAMBLE_DETECT_MIN:
                    # Estimated preamble end = pos + symbol_bytes (within ±1 symbol).
                    # Trim to 2 symbols before that so _hunt_start_sync's sweep
                    # starts before the actual start-sync boundary.
                    preamble_end = pos + self._symbol_bytes
                    trim_to = max(0, preamble_end - 2 * self._symbol_bytes)
                    del self._sample_buffer[:trim_to]
                    self._preamble_run = 0
                    self._state = "HUNT_START_SYNC"
                    return
            else:
                run = 0
            pos += self._symbol_bytes

        # No preamble found — bound memory, but only when there is no active run
        # (trimming mid-run would discard the beginning of the preamble).
        if run == 0:
            max_keep = self._PREAMBLE_DETECT_MIN * 2 * self._symbol_bytes
            if len(self._sample_buffer) > max_keep:
                del self._sample_buffer[: len(self._sample_buffer) - max_keep]

    def _hunt_start_sync(self) -> None:
        n_sync_symbols = len(self._START_SYNC_BITS) // self._n_subcarriers  # 16
        # Sweep _START_SYNC_SWEEP_FRAMES sample-frame positions (step = 1 frame =
        # 2*channels bytes) to cover the ±symbol_bytes uncertainty from preamble
        # detection.  Trying every sample-frame offset makes alignment explicit
        # and avoids the phase-sensitivity issues of the old 80-offset approach.
        sweep_bytes = self._START_SYNC_SWEEP_FRAMES * 2 * self.channels
        needed = sweep_bytes + n_sync_symbols * self._symbol_bytes
        if len(self._sample_buffer) < needed:
            return  # Wait for more data

        best_errors = self._START_SYNC_MAX_BIT_ERRORS + 1
        best_abs_i = -1.0  # tiebreaker: prefer stronger I-components
        best_byte_delta = 0
        frame_bytes = 2 * self.channels

        # Primary key: fewest bit errors.  Secondary (tiebreaker): maximum sum of
        # absolute Goertzel I-components.  A correctly-aligned window has full
        # ±amplitude magnitude; a window that overlaps preamble (all-zero phase)
        # or the wrong symbol has a partially-cancelled, smaller I-magnitude.
        # This reliably distinguishes the true start position from spurious matches
        # that arise because an all-zero preamble prefix never flips the I sign.
        for byte_delta in range(0, sweep_bytes + frame_bytes, frame_bytes):
            if byte_delta + n_sync_symbols * self._symbol_bytes > len(self._sample_buffer):
                break
            errors = 0
            total_abs_i = 0.0
            for sym_idx in range(n_sync_symbols):
                pos = byte_delta + sym_idx * self._symbol_bytes
                i_vals = self._decode_ofdm_i_at(pos)
                for iv, exp in zip(
                    i_vals,
                    self._START_SYNC_BITS[
                        sym_idx * self._n_subcarriers : (sym_idx + 1) * self._n_subcarriers
                    ],
                ):
                    if (iv >= 0) != (exp == 0):
                        errors += 1
                    total_abs_i += abs(iv)
            if errors < best_errors or (errors == best_errors and total_abs_i > best_abs_i):
                best_errors = errors
                best_abs_i = total_abs_i
                best_byte_delta = byte_delta

        if best_errors <= self._START_SYNC_MAX_BIT_ERRORS:
            self._sync_hits += 1
            data_start_abs = best_byte_delta + n_sync_symbols * self._symbol_bytes
            del self._sample_buffer[:data_start_abs]
            self._data_bits = []
            self._state = "COLLECT_DATA"
        else:
            # No match — go back to hunting preamble
            self._state = "HUNT_PREAMBLE"

    def _collect_data(self, frames: list[bytes]) -> None:
        n_end_sync_bits = len(self._END_SYNC_BITS)
        # End-sync symbols are NOT bit-repeated; only the payload data is.
        pos = 0
        while pos + self._symbol_bytes <= len(self._sample_buffer):
            bits = self._decode_ofdm_bits_at(pos)
            self._data_bits.extend(bits)
            pos += self._symbol_bytes

            # Check last n_end_sync_bits raw bits against end sync
            if len(self._data_bits) >= n_end_sync_bits:
                tail = self._data_bits[-n_end_sync_bits:]
                errors = sum(1 for a, b in zip(tail, self._END_SYNC_BITS) if a != b)
                if errors <= self._END_SYNC_MAX_BIT_ERRORS:
                    data_bits_raw = self._data_bits[:-n_end_sync_bits]
                    frame = self._decode_frame_bits(data_bits_raw)
                    if frame is not None:
                        self._frames_decoded += 1
                        frames.append(frame)
                    else:
                        self._decode_failures += 1
                    del self._sample_buffer[:pos]
                    self._data_bits = []
                    self._state = "HUNT_PREAMBLE"
                    return

        del self._sample_buffer[:pos]

    def _decode_ofdm_i_at(self, byte_pos: int) -> list[float]:
        """Return raw Goertzel I-component for each subcarrier (summed over channels)."""
        raw = self._sample_buffer[byte_pos : byte_pos + self._symbol_bytes]
        count = len(raw) // 2
        if count == 0:
            return [0.0] * self._n_subcarriers
        samples = struct.unpack("<" + "h" * count, raw)
        return [
            sum(
                _goertzel_real(samples[ch :: self.channels], coeff)
                for ch in range(self.channels)
            )
            for coeff in self._goertzel_coeffs
        ]

    def _decode_ofdm_bits_at(self, byte_pos: int) -> list[int]:
        raw = self._sample_buffer[byte_pos : byte_pos + self._symbol_bytes]
        count = len(raw) // 2
        if count == 0:
            return [0] * self._n_subcarriers
        samples = struct.unpack("<" + "h" * count, raw)
        return [
            0 if sum(
                _goertzel_real(samples[ch :: self.channels], coeff)
                for ch in range(self.channels)
            ) >= 0 else 1
            for coeff in self._goertzel_coeffs
        ]

    def _decode_frame_bits(self, bits: list[int]) -> bytes | None:
        if self.bit_repeat > 1:
            bits = _majority_vote(bits, self.bit_repeat)
        packet = _bytes_from_bits(bits)
        if len(packet) < 6:
            return None

        payload_len = struct.unpack("!H", packet[:2])[0]
        needed = 2 + payload_len + 4
        if len(packet) < needed:
            return None

        body = packet[: 2 + payload_len]
        crc_expected = struct.unpack("!I", packet[2 + payload_len : needed])[0]
        crc_actual = zlib.crc32(body) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            self._crc_failures += 1
            return None

        return body[2:]

    def snapshot_stats(self) -> dict[str, int]:
        return {
            "frames_decoded": self._frames_decoded,
            "crc_failures": self._crc_failures,
            "sync_hits": self._sync_hits,
            "decode_failures": self._decode_failures,
        }


def _majority_vote(bits: list[int], repeat: int) -> list[int]:
    """Collapse a bit list that was encoded with `repeat` copies per bit.

    For each group of `repeat` consecutive bits, vote: 1 if majority are 1,
    else 0.  Trailing incomplete groups are dropped.
    """
    out: list[int] = []
    for i in range(0, len(bits) - repeat + 1, repeat):
        group = bits[i : i + repeat]
        out.append(1 if sum(group) > repeat // 2 else 0)
    return out


def _bits_from_bytes(data: bytes) -> list[int]:
    bits: list[int] = []
    for value in data:
        for shift in range(7, -1, -1):
            bits.append((value >> shift) & 0x1)
    return bits


def _bytes_from_bits(bits: list[int]) -> bytes:
    if not bits:
        return b""

    byte_count = len(bits) // 8
    out = bytearray()
    for idx in range(byte_count):
        value = 0
        base = idx * 8
        for offset in range(8):
            value = (value << 1) | (bits[base + offset] & 0x1)
        out.append(value)
    return bytes(out)


def _find_symbol_pattern(
    symbols: list[int],
    pattern: list[int],
    *,
    start: int,
    max_errors: int,
) -> int:
    if not pattern:
        return -1
    if start < 0:
        start = 0

    last_start = len(symbols) - len(pattern)
    if last_start < start:
        return -1

    for idx in range(start, last_start + 1):
        errors = 0
        for pat_idx, expected in enumerate(pattern):
            if symbols[idx + pat_idx] != expected:
                errors += 1
                if errors > max_errors:
                    break
        if errors <= max_errors:
            return idx
    return -1


def _goertzel_power(samples: tuple[int, ...], coeff: float) -> float:
    s_prev = 0.0
    s_prev2 = 0.0
    for sample in samples:
        value = float(sample)
        s = value + (coeff * s_prev) - s_prev2
        s_prev2 = s_prev
        s_prev = s
    return (s_prev2 * s_prev2) + (s_prev * s_prev) - (coeff * s_prev * s_prev2)


def _goertzel_real(samples: tuple[int, ...], coeff: float) -> float:
    """Real (in-phase) DFT component. coeff = 2*cos(step), same as _goertzel_power."""
    s1, s2 = 0.0, 0.0
    for x in samples:
        s1, s2 = float(x) + coeff * s1 - s2, s1
    return s1 * (coeff / 2.0) - s2
