#!/usr/bin/env python3
"""
alfa-txpower-keeper
===================
Keep the ALFA AWUS036ACHM (USB VID:PID 0e8d:7610) at the target TX power.

Primary method:  patch the mt76 debugfs EEPROM at offset 0x52 with value 0x1e.
Secondary method: continuously re-apply `iw dev <iface> set txpower fixed 2000`.

The correct phy is discovered at runtime by matching the USB device against
/sys/class/ieee80211/phy*/device. Phy numbers are never hardcoded.

DISCLAIMER: 0e8d:7610 is a generic MediaTek MT7610U ID, not unique to ALFA.
Other adapters share this VID:PID. Verify your hardware.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import re
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__version__ = "1.0.0"

VENDOR_ID = "0e8d"
PRODUCT_ID = "7610"
DEFAULT_INTERFACE = None
DEFAULT_TARGET_DBM = 20
DEFAULT_INTERVAL = 3
DEFAULT_EEPROM_OFFSET = 0x52
DEFAULT_EEPROM_VALUE = 0x1E
DEFAULT_MAX_RETRIES = 30
DEFAULT_RETRY_DELAY = 1

SYSFS_USB = Path("/sys/bus/usb/devices")
SYSFS_IEEE80211 = Path("/sys/class/ieee80211")
DEBUGFS_BASE = Path("/sys/kernel/debug")

INSTALL_DIR = Path("/opt/alfa-txpower-keeper")
SERVICE_NAME = "alfa-txpower-keeper.service"
UDEV_RULE = "99-alfa-txpower-keeper.rules"
SBIN_LINK = Path("/usr/local/sbin/alfa-txpower-keeper")


@dataclass
class AdapterInfo:
    vendor_id: str
    product_id: str
    busnum: Optional[str] = None
    devnum: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    sysfs_path: Optional[Path] = None


@dataclass
class PhyInfo:
    phy: str
    interface: Optional[str] = None
    eeprom_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("alfa-txpower-keeper")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    fmt = logging.Formatter("%(name)s[%(process)d]: %(levelname)s - %(message)s")

    syslog_address = "/dev/log" if Path("/dev/log").exists() else ("localhost", 514)
    try:
        logger.addHandler(logging.handlers.SysLogHandler(address=syslog_address))
    except OSError:
        pass

    if sys.stdout.isatty():
        logger.addHandler(logging.StreamHandler(sys.stdout))

    return logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(*cmd: str, check: bool = False, timeout: int = 30) -> subprocess.CompletedProcess:
    logger = logging.getLogger("alfa-txpower-keeper")
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        result = subprocess.CompletedProcess(cmd, 1, "", "timeout")

    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )

    return result


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def require_root() -> None:
    if os.geteuid() != 0:
        print("This operation requires root privileges. Re-run with sudo.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------
def find_adapter() -> Optional[AdapterInfo]:
    """Find the first USB device with VID:PID 0e8d:7610."""
    for child in SYSFS_USB.iterdir():
        vid = read_text(child / "idVendor")
        pid = read_text(child / "idProduct")
        if vid and pid and vid.lower() == VENDOR_ID and pid.lower() == PRODUCT_ID:
            return AdapterInfo(
                vendor_id=vid.lower(),
                product_id=pid.lower(),
                busnum=read_text(child / "busnum"),
                devnum=read_text(child / "devnum"),
                manufacturer=read_text(child / "manufacturer"),
                product=read_text(child / "product"),
                sysfs_path=child.resolve(),
            )
    return None


def find_phy(adapter: AdapterInfo) -> Optional[str]:
    """Return the ieee80211 phy name associated with the USB adapter."""
    if not adapter.sysfs_path or not SYSFS_IEEE80211.is_dir():
        return None
    usb_dev = adapter.sysfs_path.resolve()
    for phy_dir in SYSFS_IEEE80211.iterdir():
        if not phy_dir.is_dir() or not phy_dir.name.startswith("phy"):
            continue
        link = phy_dir / "device"
        if not link.is_symlink():
            continue
        try:
            target = link.resolve()
        except OSError:
            continue
        if target == usb_dev or usb_dev in target.parents:
            return phy_dir.name
    return None


def find_interface(phy: str) -> Optional[str]:
    """Return the first network interface for a phy."""
    phy_dir = SYSFS_IEEE80211 / phy / "device"
    if not phy_dir.is_dir():
        return None
    for net_dir in [phy_dir / "net"] + list(phy_dir.rglob("net")):
        if net_dir.is_dir():
            for iface in net_dir.iterdir():
                if iface.is_dir():
                    return iface.name
    return None


def eeprom_path(phy: str) -> Optional[Path]:
    path = Path(f"/sys/kernel/debug/ieee80211/{phy}/mt76/eeprom")
    try:
        return path if path.exists() else None
    except PermissionError:
        return path
    except OSError:
        return None


def mount_debugfs() -> bool:
    if DEBUGFS_BASE.is_mount():
        return True
    DEBUGFS_BASE.mkdir(parents=True, exist_ok=True)
    return run("mount", "-t", "debugfs", "none", str(DEBUGFS_BASE)).returncode == 0


def read_eeprom_byte(path: Path, offset: int) -> Optional[int]:
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(1)
            return data[0] if data else None
    except OSError as exc:
        logging.getLogger("alfa-txpower-keeper").warning("Cannot read EEPROM: %s", exc)
        return None


def write_eeprom_byte(path: Path, offset: int, value: int) -> bool:
    """Attempt the write directly; do not pre-check writability."""
    logger = logging.getLogger("alfa-txpower-keeper")
    try:
        with open(path, "r+b") as fh:
            fh.seek(offset)
            fh.write(struct.pack("B", value & 0xFF))
        logger.info("Wrote 0x%02x to EEPROM offset 0x%x on %s", value & 0xFF, offset, path)
        return True
    except OSError as exc:
        logger.warning("EEPROM write failed: %s", exc)
        return False


def current_txpower(interface: str) -> Optional[float]:
    result = run("iw", "dev", interface, "info")
    if result.returncode != 0:
        return None
    m = re.search(r"txpower\s+([0-9.]+)\s+dBm", result.stdout)
    return float(m.group(1)) if m else None


def iw_set_txpower(interface: str, dbm: int) -> bool:
    result = run("iw", "dev", interface, "set", "txpower", "fixed", str(dbm * 100))
    if result.returncode == 0:
        logging.getLogger("alfa-txpower-keeper").info("Applied iw txpower fixed %d mBm on %s", dbm * 100, interface)
        return True
    logging.getLogger("alfa-txpower-keeper").warning("iw set txpower failed on %s: %s", interface, result.stderr.strip())
    return False


def iface_exists(interface: str) -> bool:
    return (Path("/sys/class/net") / interface).is_dir()


# ---------------------------------------------------------------------------
# Keeper
# ---------------------------------------------------------------------------
class Keeper:
    def __init__(
        self,
        interface: str,
        target_dbm: int,
        interval: int,
        eeprom_offset: int,
        eeprom_value: int,
        max_retries: int,
    ):
        self.interface = interface
        self.target_dbm = target_dbm
        self.interval = interval
        self.eeprom_offset = eeprom_offset
        self.eeprom_value = eeprom_value
        self.max_retries = max_retries
        self.retry_delay = DEFAULT_RETRY_DELAY
        self.logger = logging.getLogger("alfa-txpower-keeper")
        self._shutdown = False

    def signal_handler(self, _signum, _frame):
        self._shutdown = True

    def wait_for_adapter(self) -> Optional[AdapterInfo]:
        for attempt in range(1, self.max_retries + 1):
            adapter = find_adapter()
            if adapter:
                self.logger.info(
                    "Found adapter %s:%s on bus %s device %s (attempt %d/%d)",
                    adapter.vendor_id, adapter.product_id, adapter.busnum, adapter.devnum,
                    attempt, self.max_retries,
                )
                return adapter
            time.sleep(self.retry_delay)
        self.logger.error("Adapter %s:%s not found", VENDOR_ID, PRODUCT_ID)
        return None

    def wait_for_phy(self, adapter: AdapterInfo) -> Optional[PhyInfo]:
        for attempt in range(1, self.max_retries + 1):
            phy = find_phy(adapter)
            if phy:
                return PhyInfo(phy=phy, interface=find_interface(phy), eeprom_path=eeprom_path(phy))
            time.sleep(self.retry_delay)
        self.logger.error("No phy found for adapter")
        return None

    def patch_eeprom(self, phy_info: PhyInfo) -> bool:
        if not phy_info.eeprom_path:
            self.logger.info("No EEPROM debugfs node for %s", phy_info.phy)
            return False

        mount_debugfs()

        current = read_eeprom_byte(phy_info.eeprom_path, self.eeprom_offset)
        self.logger.info(
            "EEPROM offset 0x%x current=%s; writing 0x%02x",
            self.eeprom_offset,
            f"0x{current:02x}" if current is not None else "unknown",
            self.eeprom_value,
        )

        if not write_eeprom_byte(phy_info.eeprom_path, self.eeprom_offset, self.eeprom_value):
            return False

        new_value = read_eeprom_byte(phy_info.eeprom_path, self.eeprom_offset)
        if new_value == self.eeprom_value:
            self.logger.info("EEPROM patch verified: offset 0x%x = 0x%02x", self.eeprom_offset, self.eeprom_value)
            return True

        self.logger.warning("EEPROM verification failed: expected 0x%02x, got 0x%s", self.eeprom_value, new_value)
        return False

    def apply_iw(self, phy_info: PhyInfo) -> bool:
        iface = phy_info.interface
        if not iface or not iface_exists(iface):
            self.logger.warning("Interface %s does not exist", iface or "<none>")
            return False
        tx = current_txpower(iface)
        if tx is not None and tx >= self.target_dbm:
            self.logger.debug("TX power on %s already %.1f dBm", iface, tx)
            return True
        return iw_set_txpower(iface, self.target_dbm)

    def oneshot(self) -> int:
        adapter = self.wait_for_adapter()
        if not adapter:
            return 1
        phy_info = self.wait_for_phy(adapter)
        if not phy_info:
            return 1

        if self.patch_eeprom(phy_info):
            self.logger.info("EEPROM patch verified on %s", phy_info.interface or phy_info.phy)

            iface = phy_info.interface
            tx = current_txpower(iface) if iface else None

            if tx is not None and tx >= self.target_dbm:
                self.logger.info("TX power verified on %s: %.1f dBm", iface, tx)
                return 0

            self.logger.warning(
                "EEPROM byte verified but TX power is still %s dBm; trying iw fallback",
                tx if tx is not None else "unknown",
            )

            if self.apply_iw(phy_info):
                self.logger.info("iw fallback applied on %s", phy_info.interface)
                return 0

            self.logger.error("EEPROM byte verified but TX power was not verified and iw fallback failed")
            return 1

        self.logger.warning("EEPROM patch failed; trying iw fallback")
        if self.apply_iw(phy_info):
            self.logger.info("iw fallback applied on %s", phy_info.interface)
            return 0

        self.logger.error("Both EEPROM patch and iw fallback failed")
        return 1

    def daemon(self) -> int:
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGINT, self.signal_handler)

        self.logger.info("Daemon started (target=%d dBm, interval=%ds)", self.target_dbm, self.interval)

        present = False
        eeprom_done = False
        iw_active = False

        while not self._shutdown:
            adapter = find_adapter()
            if adapter is None:
                if present:
                    self.logger.info("Adapter removed; waiting for re-insertion")
                    present = False
                    eeprom_done = False
                    iw_active = False
                time.sleep(self.interval)
                continue

            if not present:
                self.logger.info("Adapter detected: %s:%s", adapter.vendor_id, adapter.product_id)
                present = True
                eeprom_done = False
                iw_active = False

            phy_info = self.wait_for_phy(adapter)
            if not phy_info:
                time.sleep(self.interval)
                continue

            # Interface name can change during enumeration; refresh it.
            discovered_iface = find_interface(phy_info.phy)
            phy_info.interface = discovered_iface or self.interface

            if not phy_info.interface:
                self.logger.warning(
                    "No interface found for %s; EEPROM patch can continue, "
                    "but iw fallback/txpower verification cannot run",
                    phy_info.phy,
                )

            if not eeprom_done:
                eeprom_done = self.patch_eeprom(phy_info)
                if eeprom_done:
                    self.logger.info("EEPROM patch verified on %s", phy_info.phy)

                    iface = phy_info.interface
                    tx = current_txpower(iface) if iface else None

                    if tx is not None and tx >= self.target_dbm:
                        self.logger.info("TX power verified on %s: %.1f dBm", iface, tx)
                        iw_active = False
                    else:
                        self.logger.warning(
                            "EEPROM byte verified but TX power is still %s dBm; enabling iw fallback loop",
                            tx if tx is not None else "unknown",
                        )
                        iw_active = True
                else:
                    self.logger.info("EEPROM patch failed; using iw fallback loop")
                    iw_active = True

            if iw_active:
                self.apply_iw(phy_info)

            time.sleep(self.interval)

        self.logger.info("Daemon stopped")
        return 0


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------
def install() -> int:
    require_root()
    logger = logging.getLogger("alfa-txpower-keeper")

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    script = INSTALL_DIR / "alfa-txpower-keeper.py"
    if not script.is_file():
        logger.error("Source script missing at %s", script)
        return 1

    if SBIN_LINK.exists() or SBIN_LINK.is_symlink():
        SBIN_LINK.unlink()
    SBIN_LINK.symlink_to(script)

    service_path = Path("/etc/systemd/system") / SERVICE_NAME
    service_path.write_text(
        f"""[Unit]
