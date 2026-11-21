"""Per-OS capability matrix (single source of truth) and Windows/macOS adapters.

Only Linux was tested on the build host. Windows/macOS detection parsers are unit-tested
with mocked command output only; every write/mount operation on those OSes fails closed.
"""
import json
import plistlib
import sys
from typing import List

from . import constants as C
from .detect import Device, HDD_SSD_WORDS, READER_WORDS, finish

W, U, B, A = C.WORKING_TESTED, C.IMPLEMENTED_UNTESTED, C.BLOCKED, C.ALTERNATIVE

CAPABILITIES = {
    "linux": {
        "detect USB flash drives": (W, "lsblk heuristics; fixture-tested + real lsblk run on build host (no USB attached there)"),
        "detect prepared layout": (W, "tested against real GPT images built with sgdisk/mkfs tools"),
        "prepare/format (image file)": (W, "real sgdisk/mkfs.vfat/mkfs.ext4/mkswap on sparse image files"),
        "prepare/format (physical USB)": (U, "same plan + TOCTOU guards; never run on a real device during development"),
        "copy app to EFI:/AI": (W, "tested via mtools on image + directory backend; SHA-256 verified"),
        "FAT32 RW mount at literal /USB": (U, "kernel vfat; needs root (sudo/pkexec); mock-tested only - no loop devices on build host"),
        "ext4 RW+exec mount at literal /USB/A.I.": (U, "kernel ext4, rw,exec,nosuid,nodev; user-owned root; probe verified only on tmp dirs"),
        "open file manager": (U, "xdg-open /USB/A.I.; mock-tested"),
        "unmount/eject": (U, "umount + udisksctl power-off; mock-tested"),
        "launch on insertion": (A, "opt-in udev rule + systemd user service installed by setup/linux_setup.sh after consent"),
        "Java 8+ Swing GUI": (W, "jar built with javac --release 8 (class major 52); self-test run under Xvfb on Temurin "
                                 "1.8.0_514 and OpenJDK 17.0.21 (Linux x64 only)"),
        "Java 8+ bootstrap + offline installer cache": (W, "launchers/java_bootstrap.sh: detect/skip, verified cached Temurin "
                                                         ".tar.gz per-user install, refusal cases - tested in a sandboxed HOME"),
        "ESP-typed FAT32 partition (ESP-ready, not bootable by itself)": (W, "GPT type C12A7328-... set by sgdisk; verified on images"),
        "VirtualBox raw-disk VMDK descriptor": (U, "VBoxManage createmedium --variant RawDisk; mock-tested only (VirtualBox "
                                                   "not installed on the build host, no physical USB)"),
        "host <-> VM drive handoff (VMDK edit/test loop)": (U, "unmount FAT32+ext4, swapoff, flush, marker blocks host mount; "
                                                              "return needs VM poweroff/aborted; guardrails mock-tested only"),
    },
    "windows": {
        "detect USB flash drives": (U, "PowerShell Get-Disk/Get-PhysicalDisk JSON parser; unit-tested with mocked JSON only"),
        "detect prepared layout": (B, "not implemented on Windows; inspect on Linux"),
        "prepare/format (physical USB)": (B, "no native ext4 formatter; prepare the drive on Linux"),
        "copy app to EFI:/AI": (A, "FAT32 gets a drive letter natively; copy the app folder manually or run on Linux"),
        "FAT32 RW mount at literal /USB": (B, "no literal /USB on Windows; FAT32 is RW natively at a drive letter (e.g. E:\\); "
                                           "optional NTFS folder mount point such as C:\\USB is ALTERNATIVE-PROPOSED"),
        "ext4 RW+exec mount at literal /USB/A.I.": (B, "no redistributable native Windows ext4 RW driver; ALTERNATIVE: WSL2 "
                                                     "`wsl --mount --partition 2 --type ext4` (admin, Win11/Store WSL) - USB "
                                                     "flash drives need usbipd-win attach first; appears under /mnt/wsl/... "
                                                     "and \\\\wsl$\\<distro>\\...; Linux binaries run only inside WSL"),
        "ext4 backend: native Ext4Fsd (default)": (U, "signed GPLv2 kernel driver, MANUAL install by user; RW except bigalloc/quota/"
                                                    "project/verity/orphan_present (forced RO); refuses ea_inode/mmp/inline_data/"
                                                    "encrypt/casefold/largedir; HVCI BSOD reports (issue #107); never run here"),
        "ext4 backend: WSL checkbox (opt-in)": (U, "WSL2 + usbipd-win; used ONLY when the WSL checkbox / -UseWSL is chosen; no fallback"),
        "ext4 backend: Custom driver checkbox": (U, "user script /AI/Driver/Windows/ext4.sh via Git Bash/MSYS2 (never WSL); explicit "
                                                  "consent; independent mount check; script not sandboxed"),
        "open file manager": (U, "explorer.exe <path>; untested"),
        "unmount/eject": (A, "use Windows 'Safely Remove Hardware' / `wsl --unmount`"),
        "launch on insertion": (A, "opt-in Task Scheduler task (event 2003, DriverFrameworks-UserMode) via setup/windows_setup.ps1; untested"),
        "Java 8+ Swing GUI": (U, "same jar; detection + support matrix work, drive operations fail closed; never run on Windows"),
        "Java 8+ bootstrap + offline installer cache": (U, "launchers/Windows/bootstrap_java.ps1 (.zip per user / signed .msi via "
                                                         "UAC; SHA-256 + Authenticode); never run, PowerShell syntax not checked"),
        "ESP-typed FAT32 partition (ESP-ready, not bootable by itself)": (A, "visible as a FAT32 drive letter only if Windows assigns one"),
        "VirtualBox raw-disk VMDK descriptor": (A, "not implemented on Windows; manual: VBoxManage createmedium disk --variant "
                                                   "RawDisk --property RawDrive=\\\\.\\PhysicalDriveN (admin)"),
        "host <-> VM drive handoff (VMDK edit/test loop)": (B, "not implemented; manual: eject/offline the disk in Disk "
                                                              "Management (admin) before starting the VM; online it after full power-off"),
    },
    "darwin": {
        "detect USB flash drives": (U, "diskutil -plist parser; unit-tested with mocked plist only"),
        "detect prepared layout": (B, "not implemented on macOS; inspect on Linux"),
        "prepare/format (physical USB)": (B, "no native ext4 formatter; prepare the drive on Linux"),
        "copy app to EFI:/AI": (A, "FAT32 auto-mounts at /Volumes/EFI; copy manually or run on Linux"),
        "FAT32 RW mount at literal /USB": (B, "root volume is read-only (SIP/SSV); /etc/synthetic.conf is a privileged host change "
                                           "and only creates links; ALTERNATIVE: `diskutil mount -mountPoint /Volumes/USB diskNs1`"),
        "ext4 RW+exec mount at literal /USB/A.I.": (B, "no native ext4; macFUSE is closed-source, not freely redistributable "
                                                     "(commercial bundling needs a paid license), needs kernel-extension approval; "
                                                     "fuse-ext2 write support is flagged risky by its author; Linux ELF binaries "
                                                     "would not execute on macOS anyway"),
        "ext4 backend: Read-only driver (ext4fuse)": (U, "ext4fuse + macFUSE via Homebrew (setup readonly-install, consent); "
                                                       "READ-ONLY, verified by refused write; app writes refused; formula deprecated "
                                                       "(disable 2027-06-21); feature compatibility unverified; never run here"),
        "ext4 backend: Homebrew checkbox (anylinuxfs RW)": (U, "Apple Silicon only (Intel BLOCKED); Linux ext4 driver in libkrun "
                                                             "microVM over NFS; GPL-3.0; installed via Homebrew with consent; never run here"),
        "ext4 backend: Custom driver checkbox": (U, "user script /AI/Driver/MacOS/ext4.sh via /bin/sh; explicit consent; "
                                                  "independent mount check; script not sandboxed"),
        "open file manager": (U, "`open <path>`; untested"),
        "unmount/eject": (A, "`diskutil eject diskN`"),
        "launch on insertion": (A, "opt-in launchd agent (StartOnMount) via setup/macos_setup.sh; untested"),
        "Java 8+ Swing GUI": (U, "same jar; detection + support matrix work, drive operations fail closed; never run on macOS"),
        "Java 8+ bootstrap + offline installer cache": (U, "same java_bootstrap.sh (.tar.gz per user / signed .pkg); never run on macOS"),
        "ESP-typed FAT32 partition (ESP-ready, not bootable by itself)": (A, "auto-mounts as FAT32; ESP may be hidden by Finder"),
        "VirtualBox raw-disk VMDK descriptor": (A, "not implemented on macOS; manual: VBoxManage ... RawDrive=/dev/diskN "
                                                   "after diskutil unmountDisk"),
        "host <-> VM drive handoff (VMDK edit/test loop)": (B, "not implemented; manual: quit apps using it, `diskutil unmountDisk "
                                                              "/dev/diskN`, start the VM; after full VM power-off `diskutil mountDisk`"),
    },
}

