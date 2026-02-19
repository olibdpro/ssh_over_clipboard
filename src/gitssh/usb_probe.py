"""USB serial transport probe utility."""

from __future__ import annotations

import argparse
import glob
from pathlib import Path
import sys
import time

from .usb_serial_transport import USBSerialTransportBackend, USBSerialTransportConfig, USBSerialTransportError


def _candidate_ports() -> list[str]:
    patterns = (
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
        "/dev/ttyGS*",
    )
    ports: list[str] = []
    for pattern in patterns:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg-usb-probe", description="Probe USB serial transport readiness")
    parser.add_argument(
        "--serial-port",
        help="Serial device path to probe. If omitted, first detected candidate is used.",
    )
    parser.add_argument(
        "--serial-baud",
        type=int,
        default=3000000,
        help="Requested serial baud rate",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List candidate serial devices and exit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Seconds to keep probe channel open before success",
    )
    parser.add_argument(
        "--no-configure-tty",
        action="store_true",
        help="Do not apply raw termios settings to the serial port",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    candidates = _candidate_ports()
    if args.list:
        if not candidates:
            print("No serial candidates found (/dev/ttyACM*, /dev/ttyUSB*, /dev/ttyGS*)")
            return 1
        for port in candidates:
            print(port)
        return 0

    serial_port = args.serial_port or (candidates[0] if candidates else None)
    if not serial_port:
        print(
            "sshg-usb-probe: no serial port found; pass --serial-port or ensure /dev/ttyACM* is present",
            file=sys.stderr,
        )
        return 2

    if not Path(serial_port).exists():
        print(f"sshg-usb-probe: serial port does not exist: {serial_port}", file=sys.stderr)
        return 2

    backend = USBSerialTransportBackend(
        USBSerialTransportConfig(
            serial_port=serial_port,
            baud_rate=max(args.serial_baud, 1),
            configure_tty=not args.no_configure_tty,
        )
    )

    deadline = time.monotonic() + max(args.timeout, 0.0)
    try:
        while time.monotonic() < deadline:
            backend.fetch_inbound()
            backend.push_outbound()
            time.sleep(0.01)
    except USBSerialTransportError as exc:
        print(f"sshg-usb-probe: {exc}", file=sys.stderr)
        return 1
    finally:
        backend.close()

    print(f"USB serial probe succeeded: {serial_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
