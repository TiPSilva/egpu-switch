#!/usr/bin/env sh
# Standalone eGPU eject, run only at system shutdown/reboot via
# egpu-shutdown-eject.service. Independent of the egpu-switch Decky plugin's
# process lifecycle - this still runs even if the plugin is disabled or
# plugin_loader has crashed, since it's driven by systemd itself, not Decky.
#
# Still depends on all-ways-egpu being installed and configured: bus IDs are
# read from `all-ways-egpu status`, the same public interface the Decky
# plugin already trusts, instead of duplicating all-ways-egpu's internal
# config path resolution (which has a SUDO_USER-based override for non-root
# installs).
#
# Root cause this exists for: all-ways-egpu-shutdown.service only flips
# boot_vga (`set-boot-vga internal`), it never unloads the nvidia kernel
# modules or detaches the eGPU from the PCI bus. The nvidia driver does not
# support surprise PCIe removal, so leaving it bound through the platform's
# power-down sequence can hang shutdown/reboot at the splash screen - the
# same crash class as physically unplugging the cable without ejecting
# first (see the egpu-switch plugin's README).

set -u

log() {
    logger -t egpu-shutdown-eject "$1"
}

# Same lookup order as the Decky plugin's find_egpu_binary(): on Bazzite /
# SteamOS (read-only /usr), all-ways-egpu's installer falls back to the
# user's ~/bin, which is not on root's PATH when systemd runs this unit.
find_egpu_binary() {
    for P in /usr/bin/all-ways-egpu /usr/local/bin/all-ways-egpu /home/*/bin/all-ways-egpu; do
        if [ -x "$P" ]; then
            echo "$P"
            return 0
        fi
    done
    return 1
}

AWE_BIN=$(find_egpu_binary) || {
    log "all-ways-egpu not found (checked /usr/bin, /usr/local/bin, /home/*/bin), nothing to do"
    exit 0
}

STATUS=$("$AWE_BIN" status 2>/dev/null)

# Lines between "Method 2, 3 setup with following Bus IDs" and the next
# non-matching line: "<bus_id> <driver>", the same format the Decky plugin's
# BUS_ID_LINE_RE already parses.
BUS_LINES=$(printf '%s\n' "$STATUS" | awk '
    /^Method 2, 3 setup with following Bus IDs$/ { grab=1; next }
    grab && /^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F][[:space:]]+[^[:space:]]+$/ { print; next }
    grab { grab=0 }
')

if [ -z "$BUS_LINES" ]; then
    log "no configured eGPU bus IDs found (all-ways-egpu not set up?), nothing to do"
    exit 0
fi

# Matches all-ways-egpu's own removeIDs(), which hardcodes the same uid for
# the same reason: Bazzite/SteamOS-style handhelds always run the primary
# session as uid 1000.
DM_UID=1000

log "stopping display manager before ejecting eGPU"
systemctl stop display-manager.service "user@${DM_UID}.service" 2>&1 | logger -t egpu-shutdown-eject

if printf '%s\n' "$BUS_LINES" | grep -q ' nvidia$'; then
    for MOD in nvidia_uvm nvidia_drm nvidia_modeset nvidia; do
        ATTEMPT=0
        while [ "$ATTEMPT" -lt 5 ]; do
            ERR=$(modprobe -r "$MOD" 2>&1)
            RC=$?
            if [ "$RC" -eq 0 ]; then
                log "modprobe -r $MOD: ok"
                break
            fi
            if printf '%s' "$ERR" | grep -qi "in use"; then
                ATTEMPT=$((ATTEMPT + 1))
                sleep 1
                continue
            fi
            log "modprobe -r $MOD failed (not retrying): $ERR"
            break
        done
    done
fi

# Detach every PCI function under the same slot as each configured bus ID
# (video + HDMI audio sibling, if present), not just the single function
# all-ways-egpu tracks - mirrors the plugin's list_pci_functions()+eject.
printf '%s\n' "$BUS_LINES" | while read -r BUS _CONFIGURED_DRIVER; do
    SLOT=${BUS%.*}
    for FUNC in /sys/bus/pci/devices/"${SLOT}".*; do
        [ -e "$FUNC" ] || continue
        FBUS=$(basename "$FUNC")
        FDRIVER=""
        if [ -e "$FUNC/driver" ]; then
            FDRIVER=$(basename "$(readlink -f "$FUNC/driver")")
        fi
        # "nvidia" is already unbound as a side effect of modprobe -r above;
        # an explicit unbind here would just fail with ENOENT. Only sibling
        # functions still on another live driver (e.g. snd_hda_intel for the
        # HDMI audio function) need the explicit unbind.
        if [ -n "$FDRIVER" ] && [ "$FDRIVER" != "nvidia" ] && [ -e "/sys/bus/pci/drivers/$FDRIVER/unbind" ]; then
            if ! echo "$FBUS" > "/sys/bus/pci/drivers/$FDRIVER/unbind" 2>/tmp/egpu-shutdown-eject-unbind.err; then
                log "unbind $FBUS from $FDRIVER failed: $(cat /tmp/egpu-shutdown-eject-unbind.err)"
            fi
        fi
        if echo 1 > "$FUNC/remove" 2>/tmp/egpu-shutdown-eject-remove.err; then
            log "ejected $FBUS (driver was: ${FDRIVER:-none})"
        else
            log "remove $FBUS failed: $(cat /tmp/egpu-shutdown-eject-remove.err)"
        fi
    done
done
rm -f /tmp/egpu-shutdown-eject-unbind.err /tmp/egpu-shutdown-eject-remove.err

log "done"
exit 0
