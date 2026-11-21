"""Linux host <-> VirtualBox guest ownership handoff for the raw-disk VMDK edit/test loop.

A VMDK descriptor does NOT unmount anything: VirtualBox simply opens the raw device. If the host still has
the partitions mounted, host and guest write the same blocks and corrupt them. So ownership is switched
deliberately:
  handoff (host -> VM): identity re-check, refuse open handles, unmount ext4 + FAT32 (any mount point),
      swapoff, sync + flush, verify nothing is mounted, record a marker. Normal mount refuses while marked.
  return (VM -> host): VM must be fully powered off (not running/paused/saved), no process may hold the raw
      device, identity re-checked, layout change => refuse auto-mount; then the normal safe fsck + mount.
No VM is chosen, configured, started or stopped by this module. Raw-disk VMDK and VirtualBox USB passthrough
are different mechanisms: never configure both for the same drive.
"""
import json
import os
import time
from typing import Callable, List, Optional

from .detect import Device
from .executor import privilege_prefix
from .layout import check_layout, read_device_layout
from .reformat import processes_using
from .runner import CommandError
from .safety import SafetyError

SAFE_VM_STATES = ("poweroff", "aborted")
GUEST_STEPS = ("In VirtualBox (manual, your VM): Settings > System > enable EFI; Storage > add the .vmdk from "
               "EFI:/AI/Cache/Virtual as a hard disk; do NOT add a USB device filter for this drive (raw disk and "
               "USB passthrough must not be combined). Start the VM. When finished, shut the guest down fully "
               "(no 'Save state'), then use 'Return drive from VM'.")


class HandoffRefused(RuntimeError):
    pass


def state_dir() -> str:
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "usbprep")


def marker_path(dev: Device, base: Optional[str] = None) -> str:
    tag = "".join(c if c.isalnum() else "_" for c in (dev.serial or os.path.basename(dev.path)))
    return os.path.join(base or state_dir(), f"vm-handoff-{tag}.json")


def read_marker(dev: Device, base: Optional[str] = None) -> Optional[dict]:
    try:
        with open(marker_path(dev, base)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def assert_host_owned(dev: Device, base: Optional[str] = None) -> None:
    """Called before any host mount: refuse while the drive is handed to a VM."""
    m = read_marker(dev, base)
    if m:
        raise SafetyError(f"{dev.path} is handed to a VM since {m.get('time')} (VM: {m.get('vm') or 'unspecified'}); "
                          "power the VM off and use 'Return drive from VM' first")


def vm_state(runner, vm: str, vbox: str = "VBoxManage") -> str:
    out = runner.run([vbox, "showvminfo", vm, "--machinereadable"]).stdout
    for line in out.splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip().strip('"')
    raise HandoffRefused(f"could not read the state of VM '{vm}'")


def handoff(dev: Device, runner, lister: Callable[[], List[Device]], privilege=None, execute=False,
            vm: Optional[str] = None, app_root: Optional[str] = None, busy: Callable = processes_using,
            base: Optional[str] = None) -> List[str]:
    cur = {d.path: d for d in lister()}.get(dev.path)
    if cur is None or cur.identity() != dev.identity():
        raise HandoffRefused(f"{dev.path} changed or disappeared; nothing done")
    if not check_layout(read_device_layout(dev.path, runner)).prepared:
        raise HandoffRefused("drive is not in the prepared layout; nothing done")
    if read_marker(dev, base):
        return [f"{dev.path} is already handed to a VM (marker {marker_path(dev, base)})"]
    mps = sorted({m for m in cur.mountpoints if m and m != "[SWAP]"}, key=len, reverse=True)
    swaps = [ch["path"] for ch in cur.raw.get("children") or [] if "[SWAP]" in (ch.get("mountpoints") or [])]
    if app_root and any(os.path.realpath(app_root).startswith(os.path.realpath(m) + os.sep) for m in mps):
        raise HandoffRefused("this app is running from the drive being handed over. Start a host copy first "
                             "(`python3 -m usbprep host-copy`, then run the printed launcher) and retry from it")
    users = busy(mps)
    if users:
        raise HandoffRefused("close these programs first (they use the drive): " + "; ".join(users))
    p = privilege_prefix(privilege)
    steps = [p + ["umount", m] for m in mps] + [p + ["swapoff", s] for s in swaps] + \
            [["sync"], p + ["blockdev", "--flushbufs", dev.path]]
    log = [f"handoff {dev.path} host -> VM" + (f" '{vm}'" if vm else "")] + ["  " + " ".join(s) for s in steps]
    if not execute:
        return log + ["DRY RUN - nothing changed", GUEST_STEPS]
    if vm and vm_state(runner, vm) not in SAFE_VM_STATES:
        raise HandoffRefused(f"VM '{vm}' must be powered off before the drive is handed over")
    for s in steps:
        runner.run(s)
    after = {d.path: d for d in lister()}.get(dev.path)
    if after is None or after.identity() != dev.identity():
        raise HandoffRefused(f"{dev.path} changed during handoff; do not start the VM - re-check the drive")
    still = [m for m in after.mountpoints if m]
    if still:
        raise HandoffRefused(f"still mounted/active after unmount: {still} (desktop automount?); do not start the VM")
    os.makedirs(base or state_dir(), exist_ok=True)
    with open(marker_path(dev, base), "w") as f:
        json.dump({"device": dev.identity(), "layout": read_device_layout(dev.path, runner).fingerprint(),
                   "vm": vm, "time": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=1)
    return log + ["drive released by the host (nothing mounted, swap off, buffers flushed)", GUEST_STEPS,
                  "Note: replugging the drive may trigger desktop automount - do not replug while the VM uses it."]


def give_back(dev: Device, runner, lister: Callable[[], List[Device]], vm: Optional[str] = None,
              vm_powered_off_ack: bool = False, busy: Callable = processes_using,
              base: Optional[str] = None) -> List[str]:
    """Checks before host reuse. The caller then runs the normal fsck + mount path."""
    m = read_marker(dev, base)
    if not m:
        raise HandoffRefused(f"{dev.path} is not marked as handed to a VM; use the normal Mount")
    vm = vm or m.get("vm")
    log = []
    if vm:
        st = vm_state(runner, vm)
        if st not in SAFE_VM_STATES:
            raise HandoffRefused(f"VM '{vm}' state is '{st}': shut the guest down fully (a saved/paused VM still "
                                 "references the disk), then retry")
        log.append(f"VM '{vm}' is {st}")
    elif not vm_powered_off_ack:
        raise HandoffRefused("confirm the VM using the drive is fully powered off (not saved/paused), or name it")
    holders = busy([dev.path] + [ch["path"] for ch in dev.raw.get("children") or []])
    if holders:
        raise HandoffRefused("the raw device is still open (VirtualBox running?): " + "; ".join(holders))
    cur = {d.path: d for d in lister()}.get(dev.path)
    if cur is None or cur.identity() != dev.identity():
        raise HandoffRefused(f"{dev.path} identity changed or the drive is missing; nothing mounted")
    try:
        lay = read_device_layout(dev.path, runner)
    except CommandError as e:
        raise HandoffRefused(f"cannot read the partition table: {e}")
    if json.loads(json.dumps(lay.fingerprint())) != m.get("layout"):
        raise HandoffRefused("the partition layout changed while the VM owned the drive; not auto-mounting. "
                             "Inspect it first:\n" + check_layout(lay).summary())
    os.remove(marker_path(dev, base))
    return log + ["drive returned to the host (VM powered off, raw device free, identity + layout unchanged)"]
