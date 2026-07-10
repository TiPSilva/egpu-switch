# eGPU Switch

A Decky Loader plugin that automates [`all-ways-egpu`](https://github.com/ewagner12/all-ways-egpu)
directly from Deck Mode, without needing to switch to Desktop Mode.

Shows whether the eGPU is connected, whether it's the active boot VGA GPU, and offers a
button to toggle between eGPU/iGPU, calling `all-ways-egpu set-boot-vga` followed by
`systemctl restart display-manager.service`, the same commands `all-ways-egpu`'s own
interactive menu already runs.

## Prerequisites

- `all-ways-egpu` already installed on the system.
- `all-ways-egpu setup` already run **manually once**, from a terminal (Desktop Mode),
  selecting the eGPU. The plugin doesn't do that interactive setup; it only toggles the
  boot VGA of an already-existing configuration.

## Compatibility

The plugin has no hardcoded bus ID, GPU model, or machine-specific path. Everything is
discovered at runtime (`lspci`, `all-ways-egpu status`, environment variables Decky
injects). In theory this covers any NVIDIA eGPU and any AMD/Intel handheld with
Thunderbolt running `all-ways-egpu` + Decky Loader + systemd.

**Actually tested** only on: AyaNeo 2S (Ryzen 7840U) + RTX 3070 via ADT-Link UT3G,
bazzite-nvidia-deck.

**Expected to work, but untested:**
- AMD eGPU / `amdgpu`: the kernel module unload step before ejecting (`eject_egpu`) is
  specifically needed for the proprietary NVIDIA driver, which doesn't handle hot-unplug
  well. For other drivers, that step is skipped automatically and eject goes straight to
  the generic unbind+remove. `amdgpu` has much better hot-unplug support, so it should
  work, but nobody has confirmed it yet.
- Other Bazzite images (non-nvidia-deck) and other gamescope+systemd distros (ChimeraOS,
  HoloISO, manual installs): the two system dependencies (`display-manager.service`,
  `user@<uid>.service`) are standard systemd conventions, not Bazzite-specific.

Tested on a different hardware/distro combo? Open an issue or PR with what worked (or
didn't); it helps close this list out.

## Install

The handheld is a full computer; no second machine or SSH setup is needed for this.

1. On the device, switch to **Desktop Mode**.
2. Open a browser and download `egpu-switch.zip` from the
   [latest release](https://github.com/TiPSilva/egpu-switch/releases/latest) (it lands in
   `~/Downloads` by default).
3. Switch back to **Game Mode**.
4. In Decky, enable **Developer Mode** if not already on (**Settings → General**).
5. Go to **Settings → Developer → Install Plugin from ZIP File**, press **Browse**, and
   pick the ZIP from wherever it downloaded (`Downloads` by default).

Decky extracts it, sets ownership/permissions, and loads the plugin automatically.

## Building from source

```sh
pnpm install
pnpm run build   # generates dist/index.js
pnpm run zip     # also packages out/egpu-switch.zip, ready to install via the flow above
```

## Manual deploy over SSH (development)

Useful for scripting redeploys while working on the plugin itself (this is what the rest
of this README's troubleshooting notes assume). Copy the files to
`$DECKY_USER_HOME/homebrew/plugins/egpu-switch/` on the device, **keeping the `dist/`
subfolder**: the loader serves the bundle from `<plugin_directory>/dist/index.js`
(`handle_plugin_dist` in `loader.py`), not from the plugin folder's root:

```
egpu-switch/
├── plugin.json
├── main.py
├── package.json
└── dist/
    └── index.js
```

Example:

```sh
ssh <user>@<ip> "mkdir -p ~/homebrew/plugins/egpu-switch/dist"
scp plugin.json main.py package.json <user>@<ip>:~/homebrew/plugins/egpu-switch/
scp dist/index.js <user>@<ip>:~/homebrew/plugins/egpu-switch/dist/
```

Also note that `name` in `plugin.json` is used raw (no `encodeURIComponent`) in the URL
the frontend uses to load the bundle (`/plugins/<name>/dist/index.js`), so avoid spaces or
special characters in that field. The pretty name shown in the UI is the `name` returned
by `definePlugin()` in `src/index.tsx`, which is independent and can have a space just
fine.

The `~/homebrew/plugins/<name>/` folder is managed by `plugin_loader` (runs as root).
Once it has loaded the plugin, the folder ends up owned by `root:root` and a direct `scp`
from your user will fail with "Permission denied". In that case, copy to `/tmp/` first and
move it with `sudo` over SSH, or fix the folder's ownership with
`sudo chown -R <user>:<user> ...` before copying again.

Then, with Decky's Developer Mode enabled, restart the `plugin_loader` service (or use the
Developer tab's reload) to load the plugin:

```sh
ssh <user>@<ip> "sudo systemctl restart plugin_loader"
```

To confirm the bundle is reachable before even opening the QAM, test directly on the
device:

```sh
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:1337/plugins/<plugin.json-name>/dist/index.js
```

Should return `200`.

## Usage

Open the quick access menu (QAM) and go to the "eGPU Switch" tab. The status shows
whether `all-ways-egpu` is installed, configured, whether the eGPU is connected, and
which GPU is currently active.

- **Switch to eGPU / Switch to iGPU**: toggles boot VGA and restarts the display manager.
  Always asks for confirmation first, since the screen flickers and the current Deck Mode
  session briefly restarts (5-15s); this is expected. Validated on hardware: the
  gamescope session automatically routes output to the TV after the restart, with no
  manual "arrange displays" step needed. Switching to eGPU always triggers a PCI rescan
  first, since after an eject the eGPU is genuinely gone from `lspci` (not just slow to
  enumerate), and `all-ways-egpu`'s own retry loop never forces a rescan on its own.
- **Eject eGPU**: **stops the display manager** (confirmed on hardware via `fuser
  /dev/nvidia*` that gamescope, mangoapp, steam, steamwebhelper and hhd-ui keep the eGPU
  open for the whole session, even with the iGPU active, so only stopping the whole
  session guarantees nothing still holds the card open), unloads the NVIDIA kernel
  modules (`nvidia_uvm`, `nvidia_drm`, `nvidia_modeset`, `nvidia`), removes the eGPU's
  PCI function(s) from the bus (video + HDMI audio function, if present), and **restarts
  the display manager again**, causing the same flicker/session restart as the main
  toggle. Only enabled when the eGPU is connected and **not** the currently active GPU.
  **Required before physically disconnecting the Thunderbolt cable**, see the section
  below. If something is still using the card, the operation aborts without removing
  anything (but the session is always restarted regardless, even on error, so the screen
  never gets stuck with no session at all).
- **Restart Display Manager (recovery)**: only restarts the display manager, without
  touching the boot VGA configuration. Useful for unsticking a bad display state.
- **Rescan for eGPU**: `echo 1 > /sys/bus/pci/rescan`. Removes nothing, just forces a new
  PCI device detection pass. Normally unnecessary (reconnecting the Thunderbolt cable
  already triggers automatic hotplug), but serves as manual recovery if the eGPU doesn't
  reappear on its own after being reconnected.

## Known limitation: NVIDIA driver hot-unplug

The proprietary NVIDIA driver **does not support surprise PCIe removal**: physically
disconnecting the Thunderbolt cable while the kernel modules are still loaded (even with
boot VGA already switched to the iGPU) can trigger a kernel `BUG()` and crash the entire
graphical session, requiring a reboot. This actually happened during this plugin's testing
(confirmed via `dmesg`: crash in `nvidia_ctl_close → nvidia_close → __fput → do_exit`).

**Safe flow to disconnect the eGPU:**
1. Switch to iGPU (**Switch to iGPU**), if not already.
2. Press **Eject eGPU** and wait for the "eGPU ejected. Safe to disconnect the Thunderbolt
   cable now." message. Only then is it safe to pull the cable.
3. If the operation fails (e.g. module busy), **do not disconnect**; resolve whatever is
   holding the card open and try again.

Reconnecting afterward is usually automatic (the kernel detects the Thunderbolt hotplug
and re-enumerates the PCI device on its own); use **Rescan for eGPU** only if that doesn't
happen.

## Testing safely

Keep an SSH session open to the device while testing. If the gamescope session doesn't
come back clean after a toggle, you can revert manually:

```sh
sudo all-ways-egpu set-boot-vga internal
sudo systemctl restart display-manager.service user@<uid>
```

If the screen freezes after a physical disconnect without ejecting first (the NVIDIA
hot-unplug scenario described above), trying to restart the display manager manually may
not fix it; in that case the safest path is `sudo reboot` over SSH. Confirm with
`sudo dmesg | tail -60` whether there's a crash related to `nvidia_close`/
`nvidia_ctl_close` before reaching for lighter recovery commands.

## License

MIT, see [LICENSE](LICENSE).
