"""Audio routing setup helpers for audio-modem transport."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


class AudioSetupError(RuntimeError):
    """Raised when audio setup operations fail."""


STATE_PATH = Path.home() / ".cache" / "sshg_audio_setup.json"


def _run_pactl(args: list[str]) -> str:
    result = subprocess.run(
        ["pactl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise AudioSetupError(f"pactl {' '.join(args)} failed: {stderr or 'unknown error'}")
    return (result.stdout or "").strip()


def _load_module(module_name: str, module_args: list[str]) -> int:
    output = _run_pactl(["load-module", module_name, *module_args])
    try:
        return int(output.strip())
    except ValueError as exc:
        raise AudioSetupError(f"Unexpected pactl module id: {output!r}") from exc


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"modules": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"modules": []}


def _write_state(state: dict[str, Any]) -> None:
    _ensure_parent(STATE_PATH)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _append_module_state(state: dict[str, Any], *, role: str, module_id: int, module_name: str) -> None:
    modules = state.setdefault("modules", [])
    modules.append(
        {
            "role": role,
            "module_id": module_id,
            "module_name": module_name,
        }
    )


def create_client_devices() -> None:
    state = _read_state()

    rx_sink = _load_module(
        "module-null-sink",
        [
            "sink_name=sshg_rx_sink",
            "sink_properties=device.description=sshg_rx_sink",
        ],
    )
    _append_module_state(state, role="client", module_id=rx_sink, module_name="module-null-sink")

    tx_sink = _load_module(
        "module-null-sink",
        [
            "sink_name=sshg_tx_sink",
            "sink_properties=device.description=sshg_tx_sink",
        ],
    )
    _append_module_state(state, role="client", module_id=tx_sink, module_name="module-null-sink")

    tx_mic = _load_module(
        "module-remap-source",
        [
            "source_name=sshg_tx_mic",
            "master=sshg_tx_sink.monitor",
            "source_properties=device.description=sshg_tx_mic",
        ],
    )
    _append_module_state(state, role="client", module_id=tx_mic, module_name="module-remap-source")

    _write_state(state)
    print("Created client audio devices:")
    print("- sink: sshg_rx_sink")
    print("- sink: sshg_tx_sink")
    print("- source: sshg_tx_mic (monitor of sshg_tx_sink)")


def create_server_devices() -> None:
    state = _read_state()

    vm_sink = _load_module(
        "module-null-sink",
        [
            "sink_name=sshg_vm_sink",
            "sink_properties=device.description=sshg_vm_sink",
        ],
    )
    _append_module_state(state, role="server", module_id=vm_sink, module_name="module-null-sink")

    vm_mic = _load_module(
        "module-remap-source",
        [
            "source_name=sshg_vm_mic",
            "master=sshg_vm_sink.monitor",
            "source_properties=device.description=sshg_vm_mic",
        ],
    )
    _append_module_state(state, role="server", module_id=vm_mic, module_name="module-remap-source")

    _write_state(state)
    print("Created server audio devices:")
    print("- sink: sshg_vm_sink")
    print("- source: sshg_vm_mic (monitor of sshg_vm_sink)")


def destroy_devices() -> None:
    state = _read_state()
    modules = state.get("modules", [])
    if not isinstance(modules, list) or not modules:
        print("No recorded sshg audio modules to unload.")
        return

    failures: list[str] = []
    for item in reversed(modules):
        module_id = item.get("module_id")
        if not isinstance(module_id, int):
            continue
        result = subprocess.run(
            ["pactl", "unload-module", str(module_id)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            failures.append(f"{module_id}: {stderr or 'unknown error'}")

    if failures:
        print("Some modules could not be unloaded:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
    else:
        print("Unloaded sshg audio modules.")

    _write_state({"modules": []})


def status() -> None:
    state = _read_state()
    print(f"State file: {STATE_PATH}")
    modules = state.get("modules", [])
    if isinstance(modules, list):
        print(f"Recorded modules: {len(modules)}")
        for item in modules:
            module_id = item.get("module_id")
            role = item.get("role")
            name = item.get("module_name")
            print(f"- id={module_id} role={role} module={name}")

    try:
        sinks = _run_pactl(["list", "short", "sinks"])
        sources = _run_pactl(["list", "short", "sources"])
    except AudioSetupError as exc:
        print(f"status warning: {exc}", file=sys.stderr)
        return

    print("\nMatching sinks:")
    for line in sinks.splitlines():
        if "sshg_" in line:
            print(f"- {line}")
    print("\nMatching sources:")
    for line in sources.splitlines():
        if "sshg_" in line:
            print(f"- {line}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg-audio-setup", description="Manage audio routing for sshg audio-modem")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-client-devices", help="Create local client sinks/sources")
    sub.add_parser("create-server-devices", help="Create VM/server sinks/sources")
    sub.add_parser("status", help="Print setup status")
    sub.add_parser("destroy", help="Unload recorded modules")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create-client-devices":
            create_client_devices()
            return 0
        if args.command == "create-server-devices":
            create_server_devices()
            return 0
        if args.command == "status":
            status()
            return 0
        if args.command == "destroy":
            destroy_devices()
            return 0
        parser.error(f"Unknown command {args.command}")
    except AudioSetupError as exc:
        print(f"sshg-audio-setup: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
