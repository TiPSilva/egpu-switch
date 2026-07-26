#!/usr/bin/env python3
"""
Self-check for the logic that is dangerous when it is wrong. No framework, no
fixtures, no dependencies:

    python3 tests/selfcheck.py

Covers three pure functions:

  mounted_storage_under        refuses an eject that would yank a mounted disk
                               out of the enclosure, so a false "nothing
                               mounted" costs data
  devices_needing_authorization decides which Thunderbolt device gets DMA
                               access, so a false positive is a security hole
  parse_status                 every other decision reads from it

Everything else in main.py talks to real hardware and is validated on the
device instead.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# decky only exists inside the plugin host; main.py imports it at module level.
sys.modules.setdefault("decky", type(sys)("decky"))
sys.modules["decky"].logger = type(
    "L", (), {m: staticmethod(lambda *a, **k: None) for m in ("info", "warning", "error")}
)()

from main import (  # noqa: E402
    devices_needing_authorization,
    mounted_storage_under,
    parse_status,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}\n    got:  {got!r}\n    want: {want!r}")


# --- parse_status ---------------------------------------------------------
# Verbatim shape of `all-ways-egpu status` output, including the trailing
# service listing that must not be mistaken for bus id lines.
STATUS = """Method 1 setup with following Bus IDs
0000:c4:00.0 amdgpu
Method 2, 3 setup with following Bus IDs
0000:66:00.0 nvidia
0000:66:00.0 eGPU connected, not set as primary with Method 2
Method 1 auto switch at startup service
"""
s = parse_status(STATUS)
check("status: finds the configured bus id", s["bus_id"], "0000:66:00.0")
check("status: sees the eGPU as connected", s["egpu_connected"], True)
check("status: not primary means not active", s["egpu_active"], False)
check("status: setup detected", s["setup_done"], True)

check(
    "status: an unconfigured system reports nothing",
    parse_status("all-ways-egpu not setup")["setup_done"],
    False,
)
check(
    "status: 'No eGPU detected, retry 1' is progress, not a bus id",
    parse_status("Method 2, 3 setup with following Bus IDs\nNo eGPU detected, retry 1")[
        "bus_id"
    ],
    None,
)

# --- devices_needing_authorization ---------------------------------------
# boltctl prints a tree, so every field carries box-drawing prefixes. An
# anchored match against the uuid finds nothing here, which is how the first
# implementation of this silently did nothing at all.
BOLT = """ ● Tapex Creek eGPU
   ├─ uuid:          8d0e5c6b-1f2a-4b3c-9d8e-7a6b5c4d3e2f
   ├─ status:        {status}
   │  └─ authflags:  none
   └─ stored:        {stored}
"""
check(
    "bolt: unauthorized and known -> authorize",
    devices_needing_authorization(
        BOLT.format(status="connected", stored="Sun 13 Jul 2026 17:00:00")
    ),
    [("8d0e5c6b-1f2a-4b3c-9d8e-7a6b5c4d3e2f", True)],
)
check(
    "bolt: unauthorized and unknown -> reported, but not stored",
    devices_needing_authorization(BOLT.format(status="connected", stored="no")),
    [("8d0e5c6b-1f2a-4b3c-9d8e-7a6b5c4d3e2f", False)],
)
check(
    "bolt: already authorized -> left alone",
    devices_needing_authorization(
        BOLT.format(status="authorized", stored="Sun 13 Jul 2026 17:00:00")
    ),
    [],
)
check(
    "bolt: a November timestamp does not read as 'no'",
    devices_needing_authorization(
        BOLT.format(status="authorized", stored="Sat 15 Nov 2025 10:00:00")
    ),
    [],
)
check("bolt: empty input", devices_needing_authorization(""), [])

# --- mounted_storage_under ------------------------------------------------
# Fake sysfs: a GPU-only slot, and a bridge with an NVMe behind it whose
# partition carries a LUKS mapper. Mirrors the real layout, where a block
# device's path already contains the BDF of every PCI device above it.
root = tempfile.mkdtemp()
gpu = f"{root}/devices/pci0000:00/0000:65:00.0/0000:66:00.0"
nvme = f"{root}/devices/pci0000:00/0000:65:00.0/0000:67:00.0/nvme/nvme0/nvme0n1"
os.makedirs(f"{gpu}/drm/card0")
os.makedirs(f"{nvme}/nvme0n1p1")
os.makedirs(f"{root}/devices/virtual/block/dm-0/slaves")
os.makedirs(f"{root}/class/block")
os.symlink(nvme, f"{root}/class/block/nvme0n1")
os.symlink(f"{nvme}/nvme0n1p1", f"{root}/class/block/nvme0n1p1")
os.symlink(f"{root}/devices/virtual/block/dm-0", f"{root}/class/block/dm-0")
os.symlink(f"{nvme}/nvme0n1p1", f"{root}/devices/virtual/block/dm-0/slaves/nvme0n1p1")


def mounts_file(*lines):
    path = tempfile.mktemp()
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


NOTHING = mounts_file("proc /proc proc rw 0 0")
NVME_MOUNTED = mounts_file("/dev/nvme0n1p1 /mnt ext4 rw 0 0")
LUKS_MOUNTED = mounts_file("/dev/dm-0 /mnt/vault ext4 rw 0 0")

check(
    "storage: GPU-only slot is safe to detach",
    mounted_storage_under("0000:66:00.0", root, NVME_MOUNTED),
    [],
)
check(
    "storage: bridge with a mounted NVMe behind it is refused",
    mounted_storage_under("0000:65:00.0", root, NVME_MOUNTED),
    ["nvme0n1p1"],
)
check(
    "storage: LUKS on that NVMe is still found through its slaves",
    mounted_storage_under("0000:65:00.0", root, LUKS_MOUNTED),
    ["dm-0"],
)
check(
    "storage: a disk that is present but unmounted does not block",
    mounted_storage_under("0000:65:00.0", root, NOTHING),
    [],
)
check(
    "storage: unreadable mounts file fails closed to empty, never crashes",
    mounted_storage_under("0000:65:00.0", root, "/nonexistent"),
    [],
)

# --- report ---------------------------------------------------------------
if failures:
    print(f"FAIL ({len(failures)})\n")
    print("\n\n".join(failures))
    sys.exit(1)
print("selfcheck: all checks passed")
