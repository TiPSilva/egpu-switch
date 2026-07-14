import asyncio
import glob
import json
import os
import pwd
import re
import subprocess

import decky

CANDIDATE_PATHS = [
    "/usr/bin/all-ways-egpu",
    "/usr/local/bin/all-ways-egpu",
]

# Regexes transcribed verbatim from the echo strings in all-ways-egpu's status() function
# (Referencias/all-ways-egpu-main/all-ways-egpu), so status output is parsed, not guessed.
NOT_SETUP_RE = re.compile(r"all-ways-egpu not setup")
EGPU_SECTION_RE = re.compile(r"^Method 2, 3 setup with following Bus IDs$")
BUS_ID_LINE_RE = re.compile(
    r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F])\s+(\S+)\s*$"
)
ACTIVE_RE = re.compile(
    r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]) "
    r"eGPU currently active as primary with Method 2$"
)
CONNECTED_NOT_PRIMARY_RE = re.compile(
    r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]) "
    r"eGPU connected, not set as primary with Method 2$"
)
# Must match only the terminal give-up message, not the "No eGPU detected,
# retry N" progress lines set-boot-vga egpu prints while it's still polling -
# a plain substring match on "No eGPU detected" false-positives on those and
# reports failure even when a later retry succeeds (rc=0, bind mounts set),
# silently skipping the display manager restart. Only surfaced once the
# auto-rescan made the retry loop actually engage instead of always
# succeeding on the first try.
NO_EGPU_DETECTED_RE = re.compile(r"No eGPU detected after \d+ retries")
NO_CONFIG_RE = re.compile(r"No configuration file|not setup")

# Order matters: uvm/drm/modeset depend on the base nvidia module, must unload first.
# NVIDIA-specific: the proprietary driver doesn't support PCIe hot-unplug without this
# preemptive unload (see README). Other vendors (amdgpu, etc.) skip this step entirely
# and go straight to the generic unbind+remove below, which is untested but expected to
# work given amdgpu's much better hot-unplug support.
NVIDIA_MODULES = ["nvidia_uvm", "nvidia_drm", "nvidia_modeset", "nvidia"]


