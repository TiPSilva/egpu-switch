import asyncio
import glob
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

    # ---- actions ----

    async def enable_egpu(self) -> dict:
        return await self._set_boot_vga("egpu")

    async def disable_egpu(self) -> dict:
        return await self._switch_to_igpu_and_eject()

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

            try:
                error, removed = await unload_and_remove_pci(functions, drivers)
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
        """Non-destructive: only detects newly-attached PCI devices, never removes anything."""
        ok, err = write_sysfs("/sys/bus/pci/rescan", "1")
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
                # is safe to run unconditionally here (never removes anything, only
                # detects new devices) and costs very little when nothing changed.
                ok, err = write_sysfs("/sys/bus/pci/rescan", "1")
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
                    error, removed = await unload_and_remove_pci(functions, drivers)
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
