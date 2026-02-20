"""Canonical audio device names and backend-aware alias resolution."""

from __future__ import annotations

from typing import Literal


class AudioDeviceNameError(ValueError):
    """Raised when a requested audio device name violates naming rules."""


CLIENT_INPUT_BASE = "server_output_receiver"
CLIENT_OUTPUT_BASE = "client_response_sender"
SERVER_INPUT_BASE = "client_output_receiver"
SERVER_OUTPUT_BASE = "server_response_sender"

_ROLE_BASE_NAMES: tuple[str, ...] = (
    CLIENT_INPUT_BASE,
    CLIENT_OUTPUT_BASE,
    SERVER_INPUT_BASE,
    SERVER_OUTPUT_BASE,
)
ROLE_ALIAS_NAMES: tuple[str, ...] = _ROLE_BASE_NAMES

PULSE_SUFFIX = "_pulse"
ALSA_SUFFIX = "_alsa"
MONITOR_SUFFIX = ".monitor"

MANAGED_PULSE_SINK_NAMES: tuple[str, ...] = tuple(f"{base}{PULSE_SUFFIX}" for base in _ROLE_BASE_NAMES)
MANAGED_ALSA_DEVICE_NAMES: tuple[str, ...] = tuple(f"{base}{ALSA_SUFFIX}" for base in _ROLE_BASE_NAMES)

_MANAGED_PULSE_SET = set(MANAGED_PULSE_SINK_NAMES)
_MANAGED_ALSA_SET = set(MANAGED_ALSA_DEVICE_NAMES)
_ROLE_ALIAS_SET = set(ROLE_ALIAS_NAMES)

_LEGACY_DEVICE_NAMES = {
    "sshg_rx_sink",
    "sshg_rx_sink.monitor",
    "sshg_tx_sink",
    "sshg_tx_sink.monitor",
    "sshg_tx_mic",
    "sshg_vm_sink",
    "sshg_vm_sink.monitor",
    "sshg_vm_mic",
}

_PULSE_BACKENDS = {"", "auto", "pulse-cli", "pulse", "pipewire"}
_ALSA_BACKENDS = {"alsa"}


def backend_family(backend: str) -> Literal["pulse", "alsa", "other"]:
    """Return a coarse backend family used by name resolution rules."""

    value = (backend or "").strip().lower()
    if value in _PULSE_BACKENDS:
        return "pulse"
    if value in _ALSA_BACKENDS:
        return "alsa"
    return "other"


def resolve_input_device_name(*, requested: str, backend: str) -> str:
    """Resolve an input device alias to the concrete backend-specific name."""

    return _resolve_device_name(kind="input", requested=requested, backend=backend)


def resolve_output_device_name(*, requested: str, backend: str) -> str:
    """Resolve an output device alias to the concrete backend-specific name."""

    return _resolve_device_name(kind="output", requested=requested, backend=backend)


def _resolve_device_name(*, kind: Literal["input", "output"], requested: str, backend: str) -> str:
    value = (requested or "").strip()
    if not value:
        return value

    _reject_legacy_name(value)

    monitor_requested = value.endswith(MONITOR_SUFFIX)
    base = value[: -len(MONITOR_SUFFIX)] if monitor_requested else value
    _reject_legacy_name(base)

    if base in _ROLE_ALIAS_SET:
        if monitor_requested:
            raise AudioDeviceNameError(
                f"Role alias '{value}' must not include '{MONITOR_SUFFIX}'. "
                f"Use '{base}' and the concrete capture source will be resolved from --audio-backend."
            )
        return _resolve_role_alias(kind=kind, alias=base, backend=backend)

    family = backend_family(backend)
    if base in _MANAGED_PULSE_SET:
        if family != "pulse":
            raise AudioDeviceNameError(
                f"Audio device '{value}' is Pulse-specific (suffix '{PULSE_SUFFIX}') "
                f"but backend '{backend}' is not Pulse-based."
            )
        if kind == "input":
            return f"{base}{MONITOR_SUFFIX}"
        if monitor_requested:
            raise AudioDeviceNameError(
                f"Audio output device '{value}' must be the sink name '{base}', not a monitor source."
            )
        return base

    if base in _MANAGED_ALSA_SET:
        if family != "alsa":
            raise AudioDeviceNameError(
                f"Audio device '{value}' is ALSA-specific (suffix '{ALSA_SUFFIX}') "
                f"but backend '{backend}' is not ALSA."
            )
        if monitor_requested:
            raise AudioDeviceNameError(
                f"ALSA device '{value}' must not include '{MONITOR_SUFFIX}'. "
                "Use the literal ALSA device name."
            )
        return base

    if family == "alsa" and monitor_requested and value.endswith(MONITOR_SUFFIX):
        raise AudioDeviceNameError(
            f"ALSA device '{value}' must not include '{MONITOR_SUFFIX}'. "
            "Use a literal ALSA device name without monitor suffix."
        )

    return value


def is_managed_pulse_device_name(name: str) -> bool:
    """Return True when a Pulse sink/source name belongs to sshg-managed names."""

    value = (name or "").strip()
    if not value:
        return False
    if value in _MANAGED_PULSE_SET:
        return True
    if value.endswith(MONITOR_SUFFIX):
        return value[: -len(MONITOR_SUFFIX)] in _MANAGED_PULSE_SET
    return False


def _resolve_role_alias(*, kind: Literal["input", "output"], alias: str, backend: str) -> str:
    family = backend_family(backend)

    if family == "pulse":
        sink = f"{alias}{PULSE_SUFFIX}"
        if kind == "input":
            return f"{sink}{MONITOR_SUFFIX}"
        return sink

    if family == "alsa":
        return f"{alias}{ALSA_SUFFIX}"

    raise AudioDeviceNameError(
        f"Cannot resolve role alias '{alias}' for backend '{backend}'. "
        "Use --audio-backend pulse-cli/auto/pipewire/pulse or --audio-backend alsa, "
        "or pass a concrete backend device name."
    )


def _reject_legacy_name(name: str) -> None:
    if name not in _LEGACY_DEVICE_NAMES:
        return
    raise AudioDeviceNameError(
        f"Legacy audio device name '{name}' is no longer supported. "
        "Use role aliases without suffixes and let --audio-backend resolve concrete names: "
        "client input=server_output_receiver, client output=client_response_sender, "
        "server input=client_output_receiver, server output=server_response_sender."
    )