def clean_subprocess_env() -> dict:
    """
    PluginLoader runs as a PyInstaller-frozen executable, which injects
    LD_LIBRARY_PATH pointing at its own bundled (older) shared libraries so its
    embedded Python interpreter can find them. That LD_LIBRARY_PATH is inherited
    by every subprocess we spawn, which breaks dynamically-linked system binaries
    (e.g. systemctl fails to load the system's newer libcrypto/libsystemd-shared).
    PyInstaller preserves the pre-injection value in LD_LIBRARY_PATH_ORIG for
    exactly this reason; restore it (or drop the var entirely) for child processes.
    """
    env = os.environ.copy()
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def find_egpu_binary() -> str | None:
    for p in CANDIDATE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    home = os.environ.get("DECKY_USER_HOME")
    if home:
        candidate = os.path.join(home, "bin", "all-ways-egpu")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for candidate in glob.glob("/home/*/bin/all-ways-egpu"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_status(raw: str) -> dict:
    result = {
        "setup_done": False,
        "bus_id": None,
        "driver": None,
        "egpu_connected": False,
        "egpu_active": False,
    }
    lines = [l.strip() for l in raw.splitlines()]

    if any(NOT_SETUP_RE.search(l) for l in lines):
        return result

    in_egpu_section = False
    configured = set()
    for line in lines:
        if EGPU_SECTION_RE.match(line):
            in_egpu_section = True
            continue
        if in_egpu_section:
            m = BUS_ID_LINE_RE.match(line)
            if m:
                configured.add(m.group(1).lower())
                if result["bus_id"] is None:
                    result["bus_id"] = m.group(1)
                    result["driver"] = m.group(2)
                continue
        m = ACTIVE_RE.match(line)
        if m:
            result["egpu_connected"] = True
            result["egpu_active"] = True
            continue
        m = CONNECTED_NOT_PRIMARY_RE.match(line)
        if m:
            result["egpu_connected"] = True
            continue

    result["setup_done"] = bool(configured)
    return result


async def lookup_gpu_name(bus_id: str) -> str | None:
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["lspci", "-D", "-s", bus_id],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_subprocess_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    line = proc.stdout.strip().splitlines()[0]
    m = re.search(r"\[([^\[\]]+)\]\s*(?:\(rev [^)]+\))?\s*$", line)
    if m:
        return m.group(1)
    parts = line.split(": ", 1)
    return parts[1].strip() if len(parts) == 2 else line


def resolve_host_uid() -> int:
    username = os.environ.get("DECKY_USER") or "deck"
    try:
        return pwd.getpwnam(username).pw_uid
    except KeyError:
        decky.logger.warning(f"Could not resolve uid for '{username}', defaulting to 1000")
        return 1000


async def list_pci_functions(slot: str) -> list[str]:
    """List every PCI function (e.g. GPU + its HDMI audio sibling) under a
    domain:bus:device slot, so eject can safely detach all of them, not just
    the single VGA function all-ways-egpu tracks in egpu-bus-ids."""
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["lspci", "-D", "-s", slot],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_subprocess_env(),
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    functions: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        bus = line.split(" ", 1)[0]
        if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", bus):
            functions.append(bus)
    return functions


def get_pci_driver(bus_id: str) -> str | None:
    try:
        return os.path.basename(os.readlink(f"/sys/bus/pci/devices/{bus_id}/driver"))
    except OSError:
        return None


PCIE_GT_TO_GEN = {
    "2.5GT/s": "Gen1",
    "5GT/s": "Gen2",
    "8GT/s": "Gen3",
    "16GT/s": "Gen4",
    "32GT/s": "Gen5",
    "64GT/s": "Gen6",
}
LNKSTA_RE = re.compile(r"LnkSta:\s*Speed\s+([0-9.]+GT/s)[^,]*,\s*Width\s+(x\d+)")


async def get_pcie_link_info(bus_id: str) -> dict | None:
    """
    Negotiated PCIe link speed/width for the given function, read from
    lspci -vv's LnkSta line. Works for any connection type (Thunderbolt or
    OCuLink) since both ultimately negotiate a plain PCIe link with their
    upstream bridge - unlike Thunderbolt-specific info, this never depends
    on boltctl being present.
    """
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["lspci", "-vv", "-s", bus_id],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_subprocess_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    m = LNKSTA_RE.search(proc.stdout)
    if not m:
        return None
    speed = m.group(1)
    return {"speed": speed, "generation": PCIE_GT_TO_GEN.get(speed), "width": m.group(2)}


async def get_thunderbolt_info() -> dict | None:
    """
    Best-effort Thunderbolt tunnel info via `boltctl list`: takes the first
    peripheral's name/generation/rx/tx speed fields, whichever appear first
    in the output. Not rigorously matched to the exact eGPU bus ID (boltctl
    doesn't expose PCI addresses), fine for a single-eGPU setup. Returns None
    when boltctl isn't installed (e.g. OCuLink, no Thunderbolt subsystem) or
    reports nothing, so the caller can omit this section entirely rather
    than showing an error.
    """
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["boltctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_subprocess_env(),
        )
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    def field(pattern: str) -> str | None:
        m = re.search(pattern, proc.stdout)
        return m.group(1).strip() if m else None

    name = field(r"name:\s*(.+)")
    generation = field(r"generation:\s*(.+)")
    rx_speed = field(r"rx speed:\s*([^\n]+)")
    tx_speed = field(r"tx speed:\s*([^\n]+)")
    if not name and not generation:
        return None
    return {"name": name, "generation": generation, "rx_speed": rx_speed, "tx_speed": tx_speed}


async def modprobe_remove(module: str, retries: int = 3, retry_delay: float = 1.0) -> tuple[bool, str]:
    """
    Confirmed on hardware: nvidia_drm can transiently report "in use" right
    after the display manager has fully stopped (systemctl stop already
    waited for every process to exit) - DRM/KMS driver teardown can have a
    short async cleanup tail before the module's use-count actually reaches
    zero. Retry a few times on that specific failure only; anything else
    (a real, persistent block) fails immediately without wasting retries.
    """
    last_err = ""
    for attempt in range(retries):
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["modprobe", "-r", module],
                capture_output=True,
                text=True,
                timeout=15,
                env=clean_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return False, f"modprobe -r {module} timed out"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        if proc.returncode == 0:
            return True, ""
        last_err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        if "in use" not in last_err.lower():
            break
        if attempt < retries - 1:
            await asyncio.sleep(retry_delay)
    return False, last_err


def write_sysfs(path: str, value: str) -> tuple[bool, str]:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True, ""
    except OSError as e:
        return False, str(e)


def find_parent_bridge(bus_id: str) -> str | None:
    """
    Finds the PCI bridge whose secondary bus hosts bus_id, matched purely by
    bus number - deliberately doesn't require bus_id's own sysfs device node
    to exist, since it won't when the eGPU failed to enumerate (the exact
    case Deep Rescan exists for: kernel refused to assign the bridge's MMIO
    window - "can't assign; no space" in dmesg - so the device never shows
    up at all). Every bound PCI-to-PCI bridge exposes a pci_bus/<domain:bus>
    subdirectory for its secondary bus regardless of whether anything is
    plugged into it, so the bridge itself is always discoverable this way.
    """
    target_bus = bus_id.split(":")[1].lower()
    for path in glob.glob("/sys/bus/pci/devices/*/pci_bus/*"):
        child_bus = os.path.basename(path).split(":")[-1].lower()
        if child_bus == target_bus:
            return os.path.basename(path.split("/pci_bus/")[0])
    return None


DEFAULT_SETTINGS = {"auto_eject": False, "deep_rescan": False}


def get_settings_path() -> str | None:
    settings_dir = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR")
    if not settings_dir:
        return None
    return os.path.join(settings_dir, "settings.json")


def load_settings() -> dict:
    path = get_settings_path()
    if not path or not os.path.isfile(path):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return {**DEFAULT_SETTINGS, **data}
    except (OSError, json.JSONDecodeError, TypeError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> tuple[bool, str]:
    path = get_settings_path()
    if not path:
        return False, "DECKY_PLUGIN_SETTINGS_DIR is not set"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(settings, f)
        return True, ""
    except OSError as e:
        return False, str(e)


async def unload_and_remove_pci(
    functions: list[str], drivers: dict[str, str | None]
) -> tuple[str | None, list[str]]:
    """
    Unloads the NVIDIA kernel modules (if any given function is nvidia-bound)
    and detaches every function from the PCI bus. Must be called with the
    display manager already stopped: gamescope, mangoapp, steam,
    steamwebhelper and hhd-ui all keep the eGPU's device nodes open for the
    lifetime of the session regardless of which GPU is active, so modprobe -r
    fails ("module in use") unless the whole session is torn down first.
    Returns (error_or_None, bus_ids_successfully_removed).
    """
    removed: list[str] = []
    if "nvidia" in drivers.values():
        for module in NVIDIA_MODULES:
            ok, err = await modprobe_remove(module)
            decky.logger.info(f"modprobe -r {module}: ok={ok} err={err!r}")
            if not ok:
                return (
                    f"Could not unload kernel module '{module}' ({err}). "
                    "Do not disconnect the eGPU yet.",
                    removed,
                )
    for f in functions:
        driver = drivers.get(f)
        # "nvidia" is already unbound as a side effect of modprobe -r above
        # (unloading the module detaches it from every device it was bound
        # to) - its /sys/bus/pci/drivers/nvidia/ directory is gone by now, so
        # an explicit unbind here would just fail with ENOENT. Only sibling
        # functions still on another live driver (e.g. the HDMI audio
        # function on snd_hda_intel, which we deliberately don't unload as a
        # module) need the explicit unbind.
        if driver and driver != "nvidia":
            ok, err = write_sysfs(f"/sys/bus/pci/drivers/{driver}/unbind", f)
            decky.logger.info(f"unbind {f} from {driver}: ok={ok} err={err!r}")
            if not ok:
                return (
                    f"Failed to unbind {f} from driver {driver}: {err}. "
                    f"Removed so far: {removed}. Do not disconnect yet.",
                    removed,
                )
        ok, err = write_sysfs(f"/sys/bus/pci/devices/{f}/remove", "1")
        decky.logger.info(f"remove {f}: ok={ok} err={err!r}")
        if not ok:
            return (
                f"Failed to remove {f} from the PCI bus: {err}. "
                f"Removed so far: {removed}. Do not disconnect yet.",
                removed,
            )
        removed.append(f)
    return None, removed


class Plugin:
    def __init__(self):
        self._operation_lock = asyncio.Lock()

    async def _main(self):
        decky.logger.info("eGPU Switch plugin started")

    async def _unload(self):
        decky.logger.info("eGPU Switch plugin unloaded")

    # ---- read-only status ----

    async def get_status(self) -> dict:
        binary = find_egpu_binary()
        if not binary:
            return {
                "installed": False,
                "setup_done": False,
                "egpu_connected": False,
                "egpu_active": False,
                "bus_id": None,
                "driver": None,
                "gpu_name": None,
                "error": None,
            }
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [binary, "status"],
                capture_output=True,
                text=True,
                timeout=15,
                env=clean_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return {
                "installed": True,
                "setup_done": False,
                "egpu_connected": False,
                "egpu_active": False,
                "bus_id": None,
                "driver": None,
                "gpu_name": None,
                "error": "all-ways-egpu status timed out",
            }
        if proc.returncode != 0:
            decky.logger.warning(
                f"all-ways-egpu status exited {proc.returncode}: {proc.stderr}"
            )
        parsed = parse_status(proc.stdout or "")
        parsed["installed"] = True
        parsed["error"] = None if proc.returncode == 0 else (proc.stderr or "").strip()
        parsed["gpu_name"] = (
            await lookup_gpu_name(parsed["bus_id"]) if parsed["bus_id"] else None
        )
        return parsed

    async def get_connection_info(self) -> dict:
        """
        Separate, on-demand RPC rather than folded into get_status(): this
        involves extra subprocess calls (lspci -vv, boltctl) that are only
        worth paying for when the user actually opens the Connection
        section, not on every 5s status poll.
        """
        binary = find_egpu_binary()
        bus_id = None
        if binary:
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [binary, "status"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=clean_subprocess_env(),
                )
                bus_id = parse_status(proc.stdout or "").get("bus_id")
            except subprocess.TimeoutExpired:
                pass

        pcie = await get_pcie_link_info(bus_id) if bus_id else None
        thunderbolt = await get_thunderbolt_info()

        return {
            "pcie_generation": pcie.get("generation") if pcie else None,
            "pcie_speed": pcie.get("speed") if pcie else None,
            "pcie_width": pcie.get("width") if pcie else None,
            "thunderbolt_generation": thunderbolt.get("generation") if thunderbolt else None,
            "thunderbolt_rx_speed": thunderbolt.get("rx_speed") if thunderbolt else None,
            "thunderbolt_tx_speed": thunderbolt.get("tx_speed") if thunderbolt else None,
            "thunderbolt_name": thunderbolt.get("name") if thunderbolt else None,
        }

    # ---- settings ----

    async def get_settings(self) -> dict:
        return load_settings()

    async def set_auto_eject(self, enabled: bool) -> dict:
        settings = load_settings()
        settings["auto_eject"] = bool(enabled)
        ok, err = save_settings(settings)
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True}

    async def set_deep_rescan(self, enabled: bool) -> dict:
        settings = load_settings()
        settings["deep_rescan"] = bool(enabled)
        ok, err = save_settings(settings)
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True}

    # ---- actions ----

    async def enable_egpu(self) -> dict:
        return await self._set_boot_vga("egpu")

    async def disable_egpu(self) -> dict:
        # Automatic eject is opt-in (Advanced setting, default off): it folds
        # a multi-second operation (stop DM, modprobe -r with retries, PCI
        # remove, start DM) into a single click, but that duration means an
        # unlucky collision with Decky's own plugin-reload/shutdown (~5s grace
        # period before SIGKILL) can leave the display manager stopped with no
        # chance to run our cleanup code. Default stays the old, faster,
        # boot_vga-only switch; eject remains a deliberate separate step.
        settings = load_settings()
        if settings.get("auto_eject"):
            return await self._switch_to_igpu_and_eject()
        return await self._set_boot_vga("internal")

    async def eject_egpu(self) -> dict:
        """
        Standalone recovery action: safely unload the nvidia kernel modules
        and detach the eGPU's PCI function(s) from the bus, so the
        Thunderbolt cable can be physically disconnected without crashing
        the nvidia driver (see README: nvidia does not support surprise
        PCIe hot-unplug). Only allowed once the eGPU is no longer the
        active boot VGA.

        disable_egpu() already folds this into the same operation when
        switching to iGPU, so this exists for the edge case where the iGPU
        is already active but the eGPU was never ejected (e.g. right after
        a plugin upgrade, or a previous eject attempt that failed).
        """
        if self._operation_lock.locked():
            return {"ok": False, "error": "An eGPU Switch operation is already in progress."}
        async with self._operation_lock:
            binary = find_egpu_binary()
            if not binary:
                return {"ok": False, "error": "all-ways-egpu binary not found on this system."}
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [binary, "status"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=clean_subprocess_env(),
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "all-ways-egpu status timed out."}
            status = parse_status(proc.stdout or "")
            if not status["egpu_connected"]:
                return {"ok": False, "error": "No eGPU currently connected."}
            if status["egpu_active"]:
                return {
                    "ok": False,
                    "error": "eGPU is currently the active boot VGA. Switch to iGPU first, then eject.",
                }
            bus_id = status["bus_id"]
            if not bus_id:
                return {"ok": False, "error": "Could not determine the eGPU's PCI bus ID."}

            slot = bus_id.rsplit(".", 1)[0]
            functions = await list_pci_functions(slot)
            if not functions:
                functions = [bus_id]
            drivers = {f: get_pci_driver(f) for f in functions}
            decky.logger.info(f"Ejecting eGPU: slot={slot} functions={functions} drivers={drivers}")

            stop_result = await self._stop_display_manager_impl()
            if not stop_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"Could not stop the display manager, aborting before touching "
                    f"anything: {stop_result.get('error')}",
                }

            # wait_for guards against kernel-side stalls: when the nvidia
            # driver is already wedged (e.g. a previous removal died with
            # "NVRM: ... non-zero usage count"), modprobe -r blocks in
            # uninterruptible D-state and subprocess.run's timeout cannot
            # reap it (SIGKILL doesn't take effect in D-state, and run()
            # then waits forever). Seen on hardware: the coroutine hung
            # inside the try, the finally never ran, and the display
            # manager stayed stopped - black screen with no recovery.
            # Abandoning the stuck worker thread here is the lesser evil:
            # the session always comes back.
            try:
                error, removed = await asyncio.wait_for(
                    unload_and_remove_pci(functions, drivers), timeout=90
                )
            except asyncio.TimeoutError:
                error, removed = (
                    "Eject stalled in the kernel (module unload or PCI remove stuck; "
                    "the nvidia driver may be in a bad state from an earlier failure). "
                    "A reboot is likely required. Do NOT disconnect the eGPU.",
                    [],
                )
            finally:
                start_result = await self._start_display_manager_impl()
                decky.logger.info(f"Restart after eject: {start_result}")

            if not start_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"eGPU eject {'succeeded' if error is None else 'failed'}, but the "
                    f"display manager did not come back up: {start_result.get('error')}. "
                    "Recover via SSH: sudo systemctl start display-manager.service user@<uid>.",
                    "restart": start_result,
                    "removed": removed,
                }
            if error is not None:
                return {"ok": False, "error": error, "restart": start_result, "removed": removed}

            return {
                "ok": True,
                "message": "eGPU ejected. Safe to disconnect the Thunderbolt cable now.",
                "removed": removed,
                "restart": start_result,
            }

    async def rescan_pci(self) -> dict:
        """
        Plain mode (default): only detects newly-attached PCI devices, never
        removes anything. Deep Rescan (Advanced setting, opt-in) additionally
        removes and re-adds the eGPU's parent PCI bridge first: on some
        platforms that bridge is sized too small at boot for the eGPU to fit
        ("bridge window ... can't assign; no space" in dmesg), which a plain
        rescan can never fix since it only adds devices into an
        already-fixed window. Off by default since removing the bridge
        briefly affects anything else sharing that same physical port.
        """
        ok, err = await self._rescan_pci_impl()
        if not ok:
            return {"ok": False, "error": err}
        return {"ok": True}

    async def restart_display_manager(self) -> dict:
        """Standalone recovery action, RPC-exposed directly."""
        if self._operation_lock.locked():
            return {"ok": False, "error": "An eGPU Switch operation is already in progress."}
        async with self._operation_lock:
            return await self._restart_display_manager_impl()

    # ---- internals ----

    async def _rescan_pci_impl(self) -> tuple[bool, str]:
        settings = load_settings()
        if settings.get("deep_rescan"):
            binary = find_egpu_binary()
            bus_id = None
            egpu_connected = False
            if binary:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        [binary, "status"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        env=clean_subprocess_env(),
                    )
                    parsed = parse_status(proc.stdout or "")
                    bus_id = parsed.get("bus_id")
                    egpu_connected = bool(parsed.get("egpu_connected"))
                except subprocess.TimeoutExpired:
                    pass
            # Only remove the parent bridge when all-ways-egpu does NOT see
            # the eGPU as connected - the functional check, not a bare sysfs
            # existence check. Confirmed on tester hardware (Strix Halo /
            # CachyOS): after an eject, automatic hotplug can re-add a
            # half-enumerated zombie node (the "bridge window ... can't
            # assign; no space" case) whose sysfs path exists but that has
            # no usable boot_vga, so an existence check skips the bridge
            # removal exactly when it's needed. As a second safety belt,
            # still skip if any function under the slot has a driver bound:
            # removing the bridge under a live nvidia-bound GPU was
            # confirmed on hardware to hit "NVRM: Attempting to remove
            # device ... with non-zero usage count!" and wedge the op
            # (gamescope/steam keep the node open even idle on the iGPU).
            if bus_id and not egpu_connected:
                slot = bus_id.rsplit(".", 1)[0].lower()
                bound = [
                    os.path.basename(f)
                    for f in glob.glob(f"/sys/bus/pci/devices/{slot}.*")
                    if get_pci_driver(os.path.basename(f))
                ]
                if bound:
                    decky.logger.info(
                        f"Deep rescan: functions still driver-bound ({bound}), "
                        "skipping bridge removal"
                    )
                else:
                    bridge = find_parent_bridge(bus_id)
                    if bridge:
                        try:
                            ok, err = await asyncio.wait_for(
                                asyncio.to_thread(
                                    write_sysfs, f"/sys/bus/pci/devices/{bridge}/remove", "1"
                                ),
                                timeout=20,
                            )
                            decky.logger.info(
                                f"Deep rescan: remove bridge {bridge}: ok={ok} err={err!r}"
                            )
                        except asyncio.TimeoutError:
                            decky.logger.error(f"Deep rescan: removing bridge {bridge} stalled")
                            return False, (
                                f"Deep rescan: removing PCI bridge {bridge} stalled (something "
                                "may still be using a device behind it). Not rescanning; a "
                                "reboot may be needed."
                            )
            elif bus_id:
                decky.logger.info(
                    "Deep rescan: eGPU connected and functional, skipping bridge removal"
                )
        return write_sysfs("/sys/bus/pci/rescan", "1")

    async def _systemctl(self, action: str, *units: str, timeout: int = 60) -> dict:
        cmd = ["/usr/bin/systemctl", action, *units]
        decky.logger.info(f"Running: {' '.join(cmd)}")
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=clean_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            decky.logger.error(f"systemctl {action} timed out")
            return {"ok": False, "error": f"systemctl {action} timed out."}
        except Exception as e:
            decky.logger.error(f"systemctl {action} raised {type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        decky.logger.info(
            f"systemctl {action} rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or "").strip() or f"exit {proc.returncode}"}
        return {"ok": True}

    async def _restart_display_manager_impl(self) -> dict:
        uid = resolve_host_uid()
        return await self._systemctl("restart", "display-manager.service", f"user@{uid}")

    async def _stop_display_manager_impl(self) -> dict:
        uid = resolve_host_uid()
        return await self._systemctl("stop", "display-manager.service", f"user@{uid}")

    async def _start_display_manager_impl(self) -> dict:
        uid = resolve_host_uid()
        return await self._systemctl("start", "display-manager.service", f"user@{uid}")

    async def _set_boot_vga(self, mode: str) -> dict:
        if self._operation_lock.locked():
            return {"ok": False, "error": "An eGPU Switch operation is already in progress."}
        async with self._operation_lock:
            binary = find_egpu_binary()
            if not binary:
                return {"ok": False, "error": "all-ways-egpu binary not found on this system."}
            if mode == "egpu":
                # After an eject, the eGPU is genuinely gone from lspci (removed from
                # the PCI bus), not just slow to enumerate - all-ways-egpu's own
                # internal retry loop only re-checks lspci, it never forces a rescan,
                # so it can never find a device that isn't on the bus at all. A rescan
                # is safe to run unconditionally here (never removes anything by
                # default, only detects new devices) and costs very little when
                # nothing changed. Goes through _rescan_pci_impl so Deep Rescan
                # (Advanced setting) also applies here, not just the standalone
                # button - this is the exact "won't reconnect after eject" case it
                # was added for.
                ok, err = await self._rescan_pci_impl()
                decky.logger.info(f"Pre-enable rescan: ok={ok} err={err!r}")
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [binary, "set-boot-vga", mode],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    env=clean_subprocess_env(),
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "all-ways-egpu set-boot-vga timed out."}
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            decky.logger.info(f"set-boot-vga {mode} rc={proc.returncode}\n{combined}")
            if proc.returncode != 0:
                return {"ok": False, "error": combined.strip() or f"exit code {proc.returncode}"}
            if NO_CONFIG_RE.search(combined):
                return {
                    "ok": False,
                    "error": "all-ways-egpu is not configured yet. Run 'all-ways-egpu setup' "
                    "once from a terminal (Desktop Mode), then try again.",
                }
            if mode == "egpu" and NO_EGPU_DETECTED_RE.search(combined):
                return {
                    "ok": False,
                    "error": "No eGPU detected. Check the Thunderbolt connection and try again.",
                }
            restart_result = await self._restart_display_manager_impl()
            if not restart_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"boot_vga set, but display manager restart failed: {restart_result.get('error')}",
                    "cli_output": combined.strip(),
                    "restart": restart_result,
                }
            return {"ok": True, "cli_output": combined.strip(), "restart": restart_result}

    async def _switch_to_igpu_and_eject(self) -> dict:
        """
        Switches boot_vga to the iGPU and, if an eGPU is connected, safely
        ejects it in the same operation (one stop/start cycle) instead of
        requiring a separate "Eject eGPU" press afterward. See
        unload_and_remove_pci()'s docstring for why the display manager has
        to be stopped before touching the nvidia modules.
        """
        if self._operation_lock.locked():
            return {"ok": False, "error": "An eGPU Switch operation is already in progress."}
        async with self._operation_lock:
            binary = find_egpu_binary()
            if not binary:
                return {"ok": False, "error": "all-ways-egpu binary not found on this system."}

            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [binary, "status"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=clean_subprocess_env(),
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "all-ways-egpu status timed out."}
            pre_status = parse_status(proc.stdout or "")

            # Snapshot PCI functions/drivers before touching boot_vga: the
            # boot_vga bind-mount trick never affects driver binding, so this
            # stays valid through the whole operation and lets us eject in
            # the same stop/start cycle instead of a second one right after.
            functions: list[str] = []
            drivers: dict[str, str | None] = {}
            if pre_status["egpu_connected"] and pre_status["bus_id"]:
                slot = pre_status["bus_id"].rsplit(".", 1)[0]
                functions = await list_pci_functions(slot)
                if not functions:
                    functions = [pre_status["bus_id"]]
                drivers = {f: get_pci_driver(f) for f in functions}

            stop_result = await self._stop_display_manager_impl()
            if not stop_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"Could not stop the display manager, aborting before touching "
                    f"anything: {stop_result.get('error')}",
                }

            error: str | None = None
            removed: list[str] = []
            try:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        [binary, "set-boot-vga", "internal"],
                        capture_output=True,
                        text=True,
                        timeout=45,
                        env=clean_subprocess_env(),
                    )
                    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
                    decky.logger.info(f"set-boot-vga internal rc={proc.returncode}\n{combined}")
                    if proc.returncode != 0:
                        error = combined.strip() or f"exit code {proc.returncode}"
                except subprocess.TimeoutExpired:
                    error = "all-ways-egpu set-boot-vga internal timed out."

                if error is None and functions:
                    # Same D-state stall guard as eject_egpu(): the finally
                    # below must always be reached so the session comes back.
                    try:
                        error, removed = await asyncio.wait_for(
                            unload_and_remove_pci(functions, drivers), timeout=90
                        )
                    except asyncio.TimeoutError:
                        error, removed = (
                            "Eject stalled in the kernel (module unload or PCI remove "
                            "stuck; the nvidia driver may be in a bad state from an "
                            "earlier failure). A reboot is likely required. Do NOT "
                            "disconnect the eGPU.",
                            [],
                        )
            finally:
                start_result = await self._start_display_manager_impl()
                decky.logger.info(f"Restart after switch-to-iGPU: {start_result}")

            if not start_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"iGPU switch {'succeeded' if error is None else 'failed'}, but the "
                    f"display manager did not come back up: {start_result.get('error')}. "
                    "Recover via SSH: sudo systemctl start display-manager.service user@<uid>.",
                    "restart": start_result,
                    "removed": removed,
                }
            if error is not None:
                return {"ok": False, "error": error, "restart": start_result, "removed": removed}

            message = "Switched to iGPU."
            if removed:
                message += " eGPU ejected, safe to disconnect the Thunderbolt cable now."
            return {"ok": True, "message": message, "removed": removed, "restart": start_result}
