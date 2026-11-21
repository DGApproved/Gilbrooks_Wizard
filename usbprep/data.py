"""Data menu: back up the CURRENT signed-in user's home to <ext4>/Data and restore it into that user's home.

Layout on the ext4 partition root:  /Data/<Linux|MacOS|Windows>/<machine-id>/<user>/
    home/                          copy of the user's home, folder structure preserved
    usbprep_manifest.json          ORIGINAL type/mode/uid/gid/mtime per path (written BEFORE chmod 777)
    usbprep_state.json             in_progress | complete | partial | failed, with counts

Linux backup: `rsync -axHAX` (requested), then - as explicitly requested - chmod 777 on regular files and
directories INSIDE the backup subtree only (never symlinks/devices/sockets/fifos, never the source home).
Restore: non-deleting copy (never --delete / robocopy /MIR), preview first, typed confirmation, files owned by the
signed-in user, original modes from the manifest (never 777 on the host; .ssh/.gnupg kept private).
Windows/macOS: command builders exist (robocopy / rsync capability detection) but the actions are DISABLED because
this project has no supported ext4 access on those OSes. Logs list paths and counts only, never file contents.
"""
import hashlib
import json
import os
try:
    import pwd
except ImportError:      # Windows: Data is disabled there anyway
    pwd = None
import re
import shutil
import socket
import stat
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple

OS_DIR = {"linux": "Linux", "darwin": "MacOS", "windows": "Windows"}
MANIFEST, STATE, TREE = "usbprep_manifest.json", "usbprep_state.json", "home"
SENSITIVE_DIRS = {".ssh", ".gnupg", ".pki", ".password-store"}
CONFLICT_MODES = ("skip-newer", "skip-existing", "overwrite")
BACKUP_WARNING = (
    "Backup copies ALL files in your home folder (including hidden files) to /Data on the USB ext4 partition.\n"
    "* As requested, the copies are set to permission 777: ANY local user on ANY computer where this USB is mounted "
    "can read and change them.\n"
    "* Your home may contain passwords, browser sessions, tokens and private keys. usbprep makes no promise of "
    "confidentiality for the backup.\n"
    "* A backup of a live, signed-in account is not an application-consistent snapshot (open databases may be "
    "mid-write). Files the OS protects, locks or you cannot read are skipped and listed by path in the report.\n"
    "* Other filesystems mounted inside your home (and this USB) are not entered (rsync -x).\n"
    "* Original permissions are recorded in usbprep_manifest.json so Restore can put them back.")
DISABLED_REASON = ("Data backup/restore needs read/write access to the USB ext4 partition. This project has no "
                   "supported ext4 access on {os} (see the support matrix: ext4 RW is BLOCKED), so the Data menu is "
                   "disabled here. Run Data on Linux, or mount the ext4 partition with a tool you install and run "
                   "robocopy/rsync yourself.")


class DataError(RuntimeError):
    pass


class DataDisabled(DataError):
    pass


def require_supported(osname: str):
    if osname != "linux":
        raise DataDisabled(DISABLED_REASON.format(os={"windows": "Windows", "darwin": "macOS"}.get(osname, osname)))


# ---------------- identity ----------------
def machine_id(osname: str, reader: Callable[[str], str] = None) -> str:
    """Stable, NON-secret id: first 16 hex of SHA-256 over the OS machine id (raw id never stored)."""
    def _read(p):
        with open(p) as f:
            return f.read().strip()
    reader = reader or _read
    raw = ""
    if osname == "linux":
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                raw = reader(p)
                if raw:
                    break
            except OSError:
                continue
    if not raw:
        raw = "host:" + socket.gethostname()      # documented fallback (hostname may change)
    return hashlib.sha256(("usbprep-data:" + raw).encode()).hexdigest()[:16]


def real_user(env=None, euid: Optional[int] = None, getpw=None) -> Dict:
    """The signed-in, non-root account (never the elevated helper's /root)."""
    env = os.environ if env is None else env
    euid = os.geteuid() if euid is None else euid
    getpw = getpw or pwd
    if euid == 0:
        if env.get("SUDO_USER") and env.get("SUDO_USER") != "root":
            pw = getpw.getpwnam(env["SUDO_USER"])
        elif env.get("PKEXEC_UID") and env.get("PKEXEC_UID") != "0":
            pw = getpw.getpwuid(int(env["PKEXEC_UID"]))
        else:
            raise DataError("running as root and the signed-in user cannot be determined (no SUDO_USER/PKEXEC_UID); "
                            "refusing to back up or restore /root")
    else:
        pw = getpw.getpwuid(euid)
    if pw.pw_uid == 0:
        raise DataError("refusing to use the root account's home")
    if not os.path.isdir(pw.pw_dir):
        raise DataError(f"home folder of {pw.pw_name} not found")
    return {"name": pw.pw_name, "uid": pw.pw_uid, "gid": pw.pw_gid, "home": os.path.realpath(pw.pw_dir),
            "elevated": euid == 0}


