"""ext4 read/write backend selection per host OS (Windows: native Ext4Fsd by default, WSL only when the user ticks
the WSL box; macOS: blocked natively, anylinuxfs from Homebrew only when the user ticks the Homebrew box).

Rules (Darek's request + project decisions):
  * Windows default = native backend (Ext4Fsd driver, older but native). WSL is NEVER used, installed or fallen
    back to unless the user explicitly selected WSL (GUI checkbox / `backend set --wsl on` / -UseWSL).
  * macOS default = native = BLOCKED (macOS has no ext4 driver). Homebrew box -> anylinuxfs (GPL-3.0, Apple
    Silicon only: runs the real Linux ext4 driver in a libkrun micro-VM and exports it over NFS; no kext, no
    security lowering). ext4fuse (read-only) and brew e2fsprogs (tools, no mount) are NEVER treated as RW.
  * Missing dependency -> clear message + manual steps. Nothing is installed here; installs happen only in
    setup/ scripts after a typed AGREE and the OS's own password/UAC prompt.
  * The choice is persisted only by an explicit `save_choice()` call (atomic write); nothing auto-enables WSL.
UNTESTED on real Windows/macOS: all probes are unit-tested with injected results only.
"""
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional

READY, MISSING, BLOCKED = "READY", "MISSING-DEPENDENCY", "BLOCKED"

EXT4FSD_URL = "https://github.com/bobranten/Ext4Fsd/releases"
ANYLINUXFS_URL = "https://github.com/nohajc/anylinuxfs"
# never accepted as an ext4 read/write route on macOS, with the reason shown to the user
MAC_NOT_RW = {
    "ext4fuse": "read-only FUSE driver (Homebrew formula deprecated); cannot write",
    "e2fsprogs": "filesystem tools (mkfs/fsck/debugfs) only; it does not mount anything",
    "fuse-ext2": "write mode flagged experimental by its author and needs the macFUSE kernel extension "
                 "(Reduced Security on Apple Silicon) - not used",
}
# Ext4Fsd 0.71 (bobranten): mounts these read-only / refuses to mount them at all
EXT4FSD_FORCE_RO = ("bigalloc", "quota", "project", "verity", "orphan_present")
EXT4FSD_UNMOUNTABLE = ("ea_inode", "mmp", "inline_data", "encrypt", "casefold", "largedir")

WIN_NATIVE_MANUAL = [
    f"Download the signed Ext4Fsd driver (GPL-2.0, free) from {EXT4FSD_URL} (v0.71+ is signed for Windows 10/11).",
    "Install it yourself (Administrator / UAC prompt). usbprep does not bundle or silently install kernel drivers.",
    "Open Ext2Mgr, select the 'AI' (ext4) partition of the USB, enable read/write and assign a drive letter "
    "(or an NTFS folder mount point such as C:\\USB\\A.I.). There is no literal /USB/A.I. path on Windows.",
    "If Ext2Mgr shows 'EXT4+' the volume uses a feature this driver cannot write: it stays read-only or unmounted.",
]
WIN_WSL_MANUAL = [
    "WSL route (only because you ticked WSL): run START.bat setup -Action Setup -UseWSL (installs WSL2 + Ubuntu and "
    "usbipd-win after you type AGREE and accept the UAC prompt), then START.bat setup -Action MountExt4 -UseWSL.",
]
MAC_RO_MANUAL = [
    "Read-only driver: install macFUSE (https://macfuse.github.io, free for personal use, closed source; on Apple "
    "Silicon it needs a kernel extension approval in System Settings) and ext4fuse yourself (brew install ext4fuse; "
    "the Homebrew formula is deprecated). Mount: ext4fuse /dev/diskNsM <folder> -o allow_other",
]
CUSTOM_MANUAL = [
    "Put your own script at /AI/Driver/<Windows|MacOS>/ext4.sh on the FAT32 partition (exact path). See "
    "Driver/README.txt for the argument contract. Windows needs Git Bash or MSYS2 (not WSL) to run it.",
]
MAC_MANUAL = [
    "Apple Silicon + Homebrew box: run Start/MacOS/start.command setup homebrew-install (shows what it installs, "
    "asks AGREE): brew tap nohajc/anylinuxfs; brew trust nohajc/anylinuxfs; brew install anylinuxfs",
    "Intel Mac or no Homebrew: use a Linux VM (UTM/Parallels/VMware) with USB pass-through and run this app's Linux "
    "mode inside it, or a commercial ext4 driver you choose and buy yourself (never installed or bought by usbprep).",
]


