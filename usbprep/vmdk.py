"""Extras > Prepare VMDK file for VirtualBox: a RAW-DISK DESCRIPTOR (a few KB of text) that points at the
whole selected USB drive. It does NOT contain the drive's data: the USB must stay connected, the device
path/permissions are host specific (regenerate on another host or after replugging under a new name), and
the guest only boots if the user installed a compatible bootloader in the ESP. Created with VirtualBox's own
VBoxManage (reads the partition table only; writes nothing to the USB). No VM is created, registered, started
or attached; the medium is unregistered from VirtualBox's media list right after creation.
"""
import json
import os
import re
import shutil
from typing import List, Optional

from . import constants as C
from .detect import Device, selectable
from .layout import check_layout, read_device_layout
from .runner import DEFAULT_RUNNER, CommandError

VIRTUAL_DIR = os.path.join("Cache", "Virtual")
MAX_DESCRIPTOR = 1024 * 1024   # a raw-disk descriptor is text; anything bigger means something went wrong
VBOX_DOWNLOAD = "https://www.virtualbox.org/wiki/Downloads"
VBOX_LINUX_DOWNLOAD = "https://www.virtualbox.org/wiki/Linux_Downloads"
VBOX_RAW_DOCS = "https://www.virtualbox.org/manual/topics/AdvancedTopics.html"   # "Raw Host Hard Disk Access"
WARNING = ("Raw-disk access: VirtualBox will read/write the REAL USB drive. Never use the drive on the host and "
           "in the VM at the same time (unmount it on the host first) - simultaneous access corrupts data. The "
           ".vmdk is only a pointer: copying it does not copy the USB contents; the USB must stay connected; the "
           "device path and permissions are specific to this host and may need regenerating. Booting needs a "
           "bootloader you install in the EFI partition and UEFI enabled in the VM.")


class VmdkError(RuntimeError):
    pass


def names(dev: Device):
    tag = re.sub(r"[^A-Za-z0-9]+", "_", (dev.serial or dev.name or os.path.basename(dev.path)))[-24:]
    base = f"usbprep-{tag}-{os.path.basename(dev.path)}"
    return base + ".vmdk", base + ".usbprep.json"


