# eGPU shutdown-eject hook

Standalone systemd unit + script, **independent of the egpu-switch Decky plugin**: fixes
shutdown/reboot hanging at the boot splash when an NVIDIA eGPU is connected.

## Why this exists

`all-ways-egpu`'s own `all-ways-egpu-shutdown.service` only runs `all-ways-egpu
set-boot-vga internal`, which flips which GPU is marked `boot_vga` but never unloads the
nvidia kernel modules or detaches the eGPU from the PCI bus. The nvidia driver does not
support surprise PCIe removal (see the plugin's main
[README](../../README.md#known-limitation-nvidia-driver-hot-unplug)), so leaving it bound
through the platform's power-down sequence can hang shutdown/reboot at the splash screen -
the same crash class as physically unplugging the cable without ejecting first.

`all-ways-egpu switch force-internal` does call the module-unload+detach sequence
(`removeIDs()`), but it also does other things this scenario doesn't need (VT/framebuffer
rebind, a second display-manager restart) and has a bug where it tries an explicit
`unbind` on the `nvidia` driver even after `modprobe -r nvidia` already detached it, which
fails. `egpu-shutdown-eject.sh` reuses the same corrected, leaner sequence the plugin's own
`eject_egpu()` already uses on hardware: stop the display manager, `modprobe -r` the
nvidia modules (with a short retry on transient "in use"), then unbind/remove every PCI
function under the eGPU's slot (video + HDMI audio sibling, if present) - no restart of
the display manager needed, since the system is shutting down anyway.

## Requirements

- `all-ways-egpu` already installed and configured (`all-ways-egpu setup` run once). This
  script reads the eGPU's bus IDs from `all-ways-egpu status`, so it does nothing without
  that.
- This is **not** installed by the Decky plugin's install flow (Decky only manages
  `~/homebrew/plugins/<name>/`, it can't install root-level systemd units). Install it
  manually, once.

## Install

```sh
sudo ./install.sh
```

Test it directly any time (safe - it's the same eject sequence as the plugin's **Eject
eGPU** button):

```sh
sudo systemctl start egpu-shutdown-eject.service
journalctl -t egpu-shutdown-eject
```

## Uninstall

```sh
sudo systemctl disable --now egpu-shutdown-eject.service
sudo rm /etc/systemd/system/egpu-shutdown-eject.service /usr/local/bin/egpu-shutdown-eject.sh
sudo systemctl daemon-reload
```

## Status

**Validated on hardware** (AyaNeo 2S + RTX 3070 via ADT-Link UT3G, bazzite-nvidia-deck):

- Manual run (`systemctl start egpu-shutdown-eject.service`): full sequence clean - all 4
  nvidia modules unloaded, both PCI functions (video + HDMI audio) detached.
- Real shutdown with the eGPU connected: previously hung at the Bazzite splash every time;
  with the hook enabled, powered off cleanly. The hook's journal entries from a real
  shutdown appear truncated after the first `modprobe` line - expected, journald itself is
  being stopped at that point; the clean power-off is the actual proof it completed.

Next step is upstreaming the fix into `all-ways-egpu` itself (either making
`all-ways-egpu-shutdown.service` actually eject, or fixing the redundant-unbind bug in
`removeIDs()`), so it benefits everyone using `all-ways-egpu`, not just this plugin's
users.

## Known platform limitation: powering ON with the eGPU connected

Separate issue observed during validation, out of scope for this hook (or any software
fix): powering the handheld on with the eGPU already connected froze it at the vendor
(AyaNeo) splash - before the Bazzite splash, i.e. in UEFI/firmware, before Linux starts.
`journalctl --list-boots` confirmed the hung boot left no journal at all: the kernel never
came up. Powering on without the eGPU and connecting the cable after boot works reliably
(standard Thunderbolt hotplug).

Recommendation: connect the eGPU after the system is booted. A vendor BIOS update is the
only thing that could genuinely fix boot-with-eGPU-attached.

Sleep/resume (suspend with the eGPU connected causing an intermittent freeze on wake) is a
separate, still-open issue - `all-ways-egpu` has no suspend/resume handling at all today.
Not addressed here yet.