def config_dir(osname: Optional[str] = None) -> str:
    env = os.environ.get("USBPREP_CONFIG_DIR")
    if env:
        return env
    if (osname or "") == "windows" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "usbprep")
    if (osname or "") == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "usbprep")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"), "usbprep")


def config_path(osname: Optional[str] = None) -> str:
    return os.path.join(config_dir(osname), "backend.json")


CHOICES = {"windows": ("native", "wsl", "custom"), "darwin": ("native", "homebrew", "readonly", "custom")}
DEFAULTS = {"windows_backend": "native", "macos_backend": "native"}
KEY = {"windows": "windows_backend", "darwin": "macos_backend"}


def load_choice(osname: Optional[str] = None) -> Dict[str, str]:
    """Saved explicit choice (mutually exclusive per OS). Missing/corrupt/unknown -> 'native' (no WSL, no Homebrew,
    no custom script). Legacy windows_wsl/macos_homebrew = true is honoured only as a literal true."""
    out = dict(DEFAULTS)
    try:
        with open(config_path(osname), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("windows_wsl") is True:
            out["windows_backend"] = "wsl"
        if data.get("macos_homebrew") is True:
            out["macos_backend"] = "homebrew"
        for o, k in KEY.items():
            if isinstance(data.get(k), str) and data[k] in CHOICES[o]:
                out[k] = data[k]
    except (OSError, ValueError, AttributeError):
        pass
    return out


def save_choice(osname: Optional[str] = None, windows: Optional[str] = None, macos: Optional[str] = None) -> Dict:
    """Explicit user action only; saving never installs, runs or remounts anything. Atomic, 0600."""
    cur = load_choice(osname)
    for o, v in (("windows", windows), ("darwin", macos)):
        if v is not None:
            if v not in CHOICES[o]:
                raise ValueError(f"{o} backend must be one of {CHOICES[o]}")
            cur[KEY[o]] = v
    cur["explicit"] = True
    cur["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    d = config_dir(osname)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".backend.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, config_path(osname))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return cur


def _win_service(name: str) -> bool:
    try:
        r = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and "SERVICE_NAME" in r.stdout.upper()
    except (OSError, subprocess.SubprocessError):
        return False


DEFAULT_PROBE = {"which": shutil.which, "machine": platform.machine, "win_service": _win_service, "exists": os.path.isfile}


def ext4fsd_feature_check(features: List[str]) -> Dict:
    f = {x.strip().lower() for x in features if x.strip()}
    bad = sorted(f & set(EXT4FSD_UNMOUNTABLE))
    ro = sorted(f & set(EXT4FSD_FORCE_RO))
    return {"mountable": not bad, "writable": not bad and not ro, "unmountable_features": bad, "read_only_features": ro}


def status(osname: str, choice: Optional[str] = None, probe: Optional[Dict[str, Callable]] = None,
           features: Optional[List[str]] = None, fat_root: Optional[str] = None) -> Dict:
    """Which backend would be used and whether it is usable. choice None -> saved explicit choice (default native).
    Never falls back to another backend: a missing selected backend is reported, not replaced. Third-party
    backends are UNTESTED on real Windows/macOS hosts by this project ('tested_here': False)."""
    p = dict(DEFAULT_PROBE, **(probe or {}))
    if osname in KEY:
        choice = load_choice(osname)[KEY[osname]] if choice is None else choice
        if choice not in CHOICES[osname]:
            raise ValueError(f"{osname} backend must be one of {CHOICES[osname]}")
    wsl, homebrew = choice == "wsl", choice == "homebrew"
    r = {"os": osname, "choice": choice or "native", "wsl_selected": wsl, "homebrew_selected": homebrew,
         "readonly_selected": choice == "readonly", "custom_selected": choice == "custom", "fallback": None,
         "manual": [], "rejected": {}, "mount_path": None, "tested_here": osname == "linux", "version": None}
    if choice == "custom":
        from . import customdriver as CD
        d = CD.discover(osname, fat_root if fat_root is not None else CD.default_fat_root(), True,
                        probe={k: p[k] for k in ("which", "exists") if k in p})
        r.update(backend="custom-driver", custom=d, read_write=None, manual=CUSTOM_MANUAL,
                 state=READY if d["state"] == "FOUND" else (BLOCKED if d["state"] == "BLOCKED" else MISSING),
                 message="Custom driver selected: " + d["message"] + " Read/write is unknown until the script ran "
                         "AND the mount point passed usbprep's own checks.")
        return r
    if osname == "linux":
        r.update(backend="linux-kernel", state=READY, read_write=True, mount_path="/USB/A.I.",
                 message="Linux kernel ext4 driver (mount via the Mount button / `usbprep mount`).")
        return r
    if osname == "windows":
        if wsl:
            have = [n for n in ("wsl", "usbipd") if p["which"](n) or p["which"](n + ".exe")]
            missing = [n for n in ("wsl", "usbipd") if n not in have]
            r.update(backend="wsl", read_write=True, mount_path="\\\\wsl.localhost\\<distro>\\USB\\A.I.",
                     manual=WIN_WSL_MANUAL,
                     state=READY if not missing else MISSING,
                     message=("WSL selected explicitly: WSL2 + usbipd-win, Linux kernel ext4 inside WSL. While attached, "
                              "the whole USB (including FAT32) disappears from Windows.") if not missing else
                     f"WSL selected explicitly but not installed: {', '.join(missing)} missing. Nothing is installed "
                     "automatically; see the manual steps.")
            return r
        drv = p["win_service"]("Ext2Fsd") or p["win_service"]("ext2fsd")
        r.update(backend="native-ext4fsd", mount_path="drive letter assigned in Ext2Mgr (e.g. F:\\)", manual=WIN_NATIVE_MANUAL,
                 state=READY if drv else MISSING, read_write=bool(drv),
                 message=("Native Windows backend: Ext4Fsd driver is installed. Assign the AI partition a drive letter "
                          "and enable write in Ext2Mgr. WSL is not used (WSL box unticked).") if drv else
                 "Native Windows backend selected (default; WSL box unticked) but the Ext4Fsd driver is not "
                 "installed. WSL is NOT used as a fallback. Install the driver manually (steps below).")
        if features is not None:
            fc = ext4fsd_feature_check(features)
            r["feature_check"] = fc
            if not fc["writable"]:
                r["read_write"] = False
                r["state"] = BLOCKED
                r["message"] += (" This ext4 volume uses features Ext4Fsd cannot write: "
                                 f"{', '.join(fc['unmountable_features'] + fc['read_only_features'])}.")
        return r
    if osname == "darwin":
        r["rejected"] = {k: v for k, v in MAC_NOT_RW.items() if p["which"](k)}
        if choice == "readonly":
            fx = p["which"]("ext4fuse")
            r["rejected"].pop("ext4fuse", None)
            r.update(backend="readonly-ext4fuse", read_write=False, manual=MAC_RO_MANUAL,
                     state=READY if fx else MISSING,
                     message=("Read-only driver selected: ext4fuse found (read-only by design; usbprep refuses every "
                              "write to the USB through it: no repair, format, clone destination or restore to USB). "
                              "Untested here; ext4 features it cannot parse may fail to mount.") if fx else
                     "Read-only driver selected but ext4fuse is not installed (needs macFUSE). Nothing is installed "
                     "automatically; no other backend is used instead.")
            return r
        if not homebrew:
            r.update(backend="native", state=BLOCKED, read_write=False, manual=MAC_MANUAL,
                     message="macOS has no native ext4 driver: ext4 read/write is BLOCKED. Tick 'Homebrew' to use "
                             "anylinuxfs (Apple Silicon only) or use a manual alternative.")
            return r
        arch = (p["machine"]() or "").lower()
        if arch not in ("arm64", "aarch64"):
            r.update(backend="homebrew-anylinuxfs", state=BLOCKED, read_write=False, manual=MAC_MANUAL,
                     message=f"Homebrew selected, but this Mac is {arch or 'unknown'} (Intel). anylinuxfs supports "
                             "Apple Silicon only and no other verified free read/write ext4 driver exists for "
                             "Intel Macs: ext4 read/write stays BLOCKED (read-only ext4fuse is not a substitute).")
            return r
        brew, alf = p["which"]("brew"), p["which"]("anylinuxfs")
        r.update(backend="homebrew-anylinuxfs", read_write=bool(alf), manual=MAC_MANUAL,
                 mount_path="/Volumes/USB/A.I. (if FAT32 is at /Volumes/USB) else anylinuxfs default under /Volumes",
                 state=READY if alf else MISSING,
                 message=("Homebrew selected: anylinuxfs found (real Linux ext4 driver in a micro-VM, shared via NFS). "
                          "Files may show as owned by another user over NFS; some apps (e.g. Word) dislike NFS.")
                 if alf else ("Homebrew selected but anylinuxfs is not installed" +
                              ("" if brew else " and Homebrew itself was not found (install it from https://brew.sh "
                               "yourself)") + ". Nothing is installed automatically; see the manual steps."))
        return r
    r.update(backend="none", state=BLOCKED, read_write=False, message=f"unsupported OS {osname}")
    return r


def anylinuxfs_mount_argv(disk_ident: str, mount_point: Optional[str] = None) -> List[str]:
    import re
    if not re.fullmatch(r"(/dev/)?disk\d+s\d+", disk_ident or ""):
        raise ValueError(f"invalid macOS partition identifier {disk_ident!r} (expected e.g. disk4s2)")
    dev = disk_ident if disk_ident.startswith("/dev/") else "/dev/" + disk_ident
    return ["anylinuxfs", "mount", dev] + ([mount_point] if mount_point else [])


class ReadOnlyBackend(RuntimeError):
    pass


def require_writable(r: Dict, action: str) -> None:
    """Guard for every USB write (repair, format, clone destination, restore/backup to USB) on Windows/macOS."""
    if r.get("read_write") is not True:
        raise ReadOnlyBackend(f"refusing '{action}': the selected ext4 backend ({r.get('backend')}) is "
                              f"{'read-only' if r.get('read_write') is False else 'not verified read/write'}.")


def ext4fuse_mount_argv(disk_ident: str, mount_point: str) -> List[str]:
    import re
    if not re.fullmatch(r"(/dev/)?disk\d+s\d+", disk_ident or ""):
        raise ValueError(f"invalid macOS partition identifier {disk_ident!r} (expected e.g. disk4s2)")
    if not mount_point or "\x00" in mount_point:
        raise ValueError("invalid mount point")
    dev = disk_ident if disk_ident.startswith("/dev/") else "/dev/" + disk_ident
    return ["ext4fuse", dev, mount_point, "-o", "allow_other"]


def summary(r: Dict) -> str:
    lines = [f"OS: {r['os']}  backend: {r['backend']}  state: {r['state']}  read/write: {r['read_write']}",
             f"selected: {r['choice']} (checkboxes are mutually exclusive; none ticked = native)   automatic "
             f"fallback: none   tested on a real {r['os']} host by this project: {'yes' if r['tested_here'] else 'no'}",
             r["message"]]
    if r.get("mount_path"):
        lines.append(f"ext4 location: {r['mount_path']}")
    for k, v in r.get("rejected", {}).items():
        lines.append(f"Found {k} but NOT used for read/write: {v}")
    if r["state"] != READY and r.get("manual"):
        lines.append("Manual steps / alternatives:")
        lines += [f"  - {m}" for m in r["manual"]]
    return "\n".join(lines)


def _default_ismount(p: str) -> bool:
    return os.path.ismount(p)


def _default_write_probe(p: str) -> bool:
    """Return True if a file could be created in p (then removed). Never touches existing files."""
    import tempfile
    try:
        fd, name = tempfile.mkstemp(prefix=".usbprep-ro-probe-", dir=p)
    except OSError:
        return False
    os.close(fd)
    try:
        os.remove(name)
    except OSError:
        pass
    return True


def verify_readonly_mount(mount_point: str, probe: Optional[Dict[str, Callable]] = None,
                          expected_device: Optional[str] = None) -> Dict:
    """Independent check after an ext4fuse mount: it must be a mount point, (optionally) of the
    selected device, and a write attempt must FAIL. Anything else -> verified False."""
    pr = {"ismount": _default_ismount, "write_probe": _default_write_probe, "device_of": None}
    pr.update(probe or {})
    if not pr["ismount"](mount_point):
        return {"verified": False, "read_write": None, "reason": "not a mount point: %s" % mount_point}
    if expected_device and pr.get("device_of"):
        dev = pr["device_of"](mount_point)
        if dev != expected_device:
            return {"verified": False, "read_write": None,
                    "reason": "mounted device %r is not the selected USB %r" % (dev, expected_device)}
    if pr["write_probe"](mount_point):
        return {"verified": False, "read_write": True,
                "reason": "write succeeded - mount is NOT read-only; refusing to treat it as the read-only backend"}
    return {"verified": True, "read_write": False, "reason": "read-only mount verified (write attempt refused)"}
