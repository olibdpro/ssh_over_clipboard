from __future__ import annotations

import pathlib
import sys
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gitssh.audio_device_discovery import AudioDiscoveryConfig, DiscoveredAudioDevices, discover_audio_devices
from gitssh.audio_io_ffmpeg import AudioDuplexIO, AudioIOError


class _FakeAudioNetwork:
    def __init__(self, routes: dict[str, list[str]]) -> None:
        self._lock = threading.Lock()
        self._routes = routes
        self._buffers: dict[str, bytearray] = {}

    def emit(self, output_device: str, data: bytes) -> None:
        targets = self._routes.get(output_device, [])
        if not targets or not data:
            return
        with self._lock:
            for input_device in targets:
                self._buffers.setdefault(input_device, bytearray()).extend(data)

    def read(self, input_device: str, max_bytes: int) -> bytes:
        if max_bytes < 1:
            return b""
        with self._lock:
            buffer = self._buffers.setdefault(input_device, bytearray())
            if not buffer:
                return b""
            count = min(max_bytes, len(buffer))
            out = bytes(buffer[:count])
            del buffer[:count]
            return out


class _FakeDuplexIO(AudioDuplexIO):
    def __init__(self, network: _FakeAudioNetwork, *, input_device: str, output_device: str) -> None:
        self._network = network
        self._input_device = input_device
        self._output_device = output_device
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        return self._network.read(self._input_device, max_bytes)

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self._network.emit(self._output_device, data)

    def close(self) -> None:
        self._closed = True


class _NoTrafficDuplexIO(AudioDuplexIO):
    def read(self, max_bytes: int) -> bytes:
        del max_bytes
        return b""

    def write(self, data: bytes) -> None:
        del data

    def close(self) -> None:
        return


class AudioDeviceDiscoveryTests(unittest.TestCase):
    def test_discovers_bidirectional_device_pair_between_two_peers(self) -> None:
        routes = {
            "c_out_good": ["s_in_good"],
            "s_out_good": ["c_in_good"],
            "c_out_dead": [],
            "s_out_dead": [],
        }
        network = _FakeAudioNetwork(routes=routes)

        config = AudioDiscoveryConfig(
            timeout=3.0,
            ping_interval=0.02,
            idle_sleep=0.001,
        )

        results: dict[str, DiscoveredAudioDevices] = {}
        errors: dict[str, Exception] = {}

        def run_client() -> None:
            try:
                results["client"] = discover_audio_devices(
                    config,
                    input_devices=["c_in_dead", "c_in_good"],
                    output_devices=["c_out_dead", "c_out_good"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover - assertion below surfaces details
                errors["client"] = exc

        def run_server() -> None:
            try:
                results["server"] = discover_audio_devices(
                    config,
                    input_devices=["s_in_good", "s_in_dead"],
                    output_devices=["s_out_dead", "s_out_good"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover - assertion below surfaces details
                errors["server"] = exc

        t_client = threading.Thread(target=run_client)
        t_server = threading.Thread(target=run_server)
        t_client.start()
        t_server.start()
        t_client.join(timeout=5.0)
        t_server.join(timeout=5.0)

        self.assertFalse(t_client.is_alive(), "client discovery thread did not finish")
        self.assertFalse(t_server.is_alive(), "server discovery thread did not finish")
        self.assertFalse(errors, str(errors))

        client = results["client"]
        server = results["server"]

        self.assertEqual(client.input_device, "c_in_good")
        self.assertEqual(client.output_device, "c_out_good")
        self.assertEqual(server.input_device, "s_in_good")
        self.assertEqual(server.output_device, "s_out_good")

    def test_prunes_old_pending_pings_without_crashing(self) -> None:
        config = AudioDiscoveryConfig(
            timeout=20.0,
            ping_interval=0.01,
            idle_sleep=0.0,
        )

        ticks = {"now": 0.0}

        def fake_monotonic() -> float:
            ticks["now"] += 1.0
            return ticks["now"]

        with (
            mock.patch("gitssh.audio_device_discovery.time.monotonic", side_effect=fake_monotonic),
            mock.patch("gitssh.audio_device_discovery.time.sleep", return_value=None),
        ):
            with self.assertRaises(AudioIOError):
                discover_audio_devices(
                    config,
                    input_devices=["only_in"],
                    output_devices=["only_out"],
                    io_factory=lambda _in_dev, _out_dev: _NoTrafficDuplexIO(),
                )


if __name__ == "__main__":
    unittest.main()