# features added later (same matrix; Windows/macOS rows are honest blockers, not driver claims)
CAPABILITIES["linux"].update({
    "Hello World boot example (stock + preserve/fresh)": (W, "stock NASM MBR code + x86-64 UEFI app built here; deployed on "
        "fresh image preparation; booted in QEMU 7.2 SeaBIOS 1.16.2 and OVMF 2022.11 (image, snapshot) - greeting, waits "
        "for Enter, resets. Physical PCs/USB untested; hybrid MBR NOT implemented (protective MBR kept)"),
    "Clone ext4 partition USB -> USB": (U, "rsync -aHAX --numeric-ids between private mounts (source ro,noload); rsync "
        "copy/verify tested on directories, device flow mock-tested only"),
    "Scripts menu (AI/Scripts/<OS>)": (W, "discovery/filtering/argv/no-root checks tested; default terminal script "
        "refuses without a verified /USB/A.I. mount; real terminal launch untested"),
    "Full format option (zero-fill each partition, then create filesystems)": (W, "image: real zero-fill of exactly "
        "each partition extent, GPT/gap untouched (tested); physical USB: same plan on partition nodes, mock-tested "
        "only. Not a secure erase, no media verification, no bad-block scan"),
    "Data backup/restore (home <-> ext4 /Data)": (U, "rsync -axHAX + chmod 777 on the backup subtree, manifest of "
        "original modes, non-deleting restore with preview/typed confirmation; tested with real rsync on fixture "
        "homes and temp dirs only - never on a real home or physical USB"),
    "Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star)": (W, "per-user stars.json (0600, "
        "atomic) keyed by serial+model+vendor+size, never /dev path; no serial -> session-only; overrides flash "
        "detection only (system/internal/no-medium/too-small refusals stay); tested on lsblk fixtures, mocks and "
        "the headless GUI self-test - never on a physical USB HDD"),
})
for _os in ("windows", "darwin"):
    CAPABILITIES[_os].update({
        "Hello World boot example (stock + preserve/fresh)": (B, "drive preparation is Linux-only; the FAT32 sources "
            "are visible and editable, build/install on Linux"),
        "Clone ext4 partition USB -> USB": (B, "no native ext4 driver; clone on Linux (Clone menu) or manually "
            "`sudo rsync -aHAX --numeric-ids` between two mounted ext4 partitions"),
        "Scripts menu (AI/Scripts/<OS>)": (U, ("PowerShell .ps1 / .bat / .cmd / .py" if _os == "windows" else
            ".sh / .command / .zsh / .py") + "; never run on this OS; default terminal script explains the ext4 blocker"),
        "Full format option (zero-fill each partition, then create filesystems)": (B, "formatting is Linux-only; the "
            "checkbox is passed to the backend, which fails closed on this OS"),
        "Data backup/restore (home <-> ext4 /Data)": (B, "DISABLED: needs ext4 RW, which is blocked on this OS. " + (
            "robocopy /E /COPY:DAT /XJ (no /MIR) + icacls builders exist, unit-tested only" if _os == "windows" else
            "rsync capability detection (Apple rsync/openrsync lack -A/-X) builders exist, unit-tested only")),
        "Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star)": (U, ("Get-Disk BusType/IsBoot/"
            "IsSystem" if _os == "windows" else "diskutil Internal/SystemImage/BusProtocol") + " parsing with the same "
            "star logic, mock-tested only; a star makes the drive listable/eligible, but drive operations stay "
            "Linux-only"),
    })


