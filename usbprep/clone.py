"""Clone menu: copy ONLY the ext4 "AI" partition contents from one prepared USB to another (Linux).

Backend (documented choice): file-level `rsync -aHAX --numeric-ids --delete` between two private
temporary mounts - source ext4 mounted READ-ONLY with `noload` (no journal replay, so the source is
never written), destination mounted exclusively. Preserves contents, ownership (numeric uid/gid),
modes, timestamps, symlinks, hardlinks, ACLs and xattrs. Not a raw block copy: each drive keeps its
own filesystem UUID/partition IDs, the source UUID is never touched, and FAT32 (app, cache, boot
sources, bootloader) and swap are never written. Verification: an rsync --checksum dry run of the
same options must report zero differences.
"""
import json
import os
import tempfile
from typing import List, Optional

from .detect import Device
from .executor import privilege_prefix
from .safety import SafetyError, proc_mounted_sources

EXCLUDES = ["--exclude=/lost+found"]
COPY_FLAGS = ["-aHAX", "--numeric-ids", "--delete", "--one-file-system"] + EXCLUDES
VERIFY_FLAGS = ["-aHAXn", "--numeric-ids", "--delete", "--one-file-system", "--checksum",
                "--itemize-changes", "--out-format=%i %n"] + EXCLUDES
MARGIN = 1.05      # need source-used * 1.05 <= destination usable size
UUID_WARNING = ("Note: the copy keeps the destination's own ext4 UUID. Projects, scripts or OS configs inside the "
                "copied data that refer to the SOURCE filesystem UUID (e.g. fstab lines) may need manual changes; "
                "usbprep does not rewrite them.")


class CloneError(RuntimeError):
    pass


def token_for(dst: Device) -> str:
    return "REPLACE " + dst.confirm_token().split(" ", 1)[1]


def rsync_copy_argv(src_dir: str, dst_dir: str) -> List[str]:
    return ["rsync"] + COPY_FLAGS + [src_dir.rstrip("/") + "/", dst_dir.rstrip("/") + "/"]


def rsync_verify_argv(src_dir: str, dst_dir: str) -> List[str]:
    return ["rsync"] + VERIFY_FLAGS + [src_dir.rstrip("/") + "/", dst_dir.rstrip("/") + "/"]


def differences(verify_stdout: str) -> List[str]:
    return [l for l in verify_stdout.splitlines() if l.strip()]