_SAFE_COMPONENT = re.compile(r"^[^/\x00]+$")


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def backup_dir(ext4_root: str, osname: str, mid: str, user: str) -> str:
    for c in (OS_DIR[osname], mid, user):
        if not _SAFE_COMPONENT.match(c) or c in (".", ".."):
            raise DataError(f"unsafe path component {c!r}")
    d = os.path.join(ext4_root, "Data", OS_DIR[osname], mid, user)
    check_dest(ext4_root, d)
    return d


def check_dest(ext4_root: str, path: str):
    """Refuse destinations outside the verified ext4 root or reached through symlinks."""
    if not ext4_root or not os.path.isdir(ext4_root):
        raise DataError("no verified ext4 mount (mount the selected USB first: /USB/A.I.)")
    root = os.path.realpath(ext4_root)
    cur = root
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(ext4_root))
    if rel.startswith(".."):
        raise DataError(f"{path} is outside the verified ext4 partition {ext4_root}")
    for part in ([] if rel == "." else rel.split(os.sep)):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise DataError(f"refusing symlink in destination path: {cur}")
    if not _inside(path, root):
        raise DataError(f"{path} resolves outside the verified ext4 partition")


# ---------------- Linux command builders ----------------
def linux_backup_argv(home: str, dest_tree: str, excludes: List[str]) -> List[str]:
    argv = ["rsync", "-axHAX", "--info=stats2"]
    argv += [f"--exclude=/{e.strip('/')}" for e in excludes]
    return argv + [home.rstrip("/") + "/", dest_tree.rstrip("/") + "/"]


def restore_argv(src_tree: str, home: str, conflict: str, dry: bool, chown: Optional[Tuple[int, int]] = None) -> List[str]:
    if conflict not in CONFLICT_MODES:
        raise DataError(f"conflict mode must be one of {CONFLICT_MODES}")
    # -rltHAX: no -p (modes come from the manifest afterwards), no -o/-g (owner = signed-in user), NEVER --delete
    argv = ["rsync", "-rltHAX", "--itemize-changes", "--out-format=%i %n"]
    argv += {"skip-newer": ["--update"], "skip-existing": ["--ignore-existing"], "overwrite": []}[conflict]
    if chown:
        argv.append(f"--chown={chown[0]}:{chown[1]}")
    if dry:
        argv.append("--dry-run")
    return argv + [src_tree.rstrip("/") + "/", home.rstrip("/") + "/"]


def rsync_outcome(rc: int) -> str:
    if rc == 0:
        return "complete"
    if rc in (23, 24):        # partial transfer due to error / vanished source files
        return "partial"
    return "failed"


_FAIL_RE = re.compile(r'^rsync: (?:\[\w+\] )?(?:send_files failed to open|opendir|readlink_stat|readlink|link_stat|'
                      r'recv_generator|lstat|stat|open)\S*\s+"([^"]+)"', re.M)


def failed_paths(stderr: str) -> List[str]:
    return sorted(set(_FAIL_RE.findall(stderr or "")))


def parse_itemized(out: str) -> Dict[str, List[str]]:
    r = {"create": [], "overwrite": [], "dirs": [], "other": []}
    for line in (out or "").splitlines():
        m = re.match(r"^([<>ch.*][fdLDS][^ ]{9,10}) (.+)$", line)
        if not m:
            continue
        code, name = m.groups()
        if code[1] == "d":
            r["dirs"].append(name.rstrip("/"))
        elif code[0] in "<>" and code[1] == "f":
            (r["create"] if "+++++++" in code else r["overwrite"]).append(name)
        elif code[0] in "ch" or code[1] in "LDS":
            r["other"].append(name)
    return r


# ---------------- manifest + chmod 777 (backup subtree only) ----------------
def write_manifest(tree: str, path: str) -> int:
    entries = {}
    for dp, dns, fns in os.walk(tree, followlinks=False):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            t = "d" if stat.S_ISDIR(st.st_mode) else "f" if stat.S_ISREG(st.st_mode) else \
                "l" if stat.S_ISLNK(st.st_mode) else "o"
            entries[os.path.relpath(p, tree)] = {"t": t, "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid,
                                                  "gid": st.st_gid, "mtime": int(st.st_mtime)}
    st = os.lstat(tree)
    entries["."] = {"t": "d", "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
                    "mtime": int(st.st_mtime)}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 1, "entries": entries}, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)          # the manifest itself is not opened up
    return len(entries)


