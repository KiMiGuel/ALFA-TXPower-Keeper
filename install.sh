#!/bin/bash
# install.sh — install alfa-txpower-keeper as a system service.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This installer must be run as root. Try: sudo ./install.sh" >&2
    exit 1
fi

INSTALL_DIR="/opt/alfa-txpower-keeper"
SBIN_LINK="/usr/local/sbin/alfa-txpower-keeper"

mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    cp -f "$SCRIPT_DIR/alfa-txpower-keeper.py" "$INSTALL_DIR/"
fi
chmod 755 "$INSTALL_DIR/alfa-txpower-keeper.py"

if [ -L "$SBIN_LINK" ] || [ -e "$SBIN_LINK" ]; then
    rm -f "$SBIN_LINK"
fi
ln -s "$INSTALL_DIR/alfa-txpower-keeper.py" "$SBIN_LINK"

"$SBIN_LINK" install

systemctl daemon-reload
systemctl enable alfa-txpower-keeper.service
systemctl start alfa-txpower-keeper.service

echo ""
echo "alfa-txpower-keeper installed and started."
echo "  Status: sudo systemctl status alfa-txpower-keeper"
echo "  Logs:   sudo journalctl -u alfa-txpower-keeper -f"
echo "  Remove: sudo $INSTALL_DIR/uninstall.sh"
