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

# Root runs all-ways-egpu here, so only a copy root alone can change: the
# file and every directory above it owned by root, not writable by group or
# others. On Bazzite / SteamOS its installer puts it in ~/bin, which any
# program running as the user can replace; that copy is not used. Install it
# system-wide once with:
#   sudo install -o root -g root -m 755 ~/bin/all-ways-egpu /usr/local/bin/
trusted() {
    P=$(readlink -f -- "$1") || return 1
    [ -f "$P" ] && [ -x "$P" ] || return 1
    while :; do
        OWNER=$(stat -c '%u' -- "$P") || return 1
        MODE=$(stat -c '%a' -- "$P") || return 1
        [ "$OWNER" = 0 ] || return 1
        [ $(( 0$MODE & 022 )) -eq 0 ] || return 1
        [ "$P" = / ] && return 0
        P=$(dirname -- "$P")
    done
}

# Same lookup as the eject script itself.
FOUND=""
for P in /usr/bin/all-ways-egpu /usr/local/bin/all-ways-egpu; do
    if trusted "$P"; then
        FOUND="$P"
        break
    fi
done
if [ -z "$FOUND" ]; then
    echo "No root-owned all-ways-egpu in /usr/bin or /usr/local/bin." >&2
    echo "If yours is in ~/bin, install it system-wide first:" >&2
    echo "  sudo install -o root -g root -m 755 ~/bin/all-ways-egpu /usr/local/bin/" >&2
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