Description=ALFA AWUS036ACHM TX power keeper
After=sys-kernel-debug.mount network.target
Wants=sys-kernel-debug.mount

[Service]
Type=simple
ExecStart={SBIN_LINK} daemon
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=alfa-txpower-keeper

[Install]
WantedBy=multi-user.target
"""
    )

    udev_path = Path("/etc/udev/rules.d") / UDEV_RULE
    udev_path.write_text(
        f"""# Trigger alfa-txpower-keeper when a 0e8d:7610 adapter is connected.
ACTION=="add", SUBSYSTEM=="usb", ATTR{{idVendor}}=="{VENDOR_ID}", ATTR{{idProduct}}=="{PRODUCT_ID}", TAG+="systemd", ENV{{SYSTEMD_WANTS}}+="{SERVICE_NAME}"
ACTION=="change", SUBSYSTEM=="usb", ATTR{{idVendor}}=="{VENDOR_ID}", ATTR{{idProduct}}=="{PRODUCT_ID}", TAG+="systemd", ENV{{SYSTEMD_WANTS}}+="{SERVICE_NAME}"
"""
    )

    run("systemctl", "daemon-reload", check=True)
    run("udevadm", "control", "--reload-rules", check=True)
    run("systemctl", "enable", SERVICE_NAME, check=True)

    logger.info("Installed systemd service and udev rules")
    return 0


def uninstall() -> int:
    require_root()
    logger = logging.getLogger("alfa-txpower-keeper")

    run("systemctl", "stop", SERVICE_NAME, check=False)
    run("systemctl", "disable", SERVICE_NAME, check=False)

    for path in [Path("/etc/systemd/system") / SERVICE_NAME, Path("/etc/udev/rules.d") / UDEV_RULE]:
        if path.exists():
            path.unlink()
            logger.info("Removed %s", path)

    run("systemctl", "daemon-reload", check=True)
    run("udevadm", "control", "--reload-rules", check=True)
    logger.info("Uninstalled %s", SERVICE_NAME)
    return 0


def status() -> int:
    adapter = find_adapter()
    if not adapter:
        print("Adapter: NOT PRESENT")
        return 0

    print(f"Adapter:  {adapter.vendor_id}:{adapter.product_id}")
    print(f"  Bus:    {adapter.busnum}:{adapter.devnum}")
    print(f"  Vendor: {adapter.manufacturer or 'N/A'}")
    print(f"  Product: {adapter.product or 'N/A'}")

    phy = find_phy(adapter)
    if phy:
        iface = find_interface(phy)
        tx = current_txpower(iface) if iface else None
        eeprom = eeprom_path(phy)
        print(f"  Phy:    {phy}")
        print(f"  Iface:  {iface or 'N/A'}")
        print(f"  TX pwr: {tx if tx is not None else 'N/A'} dBm")
        print(f"  EEPROM: {eeprom or 'N/A'}")
    else:
        print("  Phy:    NOT FOUND")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alfa-txpower-keeper")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-i", "--interface", default=DEFAULT_INTERFACE,
                        help="Optional interface name for iw fallback (default: auto-discover from phy)")
    parser.add_argument("-t", "--target-dbm", type=int, default=DEFAULT_TARGET_DBM)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--eeprom-offset", type=lambda x: int(x, 0), default=DEFAULT_EEPROM_OFFSET)
    parser.add_argument("--eeprom-value", type=lambda x: int(x, 0), default=DEFAULT_EEPROM_VALUE)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)

    sub = parser.add_subparsers(dest="command")
    for name, help_text in {
        "daemon": "Run continuously (default)",
        "oneshot": "Run once and exit",
        "install": "Install systemd service and udev rules",
        "uninstall": "Remove systemd service and udev rules",
        "status": "Show adapter status",
    }.items():
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-v", "--verbose", action="store_true")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose)

    command = args.command or "daemon"
    keeper = Keeper(
        interface=args.interface,
        target_dbm=args.target_dbm,
        interval=args.interval,
        eeprom_offset=args.eeprom_offset,
        eeprom_value=args.eeprom_value,
        max_retries=args.max_retries,
    )

    try:
        if command == "daemon":
            return keeper.daemon()
        if command == "oneshot":
            return keeper.oneshot()
        if command == "install":
            return install()
        if command == "uninstall":
            return uninstall()
        if command == "status":
            return status()
        parser.print_help()
        return 1
    except KeyboardInterrupt:
        logging.getLogger("alfa-txpower-keeper").info("Interrupted")
        return 130
    except Exception as exc:
        logging.getLogger("alfa-txpower-keeper").exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