def current_os() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)


def matrix_markdown() -> str:
    caps = list(CAPABILITIES["linux"].keys())
    for k in CAPABILITIES["windows"]:
        if k not in caps:
            caps.append(k)
    rows = ["| Capability | Linux | Windows | macOS |", "|---|---|---|---|"]
    for cap in caps:
        cells = [CAPABILITIES[o].get(cap, ("n/a", ""))[0] for o in ("linux", "windows", "darwin")]
        rows.append(f"| {cap} | " + " | ".join(cells) + " |")
    notes = []
    for o, name in (("linux", "Linux"), ("windows", "Windows"), ("darwin", "macOS")):
        notes.append(f"\n**{name} notes**\n")
        notes += [f"- *{cap}* - {st}: {why}" for cap, (st, why) in CAPABILITIES[o].items()]
    return "\n".join(rows + notes)


class UnsupportedOperation(RuntimeError):
    pass


def fail_closed(os_name: str, capability: str):
    st, why = CAPABILITIES[os_name].get(capability, (B, "unsupported"))
    raise UnsupportedOperation(f"{capability} on {os_name}: {st} - {why}")


# ---------- Windows (UNTESTED; mocked-output unit tests only) ----------
WINDOWS_DETECT_PS = ("Get-Disk | ForEach-Object { $p = Get-PhysicalDisk -DeviceNumber $_.Number "
                     "-ErrorAction SilentlyContinue; [pscustomobject]@{Number=$_.Number; FriendlyName=$_.FriendlyName; "
                     "SerialNumber=$_.SerialNumber; Size=$_.Size; BusType=[string]$_.BusType; IsBoot=$_.IsBoot; "
                     "IsSystem=$_.IsSystem; MediaType=[string]$p.MediaType; LogicalSectorSize=$_.LogicalSectorSize} } "
                     "| ConvertTo-Json")


