from __future__ import annotations

import pathlib
import sys
import threading
import time
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

    def test_opens_parallel_writer_and_listener_channels_for_all_devices(self) -> None:
        config = AudioDiscoveryConfig(
            timeout=0.25,
            ping_interval=0.01,
            found_interval=0.01,
            max_silent_seconds=0.2,
            idle_sleep=0.0,
        )

        open_calls: list[tuple[str, str]] = []

        def io_factory(in_dev: str, out_dev: str) -> AudioDuplexIO:
            open_calls.append((in_dev, out_dev))
            return _NoTrafficDuplexIO()

        with self.assertRaises(AudioIOError):
            discover_audio_devices(
                config,
                input_devices=["in_a", "in_b"],
                output_devices=["out_1", "out_2", "out_3"],
                io_factory=io_factory,
            )

        expected_open_sequence = [
            ("in_a", "out_1"),
            ("in_a", "out_2"),
            ("in_a", "out_3"),
            ("in_a", "out_1"),
            ("in_b", "out_1"),
        ]
        self.assertEqual(open_calls, expected_open_sequence)
        self.assertEqual({in_dev for in_dev, _out_dev in open_calls}, {"in_a", "in_b"})
        self.assertEqual({out_dev for _in_dev, out_dev in open_calls}, {"out_1", "out_2", "out_3"})

    def test_discovers_when_some_matrix_channels_fail_to_open(self) -> None:
        routes = {
            "c_out_good": ["s_in_good"],
            "s_out_good": ["c_in_good"],
            "c_out_bad": [],
            "s_out_bad": [],
        }
        network = _FakeAudioNetwork(routes=routes)

        config = AudioDiscoveryConfig(
            timeout=3.0,
            ping_interval=0.02,
            found_interval=0.02,
            candidate_grace=1.0,
            max_silent_seconds=1.0,
            idle_sleep=0.001,
        )

        results: dict[str, DiscoveredAudioDevices] = {}
        errors: dict[str, Exception] = {}

        def selective_factory(in_dev: str, out_dev: str) -> AudioDuplexIO:
            if in_dev.endswith("_bad") or out_dev.endswith("_bad"):
                raise AudioIOError("open rejected for test")
            return _FakeDuplexIO(
                network,
                input_device=in_dev,
                output_device=out_dev,
            )

        def run_client() -> None:
            try:
                results["client"] = discover_audio_devices(
                    config,
                    input_devices=["c_in_bad", "c_in_good"],
                    output_devices=["c_out_bad", "c_out_good"],
                    io_factory=selective_factory,
                )
            except Exception as exc:  # pragma: no cover
                errors["client"] = exc

        def run_server() -> None:
            try:
                results["server"] = discover_audio_devices(
                    config,
                    input_devices=["s_in_good", "s_in_bad"],
                    output_devices=["s_out_good", "s_out_bad"],
                    io_factory=selective_factory,
                )
            except Exception as exc:  # pragma: no cover
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
        self.assertEqual(results["client"].input_device, "c_in_good")
        self.assertEqual(results["client"].output_device, "c_out_good")
        self.assertEqual(results["server"].input_device, "s_in_good")
        self.assertEqual(results["server"].output_device, "s_out_good")

    def test_discovers_with_startup_skew(self) -> None:
        routes = {
            "c_out_good": ["s_in_good"],
            "s_out_good": ["c_in_good"],
        }
        network = _FakeAudioNetwork(routes=routes)

        config = AudioDiscoveryConfig(
            timeout=4.0,
            ping_interval=0.02,
            found_interval=0.02,
            candidate_grace=1.0,
            max_silent_seconds=1.0,
            idle_sleep=0.001,
        )

        results: dict[str, DiscoveredAudioDevices] = {}
        errors: dict[str, Exception] = {}

        def run_server() -> None:
            try:
                results["server"] = discover_audio_devices(
                    config,
                    input_devices=["s_in_good"],
                    output_devices=["s_out_good"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover
                errors["server"] = exc

        def run_client() -> None:
            time.sleep(0.35)
            try:
                results["client"] = discover_audio_devices(
                    config,
                    input_devices=["c_in_good"],
                    output_devices=["c_out_good"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover
                errors["client"] = exc

        t_server = threading.Thread(target=run_server)
        t_client = threading.Thread(target=run_client)
        t_server.start()
        t_client.start()
        t_server.join(timeout=6.0)
        t_client.join(timeout=6.0)

        self.assertFalse(t_server.is_alive(), "server discovery thread did not finish")
        self.assertFalse(t_client.is_alive(), "client discovery thread did not finish")
        self.assertFalse(errors, str(errors))
        self.assertEqual(results["server"].input_device, "s_in_good")
        self.assertEqual(results["server"].output_device, "s_out_good")
        self.assertEqual(results["client"].input_device, "c_in_good")
        self.assertEqual(results["client"].output_device, "c_out_good")

    def test_one_way_route_times_out_with_diagnostics(self) -> None:
        routes = {
            "a_out": ["b_in"],
            "b_out": [],
        }
        network = _FakeAudioNetwork(routes=routes)

        config = AudioDiscoveryConfig(
            timeout=1.6,
            ping_interval=0.02,
            found_interval=0.02,
            candidate_grace=0.2,
            max_silent_seconds=0.5,
            idle_sleep=0.001,
        )

        errors: dict[str, Exception] = {}

        def run_a() -> None:
            try:
                discover_audio_devices(
                    config,
                    input_devices=["a_in"],
                    output_devices=["a_out"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover
                errors["a"] = exc

        def run_b() -> None:
            try:
                discover_audio_devices(
                    config,
                    input_devices=["b_in"],
                    output_devices=["b_out"],
                    io_factory=lambda in_dev, out_dev: _FakeDuplexIO(
                        network,
                        input_device=in_dev,
                        output_device=out_dev,
                    ),
                )
            except Exception as exc:  # pragma: no cover
                errors["b"] = exc

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

        self.assertFalse(t_a.is_alive(), "thread a did not finish")
        self.assertFalse(t_b.is_alive(), "thread b did not finish")
        self.assertIn("a", errors)
        self.assertIn("b", errors)
        self.assertIsInstance(errors["a"], AudioIOError)
        self.assertIsInstance(errors["b"], AudioIOError)
        self.assertIn("Discovery stats:", str(errors["a"]))
        self.assertIn("Discovery stats:", str(errors["b"]))
        self.assertIn("pings_sent=", str(errors["a"]))
        self.assertIn("pings_sent=", str(errors["b"]))

    def test_prunes_old_pending_pings_without_crashing(self) -> None:
        config = AudioDiscoveryConfig(
            timeout=20.0,
            ping_interval=0.01,
            found_interval=0.01,
            max_silent_seconds=1.0,
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
