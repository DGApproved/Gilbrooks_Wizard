"""'Custom driver' checkbox (Windows + macOS, OFF by default): a user-supplied ext4 integration script at the EXACT
FAT32-root-relative path /AI/Driver/Windows/ext4.sh or /AI/Driver/MacOS/ext4.sh.

Safety contract (see Driver/README.txt):
  * discover() only looks at files; it never executes anything (no run on discovery, refresh or USB insertion).
  * run() needs: checkbox ticked, the exact validated path, and explicit trust consent for THAT path.
  * The script is user code: it may change the host. usbprep cannot sandbox or audit it.
  * usbprep refuses to start it from an elevated/root process (no inherited blanket admin); if the script needs
    admin it must ask through the OS's own prompt (sudo / UAC) itself.
  * argv only, no shell string: <interpreter> <script> <operation> --device <id> --mount-point <path> --fat-root <path>
    plus env USBPREP_OPERATION / USBPREP_DEVICE / USBPREP_MOUNT_POINT / USBPREP_FAT_ROOT. Operation: mount|unmount|status.
  * Exit 0 does NOT mean ext4 is mounted read/write: verify_mount() checks the mount point independently.
  * Windows: .sh is not native; needs Git Bash or MSYS2 bash already installed. WSL's bash.exe is rejected; nothing is
    installed; no WSL fallback. macOS: /bin/sh.
UNTESTED on real Windows/macOS: covered by unit tests with fixtures and injected probes only.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional

OS_DIR = {"windows": "Windows", "darwin": "MacOS"}
OPERATIONS = ("mount", "unmount", "status")
SCRIPT_NAME = "ext4.sh"
REDACT = re.compile(r"(?i)\b(pass(word)?|token|secret|api[_-]?key)\s*[=:]\s*\S+")


class CustomDriverError(RuntimeError):
    pass


def default_fat_root() -> Optional[str]:
    """The app lives in <FAT32 root>/AI, so the FAT32 root is the parent of that folder."""
    from .appcopy import APP_ROOT
    return os.path.dirname(APP_ROOT) if os.path.basename(APP_ROOT) == "AI" else None


def script_relpath(osname: str) -> str:
    if osname not in OS_DIR:
        raise CustomDriverError(f"the Custom driver option exists only on Windows and macOS (not {osname})")
    return "/".join(("AI", "Driver", OS_DIR[osname], SCRIPT_NAME))


def _windows_bash_candidates() -> List[str]:
    pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    c = [os.path.join(p, "Git", "bin", "bash.exe") for p in pf if p]
    if os.environ.get("LOCALAPPDATA"):
        c.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Git", "bin", "bash.exe"))
    c += [r"C:\msys64\usr\bin\bash.exe", r"C:\msys32\usr\bin\bash.exe"]
    return c


def is_wsl_bash(path: str) -> bool:
    p = (path or "").replace("/", "\\").lower()
    return p.endswith("\\system32\\bash.exe") or "\\windowsapps\\" in p or p.split("\\")[-1] in ("wsl.exe", "wslhost.exe")


def find_interpreter(osname: str, probe: Optional[Dict[str, Callable]] = None) -> Dict:
    p = {"which": shutil.which, "exists": os.path.isfile}
    p.update(probe or {})
    if osname == "darwin":
        return {"path": "/bin/sh", "kind": "posix-sh"} if p["exists"]("/bin/sh") else \
            {"path": None, "error": "/bin/sh not found"}
    if osname == "windows":
        seen = [c for c in _windows_bash_candidates() if p["exists"](c)]
        w = p["which"]("bash")
        if w:
            seen.append(w)
        for c in seen:
            if not is_wsl_bash(c):
                return {"path": c, "kind": "msys-bash"}
        return {"path": None, "error": "no non-WSL bash found. ext4.sh needs Git for Windows (Git Bash, "
                "https://git-scm.com/download/win) or MSYS2 (https://www.msys2.org) installed by you. "
                "WSL's bash is never used for the Custom driver and nothing is installed automatically."}
    return {"path": None, "error": f"unsupported OS {osname}"}


def to_msys_path(win_path: str) -> str:
    """E:\\AI\\Driver\\Windows\\ext4.sh -> /e/AI/Driver/Windows/ext4.sh (Git Bash / MSYS2 convention)."""
    m = re.fullmatch(r"([A-Za-z]):[\\/]?(.*)", win_path or "")
    if not m:
        raise CustomDriverError(f"not an absolute drive-letter path: {win_path!r}")
    return "/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")


def discover(osname: str, fat_root: Optional[str], enabled: bool, probe: Optional[Dict[str, Callable]] = None) -> Dict:
    """Look only; never execute. Returns state DISABLED | MISSING-SCRIPT | MISSING-INTERPRETER | FOUND | BLOCKED."""
    r = {"os": osname, "enabled": bool(enabled), "expected": None, "path": None, "interpreter": None,
         "state": "DISABLED", "executed": False, "trusted": False,
         "message": "Custom driver box unticked: no user script is looked for or run."}
    if osname not in OS_DIR:
        r.update(state="BLOCKED", message="Custom driver is offered on Windows and macOS only.")
        return r
    if not enabled:
        return r
    if not fat_root or not os.path.isdir(fat_root):
        r.update(state="BLOCKED", message="FAT32 root of the selected USB is not known/mounted.")
        return r
    rel = script_relpath(osname)
    base = os.path.realpath(os.path.join(fat_root, "AI", "Driver", OS_DIR[osname]))
    cand = os.path.join(fat_root, *rel.split("/"))
    r["expected"] = cand
    if os.path.islink(cand) or not os.path.isfile(cand) or os.path.dirname(os.path.realpath(cand)) != base:
        r.update(state="MISSING-SCRIPT", message=f"no regular file at {cand} (symlinks and other names are ignored).")
        return r
    r["path"] = cand
    it = find_interpreter(osname, probe)
    r["interpreter"] = it.get("path")
    if not it.get("path"):
        r.update(state="MISSING-INTERPRETER", message=it["error"])
        return r
    r.update(state="FOUND", message=f"found {cand}; it will run only after you approve it (it is NOT trusted "
             "because it exists). It can change this computer; usbprep cannot check or sandbox it.")
    return r


def trust_warning(path: str) -> str:
    return (f"Run user-supplied script?\n  {path}\nThis is code you or someone else put on the USB. It runs as you "
            "and can change this computer (install drivers, change files). usbprep does not check or sandbox it. "
            "If it needs admin rights it must ask through the system's own password/UAC prompt.")


def build_argv(osname: str, d: Dict, operation: str, device: str, mount_point: str, fat_root: str) -> List[str]:
    if operation not in OPERATIONS:
        raise CustomDriverError(f"operation must be one of {OPERATIONS}")
    for name, v in (("device", device), ("mount point", mount_point), ("FAT32 root", fat_root)):
        if not v or "\x00" in v or "\n" in v:
            raise CustomDriverError(f"invalid {name}: {v!r}")
    script, mp, root = d["path"], mount_point, fat_root
    if osname == "windows":
        script = to_msys_path(script)
        root = to_msys_path(root) if re.match(r"[A-Za-z]:", root) else root
        mp = to_msys_path(mp) if re.match(r"[A-Za-z]:", mp) else mp
    return [d["interpreter"], script, operation, "--device", device, "--mount-point", mp, "--fat-root", root]


def _is_elevated(osname: str) -> bool:
    if osname == "windows":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def redact(text: str) -> str:
    return REDACT.sub(lambda m: m.group(1) + "=<redacted>", text or "")


def run(osname: str, d: Dict, operation: str, device: str, mount_point: str, fat_root: str, consent_path: Optional[str],
        log_dir: Optional[str] = None, timeout: int = 600, runner: Callable = subprocess.run,
        elevated: Optional[Callable[[str], bool]] = None, verify: Optional[Callable] = None) -> Dict:
    if d.get("state") != "FOUND":
        raise CustomDriverError(f"cannot run: {d.get('message')}")
    if consent_path != d["path"]:
        raise CustomDriverError("explicit trust consent for this exact script path is required: " + str(d["path"]))
    if (elevated or _is_elevated)(osname):
        raise CustomDriverError("refusing to start a user script from an elevated/root process; run usbprep as your "
                                "normal user (the script must request admin through the OS prompt itself)")
    argv = build_argv(osname, d, operation, device, mount_point, fat_root)
    env = dict(os.environ, USBPREP_OPERATION=operation, USBPREP_DEVICE=device, USBPREP_MOUNT_POINT=argv[6],
               USBPREP_FAT_ROOT=argv[8])
    t0 = time.time()
    try:
        cp = runner(argv, env=env, capture_output=True, text=True, timeout=timeout, shell=False)
        rc, out = cp.returncode, (cp.stdout or "") + (cp.stderr or "")
    except subprocess.TimeoutExpired:
        rc, out = 124, f"timed out after {timeout}s"
    except OSError as e:
        rc, out = 127, f"could not start interpreter: {e}"
    res = {"argv": argv, "exit_code": rc, "output_tail": redact(out)[-4000:], "seconds": round(time.time() - t0, 1),
           "script_succeeded": rc == 0, "verified": None, "read_write": False}
    if rc != 0:
        res["message"] = f"ext4.sh {operation} failed (exit {rc}); nothing is assumed mounted."
    elif operation == "mount":
        v = (verify or verify_mount)(mount_point)
        res.update(verified=v["ok"], read_write=v["ok"], verification=v,
                   message="ext4.sh exited 0 and the mount point passed independent checks." if v["ok"] else
                   "ext4.sh exited 0 but the mount point did NOT pass independent checks: dependent features stay "
                   "disabled. " + v["reason"])
    else:
        res["message"] = f"ext4.sh {operation} exited 0."
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "custom-driver.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} op={operation} exit={rc} verified={res['verified']} "
                    f"argv={argv[1:]}\n{res['output_tail'][-1500:]}\n")
    return res


def verify_mount(mount_point: str, probe: Optional[Dict[str, Callable]] = None) -> Dict:
    """Independent of the script's exit code: the path must be a real mount (or drive root), not a plain folder on
    another disk, and a create/read/delete probe must succeed. Filesystem type is checked where the probe can tell."""
    p = {"ismount": os.path.ismount, "isdir": os.path.isdir, "fstype": lambda mp: None}
    p.update(probe or {})
    if not p["isdir"](mount_point):
        return {"ok": False, "reason": f"{mount_point} does not exist."}
    if not p["ismount"](mount_point):
        return {"ok": False, "reason": f"{mount_point} is not a mount point / drive root (could be an ordinary folder)."}
    fs = p["fstype"](mount_point)
    if fs is not None and "ext4" not in fs.lower() and "nfs" not in fs.lower():
        return {"ok": False, "reason": f"{mount_point} is mounted as {fs}, not ext4."}
    try:
        fd, tmp = tempfile.mkstemp(prefix=".usbprep-rw-", dir=mount_point)
        os.write(fd, b"usbprep")
        os.close(fd)
        with open(tmp, "rb") as f:
            ok = f.read() == b"usbprep"
        os.unlink(tmp)
    except OSError as e:
        return {"ok": False, "reason": f"write probe failed at {mount_point}: {e}"}
    return {"ok": ok, "reason": "" if ok else "read-back mismatch", "fstype": fs or "not determinable on this OS"}