def chmod_777_subtree(tree: str, ext4_root: str) -> Dict[str, int]:
    """chmod 777 regular files + dirs under tree; never follows symlinks, never touches anything outside."""
    check_dest(ext4_root, tree)
    n = {"changed": 0, "skipped_special": 0, "errors": 0}
    for dp, dns, fns in os.walk(tree, followlinks=False):
        for nm in dns + fns + ([""] if dp == tree else []):
            p = os.path.join(dp, nm) if nm else dp
            try:
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode):
                    if not _inside(p, tree):
                        n["errors"] += 1
                        continue
                    os.chmod(p, 0o777)
                    n["changed"] += 1
                else:
                    n["skipped_special"] += 1        # symlink / fifo / socket / device: left as is
            except OSError:
                n["errors"] += 1
    return n


def _default_mode(rel: str, is_dir: bool, umask: int) -> int:
    if set(rel.split(os.sep)) & SENSITIVE_DIRS:
        return 0o700 if is_dir else 0o600
    return (0o777 if is_dir else 0o666) & ~umask


def restore_modes(home: str, paths: List[str], manifest: Dict, umask: int = 0o022,
                  chown: Optional[Tuple[int, int]] = None) -> Dict[str, int]:
    """Apply ORIGINAL modes (manifest) to restored paths only; never 777, never follows symlinks."""
    ents = manifest.get("entries", {})
    n = {"from_manifest": 0, "default": 0, "skipped": 0}
    for rel in paths:
        rel = rel.rstrip("/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            n["skipped"] += 1
            continue
        p = os.path.join(home, rel)
        try:
            st = os.lstat(p)
        except OSError:
            n["skipped"] += 1
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)) or not _inside(p, home):
            n["skipped"] += 1
            continue
        e = ents.get(rel)
        mode = e["mode"] if e and e["t"] in "fd" else _default_mode(rel, stat.S_ISDIR(st.st_mode), umask)
        if set(rel.split(os.sep)) & SENSITIVE_DIRS:
            mode &= 0o700 if stat.S_ISDIR(st.st_mode) else 0o600
        os.chmod(p, mode)
        if chown:
            os.lchown(p, *chown)
        n["from_manifest" if e else "default"] += 1
    return n


# ---------------- cross-OS name checks ----------------
_WIN_BAD = set('<>:"\\|?*')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def name_incompatibilities(relpaths: List[str], target_os: str) -> List[Dict[str, str]]:
    out, seen = [], {}
    for rel in relpaths:
        for comp in rel.split("/"):
            if target_os == "windows":
                if any(c in _WIN_BAD or ord(c) < 32 for c in comp) or comp.endswith((" ", ".")) \
                        or comp.split(".")[0].upper() in _WIN_RESERVED:
                    out.append({"path": rel, "problem": "name not allowed on Windows/NTFS"})
                    break
            elif target_os == "darwin" and ":" in comp:
                out.append({"path": rel, "problem": "':' is shown as '/' in Finder"})
                break
        if target_os in ("windows", "darwin"):
            k = rel.lower()
            if k in seen and seen[k] != rel:
                out.append({"path": rel, "problem": f"case-only collision with {seen[k]} (case-insensitive target)"})
            seen.setdefault(k, rel)
    return out


# ---------------- Windows / macOS builders (DISABLED: ext4 blocked; unit-tested only) ----------------
def robocopy_backup_argv(src: str, dst: str, log: str) -> List[str]:
    return ["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/R:1", "/W:1", "/NP", "/UNILOG:" + log]


def robocopy_restore_argv(src: str, dst: str, conflict: str, log: str) -> List[str]:
    extra = {"skip-newer": ["/XO"], "skip-existing": ["/XC", "/XN", "/XO"], "overwrite": ["/IS"]}[conflict]
    return ["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT", "/XJ", "/R:1", "/W:1", "/NP"] + extra + \
        ["/UNILOG:" + log]


def robocopy_outcome(rc: int) -> str:
    """robocopy: bit 1 copied, 2 extra, 4 mismatch, 8 failures, 16 fatal."""
    if rc >= 8:
        return "failed" if rc >= 16 else "partial"
    return "complete"


def windows_backup_acl_argv(dst: str) -> List[str]:
    # BUILTIN\Users full control on the BACKUP folder only (closest NTFS analogue to 777; not an exact mapping)
    return ["icacls", dst, "/grant", "*S-1-5-32-545:(OI)(CI)F", "/T", "/C", "/Q"]


def windows_restore_acl_argvs(path: str, user: str) -> List[List[str]]:
    return [["icacls", path, "/setowner", user, "/C", "/Q"], ["icacls", path, "/inheritance:e", "/C", "/Q"]]