def parse_windows_disks(text: str) -> List[Device]:
    data = json.loads(text)
    data = [data] if isinstance(data, dict) else data
    out = []
    for d in data:
        dev = Device(path=f"\\\\.\\PHYSICALDRIVE{d.get('Number')}", name=f"disk{d.get('Number')}",
                     model=(d.get("FriendlyName") or "").strip(), serial=(d.get("SerialNumber") or "").strip(),
                     size=int(d.get("Size") or 0), transport=(d.get("BusType") or "").lower(),
                     sector_size=int(d.get("LogicalSectorSize") or 512))
        media = (d.get("MediaType") or "").upper()
        hard, excl, amb = [], [], []
        if dev.transport != "usb":
            hard.append(f"bus {dev.transport or '?'} is not USB")
        if d.get("IsBoot") or d.get("IsSystem"):
            hard.append("system/boot disk")
        if media == "HDD":
            excl.append("MediaType HDD")
        if media == "SSD" or HDD_SSD_WORDS.search(dev.model):
            excl.append("external SSD/HDD")
        if dev.size == 0:
            hard.append("no medium")
        elif dev.size < C.MIN_DEVICE_SIZE:
            hard.append("too small")
        if READER_WORDS.search(dev.model):
            amb.append("card reader")
        if media in ("", "UNSPECIFIED"):
            amb.append("media type unspecified (typical for flash, but not conclusive)")
        out.append(finish(dev, hard, excl, amb))
    return out


# ---------- macOS (UNTESTED; mocked-plist unit tests only) ----------
def parse_macos_diskinfo(plist_bytes: bytes) -> Device:
    d = plistlib.loads(plist_bytes)
    dev = Device(path=f"/dev/{d.get('DeviceIdentifier')}", name=d.get("DeviceIdentifier", ""),
                 model=(d.get("MediaName") or "").strip(), serial=(d.get("IORegistryEntryName") or "").strip(),
                 size=int(d.get("TotalSize") or d.get("Size") or 0), transport=(d.get("BusProtocol") or "").lower(),
                 removable=bool(d.get("RemovableMedia") or d.get("Removable")),
                 rotational=not d.get("SolidState", False) if "SolidState" in d else None,
                 sector_size=int(d.get("DeviceBlockSize") or 512))
    hard, excl, amb = [], [], []
    if dev.transport != "usb":
        hard.append(f"bus {dev.transport or '?'} is not USB")
    if d.get("Internal"):
        hard.append("internal disk")
    if d.get("SystemImage") or d.get("BooterDeviceIdentifier"):
        hard.append("system disk")
    if HDD_SSD_WORDS.search(dev.model):
        excl.append("external SSD/HDD")
    if dev.rotational:
        excl.append("rotational (HDD)")
    if dev.size < C.MIN_DEVICE_SIZE:
        hard.append("too small or no medium")
    if READER_WORDS.search(dev.model):
        amb.append("card reader")
    if not dev.removable:
        amb.append("not flagged removable")
    return finish(dev, hard, excl, amb)
