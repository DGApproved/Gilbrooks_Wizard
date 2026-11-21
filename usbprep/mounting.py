"""Linux mounting: FAT32 at /USB, ext4 at /USB/A.I. (kernel drivers only), checks, unmount/eject.

Mounting to the literal /USB requires root (creating a directory in / and calling mount);
udisks2 cannot mount at arbitrary literal paths, so explicit sudo/pkexec approval is used.
"""
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import constants as C
from .executor import privilege_prefix
from .runner import DEFAULT_RUNNER, CommandError

EXT4_RW_OPTS = "rw,exec,nosuid,nodev,relatime"   # exec allowed; no setuid/device files
EXT4_RO_OPTS = "ro,noload,nosuid,nodev"          # read-only fallback (no journal replay)


class MountError(RuntimeError):
    pass


@dataclass
class MountReport:
    executed: bool
    log: List[str] = field(default_factory=list)
    ok: bool = True
    ext4_mode: str = ""          # "rw" | "ro-fallback" | ""
    checks: dict = field(default_factory=dict)


def check_mountpoint_free(path: str) -> Optional[str]:
    """None if path is absent or an empty, unmounted directory; else refusal reason."""
    if os.path.ismount(path):
        return f"{path} is already a mount point; refusing to stack/hide it"
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
            return f"{path} exists and is not a plain directory; refusing"
        if os.listdir(path):
            return f"{path} exists and is not empty; refusing to hide its contents"
    return None


def ext4_health(dumpe2fs_header: str) -> List[str]:
    """Problems that make RW mounting unsafe (empty list => clean)."""
    probs = []
    fields = {}
    for line in dumpe2fs_header.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    state = fields.get("Filesystem state", "")
    if state != "clean":
        probs.append(f"filesystem state is '{state or 'unknown'}' (needs fsck)")
    if "needs_recovery" in fields.get("Filesystem features", ""):
        probs.append("journal needs recovery (unclean unmount)")
    if fields.get("Filesystem magic number", "0xEF53") != "0xEF53":
        probs.append("not an ext2/3/4 superblock")
    return probs


def parse_findmnt(text: str) -> dict:
    fs = json.loads(text)["filesystems"][0]
    return {"target": fs.get("target"), "source": fs.get("source"), "fstype": fs.get("fstype"),
            "options": fs.get("options", "").split(",")}


