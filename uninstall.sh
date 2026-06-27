#!/bin/bash
# uninstall.sh — remove alfa-txpower-keeper from the system.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This uninstaller must be run as root. Try: sudo ./uninstall.sh" >&2
    exit 1
fi

INSTALL_DIR="/opt/alfa-txpower-keeper"
SBIN_LINK="/usr/local/sbin/alfa-txpower-keeper"

if [ -x "$SBIN_LINK" ]; then
    "$SBIN_LINK" uninstall
elif [ -x "$INSTALL_DIR/alfa-txpower-keeper.py" ]; then
    "$INSTALL_DIR/alfa-txpower-keeper.py" uninstall
fi

rm -f "$SBIN_LINK"
rm -rf "$INSTALL_DIR"

echo "alfa-txpower-keeper has been uninstalled."