def readme_text(vmdk_path: str, dev: Device, vbox_version: str) -> str:
    """Manual, step-by-step guidance shown after creation. The app never creates/starts/attaches a VM."""
    v = os.path.basename(vmdk_path)
    return f"""VirtualBox raw-disk VMDK - what was created and how to use it
================================================================
Created file : {vmdk_path}
Points at    : {dev.path}  ({dev.model or '?'}, serial {dev.serial or '?'}, {dev.size} bytes)
Created with : VBoxManage {vbox_version}

WHAT THIS FILE IS (and is not)
- It is a small text DESCRIPTOR that points at the WHOLE physical USB drive (all 3 partitions). It is NOT a
  self-contained disk image: copying {v} copies none of the USB's contents.
- The USB must stay connected. The device reference ({dev.path}) and its permissions belong to THIS host;
  on another computer, or if the drive comes back under a different name, regenerate the descriptor there
  (Extras > Prepare VMDK file for VirtualBox) - an old descriptor would point at the wrong disk or fail.
- Importing/attaching this VMDK does NOT unmount the drive from the host and does not hand ownership to the
  VM. Use Extras > "Hand drive to VM (VirtualBox)..." before starting the VM, and "Return drive from VM..."
  after the VM is fully powered off. Host and guest must never use the drive at the same time.
- VirtualBox documents raw disk access as "expert users only" with a risk of total data loss:
  {VBOX_RAW_DOCS} (section "Raw Host Hard Disk Access").

1. INSTALL VIRTUALBOX (manual; nothing is downloaded by this app)
   Official downloads: {VBOX_DOWNLOAD}   (Linux packages: {VBOX_LINUX_DOWNLOAD})
   The base package is GPLv3. The Extension Pack (separate PUEL licence) is NOT needed for this.

2. HOST PRIVILEGES (Linux)
   VirtualBox opens {dev.path} as your user. That normally requires membership in the 'disk' group:
       sudo usermod -aG disk $USER      (then log out and in again)
   This gives your user raw access to ALL disks; remove it later with: sudo gpasswd -d $USER disk
   This app does not change group membership for you.

3. RELEASE THE DRIVE ON THE HOST (every time, before starting the VM)
   In this app select the drive and use Extras > "Hand drive to VM (VirtualBox)...". It unmounts /USB/A.I. and
   /USB (or any other mount of the drive), turns its swap off, flushes buffers and records that the VM owns
   the drive. If the app runs from this USB it refuses: start a host copy first (python3 -m usbprep host-copy).
   CLI: sh launchers/Linux/run.sh vm-handoff {dev.path} --execute --privilege pkexec [--vm "NAME"]

4. CREATE THE VIRTUAL MACHINE (VirtualBox Manager, VirtualBox 7.x wording)
   a. Machine > New. Name: e.g. USB-Boot-Test. Type "Other", Version "Other/Unknown (64-bit)".
      Leave "ISO Image" empty. Tick "Skip Unattended Installation" if offered.
   b. Hardware: 512 MB RAM and 1 CPU are plenty for the included Hello World example.
      "Enable EFI (special OSes only)": tick it for UEFI boot (EFI/BOOT/BOOTX64.EFI on the FAT32 partition);
      leave it unticked for legacy BIOS boot (boot code in the protective MBR). See FIRMWARE below.
   c. Hard Disk: choose "Use an Existing Virtual Hard Disk File", click the folder icon, Add, and select
      {vmdk_path}
      (or a copy of it in another folder on this host). Finish.
   d. Settings > Storage: the VMDK should be the only hard disk. Do NOT add this drive under Settings > USB as a
      USB device filter - raw-disk access and USB passthrough are different mechanisms; never combine them for
      the same drive.
   Command-line equivalent (check `VBoxManage <command> --help` on your version):
       VBoxManage createvm --name USB-Boot-Test --ostype Other_64 --register
       VBoxManage modifyvm USB-Boot-Test --memory 512 --firmware efi      (or --firmware bios)
       VBoxManage storagectl USB-Boot-Test --name SATA --add sata
       VBoxManage storageattach USB-Boot-Test --storagectl SATA --port 0 --device 0 --type hdd --medium "{vmdk_path}"

FIRMWARE: UEFI vs legacy BIOS
   - UEFI (EFI enabled): the firmware looks for \EFI\BOOT\BOOTX64.EFI on the FAT32 partition (GPT type ESP).
     x86-64 only for the included example; VirtualBox Secure Boot is not supported by the example (unsigned).
   - BIOS (EFI disabled): the firmware runs the 440-byte boot code in sector 0 (protective MBR). A freshly
     prepared drive has NO boot code there until you install some (e.g. Extras > Install Hello World boot
     example). Some BIOS implementations refuse to boot GPT disks at all.
   - An ESP partition type alone does not make the drive bootable: you provide the bootloader.

5. START, TEST, SHUT DOWN
   Start the VM. When finished, shut the guest down FULLY (Machine > ACPI Shutdown or Close > Power off the
   machine). Do NOT use "Save the machine state": a saved/paused VM still references the disk.

6. RETURN THE DRIVE TO THE HOST (before editing files again)
   Extras > "Return drive from VM..." (checks the VM is powered off, the raw device is no longer open, the
   identity and partition layout are unchanged), then the normal pre-mount check and mount at /USB and /USB/A.I.
   CLI: sh launchers/Linux/run.sh vm-return {dev.path} --execute --privilege pkexec --vm "NAME"
   Edit your bootloader files on /USB, hand the drive to the VM again, repeat. When it works, test on real
   hardware.
"""


def identity_record(dev: Device, fingerprint: dict) -> dict:
    return {"device": dev.identity(), "layout": fingerprint, "note": "raw-disk descriptor; see README in Cache/Virtual"}


