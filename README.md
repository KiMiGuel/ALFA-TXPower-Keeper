```text
 .--..--..--..--..--..--..--..--..--..--..--. 
/ .. \.. \.. \.. \.. \.. \.. \.. \.. \.. \.. \
\ \/\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ \/ /
 \/ /`--'`--'`--'`--'`--'`--'`--'`--'`--'\/ / 
 / /\                                    / /\ 
/ /\ \  .d8b.  db      d88888b  .d8b.   / /\ \
\ \/ / d8' `8b 88      88'     d8' `8b  \ \/ /
 \/ /  88ooo88 88      88ooo   88ooo88   \/ /   AWUS036ACHM 
 / /\  88~~~88 88      88~~~   88~~~88   / /\ 
/ /\ \ 88   88 88booo. 88      88   88  / /\ \
\ \/ / YP   YP Y88888P YP      YP   YP  \ \/ /
 \/ /                                    \/ / 
 / /\.--..--..--..--..--..--..--..--..--./ /\ 
/ /\ \.. \.. \.. \.. \.. \.. \.. \.. \.. \/\ \
\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `'\ `' /
 `--'`--'`--'`--'`--'`--'`--'`--'`--'`--'`--' 
```

# ALFA-TXPower-Keeper
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.7+](https://img.shields.io/badge/python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Linux](https://img.shields.io/badge/platform-linux-lightgrey.svg)](https://www.kernel.org/)
[![GitHub stars](https://img.shields.io/github/stars/KiMiGuel/ALFA-TXPower-Keeper?style=social)](https://github.com/KiMiGuel/ALFA-TXPower-Keeper/stargazers)

Production-ready tool that keeps the ALFA AWUS036ACHM USB Wi-Fi adapter at the
target TX power level on Linux.

> **Disclaimer:** The USB ID `0e8d:7610` used by the AWUS036ACHM is a generic
> MediaTek MT7610U identifier. Other adapters (e.g. Sabrent NTWLAC) share the
> same VID:PID. This tool only patches devices that expose `0e8d:7610`; verify
> your hardware before use.
>
> **Interface discovery:** The tool automatically discovers the network
> interface associated with the matched `phyX`. You only need `-i/--interface`
> if auto-discovery fails or you want to force a specific name.

## What it does

The AWUS036ACHM is often reported at ~7 dBm (or ~2 dBm on older kernels)
because the `mt76x0u` driver reads invalid power-table values from the
adapter EEPROM/efuse.

`alfa-txpower-keeper` works around this with two methods:

1. **Primary — EEPROM patch:** Writes `0x1e` to EEPROM offset `0x52` via the
   debugfs node `/sys/kernel/debug/ieee80211/<PHY>/mt76/eeprom`, then reads
   the byte back to verify.
2. **Secondary — `iw` fallback loop:** If the EEPROM patch fails or cannot be
   verified, the tool continuously runs
   `iw dev <interface> set txpower fixed 2000`.

The correct `<PHY>` is discovered at runtime by matching the USB device
`0e8d:7610` against `/sys/class/ieee80211/phy*/device`. PHY numbers are never
hardcoded, so the tool survives driver reloads and USB unplug/replug events.

## Requirements

- Linux with Python 3.7+
- `iw` installed
- root privileges (for debugfs / `iw` / systemd integration)
- systemd (for service/udev integration)

## Installation

```bash
cd /opt/alfa-txpower-keeper
sudo ./install.sh
```

This will:

- Copy the keeper to `/opt/alfa-txpower-keeper/`
- Create `/usr/local/sbin/alfa-txpower-keeper`
- Install the systemd service `alfa-txpower-keeper.service`
- Install the udev rule `99-alfa-txpower-keeper.rules`
- Start and enable the service

Check status:

```bash
sudo systemctl status alfa-txpower-keeper
sudo journalctl -u alfa-txpower-keeper -f
```

## Usage

### Manual commands

```bash
# Show adapter state
sudo alfa-txpower-keeper status

# Run a single patch/verify cycle
sudo alfa-txpower-keeper oneshot

# Run continuously in the foreground
sudo alfa-txpower-keeper daemon

# Verbose output
sudo alfa-txpower-keeper -v daemon
sudo alfa-txpower-keeper oneshot -v
```

### Command-line options

| Option | Default | Description |
|--------|---------|-------------|
| `-i, --interface` | auto-discover | Optional interface name for `iw` fallback |
| `-t, --target-dbm` | `20` | Target TX power in dBm |
| `--interval` | `3` | Daemon check interval in seconds |
| `--eeprom-offset` | `0x52` | EEPROM offset to patch |
| `--eeprom-value` | `0x1e` | EEPROM value to write |
| `--max-retries` | `30` | Retries when waiting for adapter/phy |

## Uninstallation

```bash
sudo /opt/alfa-txpower-keeper/uninstall.sh
```

This stops the service, removes the systemd/udev integration, and deletes
`/opt/alfa-txpower-keeper` and `/usr/local/sbin/alfa-txpower-keeper`.

## How it works

1. On startup (or when the daemon wakes), enumerate USB devices for
   `idVendor=0e8d`, `idProduct=7610`.
2. For each `phy*` in `/sys/class/ieee80211/`, resolve the `device` symlink
   and check whether it belongs to the matched USB device.
3. Mount debugfs if necessary.
4. Attempt to write `0x1e` to offset `0x52` of the discovered EEPROM node.
5. Read back the byte. If it equals `0x1e`, the patch is verified.
6. If verification fails, enter the `iw` fallback loop.
7. If the adapter disappears, reset state and wait for re-insertion.

## Important notes

- The debugfs EEPROM node may appear as mode `0400` (read-only). The tool
  attempts the write directly, because many kernels still allow root to write
  to it via `dd`.
- The `iw set txpower` fallback is a best-effort workaround. The `mt76x0u`
  driver binds TX power to EEPROM values, so `iw` may be ignored depending on
  kernel version and driver state.
- A complete fix requires patching the kernel driver itself; this tool is a
  user-space workaround.

## Troubleshooting

**Service fails to start:**

```bash
sudo journalctl -u alfa-txpower-keeper -n 50
```

**Adapter not detected:**

- Confirm the adapter is plugged in.
- Check `lsusb | grep 0e8d:7610`.

**EEPROM patch fails:**

- Some kernels hard-lock the debugfs EEPROM node. The tool will automatically
  fall back to the `iw` loop.

**Wrong or missing interface name:**

- The tool auto-discovers the interface from the matched `phyX`. If that
  fails, pass `-i <interface>` or set it in the systemd service `ExecStart`
  line.
- If no interface is found, EEPROM patching still runs, but `iw` fallback
  and TX power verification are skipped.

## License

Provided as-is for research and troubleshooting on hardware you own or have
permission to test.
