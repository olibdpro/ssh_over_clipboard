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
MODULATION_AUTO = "auto"
SUPPORTED_AUDIO_MODULATIONS = (
    MODULATION_LEGACY,
    MODULATION_ROBUST_V1,
    MODULATION_PCOIP_SAFE,
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
    if cleaned in {MODULATION_LEGACY, MODULATION_ROBUST_V1, MODULATION_PCOIP_SAFE}:
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
        # Bias toward higher throughput for interactive control traffic while keeping
        # enough redundancy/sync tolerance to survive typical remoting losses.
        return RobustFskFrameCodec(
            sample_rate=max(sample_rate, 8000),
            symbol_rate=1800,
            bit_repeat=3,
            amplitude=13000,
            preamble_pairs=8,
            start_gate_tail_symbols=8,
            start_max_errors=3,
            end_max_errors=2,
        )

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
    _DEFAULT_START_SYNC = [0, 1, 3, 2, 0, 2, 3, 1, 1, 3, 0, 2, 2, 0, 1, 3]
    _DEFAULT_END_SYNC = [3, 2, 0, 1, 3, 1, 0, 2, 2, 0, 3, 1, 1, 3, 2, 0]

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        symbol_rate: int = 1200,
        bit_repeat: int = 3,
        amplitude: int = 9000,
        preamble_pairs: int = 32,
        start_sync: list[int] | None = None,
        end_sync: list[int] | None = None,
        start_gate_tail_symbols: int = 16,
        start_max_errors: int = 2,
        end_max_errors: int = 1,
    ) -> None:
        self.sample_rate = max(sample_rate, 8000)
        self.symbol_rate = max(symbol_rate, 100)
        self.samples_per_symbol = max(int(round(self.sample_rate / float(self.symbol_rate))), 8)
        self.bit_repeat = max(bit_repeat, 1)
        self.amplitude = max(min(amplitude, 20000), 1000)
        self.preamble_pairs = max(preamble_pairs, 1)
        self.start_max_errors = max(start_max_errors, 0)
        self.end_max_errors = max(end_max_errors, 0)

        self._phase = 0.0
        self._sample_buffer = bytearray()
        self._symbol_buffer: list[int] = []

        self._symbol_bytes = self.samples_per_symbol * 2
        self._steps = tuple((2.0 * math.pi * freq) / float(self.sample_rate) for freq in self._FREQS)
        self._goertzel_coeffs = tuple(2.0 * math.cos(step) for step in self._steps)

        preamble_base = [0, 3] * self.preamble_pairs
        self._preamble = preamble_base[: self.preamble_pairs * 2]
        start_sync_values = list(start_sync or self._DEFAULT_START_SYNC)
        end_sync_values = list(end_sync or self._DEFAULT_END_SYNC)
        if not start_sync_values or not end_sync_values:
            raise AudioCodecError("Robust FSK sync patterns cannot be empty.")
        self._start_sync = [int(symbol) & 0x3 for symbol in start_sync_values]
        self._end_sync = [int(symbol) & 0x3 for symbol in end_sync_values]
        self.start_gate_tail_symbols = min(max(start_gate_tail_symbols, 1), len(self._preamble))
        self._start_gate = self._preamble[-self.start_gate_tail_symbols :] + self._start_sync

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
            start_idx = _find_symbol_pattern(
                self._symbol_buffer,
                self._start_gate,
                start=0,
                max_errors=self.start_max_errors,
            )
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
                max_errors=self.end_max_errors,
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
                samples.append(int(self.amplitude * math.sin(phase)))
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
            power = _goertzel_power(samples, coeff)
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