class CloneJob:
    def __init__(self, session, src: Device, dst: Device, mounted_sources: Optional[set] = None):
        self.s, self.src, self.dst = session, src, dst
        self.mounted = proc_mounted_sources() if mounted_sources is None else mounted_sources

    # ---- checks shared by dry run and execution (all BEFORE any write) ----
    def check(self):
        from .vmhandoff import assert_host_owned
        src, dst = self.src, self.dst
        if os.path.realpath(src.path) == os.path.realpath(dst.path):
            raise CloneError("source and destination are the same physical disk; refusing")
        if src.serial and src.serial == dst.serial and src.model == dst.model:
            raise CloneError("source and destination report the same serial/model (ambiguous identity); refusing")
        if self.s.source_disk and os.path.realpath(self.s.source_disk) == os.path.realpath(dst.path):
            raise CloneError("the destination is the USB this app runs from; refusing")
        for d in (src, dst):
            assert_host_owned(d)
        self.src_ext = self.s._parts(src)[1]
        self.dst_ext = self.s._parts(dst)[1]
        for d, part in ((src, self.src_ext), (dst, self.dst_ext)):
            kids = {c.get("path"): c for c in d.raw.get("children") or []}
            mp = [m for m in (kids.get(part.path) or {}).get("mountpoints") or [] if m]
            if mp or os.path.realpath(part.path) in self.mounted:
                raise CloneError(f"{part.path} (ext4) is mounted{' at ' + mp[0] if mp else ''}; unmount it first "
                                 "(Unmount button) - both ext4 partitions must be unmounted for a consistent clone")
        return self

    def _guard(self):
        live = {d.path: d for d in self.s.lister()}
        for d in (self.src, self.dst):
            cur = live.get(d.path)
            if cur is None or cur.identity() != d.identity():
                raise SafetyError(f"{d.path} changed or disappeared; clone aborted")

    def summary(self) -> List[str]:
        s, d = self.src, self.dst
        return [f"CLONE ext4 'AI' partition ONLY (FAT32 app/cache/boot and swap are not touched)",
                f"  source     : {s.path} {s.model} serial={s.serial or '?'} {s.size_human} -> {self.src_ext.path} (read only)",
                f"  destination: {d.path} {d.model} serial={d.serial or '?'} {d.size_human} -> {self.dst_ext.path}",
                f"  ALL EXISTING FILES ON THE DESTINATION'S A.I. PARTITION WILL BE REPLACED (deleted, then copied).",
                f"  backend: rsync {' '.join(COPY_FLAGS)} (filesystem UUIDs unchanged)",
                "  " + UUID_WARNING]

    # ---- run ----
    def run(self, execute=False, confirmation=None, privilege=None) -> List[str]:
        self.check()
        log = self.summary()
        if not execute:
            return ["DRY RUN - nothing written"] + log + [
                f"  steps: e2fsck -n both; mount source ro,noload; mount destination; capacity check; rsync; "
                f"checksum verify; unmount", f"To execute type: --execute --confirm '{token_for(self.dst)}'"]
        if (confirmation or "").strip() != token_for(self.dst):
            raise CloneError(f"confirmation token mismatch: type exactly '{token_for(self.dst)}'")
        pre = privilege_prefix(privilege)
        run = self.s.runner.run
        for part in (self.src_ext, self.dst_ext):          # read-only checks (no repair here)
            self._guard()
            r = run(pre + ["e2fsck", "-n", "-f", part.path], check=False)
            if r.returncode != 0:
                raise CloneError(f"{part.path} is not clean (e2fsck -n rc={r.returncode}); use Extras > Check/Repair "
                                 f"first. Nothing was written.")
        smnt = tempfile.mkdtemp(prefix="usbprep-clone-src-")
        dmnt = tempfile.mkdtemp(prefix="usbprep-clone-dst-")
        mounted = []
        try:
            self._guard()
            run(pre + ["mount", "-t", "ext4", "-o", "ro,noload,nosuid,nodev,noexec", self.src_ext.path, smnt])
            mounted.append(smnt)
            self._guard()
            run(pre + ["mount", "-t", "ext4", "-o", "nosuid,nodev", self.dst_ext.path, dmnt])
            mounted.append(dmnt)
            for mnt, part in ((smnt, self.src_ext), (dmnt, self.dst_ext)):
                fm = json.loads(run(["findmnt", "-J", "-o", "SOURCE,FSTYPE", "--mountpoint", mnt]).stdout)
                f0 = fm["filesystems"][0]
                if os.path.realpath(f0["source"]) != os.path.realpath(part.path) or f0["fstype"] != "ext4":
                    raise CloneError(f"mount check failed for {mnt}: {f0}")
            used = int(run(["df", "-B1", "--output=used", smnt]).stdout.split()[-1])
            dsz = run(["df", "-B1", "--output=size", dmnt]).stdout.split()[-1]
            if used * MARGIN > int(dsz):
                raise CloneError(f"destination too small: source uses {used} B, destination ext4 holds {dsz} B "
                                 f"(needs {MARGIN:.2f}x). Nothing copied; partitions are never resized.")
            log.append(f"capacity ok: source uses {used} B, destination ext4 size {dsz} B")
            self._guard()
            run(pre + rsync_copy_argv(smnt, dmnt), timeout=24 * 3600)
            run(["sync"], check=False)
            diff = differences(run(pre + rsync_verify_argv(smnt, dmnt), timeout=24 * 3600).stdout)
            if diff:
                raise CloneError("verification found differences after the copy (destination left mounted-off; "
                                 "re-run Clone): " + "; ".join(diff[:5]))
            log.append("copied and verified: rsync --checksum dry run reports 0 differences in content, ownership, "
                       "modes, times, symlinks, hardlinks, ACLs and xattrs (verified files only - not bootability)")
        finally:
            for m in reversed(mounted):
                run(pre + ["umount", m], check=False)
            for m in (smnt, dmnt):
                try:
                    os.rmdir(m)
                except OSError:
                    log.append(f"WARNING: temporary mount point {m} not removed")
        return log + ["both ext4 partitions unmounted; source was mounted read-only (noload) and not modified"]