def status(out_dir: str, dev: Device, fingerprint: dict) -> str:
    vm, side = names(dev)
    if not os.path.exists(os.path.join(out_dir, vm)):
        return "absent"
    try:
        with open(os.path.join(out_dir, side)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return "foreign"          # not created by usbprep: never overwritten
    return "current" if rec.get("device") == dev.identity() and rec.get("layout") == json.loads(json.dumps(fingerprint)) else "stale"


_VERSION_LINE = re.compile(r"^\s*(\d+)\.(\d+)[.\w]*\s*$", re.M)


def parse_vbox_version(output: str):
    """Return (version_string, warnings) from `VBoxManage --version` output.

    On Linux VBoxManage may print a multi-line warning (e.g. "WARNING: The vboxdrv kernel module is not
    loaded...") BEFORE the version, so the version is not necessarily the first line. Raises VmdkError if no
    version line is found at all."""
    output = output or ""
    m = None
    for m in _VERSION_LINE.finditer(output):
        pass                                   # keep the LAST matching line: the version comes after warnings
    if not m:
        shown = output.strip() or "(no output)"
        raise VmdkError("could not read the VirtualBox version from `VBoxManage --version`; it printed:\n"
                        f"{shown}\nCheck that VirtualBox is installed correctly (on Linux: `sudo /sbin/vboxconfig`).")
    version = m.group(0).strip()
    warnings = (output[:m.start()] + output[m.end():]).strip()
    return version, warnings


def vbox_command(vbox: str, version: str, out: str, dev_path: str) -> List[str]:
    m = re.match(r"\s*(\d+)", version or "")
    if not m:
        raise VmdkError(f"unrecognised VirtualBox version {version!r}")
    major = int(m.group(1))
    if major >= 7:
        return [vbox, "createmedium", "disk", "--filename", out, "--format", "VMDK", "--variant", "RawDisk",
                "--property", f"RawDrive={dev_path}"]
    return [vbox, "internalcommands", "createrawvmdk", "-filename", out, "-rawdisk", dev_path]


def prepare_vmdk(dev: Device, out_dir: Optional[str], execute=False, confirmed=False, replace=False,
                 allow_ambiguous=False, runner=DEFAULT_RUNNER, lister=None, vbox=None,
                 access=os.access) -> List[str]:
    why = selectable(dev, allow_ambiguous)
    if why:
        raise VmdkError(f"{dev.path}: {why}")
    lay = read_device_layout(dev.path, runner)
    if not check_layout(lay).prepared:
        raise VmdkError("the drive does not have the prepared layout; prepare it first")
    if not out_dir:
        raise VmdkError("cannot find the drive's EFI:/AI folder to store the descriptor: mount the drive first "
                        "(Mount button) or run the app from it")
    fp = lay.fingerprint()
    vm, side = names(dev)
    out, side_p = os.path.join(out_dir, vm), os.path.join(out_dir, side)
    st = status(out_dir, dev, fp)
    log = [f"descriptor: {out} (status: {st})", "WARNING: " + WARNING]
    if st == "foreign":
        raise VmdkError(f"{out} exists and was not created by usbprep; refusing to overwrite it")
    if st == "current" and not replace:
        rd = out[:-5] + ".README.txt"
        return log + ["descriptor is current for this device and layout - nothing to do"] + \
            ([f"step-by-step VirtualBox instructions: {rd}"] if os.path.exists(rd) else [])
    if st == "stale" and not replace:
        raise VmdkError("existing descriptor belongs to a different device identity or partition layout (stale); "
                        "re-run with replace consent to regenerate it")
    vbox = vbox or shutil.which("VBoxManage") or shutil.which("vboxmanage")
    if not vbox:
        raise VmdkError(f"VirtualBox (VBoxManage) is not installed. Install it from {VBOX_DOWNLOAD} or your "
                        "distribution's packages (base package GPLv3; the Extension Pack has a separate PUEL "
                        "license and is not needed for this), then retry. Nothing is downloaded automatically.")
    res = runner.run([vbox, "--version"], check=False)
    version, vbox_warn = parse_vbox_version((res.stdout or "") + "\n" + (res.stderr or ""))
    if vbox_warn:
        log.append("VirtualBox reported: " + vbox_warn)
        if "vboxdrv" in vbox_warn:
            log.append("NOTE: the descriptor can still be created, but VMs will not START until the vboxdrv kernel "
                       "module loads (Linux: run `sudo /sbin/vboxconfig`; with Secure Boot on, the module must be "
                       "signed/enrolled or Secure Boot disabled)")
    cmd = vbox_command(vbox, version, out, dev.path)
    log.append("command: " + " ".join(cmd))
    if not access(dev.path, os.R_OK | os.W_OK):
        log.append(f"NOTE: your user cannot open {dev.path} for raw access; on Linux this usually needs membership "
                   "in the 'disk' group (log out/in after `sudo usermod -aG disk $USER`) - a host-specific step "
                   "this tool does not perform for you")
    if not execute:
        return log + ["DRY RUN - add --execute (GUI: untick Dry run) after reading the warning"]
    if not confirmed:
        raise VmdkError("informed confirmation of the raw-disk warning is required")
    if not access(dev.path, os.R_OK | os.W_OK):
        raise VmdkError(f"no raw access to {dev.path}; perform the host step above, then retry")
    cur = {d.path: d for d in (lister() if lister else [])}.get(dev.path) if lister else dev
    if cur is None or cur.identity() != dev.identity():
        raise VmdkError(f"{dev.path} changed or disappeared; aborting")
    os.makedirs(out_dir, exist_ok=True)
    if st == "stale":
        for p in (out, side_p, out[:-5] + ".README.txt"):
            if os.path.exists(p):
                os.replace(p, p + ".stale")
        log.append("previous stale descriptor kept as *.stale")
    try:
        runner.run(cmd)
    except CommandError as e:
        raise VmdkError(f"VBoxManage failed: {e}")
    runner.run([vbox, "closemedium", "disk", out], check=False)   # unregister only; the file stays
    created = [f for f in os.listdir(out_dir) if f.startswith(vm[:-5])]
    big = [f for f in created if os.path.getsize(os.path.join(out_dir, f)) > MAX_DESCRIPTOR]
    if not os.path.isfile(out) or big:
        raise VmdkError(f"unexpected VBoxManage output {created}; expected a small descriptor only")
    with open(out, errors="replace") as f:
        head = f.read(4096)
    if "Disk DescriptorFile" not in head or dev.path not in head:
        raise VmdkError(f"{out} does not look like a raw-disk descriptor for {dev.path}")
    with open(side_p, "w") as f:
        json.dump(identity_record(dev, fp), f, indent=1)
    readme = write_readme(out, dev, version)
    return log + [f"created raw-disk descriptor {out} ({os.path.getsize(out)} B); not registered with any VM",
                  f"step-by-step VirtualBox instructions: {readme}", "", open(readme).read()]


def write_readme(vmdk_path: str, dev: Device, vbox_version: str) -> str:
    path = vmdk_path[:-len(".vmdk")] + ".README.txt"
    with open(path, "w", newline="\r\n") as f:          # CRLF: readable in Notepad too
        f.write(readme_text(vmdk_path, dev, vbox_version))
    return path


def find_ai_dir(dev: Device, source_disk: Optional[str], app_root: str) -> Optional[str]:
    """EFI:/AI of the selected drive: the running app folder (source drive) or the mounted p1."""
    if source_disk and dev.path == source_disk:
        return app_root
    for ch in dev.raw.get("children") or []:
        for mp in ch.get("mountpoints") or []:
            if mp and os.path.isfile(os.path.join(mp, C.APP_DIR_NAME, C.MANIFEST_NAME)):
                return os.path.join(mp, C.APP_DIR_NAME)
    return None
