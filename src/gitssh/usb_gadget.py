"""Linux USB CDC gadget helper for local fake-device setup."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

_CONFIGFS = Path("/sys/kernel/config")
_GADGET_ROOT = _CONFIGFS / "usb_gadget"


class USBGadgetError(RuntimeError):
    """Raised when USB gadget lifecycle operations fail."""


def _require_root() -> None:
    if os.geteuid() != 0:
        raise USBGadgetError("Root privileges are required (run with sudo)")


def _configfs_is_mounted() -> bool:
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return False

    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        _source, mount_point, fs_type = parts[:3]
        if mount_point == "/sys/kernel/config" and fs_type == "configfs":
            return True
    return False


def _ensure_configfs_mounted() -> None:
    if not _CONFIGFS.exists():
        raise USBGadgetError("configfs path does not exist: /sys/kernel/config")

    if _configfs_is_mounted():
        return

    result = subprocess.run(
        ["mount", "-t", "configfs", "none", "/sys/kernel/config"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if _configfs_is_mounted() or "already mounted" in lowered or "mount point busy" in lowered:
            return
        raise USBGadgetError(f"Failed to mount configfs: {stderr or 'unknown error'}")


def _ensure_usb_gadget_subsystem() -> None:
    if _GADGET_ROOT.exists():
        return

    result = subprocess.run(
        ["modprobe", "libcomposite"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise USBGadgetError(
            "USB gadget subsystem is unavailable and loading libcomposite failed: "
            f"{stderr or 'unknown error'}"
        )

    if not _GADGET_ROOT.exists():
        raise USBGadgetError(
            "USB gadget configfs path is still unavailable after loading libcomposite. "
            "This kernel or hardware likely does not support USB gadget mode on this host."
        )


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="ascii")


def _list_udcs() -> list[str]:
    udc_root = Path("/sys/class/udc")
    if not udc_root.exists():
        return []
    return sorted(child.name for child in udc_root.iterdir() if child.is_dir())


def _gadget_path(name: str) -> Path:
    return _GADGET_ROOT / name


def create_gadget(
    *,
    name: str,
    id_vendor: str,
    id_product: str,
    serial_number: str,
    manufacturer: str,
    product: str,
    max_power_ma: int,
    udc: str | None,
) -> str:
    _require_root()
    _ensure_configfs_mounted()
    _ensure_usb_gadget_subsystem()

    udcs = _list_udcs()
    if not udcs:
        raise USBGadgetError(
            "No UDC controller found under /sys/class/udc; this host may not support USB gadget mode"
        )

    selected_udc = udc or udcs[0]
    if selected_udc not in udcs:
        raise USBGadgetError(f"UDC '{selected_udc}' is not available ({', '.join(udcs)})")

    base = _gadget_path(name)
    strings = base / "strings/0x409"
    config = base / "configs/c.1"
    config_strings = config / "strings/0x409"
    function = base / "functions/acm.usb0"
    link = config / "acm.usb0"

    base.mkdir(parents=True, exist_ok=True)
    _write_text(base / "idVendor", id_vendor)
    _write_text(base / "idProduct", id_product)
    _write_text(base / "bcdDevice", "0x0100")
    _write_text(base / "bcdUSB", "0x0200")

    strings.mkdir(parents=True, exist_ok=True)
    _write_text(strings / "serialnumber", serial_number)
    _write_text(strings / "manufacturer", manufacturer)
    _write_text(strings / "product", product)

    config.mkdir(parents=True, exist_ok=True)
    config_strings.mkdir(parents=True, exist_ok=True)
    _write_text(config_strings / "configuration", "CDC ACM")
    _write_text(config / "MaxPower", str(max(max_power_ma // 2, 1)))

    function.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(function)

    _write_text(base / "UDC", selected_udc)
    return selected_udc


def destroy_gadget(name: str) -> None:
    _require_root()
    base = _gadget_path(name)
    if not base.exists():
        return

    udc = base / "UDC"
    if udc.exists():
        try:
            _write_text(udc, "")
        except OSError:
            pass

    paths = [
        base / "configs/c.1/acm.usb0",
        base / "functions/acm.usb0",
        base / "configs/c.1/strings/0x409/configuration",
        base / "configs/c.1/strings/0x409",
        base / "configs/c.1/MaxPower",
        base / "configs/c.1",
        base / "strings/0x409/serialnumber",
        base / "strings/0x409/manufacturer",
        base / "strings/0x409/product",
        base / "strings/0x409",
        base / "idVendor",
        base / "idProduct",
        base / "bcdDevice",
        base / "bcdUSB",
        base / "UDC",
    ]
    for path in paths:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError:
            pass

    try:
        base.rmdir()
    except OSError as exc:
        raise USBGadgetError(f"Failed to remove gadget '{name}': {exc}") from exc


def gadget_status(name: str) -> tuple[bool, str | None]:
    base = _gadget_path(name)
    if not base.exists():
        return False, None

    udc_file = base / "UDC"
    if not udc_file.exists():
        return True, None
    value = udc_file.read_text(encoding="ascii").strip()
    return True, (value or None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshg-usb-gadget", description="Manage a local CDC ACM USB gadget")
    parser.add_argument("--name", default="sshg_cdc0", help="Gadget name under /sys/kernel/config/usb_gadget")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create and bind gadget")
    create.add_argument("--id-vendor", default="0x1209", help="USB vendor ID (hex string)")
    create.add_argument("--id-product", default="0x0001", help="USB product ID (hex string)")
    create.add_argument("--serial-number", default="sshg-serial-001", help="USB serial number string")
    create.add_argument("--manufacturer", default="sshg", help="USB manufacturer string")
    create.add_argument("--product", default="sshg CDC bridge", help="USB product string")
    create.add_argument("--max-power-ma", type=int, default=250, help="Advertised USB max power in mA")
    create.add_argument("--udc", help="Explicit UDC controller name (default: first available)")

    subparsers.add_parser("destroy", help="Unbind and remove gadget")
    subparsers.add_parser("status", help="Show gadget status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            selected_udc = create_gadget(
                name=args.name,
                id_vendor=args.id_vendor,
                id_product=args.id_product,
                serial_number=args.serial_number,
                manufacturer=args.manufacturer,
                product=args.product,
                max_power_ma=max(args.max_power_ma, 2),
                udc=args.udc,
            )
            print(f"Created gadget '{args.name}' and bound to UDC '{selected_udc}'")
            return 0

        if args.command == "destroy":
            destroy_gadget(args.name)
            print(f"Destroyed gadget '{args.name}'")
            return 0

        exists, bound_udc = gadget_status(args.name)
        if not exists:
            print(f"Gadget '{args.name}' does not exist")
            return 1
        if bound_udc:
            print(f"Gadget '{args.name}' is bound to UDC '{bound_udc}'")
            return 0
        print(f"Gadget '{args.name}' exists but is not bound to a UDC")
        return 0
    except USBGadgetError as exc:
        print(f"sshg-usb-gadget: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
