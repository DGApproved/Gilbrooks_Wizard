"""Safety guards: target validation, mount checks and TOCTOU revalidation."""
import os
import stat
from typing import Callable, Optional

from . import constants as C
from .detect import Device, list_linux_devices, selectable
from .layout import read_device_layout
from .runner import DEFAULT_RUNNER

# Set True by the test suite (conftest.py). When True, block devices are refused
# unless REAL_DEVICE_ENV == "1" (the test suite never sets it).
REAL_DEVICES_BLOCKED = False


class SafetyError(RuntimeError):
    pass


def check_image_target(path: str) -> None:
    if not os.path.exists(path):
        raise SafetyError(f"image {path} does not exist")
    st = os.stat(path)  # follows symlinks: a symlink to /dev/sdX is caught here
    if not stat.S_ISREG(st.st_mode):
        raise SafetyError(f"{path} is not a regular image file; refusing")


def check_device_target(path: str) -> None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise SafetyError(f"device {path} not present (unplugged?)")
    if not stat.S_ISBLK(st.st_mode):
        raise SafetyError(f"{path} is not a block device")
    if REAL_DEVICES_BLOCKED and os.environ.get(C.REAL_DEVICE_ENV) != "1":
        raise SafetyError(f"real block device {path} refused in test/safe mode")


def proc_mounted_sources(mounts_file="/proc/self/mounts", swaps_file="/proc/swaps") -> set:
    src = set()
    for fn, col in ((mounts_file, 0), (swaps_file, 0)):
        try:
            with open(fn) as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[col].startswith("/dev/"):
                        src.add(os.path.realpath(parts[col]))
        except OSError:
            pass
    return src


def device_busy(dev: Device, mounted_sources: Optional[set] = None) -> list:
    """Mounted/swap-active nodes belonging to dev (lsblk view + /proc view)."""
    mounted_sources = proc_mounted_sources() if mounted_sources is None else mounted_sources
    nodes = {dev.path} | {c.get("path") for c in dev.raw.get("children") or [] if c.get("path")}
    busy = sorted(n for n in nodes if os.path.realpath(n) in mounted_sources)
    if dev.mountpoints:
        busy += [f"mounted at {m}" for m in dev.mountpoints]
    return busy


def revalidate(expected: Device, expected_fingerprint: Optional[dict], allow_ambiguous: bool = False,
               runner=DEFAULT_RUNNER, lister: Callable = None, mounted_sources: Optional[set] = None,
               check_node: bool = True) -> Device:
    """Re-read everything immediately before a write; raise SafetyError on ANY change."""
    if check_node:
        check_device_target(expected.path)
    current = {d.path: d for d in (lister or (lambda: list_linux_devices(runner)))()}
    cur = current.get(expected.path)
    if cur is None:
        raise SafetyError(f"{expected.path} disappeared (unplugged/replugged?)")
    if cur.identity() != expected.identity():
        raise SafetyError(f"device identity changed: was {expected.identity()}, now {cur.identity()}")
    why = selectable(cur, allow_ambiguous)
    if why:
        raise SafetyError(f"device no longer eligible: {why}")
    busy = device_busy(cur, mounted_sources)
    if busy:
        raise SafetyError(f"device is in use: {', '.join(busy)}")
    if expected_fingerprint is None:  # after our own table write the layout legitimately changed
        return cur
    fp = read_device_layout(expected.path, runner).fingerprint()
    if fp != expected_fingerprint:
        raise SafetyError("partition layout changed since it was reviewed (concurrent modification?)")
    return cur
