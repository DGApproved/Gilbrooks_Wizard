"""Extras > Reformat (Linux): back up EFI:/AI to a private /tmp workspace, relaunch from there,
re-prepare the selected drive (may be the drive the app runs from), restore + verify the software.

ext4 user data is NOT backed up. Any failure before the first destructive write aborts; any failure
after it keeps the /tmp backup and reports its path. The backup is removed only after a verified restore.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional

from . import constants as C
from .appcopy import DirBackend, CopyError, hash_tree, restore_tree, SKIP
from .detect import selectable
from .executor import privilege_prefix
from .runner import CommandError
from .safety import SafetyError

SECRET_RE = re.compile(r"(^|/)(\.env(\.[^/]*)?|id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|[^/]*\.(pem|key|p12|pfx|kdbx|gpg)|"
                       r"[^/]*(secret|token|password|passwd|credential)[^/]*)$", re.I)
JOB_FILE, READY_FILE, BACKUP_MANIFEST, LOG_FILE = "reformat_job.json", "ready.json", "backup_manifest.json", "reformat.log"
MONITOR_JAR = os.path.join("gui-java", "dist", "usbprep-gui.jar")


class StageError(RuntimeError):
    pass


class HandoffError(RuntimeError):
    pass


def stage_backup(src_ai_dir: str, base: str = "/tmp") -> dict:
    """Copy the whole AI folder (minus secret-looking files) into a unique 0700 dir and verify it."""
    if not os.path.isfile(os.path.join(src_ai_dir, C.MANIFEST_NAME)):
        raise StageError(f"{src_ai_dir} is not a usbprep AI folder (no {C.MANIFEST_NAME})")
    need = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(src_ai_dir) for f in fs)
    free = shutil.disk_usage(base).free
    if need + 64 * C.MiB > free:   # verify the backup fits BEFORE anything is copied or wiped
        raise StageError(f"staging aborted, nothing was formatted: backup needs {need} B (+64 MiB margin) "
                         f"but {base} has only {free} B free")
    work = tempfile.mkdtemp(prefix="usbprep-reformat-", dir=base)   # mkdtemp => mode 0700, unique
    staged = os.path.join(work, C.APP_DIR_NAME)
    excluded: List[str] = []

    def ignore(d, names):
        rel_d = os.path.relpath(d, src_ai_dir).replace(os.sep, "/")
        out = set()
        for n in names:
            rel = n if rel_d == "." else f"{rel_d}/{n}"
            if n in SKIP or n.endswith(SKIP):
                out.add(n)
            elif SECRET_RE.search(rel) and os.path.isfile(os.path.join(d, n)):
                excluded.append(rel)
                out.add(n)
        return out

    try:
        shutil.copytree(src_ai_dir, staged, ignore=ignore)
        src_hashes = {k: v for k, v in hash_tree(src_ai_dir).items() if k not in excluded}
        got = hash_tree(staged)
        if got != src_hashes:
            diff = sorted(set(src_hashes.items()) ^ set(got.items()))[:5]
            raise StageError(f"backup verification failed: {diff}")
    except (OSError, StageError) as e:
        shutil.rmtree(work, ignore_errors=True)
        raise StageError(f"staging aborted, nothing was formatted: {e}")
    with open(os.path.join(work, BACKUP_MANIFEST), "w") as f:
        json.dump({"source": src_ai_dir, "files": got, "excluded_secrets": excluded}, f, indent=1)
    return {"work": work, "staged": staged, "hashes": got, "excluded": excluded}


def write_job(stage: dict, dev, fingerprint: dict, token: str, privilege: Optional[str],
              allow_ambiguous: bool, mount_root: str, wait_pids: Optional[List[int]] = None, full_format=False) -> str:
    job = {"device": dev.identity(), "fingerprint": fingerprint, "token": token, "privilege": privilege,
           "full_format": bool(full_format),
           "allow_ambiguous": allow_ambiguous, "mount_root": mount_root, "wait_pids": wait_pids or [], **stage}
    path = os.path.join(stage["work"], JOB_FILE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(job, f, indent=1)
    return path


def relaunch_from_stage(job_path: str, gui: bool = False, timeout: float = 30.0, popen=subprocess.Popen) -> int:
    """Start the staged copy; return its pid once it confirmed (ready.json) it runs from the stage."""
    with open(job_path) as f:
        job = json.load(f)
    staged = os.path.realpath(job["staged"])
    env = dict(os.environ, PYTHONPATH=staged, PYTHONDONTWRITEBYTECODE="1", USBPREP_PARENT_PID=str(os.getpid()))
    argv = [sys.executable, "-m", "usbprep", "reformat-resume", job_path] + (["--monitor"] if gui else [])
    # Output goes to a log in the private work dir: the parent (and any GUI pipe) is about to exit.
    with open(os.path.join(job["work"], LOG_FILE), "ab") as logf:
        child = popen(argv, cwd=staged, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                      stdout=logf, stderr=subprocess.STDOUT)
    ready = os.path.join(job["work"], READY_FILE)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(ready):
            with open(ready) as f:
                info = json.load(f)
            if not os.path.realpath(info.get("module_file", "")).startswith(staged + os.sep):
                child.kill()
                raise HandoffError(f"relaunched copy is not running from the backup: {info}")
            return child.pid
        if child.poll() is not None:
            raise HandoffError(f"relaunched copy exited early (code {child.returncode}); nothing formatted")
        time.sleep(0.2)
    child.kill()
    raise HandoffError("relaunched copy did not confirm the handoff in time; nothing formatted")


def child_handshake(job: dict, parent_pid: Optional[int], timeout: float = 60.0) -> None:
    """In the staged copy: confirm we run from the stage, then wait for the original process(es) to exit
    (the Python parent plus any job["wait_pids"], e.g. the Java GUI whose jar lives on the target)."""
    import usbprep
    staged = os.path.realpath(job["staged"])
    mod = os.path.realpath(usbprep.__file__)
    if not mod.startswith(staged + os.sep) or not os.path.realpath(os.getcwd()).startswith(staged):
        raise HandoffError(f"not running from the backup (module {mod}, cwd {os.getcwd()})")
    with open(os.path.join(job["work"], READY_FILE), "w") as f:
        json.dump({"pid": os.getpid(), "module_file": mod, "cwd": os.getcwd()}, f)
    pending = {p for p in [parent_pid] + list(job.get("wait_pids") or []) if p}
    t0 = time.time()
    while pending and time.time() - t0 < timeout:
        for pid in list(pending):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pending.discard(pid)
            except PermissionError:
                pass
        if pending:
            time.sleep(0.2)
    if pending:
        raise HandoffError(f"original process(es) {sorted(pending)} did not exit; nothing formatted")


def start_monitor(job: dict, popen=subprocess.Popen):
    """Progress window for the GUI user, started from the STAGED jar (no handle on the target). Optional."""
    jar = os.path.join(job["staged"], MONITOR_JAR)
    java = shutil.which("java")
    if not (java and os.path.isfile(jar)):
        print(f"(no progress window: java or {jar} missing; follow {os.path.join(job['work'], LOG_FILE)})", flush=True)
        return None
    return popen([java, "-jar", jar, "--monitor", os.path.join(job["work"], LOG_FILE)], cwd=job["staged"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def processes_using(paths: List[str], proc: str = "/proc") -> List[str]:
    """Processes (readable by us) with cwd/exe/open files under any of paths."""
    roots = [os.path.realpath(p) for p in paths if p]
    hits, me = [], os.getpid()
    if not roots:
        return hits
    for pid in (p for p in os.listdir(proc) if p.isdigit() and int(p) != me):
        links = [f"{proc}/{pid}/cwd", f"{proc}/{pid}/exe"]
        try:
            links += [f"{proc}/{pid}/fd/{fd}" for fd in os.listdir(f"{proc}/{pid}/fd")]
        except OSError:
            pass
        for ln in links:
            try:
                tgt = os.readlink(ln)
            except OSError:
                continue
            if any(tgt == r or tgt.startswith(r + "/") for r in roots):
                hits.append(f"pid {pid}: {tgt}")
                break
    return hits


@contextlib.contextmanager
def udisks_temp_mount(runner, part: str):
    out = runner.run(["udisksctl", "mount", "-b", part]).stdout
    mnt = out.strip().split(" at ", 1)[-1].rstrip(".") if " at " in out else ""
    if not mnt or not os.path.isdir(mnt):
        raise CopyError(f"could not determine temporary mount point from: {out.strip()}")
    try:
        yield DirBackend(mnt)
    finally:
        runner.run(["udisksctl", "unmount", "-b", part], check=False)


def run_job(job: dict, session, log: Callable[[str], None] = print, restore_backend: Callable = None,
            busy_scanner: Callable = processes_using, mounted_sources: Optional[set] = None) -> bool:
    """Phase B, executed by the staged copy. Returns True only after a verified restore."""
    keep = f"Backup kept at {job['work']} (restore manually: copy its AI folder to the EFI partition)."
    priv = privilege_prefix(job.get("privilege"))
    ident = job["device"]
    cur = {d.path: d for d in session.lister()}.get(ident["path"])
    if cur is None or cur.identity() != ident:
        log(f"ABORT (nothing formatted): device changed or missing. {keep}")
        return False
    why = selectable(cur, job.get("allow_ambiguous", False))
    if why:
        log(f"ABORT (nothing formatted): {why}. {keep}")
        return False
    mps = sorted(set(cur.mountpoints) - {"[SWAP]"}, key=len, reverse=True)
    users = busy_scanner(mps)
    if users:
        log(f"ABORT (nothing formatted): processes still use the drive: {'; '.join(users)}. {keep}")
        return False
    try:
        for mp in mps:
            session.runner.run(priv + ["umount", mp])
            log(f"unmounted {mp}")
        for ch in cur.raw.get("children") or []:
            if "[SWAP]" in (ch.get("mountpoints") or []):
                session.runner.run(priv + ["swapoff", ch["path"]])
                log(f"swapoff {ch['path']}")
    except CommandError as e:
        log(f"ABORT (nothing formatted): could not release the drive: {e}. {keep}")
        return False
    cur = {d.path: d for d in session.lister()}.get(ident["path"])
    if cur is None or cur.identity() != ident:
        log(f"ABORT (nothing formatted): device changed while unmounting. {keep}")
        return False
    try:
        r = session.prepare(cur, True, job["token"], job.get("privilege"), job.get("allow_ambiguous", False),
                            mounted_sources, force=True, reformat=True, full=job.get("full_format", False))
    except (SafetyError, RuntimeError) as e:
        log(f"ABORT: {e}. {keep}")
        return False
    for line in r.log:
        log(line)
    if not r.ok:
        log(f"FORMAT FAILED - the drive is not in a valid state. {keep}")
        return False
    try:
        p1 = session._parts(cur)[0].path
        ctx = restore_backend(p1) if restore_backend else udisks_temp_mount(session.runner, p1)
        with ctx as backend:
            for line in restore_tree(backend, job["staged"], job["hashes"]):
                log(line)
    except (CopyError, CommandError, OSError, RuntimeError) as e:
        log(f"RESTORE FAILED: {e}. {keep}")
        return False
    if job.get("excluded"):
        log("Not restored (secret-looking files were never copied to /tmp): " + ", ".join(job["excluded"]))
    shutil.rmtree(job["work"], ignore_errors=True)
    log(f"Reformat complete; verified restore done; temporary backup {job['work']} removed.")
    return True
