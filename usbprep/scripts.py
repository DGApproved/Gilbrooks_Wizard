"""Scripts menu: list and run user scripts from <AI>/Scripts/<Linux|MacOS|Windows> for the CURRENT OS.

- Listed by exact on-disk filename; only regular files with a supported extension (symlinks, folders,
  traversal, hidden files, unsupported types are skipped with a reason).
- Nothing runs on discovery/refresh/insertion: run() is only called for an explicit user selection
  with trust consent.
- Launched as an argv list (no shell string), cwd = the script folder, as the ordinary user: refused when
  this process is root/elevated. Optional verified ext4 mount path passed via USBPREP_EXT4_MOUNT.
- usbprep does not audit or sandbox user scripts and cannot guarantee what they do.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

from .appcopy import APP_ROOT

OS_FOLDER = {"linux": "Linux", "darwin": "MacOS", "windows": "Windows"}
INTERPRETERS = {
    "linux": {".sh": ["sh"], ".bash": ["bash"], ".py": ["python3"]},
    "darwin": {".sh": ["/bin/sh"], ".command": ["/bin/bash"], ".zsh": ["/bin/zsh"], ".py": ["python3"]},
    "windows": {".ps1": ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
                ".bat": ["cmd.exe", "/d", "/c"], ".cmd": ["cmd.exe", "/d", "/c"], ".py": ["py", "-3"]},
}
CMD_UNSAFE = set('&|<>^%!"()')     # cmd.exe re-parses its command line: refuse such names for .bat/.cmd
TRUST_WARNING = ("Scripts can run any command with your user rights. usbprep does not check what a script does "
                 "and cannot guarantee its behaviour. Run it only if you trust it.")


class ScriptError(RuntimeError):
    pass


def scripts_dir(ai_root: Optional[str], osname: str) -> str:
    return os.path.join(ai_root or APP_ROOT, "Scripts", OS_FOLDER[osname])


def _classify(d: str, name: str, osname: str):
    p = os.path.join(d, name)
    ext = os.path.splitext(name)[1].lower()
    if name.startswith(".") or "/" in name or "\\" in name or name in ("..", "."):
        return None, "hidden or invalid name"
    if os.path.islink(p):
        return None, "symlink (not allowed)"
    if not os.path.isfile(p):
        return None, "not a regular file"
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(d):
        return None, "resolves outside the Scripts folder"
    if ext not in INTERPRETERS[osname]:
        return None, f"unsupported type '{ext or 'no extension'}' (supported: {', '.join(INTERPRETERS[osname])})"
    if osname == "windows" and ext in (".bat", ".cmd") and CMD_UNSAFE & set(name):
        return None, "name contains characters cmd.exe would re-interpret (& | < > ^ % ! \" ( )); rename it"
    return INTERPRETERS[osname][ext], ""


def list_scripts(ai_root: Optional[str], osname: str) -> Dict[str, list]:
    """{'dir':..., 'scripts': [{'name', 'interpreter', 'available'}], 'skipped': [{'name','reason'}]}. No execution."""
    d = scripts_dir(ai_root, osname)
    out = {"dir": d, "os": osname, "scripts": [], "skipped": [], "warning": TRUST_WARNING}
    if not os.path.isdir(d) or os.path.islink(d):
        return out
    for name in sorted(os.listdir(d), key=str.lower):
        interp, why = _classify(d, name, osname)
        if interp is None:
            out["skipped"].append({"name": name, "reason": why})
        else:
            out["scripts"].append({"name": name, "interpreter": interp[0],
                                   "available": bool(shutil.which(interp[0]))})
    return out


def is_elevated(osname: str) -> bool:
    if osname == "windows":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def build_argv(ai_root: Optional[str], name: str, osname: str) -> List[str]:
    d = scripts_dir(ai_root, osname)
    interp, why = _classify(d, name, osname) if os.path.isdir(d) else (None, "Scripts folder missing")
    if interp is None:
        raise ScriptError(f"refusing to run '{name}': {why}")
    exe = shutil.which(interp[0])
    if not exe:
        raise ScriptError(f"'{name}' needs {interp[0]}, which is not installed or not on PATH; install it first "
                          f"(nothing was run)")
    return [exe] + interp[1:] + [os.path.join(d, name)]


def _lock_path(d: str, name: str) -> str:
    base = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "usbprep", "script-locks")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, hashlib.sha256(os.path.join(d, name).encode()).hexdigest()[:16] + ".lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def run(ai_root: Optional[str], name: str, osname: str, consent: bool, ext4_mount: Optional[str] = None,
        device: Optional[str] = None, popen=subprocess.Popen, elevated: Optional[bool] = None, out=sys.stdout) -> int:
    """Run ONE explicitly selected script; returns its exit code."""
    if not consent:
        raise ScriptError("not run: explicit trust consent is required. " + TRUST_WARNING)
    if (is_elevated(osname) if elevated is None else elevated):
        raise ScriptError("refusing to run a user script with administrator/root rights (this app is elevated). "
                          "Relaunch usbprep as your normal user; a script that needs admin rights must ask for it "
                          "itself (sudo/pkexec/UAC prompt)")
    argv = build_argv(ai_root, name, osname)
    d = scripts_dir(ai_root, osname)
    lock = _lock_path(d, name)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            pid = int(open(lock).read().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            raise ScriptError(f"'{name}' is already running (pid {pid}); not started twice")
        os.remove(lock)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    env = dict(os.environ)
    env.pop("USBPREP_EXT4_MOUNT", None)
    if ext4_mount:
        env["USBPREP_EXT4_MOUNT"] = ext4_mount
    if device:
        env["USBPREP_DEVICE"] = device
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        print(f"running {name} ({' '.join(argv[:-1])}) in {d}", file=out, flush=True)
        p = popen(argv, cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        for line in iter(p.stdout.readline, b""):
            out.write(line.decode(errors="replace"))
        rc = p.wait()
        print(f"{name} finished with exit code {rc}", file=out, flush=True)
        return rc
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def resolve_ext4_mount(session, dev, mount_point: str) -> Optional[str]:
    """Linux: return mount_point only if it is mounted from THIS device's ext4 partition (verified)."""
    try:
        ext = session._parts(dev)[1]
        fm = json.loads(session.runner.run(["findmnt", "-J", "-o", "SOURCE,FSTYPE", "--mountpoint", mount_point],
                                           check=False).stdout or "{}")
        f0 = (fm.get("filesystems") or [{}])[0]
        if f0.get("fstype") == "ext4" and os.path.realpath(f0.get("source", "")) == os.path.realpath(ext.path):
            return mount_point
    except Exception:
        return None
    return None