def macos_rsync_flags(version_text: str) -> Tuple[List[str], List[str]]:
    """Returns (flags, metadata NOT preserved). Apple's rsync 2.6.9 / openrsync lack -A/-X."""
    v = version_text or ""
    m = re.search(r"rsync\s+version\s+v?(\d+)\.(\d+)", v)
    if "openrsync" in v:
        return ["-axH"], ["ACLs", "extended attributes"]
    if m and int(m.group(1)) >= 3:
        lost = [] if ("ACLs" in v and "xattrs" in v) or "no ACLs" not in v else ["ACLs"]
        return ["-axHAX"], lost
    if m:
        return ["-axHE"], ["ACLs"]
    return ["-axH"], ["ACLs", "extended attributes"]


# ---------------- Linux jobs ----------------
Runner = Callable[[List[str]], Tuple[int, str, str]]


def _run(argv: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _du(home: str) -> int:
    p = subprocess.run(["du", "-sxb", home], capture_output=True, text=True)
    try:
        return int(p.stdout.split()[0])
    except (IndexError, ValueError):
        raise DataError("could not measure the home folder size (du failed)")


def _write_state(d: str, **kw):
    kw["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(os.path.join(d, STATE), "w") as f:
        json.dump(kw, f, indent=1)


# ---------------- classification (no file is ever opened for execution; extension is NOT proof of safety) -------
SHARED_EXT = {   # conservative allowlist of cross-OS data formats (lower-case, editable)
    "Music": {"mp3", "flac", "ogg", "oga", "opus", "m4a", "aac", "wav", "aif", "aiff"},
    "Pictures": {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "heic", "svg"},
    "Videos": {"mp4", "m4v", "mkv", "webm", "mov", "avi"},
    "Documents": {"pdf", "txt", "md", "rtf", "csv", "odt", "ods", "odp", "doc", "docx", "xls", "xlsx", "ppt",
                  "pptx", "epub"},
}
OS_EXT = {
    "windows": {"exe", "msi", "msix", "appx", "cab", "dll", "sys", "bat", "cmd", "ps1", "lnk", "scr", "com"},
    "darwin": {"app", "pkg", "mpkg", "dmg", "dylib", "kext", "command", "bundle", "framework", "plugin"},
    "linux": {"deb", "rpm", "appimage", "snap", "flatpak", "flatpakref", "so", "run", "desktop"},
}
BUNDLE_EXT = {"app", "pkg", "mpkg", "bundle", "framework", "plugin", "kext"}   # macOS directory bundles: indivisible
SHARED_DIR = "Shared"
INDEX = "usbprep_shared_index.json"
VERSIONS = "_versions"


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[1].lower() if "." in name.strip(".") else ""


def classify(home: str, osname: str, skip_paths=()) -> Dict:
    """Route every entry of home. Returns {"shared": {cat: [rel]}, "os": {os: [rel]}, "skipped": [...]}
    - shared: regular, non-hidden files with an allowlisted extension outside any bundle/hidden folder
    - os[osname]: everything else (hidden/config, unknown, symlinks, specials, directories)
    - os[other]: files/bundles with that OS's known extension (e.g. .exe -> windows); bundles kept whole
    Other filesystems below home are not entered (same rule as rsync -x)."""
    dev = os.lstat(home).st_dev
    skip = {os.path.realpath(p) for p in skip_paths}
    r = {"shared": {c: [] for c in SHARED_EXT}, "os": {o: [] for o in OS_EXT}, "skipped": [], "meta": {}}

    def add(bucket, rel, st):
        bucket.append(rel)
        r["meta"][rel] = {"mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
                          "mtime": int(st.st_mtime), "t": "d" if stat.S_ISDIR(st.st_mode) else
                          "f" if stat.S_ISREG(st.st_mode) else "l" if stat.S_ISLNK(st.st_mode) else "o"}

    def whole_tree(path, bucket):
        for dp, dns, fns in os.walk(path, followlinks=False):
            for n in dns + fns:
                p = os.path.join(dp, n)
                add(bucket, os.path.relpath(p, home), os.lstat(p))

    for dp, dns, fns in os.walk(home, followlinks=False):
        rel_dp = os.path.relpath(dp, home)
        hidden_dir = rel_dp != "." and any(c.startswith(".") for c in rel_dp.split(os.sep))
        keep = []
        for n in sorted(dns):
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, home)
            st = os.lstat(p)
            if os.path.realpath(p) in skip or (stat.S_ISDIR(st.st_mode) and st.st_dev != dev):
                r["skipped"].append({"path": rel, "reason": "other filesystem / the USB itself (not entered)"})
                continue
            e = _ext(n)
            if stat.S_ISDIR(st.st_mode) and e in BUNDLE_EXT:
                b = r["os"]["darwin"]
                add(b, rel, st)
                whole_tree(p, b)           # bundle copied as one unit, never split into shared types
                continue
            add(r["os"][osname], rel, st)
            if not stat.S_ISLNK(st.st_mode):
                keep.append(n)
        dns[:] = keep
        for n in sorted(fns):
            p = os.path.join(dp, n)
            rel = os.path.relpath(p, home)
            st = os.lstat(p)
            e = _ext(n)
            cat = next((c for c, ex in SHARED_EXT.items() if e in ex), None)
            if cat and stat.S_ISREG(st.st_mode) and not hidden_dir and not n.startswith("."):
                add(r["shared"][cat], rel, st)
                continue
            target = next((o for o, ex in OS_EXT.items() if e in ex), osname) if stat.S_ISREG(st.st_mode) else osname
            add(r["os"][target], rel, st)
    return r


def _sha256(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _files_from_argv(home: str, dest: str, listfile: str, extra=()) -> List[str]:
    # rsync -axHAX as required; explicit NUL-separated list keeps the home-relative structure (implied --relative)
    return ["rsync", "-axHAX", "--from0", f"--files-from={listfile}", *extra, "--info=stats2",
            home.rstrip("/") + "/", dest.rstrip("/") + "/"]


def _write_list(path: str, rels: List[str]):
    with open(path, "wb") as f:
        f.write(b"\0".join(r.encode("utf-8", "surrogateescape") for r in rels))


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def backup(ext4_root: str, osname: str = "linux", execute: bool = False, consent: bool = False,
           user: Dict = None, mid: str = None, runner: Runner = _run, du: Callable[[str], int] = _du,
           free: Callable[[str], int] = None, work: Optional[str] = None) -> Dict:
    require_supported(osname)
    user = user or real_user()
    mid = mid or machine_id(osname)
    home = user["home"]
    if _inside(ext4_root, home) and os.stat(ext4_root).st_dev == os.stat(home).st_dev:
        raise DataError("the ext4 destination is a plain folder inside your home (same filesystem): refusing recursion")
    data = os.path.join(ext4_root, "Data")
    check_dest(ext4_root, data)
    need, avail = du(home), (free or (lambda p: shutil.disk_usage(p).free))(ext4_root)
    if need > avail * 0.95:
        raise DataError(f"not enough space on the ext4 partition: need about {need} B, free {avail} B")
    cls = classify(home, osname, skip_paths=[ext4_root])
    os_dirs = {o: backup_dir(ext4_root, o, mid, user["name"]) for o, rels in cls["os"].items() if rels or o == osname}
    plan = {"user": user["name"], "home": home, "machine_id": mid, "warning": BACKUP_WARNING, "executed": False,
            "estimated_bytes": need, "free_bytes": avail,
            "shared_counts": {c: len(v) for c, v in cls["shared"].items()},
            "os_counts": {o: len(v) for o, v in cls["os"].items()},
            "destinations": {**{o: d for o, d in os_dirs.items()},
                             "shared": os.path.join(data, SHARED_DIR)},
            "not_entered": cls["skipped"], "chmod": "777 on regular files/dirs of these backup copies only"}
    if not execute:
        return plan
    if not consent:
        raise DataError("backup needs explicit consent to the warning (--i-understand-777)")
    if runner is _run and not shutil.which("rsync"):
        raise DataError("rsync is not installed (install the 'rsync' package)")
    work = work or os.path.join(data, ".usbprep_work")
    os.makedirs(work, exist_ok=True)
    check_dest(ext4_root, work)
    results, fails, worst = [], [], "complete"

    def run_copy(rels, dest, tag, extra=()):
        nonlocal worst
        if not rels:
            return
        os.makedirs(dest, exist_ok=True)
        check_dest(ext4_root, dest)
        lf = os.path.join(work, f"{tag}.list")
        _write_list(lf, rels)
        rc, out, err = runner(_files_from_argv(home, dest, lf, extra))
        o = rsync_outcome(rc)
        results.append({"part": tag, "rsync_exit": rc, "status": o, "entries": len(rels)})
        fails.extend(failed_paths(err))
        worst = "failed" if "failed" in (o, worst) else "partial" if "partial" in (o, worst) else worst

    # 1) OS buckets -> /Data/<OS>/<mid>/<user>/home + manifest of ORIGINAL modes (taken from the source lstat)
    for o, d in os_dirs.items():
        os.makedirs(d, exist_ok=True)
        check_dest(ext4_root, d)
        _write_state(d, status="in_progress", user=user["name"])
        run_copy(cls["os"][o], os.path.join(d, TREE), f"os-{o}")
    # 2) shared: dedupe identical content, keep different versions apart, never overwrite (--ignore-existing)
    sdir = os.path.join(data, SHARED_DIR)
    os.makedirs(sdir, exist_ok=True)
    idx = _load_json(os.path.join(sdir, INDEX), {"version": 1, "entries": {}})
    prov = {"os": OS_DIR[osname], "machine": mid, "user": user["name"]}
    shared_report = {"new": 0, "identical_dedup": 0, "version_kept": 0}
    for cat, rels in cls["shared"].items():
        main, vers = [], []
        for rel in rels:
            tgt = os.path.join(sdir, cat, rel)
            try:
                sha = _sha256(os.path.join(home, rel))
            except OSError:
                fails.append(os.path.join(home, rel))
                worst = "partial" if worst == "complete" else worst
                continue
            key = f"{cat}/{rel}"
            ent = idx["entries"].setdefault(key, [])
            m = cls["meta"][rel]
            if os.path.lexists(tgt) and not os.path.islink(tgt) and os.path.isfile(tgt) and _sha256(tgt) == sha:
                loc, kind = f"{cat}/{rel}", "identical_dedup"
            elif os.path.lexists(tgt):
                loc, kind = f"{cat}/{VERSIONS}/{mid}-{user['name']}/{rel}", "version_kept"
                vers.append(rel)
            else:
                loc, kind = f"{cat}/{rel}", "new"
                main.append(rel)
            shared_report[kind] += 1
            ent[:] = [x for x in ent if not (x["machine"] == mid and x["user"] == user["name"])]
            ent.append({**prov, "orig_rel": rel, "sha256": sha, "mode": m["mode"], "mtime": m["mtime"], "loc": loc})
        run_copy(main, os.path.join(sdir, cat), f"shared-{cat}", ("--ignore-existing",))
        run_copy(vers, os.path.join(sdir, cat, VERSIONS, f"{mid}-{user['name']}"), f"shared-{cat}-v",
                 ("--ignore-existing",))
    tmp = os.path.join(sdir, INDEX + ".tmp")
    with open(tmp, "w") as f:
        json.dump(idx, f)
    os.replace(tmp, os.path.join(sdir, INDEX))
    # 3) manifests (original metadata) BEFORE chmod 777, then chmod the backup copies only
    chm = {"changed": 0, "skipped_special": 0, "errors": 0}
    for o, d in os_dirs.items():
        ents = {rel: cls["meta"][rel] for rel in cls["os"][o]}
        st = os.lstat(home)
        ents["."] = {"t": "d", "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
                     "mtime": int(st.st_mtime)}
        with open(os.path.join(d, MANIFEST), "w") as f:
            json.dump({"version": 2, "entries": ents, "shared_index": f"{SHARED_DIR}/{INDEX}", **prov}, f)
        os.chmod(os.path.join(d, MANIFEST), 0o600)
    if worst != "failed":
        for t in [os.path.join(d, TREE) for d in os_dirs.values()] + [os.path.join(sdir, c) for c in SHARED_EXT]:
            if os.path.isdir(t):
                for k, v in chmod_777_subtree(t, ext4_root).items():
                    chm[k] += v
    if chm["errors"] and worst == "complete":
        worst = "partial"
    for o, d in os_dirs.items():
        _write_state(d, status=worst, results=results, failed_paths=sorted(set(fails)), chmod=chm,
                     shared=shared_report)
        if user.get("elevated"):
            os.chown(d, user["uid"], user["gid"])
    shutil.rmtree(work, ignore_errors=True)
    plan.update(executed=True, status=worst, results=results, failed_paths=sorted(set(fails)), chmod=chm,
                shared=shared_report)
    return plan


def list_backups(ext4_root: str) -> List[Dict]:
    out = []
    base = os.path.join(ext4_root, "Data")
    if not os.path.isdir(base):
        return out
    idx = _load_json(os.path.join(base, SHARED_DIR, INDEX), {"entries": {}})
    srcs = {}
    for key, ents in idx["entries"].items():
        for e in ents:
            k = (e["os"], e["machine"], e["user"])
            srcs.setdefault(k, {}).setdefault(key.split("/")[0], 0)
            srcs[k][key.split("/")[0]] += 1
    for (o, m, u), cats in sorted(srcs.items()):
        out.append({"id": f"{SHARED_DIR}:{m}/{u}", "os": o, "machine": m, "user": u, "kind": "shared",
                    "categories": cats, "status": None, "time": None})
    for osd in sorted(x for x in os.listdir(base) if x in OS_DIR.values()):
        for mid in sorted(os.listdir(os.path.join(base, osd))) if os.path.isdir(os.path.join(base, osd)) else []:
            p = os.path.join(base, osd, mid)
            for u in sorted(os.listdir(p)) if os.path.isdir(p) and not os.path.islink(p) else []:
                d = os.path.join(p, u)
                if os.path.islink(d) or not os.path.isdir(os.path.join(d, TREE)):
                    continue
                try:
                    with open(os.path.join(d, STATE)) as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    st = {"status": "unknown"}
                out.append({"id": f"{osd}/{mid}/{u}", "os": osd, "machine": mid, "user": u, "kind": "os",
                            "status": st.get("status"), "time": st.get("time")})
    return out


def restore_token(user: str) -> str:
    return f"RESTORE {user}"


def restore(ext4_root: str, backup_id: str, osname: str = "linux", conflict: str = "skip-newer",
            execute: bool = False, confirm: Optional[str] = None, user: Dict = None, runner: Runner = _run,
            umask: int = 0o022, allow_foreign_os: bool = False) -> Dict:
    """Restore the OS-specific part (/Data/<OS>/<machine>/<user>/home) into the signed-in user's home."""
    require_supported(osname)
    user = user or real_user()
    parts = backup_id.split("/")
    if len(parts) != 3 or any(not _SAFE_COMPONENT.match(p) or p in (".", "..") for p in parts) \
            or parts[0] not in OS_DIR.values():
        raise DataError(f"invalid backup id {backup_id!r} (expected OS/machine/user from 'data list')")
    if parts[0] != OS_DIR[osname] and not allow_foreign_os:
        raise DataError(f"{backup_id} holds {parts[0]}-specific files; restoring them on {OS_DIR[osname]} needs "
                        "--allow-foreign-os (they are copied as files only, not made runnable). Shared data "
                        "(Music/Pictures/...) restores cross-OS with 'data restore-shared'.")
    d = os.path.join(ext4_root, "Data", *parts)
    check_dest(ext4_root, d)
    tree = os.path.join(d, TREE)
    if not os.path.isdir(tree):
        raise DataError(f"backup {backup_id} not found")
    chown = (user["uid"], user["gid"]) if user.get("elevated") else None
    home = user["home"]
    rc, out, err = runner(restore_argv(tree, home, conflict, dry=True, chown=chown))
    if rsync_outcome(rc) == "failed":
        raise DataError(f"restore preview failed (rsync exit {rc})")
    items = parse_itemized(out)
    target_os = osname
    incompat = name_incompatibilities(items["create"] + items["overwrite"] + items["dirs"], target_os)
    rep = {"backup": backup_id, "target_home": home, "target_user": user["name"], "conflict_mode": conflict,
           "would_create": len(items["create"]), "would_overwrite": len(items["overwrite"]),
           "overwrite_paths": items["overwrite"][:200], "new_dirs": len(items["dirs"]),
           "incompatibilities": incompat, "deletes": 0, "confirm_token": restore_token(user["name"]),
           "cross_os": parts[0] != OS_DIR[osname], "executed": False}
    if parts[0] != OS_DIR[osname]:
        rep["cross_os_note"] = (f"backup made on {parts[0]}: permissions/ACLs from that OS may not map; apps in it "
                                "are not made runnable here")
    if not execute:
        return rep
    if confirm != restore_token(user["name"]):
        raise DataError(f"restore needs the typed confirmation '{restore_token(user['name'])}'")
    if incompat and target_os != "linux":
        raise DataError("names incompatible with this OS were found; nothing restored")
    rc, out, err = runner(restore_argv(tree, home, conflict, dry=False, chown=chown))
    outcome = rsync_outcome(rc)
    done = parse_itemized(out)
    try:
        with open(os.path.join(d, MANIFEST)) as f:
            man = json.load(f)
    except (OSError, ValueError):
        man = {"entries": {}}
        rep["manifest_missing"] = True
    modes = restore_modes(home, done["create"] + done["overwrite"] + done["dirs"], man, umask, chown)
    rep.update(executed=True, status=outcome, rsync_exit=rc, failed_paths=failed_paths(err),
               restored_files=len(done["create"]) + len(done["overwrite"]), modes=modes)
    return rep


XDG_KEYS = {"Music": "XDG_MUSIC_DIR", "Pictures": "XDG_PICTURES_DIR", "Videos": "XDG_VIDEOS_DIR",
            "Documents": "XDG_DOCUMENTS_DIR"}


def known_folder_map(home: str) -> Dict[str, str]:
    """Linux: map English top-level known folders to the user's (possibly localized) XDG folders."""
    m = {}
    try:
        with open(os.path.join(home, ".config", "user-dirs.dirs")) as f:
            txt = f.read()
    except OSError:
        return m
    for name, key in XDG_KEYS.items():
        mm = re.search(rf'^{key}="\$HOME/([^"]+)"', txt, re.M)
        if mm and mm.group(1) != name and "/" not in mm.group(1).strip("/") and ".." not in mm.group(1):
            m[name] = mm.group(1).strip("/")
    return m


def restore_shared(ext4_root: str, machine: str, src_user: str, osname: str = "linux",
                   categories: Optional[List[str]] = None, conflict: str = "skip-newer", execute: bool = False,
                   confirm: Optional[str] = None, user: Dict = None, runner: Runner = _run,
                   umask: int = 0o022, work: Optional[str] = None) -> Dict:
    """Restore ONE explicitly chosen source's shared files (its own versions) into the signed-in user's home,
    at their original home-relative paths (top-level known folders mapped to localized XDG names)."""
    require_supported(osname)
    user = user or real_user()
    cats = categories or list(SHARED_EXT)
    if any(c not in SHARED_EXT for c in cats):
        raise DataError(f"categories must be among {list(SHARED_EXT)}")
    sdir = os.path.join(ext4_root, "Data", SHARED_DIR)
    check_dest(ext4_root, sdir)
    idx = _load_json(os.path.join(sdir, INDEX), {"entries": {}})
    home = user["home"]
    fmap = known_folder_map(home)
    groups: Dict[Tuple[str, str], List[str]] = {}
    modes: Dict[str, Dict] = {}
    for key, ents in idx["entries"].items():
        cat = key.split("/")[0]
        if cat not in cats:
            continue
        for e in ents:
            if e["machine"] != machine or e["user"] != src_user:
                continue
            rel, loc = e["orig_rel"], e["loc"]
            if rel.startswith("/") or ".." in rel.split("/") or not loc.endswith(rel):
                continue
            src_base = os.path.join(sdir, loc[: len(loc) - len(rel)].rstrip("/"))
            top, _, rest = rel.partition("/")
            dest_rel = f"{fmap[top]}/{rest}" if rest and top in fmap else rel
            dst_base = home if dest_rel == rel else os.path.join(home, fmap[top])
            item = rel if dest_rel == rel else rest
            if dest_rel != rel:
                src_base = os.path.join(src_base, top)
            groups.setdefault((src_base, dst_base), []).append(item)
            modes[dest_rel] = {"t": "f", "mode": e["mode"]}
    tok = restore_token(user["name"])
    total = sum(len(v) for v in groups.values())
    rep = {"source": f"{machine}/{src_user}", "categories": cats, "target_home": home, "target_user": user["name"],
           "conflict_mode": conflict, "files": total, "known_folder_map": fmap, "deletes": 0,
           "confirm_token": tok, "executed": False, "would_create": 0, "would_overwrite": 0, "overwrite_paths": []}
    if total == 0:
        raise DataError(f"no shared files recorded for {machine}/{src_user} in {cats}")
    work = work or os.path.join(ext4_root, "Data", ".usbprep_work")
    os.makedirs(work, exist_ok=True)
    check_dest(ext4_root, work)
    chown = (user["uid"], user["gid"]) if user.get("elevated") else None

    def go(dry):
        res, done = [], {"create": [], "overwrite": [], "dirs": []}
        for n, ((sb, db), items) in enumerate(sorted(groups.items())):
            check_dest(ext4_root, sb)
            if not _inside(db, home):
                raise DataError("restore target escapes the home folder")
            lf = os.path.join(work, f"rs{n}.list")
            _write_list(lf, items)
            argv = restore_argv(sb, db, conflict, dry, chown)
            argv[1:1] = ["--from0", f"--files-from={lf}"]
            rc, out, err = runner(argv)
            if True:
                prefix = os.path.relpath(db, home)
                for k, v in parse_itemized(out).items():
                    if k in done:
                        done[k] += [x if prefix == "." else f"{prefix}/{x}" for x in v]
            res.append({"rsync_exit": rc, "status": rsync_outcome(rc), "failed": failed_paths(err)})
        return res, done

    res, prev = go(True)
    rep.update(would_create=len(prev["create"]), would_overwrite=len(prev["overwrite"]),
               overwrite_paths=prev["overwrite"][:200],
               incompatibilities=name_incompatibilities(prev["create"] + prev["overwrite"], osname))
    if not execute:
        shutil.rmtree(work, ignore_errors=True)
        return rep
    if confirm != tok:
        raise DataError(f"restore needs the typed confirmation '{tok}'")
    res, done = go(False)
    st = [r["status"] for r in res]
    status = "failed" if "failed" in st else "partial" if "partial" in st else "complete"
    m = restore_modes(home, done["create"] + done["overwrite"], {"entries": modes}, umask, chown)
    shutil.rmtree(work, ignore_errors=True)
    rep.update(executed=True, status=status, results=res, modes=m,
               restored_files=len(done["create"]) + len(done["overwrite"]))
    return rep
