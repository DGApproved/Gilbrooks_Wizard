"""Pre-mount ext4 check/repair (Linux, kernel/e2fsprogs tools only). Never touches a mounted fs.

Policy:
  * automatic path: `e2fsck -p` (preen = only repairs e2fsck itself deems safe without questions)
  * exit 4 (uncorrected) -> read-only report `e2fsck -fn`; full `e2fsck -fy` ONLY with explicit consent
  * bad blocks: separate opt-in READ-ONLY `badblocks` scan; results recorded with `e2fsck -l` only
    after consent; aborts on scan errors / device change / implausible counts. Never write-mode tests.
  * any unresolved result => RW mount must be blocked (caller enforces).
FAT32 and swap are not repaired here.
"""
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .executor import privilege_prefix
from .runner import DEFAULT_RUNNER, CommandError
from .safety import SafetyError, proc_mounted_sources

# e2fsck exit-status bits (man e2fsck)
FS_OK, FS_FIXED, FS_REBOOT, FS_UNCORRECTED, FS_OPERR, FS_USAGE, FS_CANCELED, FS_LIBERR = 0, 1, 2, 4, 8, 16, 32, 128
MAX_BADBLOCKS = 256           # more than this => treat device as failing / unstable, refuse to mark
BADBLOCKS_MAX_FRACTION = 0.001


class FsckError(RuntimeError):
    pass


@dataclass
class FsckResult:
    status: str               # clean | repaired | needs-consent | failed | aborted
    exit_code: Optional[int] = None
    log: List[str] = field(default_factory=list)
    report: str = ""          # read-only e2fsck -fn output when consent is needed

    @property
    def mount_ok(self) -> bool:
        return self.status in ("clean", "repaired")


def interpret(rc: int) -> str:
    if rc & (FS_OPERR | FS_USAGE | FS_CANCELED | FS_LIBERR):
        return "failed"
    if rc & FS_UNCORRECTED:
        return "needs-consent"
    if rc & (FS_FIXED | FS_REBOOT):
        return "repaired"
    return "clean"


def ensure_unmounted(target: str, mounted_sources: Optional[set] = None) -> None:
    """Raise if the partition (or image) is mounted or active as swap anywhere."""
    src = proc_mounted_sources() if mounted_sources is None else mounted_sources
    if os.path.realpath(target.split("?", 1)[0]) in src:
        raise SafetyError(f"{target} is mounted (desktop automount?) - unmount it first "
                          f"(e.g. `udisksctl unmount -b {target}`); refusing to check a mounted filesystem")


class Ext4Checker:
    def __init__(self, runner=DEFAULT_RUNNER, privilege: Optional[str] = None,
                 guard: Optional[Callable[[], None]] = None, mounted_sources: Optional[set] = None):
        """guard(): caller-supplied identity revalidation (raises SafetyError on change)."""
        self.runner, self.priv = runner, privilege_prefix(privilege)
        self.guard, self.mounted = guard, mounted_sources

    def _pre(self, target):
        if self.guard:
            self.guard()
        ensure_unmounted(target, self.mounted)

    def _e2fsck(self, target, *opts) -> tuple:
        self._pre(target)
        r = self.runner.run(self.priv + ["e2fsck", *opts, target], check=False, timeout=7200)
        return r.returncode, (r.stdout + r.stderr).strip()

    def auto_check(self, target: str) -> FsckResult:
        """Insertion/mount path: safe preen repair only."""
        try:
            rc, out = self._e2fsck(target, "-p")
        except SafetyError as e:
            return FsckResult("aborted", None, [f"ABORTED: {e}"])
        st = interpret(rc)
        res = FsckResult(st, rc, [f"e2fsck -p {target}: exit {rc} -> {st}"] + out.splitlines()[-5:])
        if st == "needs-consent":
            _, rep = self._e2fsck(target, "-f", "-n")  # read-only report of what a full repair would change
            res.report = rep
            res.log.append("Errors need a full repair that may move/delete damaged files to lost+found. "
                           "Back up anything important first. Explicit consent required (RW mount blocked).")
        elif st == "failed":
            res.log.append("e2fsck could not complete (I/O or operational error): device may be failing "
                           "or disconnected. RW mount blocked. Back up data and consider replacing the drive.")
        return res

    def full_repair(self, target: str, consent: bool) -> FsckResult:
        """Escalated repair: answers 'yes' to all e2fsck questions - ONLY after explicit user consent."""
        if not consent:
            return FsckResult("needs-consent", None, ["full repair not consented; nothing done"])
        try:
            rc, out = self._e2fsck(target, "-f", "-y")
        except SafetyError as e:
            return FsckResult("aborted", None, [f"ABORTED: {e}"])
        st = interpret(rc)
        if st == "needs-consent":  # still uncorrected after -y => give up
            st = "failed"
        return FsckResult(st, rc, [f"e2fsck -f -y {target}: exit {rc} -> {st}"] + out.splitlines()[-8:])

    def block_size(self, target: str) -> int:
        out = self.runner.run(self.priv + ["dumpe2fs", "-h", target]).stdout
        for line in out.splitlines():
            if line.startswith("Block size:"):
                return int(line.split(":")[1])
        raise FsckError("cannot determine ext4 block size")

    def scan_bad_blocks(self, target: str, total_blocks: Optional[int] = None) -> tuple:
        """Opt-in READ-ONLY scan. Returns (list_file, blocks). Raises FsckError on unstable conditions."""
        self._pre(target)
        bs = self.block_size(target)
        fd, out = tempfile.mkstemp(prefix="usbprep-badblocks-", suffix=".txt")
        os.close(fd)
        # badblocks without -w/-n is the read-only test mode.
        r = self.runner.run(self.priv + ["badblocks", "-s", "-v", "-b", str(bs), "-o", out, target],
                            check=False, timeout=86400)
        if r.returncode != 0:
            raise FsckError(f"badblocks failed (exit {r.returncode}): {r.stderr.strip()[-200:]} - "
                            "not marking anything (could be a disconnect or permission problem)")
        if self.guard:
            self.guard()  # device must still be the same one after a long scan
        with open(out) as f:
            blocks = [int(x) for x in f.read().split() if x.isdigit()]
        limit = MAX_BADBLOCKS if not total_blocks else min(MAX_BADBLOCKS, int(total_blocks * BADBLOCKS_MAX_FRACTION) + 1)
        if len(blocks) > limit:
            raise FsckError(f"{len(blocks)} unreadable blocks (> {limit}): the drive looks failing or unstable. "
                            "Not marking; back up the data and replace the drive.")
        return out, blocks

    def mark_bad_blocks(self, target: str, list_file: str, blocks: List[int], consent: bool) -> FsckResult:
        """Record blocks in the ext4 bad-block inode (logical only; does not repair the flash)."""
        if not blocks:
            return FsckResult("clean", 0, ["no unreadable blocks found; nothing marked"])
        if not consent:
            return FsckResult("needs-consent", None, [f"{len(blocks)} unreadable blocks found; marking needs consent. "
                                                      "Recommendation: back up and replace the drive."])
        try:
            rc, out = self._e2fsck(target, "-f", "-p", "-l", list_file)
        except SafetyError as e:
            return FsckResult("aborted", None, [f"ABORTED: {e}"])
        st = interpret(rc)
        return FsckResult(st, rc, [f"marked {len(blocks)} blocks bad via e2fsck -l: exit {rc} -> {st}",
                                   "Note: this only stops ext4 from using those blocks; it does not fix failing "
                                   "flash or replace the controller's own remapping. Replace the drive soon."]
                          + out.splitlines()[-5:])
