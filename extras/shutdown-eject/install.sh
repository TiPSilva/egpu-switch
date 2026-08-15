#!/usr/bin/env sh
# One-time manual installer for the eGPU shutdown-eject safety hook.
# Run once, as root, from a terminal (Desktop Mode or SSH). Requires
# all-ways-egpu already installed and configured (`all-ways-egpu setup`).
# Independent of the egpu-switch Decky plugin - installing/uninstalling the
# plugin does not touch this, and vice versa.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this as root (sudo $0)" >&2
    exit 1
fi

# Same lookup as the eject script itself: on Bazzite / SteamOS (read-only
# /usr), all-ways-egpu installs to the user's ~/bin, not on root's PATH.
FOUND=""
for P in /usr/bin/all-ways-egpu /usr/local/bin/all-ways-egpu /home/*/bin/all-ways-egpu; do
    if [ -x "$P" ]; then
        FOUND="$P"
        break
    fi
done
if [ -z "$FOUND" ]; then
    echo "all-ways-egpu not found (checked /usr/bin, /usr/local/bin, /home/*/bin)." >&2
    echo "Install it and run 'all-ways-egpu setup' first." >&2
    exit 1
fi
echo "Found all-ways-egpu at: $FOUND"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -m 755 "$SCRIPT_DIR/egpu-shutdown-eject.sh" /usr/local/bin/egpu-shutdown-eject.sh
install -m 644 "$SCRIPT_DIR/egpu-shutdown-eject.service" /etc/systemd/system/egpu-shutdown-eject.service

systemctl daemon-reload
systemctl enable egpu-shutdown-eject.service

echo "Installed and enabled egpu-shutdown-eject.service."
echo "Test it directly (safe to run any time - same eject sequence as the plugin's Eject eGPU button):"
echo "  sudo systemctl start egpu-shutdown-eject.service"
echo "  journalctl -t egpu-shutdown-eject"
echo "To uninstall: sudo systemctl disable --now egpu-shutdown-eject.service && sudo rm /etc/systemd/system/egpu-shutdown-eject.service /usr/local/bin/egpu-shutdown-eject.sh && sudo systemctl daemon-reload"