def invoking_user() -> tuple:
    """Real user even when run via sudo (SUDO_UID) or pkexec (PKEXEC_UID)."""
    uid = int(os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID") or os.getuid())
    gid = int(os.environ.get("SUDO_GID") or os.getgid())
    return uid, gid


def verify_rw_exec(ai_root: str, findmnt_info: Optional[dict], uid: int) -> dict:
    """Real write + execute probe on the mounted ext4 root, plus option/ownership report."""
    res = {"owner_uid": None, "owner_ok": False, "writable": False, "exec_ok": False,
           "noexec": False, "readonly": False, "nosuid": False, "problems": []}
    if findmnt_info:
        opts = findmnt_info["options"]
        res.update(noexec="noexec" in opts, readonly="ro" in opts, nosuid="nosuid" in opts)
        if res["noexec"]:
            res["problems"].append("mounted noexec: programs on the USB cannot be executed")
        if res["readonly"]:
            res["problems"].append("mounted read-only")
    try:
        res["owner_uid"] = os.stat(ai_root).st_uid
        res["owner_ok"] = res["owner_uid"] == uid
        if not res["owner_ok"]:
            res["problems"].append(f"ext4 root owned by uid {res['owner_uid']}, not by you (uid {uid})")
    except OSError as e:
        res["problems"].append(f"cannot stat {ai_root}: {e}")
        return res
    probe = os.path.join(ai_root, ".usbprep-probe.sh")
    try:
        with open(probe, "w") as f:
            f.write("#!/bin/sh\necho usbprep-exec-ok\n")
        os.chmod(probe, 0o755)
        res["writable"] = True
        out = subprocess.run([probe], capture_output=True, text=True, timeout=10)
        res["exec_ok"] = out.stdout.strip() == "usbprep-exec-ok"
        if not res["exec_ok"]:
            res["problems"].append(f"execute probe failed: {out.stderr.strip()}")
    except OSError as e:
        res["problems"].append(f"write/exec probe failed: {e}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
    return res


class LinuxMounter:
    def __init__(self, runner=DEFAULT_RUNNER, root: str = C.MOUNT_FAT):
        self.runner, self.root = runner, root
        self.ai = os.path.join(root, C.MOUNT_EXT4_NAME)

    def _priv(self, privilege, argv):
        return privilege_prefix(privilege) + argv

    def mount(self, fat_dev: str, ext_dev: str, execute: bool = False, privilege: Optional[str] = None,
              allow_ro_fallback: bool = False, fix_owner: bool = False,
              after_fat: Optional[Callable[[str], List[str]]] = None, fsck_mode: str = "preen",
              full_repair_consent: bool = False, guard: Optional[Callable[[], None]] = None,
              mounted_sources: Optional[set] = None) -> MountReport:
        """after_fat(root) is called once FAT is mounted (e.g. to copy the app into /USB/AI)."""
        uid, gid = invoking_user()
        fat_opts = f"rw,nosuid,nodev,uid={uid},gid={gid},umask=0022,shortname=mixed,utf8,flush"
        steps = [
            ["mkdir", "-p", self.root],
            ["mount", "-t", "vfat", "-o", fat_opts, fat_dev, self.root],
            ["mkdir", "-p", self.ai],
            ["mount", "-t", "ext4", "-o", EXT4_RW_OPTS, ext_dev, self.ai],
        ]
        rep = MountReport(executed=execute)
        why = check_mountpoint_free(self.root)
        if why:
            rep.ok, rep.log = False, [f"REFUSED: {why}"]
            return rep
        if not execute:
            rep.log = ["DRY RUN - would run (with privilege prefix if approved):"] + \
                      [f"  {shlex.join(self._priv(privilege, s))}" for s in steps] + \
                      [f"  before mounting: e2fsck -p {ext_dev} (safe preen; unmounted only; unresolved => RW blocked)",
                       "  then: verify ext4 health (dumpe2fs -h), RW/exec access probe, open file manager"]
            return rep
        force_ro = False
        if fsck_mode == "preen":
            from .fsck import Ext4Checker
            chk = Ext4Checker(self.runner, privilege, guard, mounted_sources)
            fr = chk.auto_check(ext_dev)
            rep.log += fr.log
            if fr.status == "needs-consent" and full_repair_consent:
                fr = chk.full_repair(ext_dev, True)
                rep.log += fr.log
            rep.checks["fsck"] = fr.status
            if not fr.mount_ok and not allow_ro_fallback:
                rep.ok = False
                if fr.report:
                    rep.log.append("read-only e2fsck report:\n" + fr.report[-2000:])
                rep.log.append("RW mount BLOCKED until the ext4 check passes (consent to full repair, or "
                               "allow the read-only fallback).")
                return rep
            force_ro = not fr.mount_ok
        try:
            hdr = self.runner.run(self._priv(privilege, ["dumpe2fs", "-h", ext_dev])).stdout
        except CommandError as e:
            rep.ok, rep.log = False, [f"cannot read ext4 superblock: {e}"]
            return rep
        probs = ext4_health(hdr)
        ext_opts = EXT4_RW_OPTS
        if force_ro:
            probs.append("ext4 check did not pass")
        if probs:
            if not allow_ro_fallback:
                rep.ok = False
                rep.log += [f"REFUSED RW mount of ext4: {'; '.join(probs)}",
                           f"Run `sudo e2fsck -f {ext_dev}` or allow the read-only fallback."]
                return rep
            ext_opts = EXT4_RO_OPTS
            rep.log.append(f"ext4 unclean ({'; '.join(probs)}) - mounting READ-ONLY by user choice")
        steps[3] = ["mount", "-t", "ext4", "-o", ext_opts, ext_dev, self.ai]
        created_root = not os.path.exists(self.root)
        try:
            self.runner.run(self._priv(privilege, steps[0]))
            self.runner.run(self._priv(privilege, steps[1]))
            rep.log.append(f"mounted FAT32 {fat_dev} at {self.root}")
            if after_fat:
                rep.log += after_fat(self.root)
            why = check_mountpoint_free(self.ai)
            if why:
                raise MountError(why)
            self.runner.run(steps[2])  # FAT is owned by the user via uid= option
            if guard:
                guard()  # same device still present?
            if fsck_mode == "preen":
                from .fsck import ensure_unmounted
                ensure_unmounted(ext_dev, mounted_sources)  # automount race since the check
            self.runner.run(self._priv(privilege, steps[3]))
            rep.log.append(f"mounted ext4 {ext_dev} at {self.ai} ({ext_opts})")
        except (CommandError, MountError, OSError, RuntimeError) as e:
            rep.ok = False
            rep.log.append(f"FAILED: {e}; rolling back mounts")
            self.unmount(privilege=privilege, execute=True, remove_root=created_root)
            return rep
        rep.ext4_mode = "rw" if ext_opts == EXT4_RW_OPTS else "ro-fallback"
        info = None
        try:
            info = parse_findmnt(self.runner.run(["findmnt", "-J", "-T", self.ai]).stdout)
        except (CommandError, ValueError, KeyError, IndexError):
            rep.log.append("warning: could not read mount options via findmnt")
        if rep.ext4_mode == "rw":
            st_uid = os.stat(self.ai).st_uid
            if st_uid != uid and fix_owner:
                # Top-level directory only, never recursive, never chmod 777.
                self.runner.run(self._priv(privilege, ["chown", f"{uid}:{gid}", self.ai]))
                rep.log.append(f"changed owner of {self.ai} (top level only) to {uid}:{gid}")
            rep.checks.update(verify_rw_exec(self.ai, info, uid))
            rep.log.append("ext4 access check: " + ("OK (read/write, user-owned, exec permitted)"
                           if not rep.checks["problems"] else "; ".join(rep.checks["problems"])))
        rep.ok = not rep.checks.get("problems")
        return rep

    def unmount(self, privilege=None, execute=False, remove_root=False, eject_dev: Optional[str] = None):
        steps = []
        for mp in (self.ai, self.root):
            if os.path.ismount(mp) or not execute:
                steps.append(["umount", mp])
        if remove_root:
            steps.append(["rmdir", self.root])  # rmdir only succeeds when empty
        if eject_dev:
            steps.append(["udisksctl", "power-off", "-b", eject_dev])
        if not execute:
            return MountReport(False, ["DRY RUN - would run:"] + [f"  {shlex.join(self._priv(privilege, s))}" for s in steps])
        rep = MountReport(True)
        for s in steps:
            argv = s if s[0] == "udisksctl" else self._priv(privilege, s)
            try:
                self.runner.run(argv)
                rep.log.append(f"ok: {shlex.join(argv)}")
            except CommandError as e:
                rep.ok = False
                rep.log.append(f"FAILED: {e}")
                if s[0] == "umount":
                    break  # never power off a drive that is still mounted
        return rep


def open_file_manager(path: str, runner=DEFAULT_RUNNER, platform: str = "linux") -> List[str]:
    argv = {"linux": ["xdg-open", path], "darwin": ["open", path], "win32": ["explorer.exe", path]}[platform]
    try:
        runner.run(argv, timeout=20)
        return [f"opened file manager: {shlex.join(argv)}"]
    except CommandError as e:
        return [f"could not open file manager ({e}); browse to {path} manually"]
