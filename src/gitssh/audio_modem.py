"""Audio frame codec utilities for the audio-modem transport."""

from __future__ import annotations

from collections import Counter
import struct
from typing import List


class AudioCodecError(RuntimeError):
    """Raised when audio codec operations fail."""


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

