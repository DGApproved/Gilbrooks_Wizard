import json
import os

import pytest

from usbprep import vmhandoff
from usbprep.fsck import MAX_BADBLOCKS, Ext4Checker, FsckError, ensure_unmounted, interpret
from usbprep.mounting import EXT4_RO_OPTS, LinuxMounter, check_mountpoint_free, ext4_health, verify_rw_exec
from usbprep.runner import MockRunner
from usbprep.safety import SafetyError

from helpers import prepared_lsblk_device, session_with

CLEAN = "Filesystem magic number:  0xEF53\nFilesystem state:         clean\nFilesystem features: has_journal extent\nBlock size: 4096\n"
DIRTY = "Filesystem magic number:  0xEF53\nFilesystem state:         not clean\nFilesystem features: has_journal needs_recovery\n"


def test_mountpoint_checks(workdir):
    assert check_mountpoint_free(str(workdir / "absent")) is None
    (workdir / "empty").mkdir()
    assert check_mountpoint_free(str(workdir / "empty")) is None
    (workdir / "full").mkdir()
    (workdir / "full" / "f").write_text("x")
    assert "not empty" in check_mountpoint_free(str(workdir / "full"))
    (workdir / "file").write_text("x")
    assert "not a plain directory" in check_mountpoint_free(str(workdir / "file"))
    os.symlink(str(workdir / "empty"), workdir / "link")
    assert "not a plain directory" in check_mountpoint_free(str(workdir / "link"))
    assert "already a mount point" in check_mountpoint_free("/proc")


def test_ext4_health():
    assert ext4_health(CLEAN) == []
    probs = ext4_health(DIRTY)
    assert any("not clean" in p for p in probs) and any("recovery" in p for p in probs)


@pytest.mark.parametrize("rc,st", [(0, "clean"), (1, "repaired"), (2, "repaired"), (4, "needs-consent"),
                                   (8, "failed"), (12, "failed"), (32, "failed"), (128, "failed")])
def test_interpret(rc, st):
    assert interpret(rc) == st


def test_never_check_mounted():
    with pytest.raises(SafetyError):
        ensure_unmounted("/dev/sdx2", {"/dev/sdx2"})
    r = MockRunner()
    res = Ext4Checker(r, mounted_sources={"/dev/sdx2"}).auto_check("/dev/sdx2")
    assert res.status == "aborted" and not res.mount_ok and r.calls == []


def test_preen_and_consent_flow():
    r = MockRunner(responses={"e2fsck -p": (4, "UNEXPECTED INCONSISTENCY", ""), "e2fsck -f -n": (4, "would fix X", ""),
                              "e2fsck -f -y": (1, "fixed", "")})
    c = Ext4Checker(r, mounted_sources=set())
    res = c.auto_check("/dev/sdx2")
    assert res.status == "needs-consent" and not res.mount_ok and "would fix X" in res.report
    assert not any("-y" in a for a in r.calls)                  # nothing escalated automatically
    assert c.full_repair("/dev/sdx2", consent=False).status == "needs-consent"
    assert not any("-y" in a for a in r.calls)
    assert c.full_repair("/dev/sdx2", consent=True).status == "repaired"
    r2 = MockRunner(responses={"e2fsck -f -y": (4, "", "")})
    assert Ext4Checker(r2, mounted_sources=set()).full_repair("/dev/sdx2", True).status == "failed"


def _badblocks_runner(n_blocks, rc=0):
    def bb(argv):
        out = argv[argv.index("-o") + 1]
        with open(out, "w") as f:
            f.write("\n".join(str(1000 + i) for i in range(n_blocks)))
        return rc, "", "io error" if rc else ""
    return MockRunner(responses={"dumpe2fs -h": (0, CLEAN, ""), "badblocks": (bb, None, None)})


def test_badblocks_readonly_and_limits():
    r = _badblocks_runner(3)
    c = Ext4Checker(r, mounted_sources=set())
    lst, blocks = c.scan_bad_blocks("/dev/sdx2", 10_000_000)
    argv = next(a for a in r.calls if a[0] == "badblocks")
    assert "-w" not in argv and "-n" not in argv                # read-only mode only
    assert len(blocks) == 3
    assert c.mark_bad_blocks("/dev/sdx2", lst, blocks, consent=False).status == "needs-consent"
    assert not any("-l" in a for a in r.calls)
    c.mark_bad_blocks("/dev/sdx2", lst, blocks, consent=True)
    assert any(a[:1] == ["e2fsck"] and "-l" in a for a in r.calls)
    os.remove(lst)
    with pytest.raises(FsckError, match="failing"):
        Ext4Checker(_badblocks_runner(MAX_BADBLOCKS + 1), mounted_sources=set()).scan_bad_blocks("/dev/sdx2")
    with pytest.raises(FsckError, match="badblocks failed"):
        Ext4Checker(_badblocks_runner(0, rc=1), mounted_sources=set()).scan_bad_blocks("/dev/sdx2")


def test_verify_rw_exec_real_dir(workdir):
    res = verify_rw_exec(str(workdir), {"options": ["rw", "nosuid"]}, os.getuid())
    assert res["writable"] and res["exec_ok"] and res["owner_ok"] and not res["problems"]
    res = verify_rw_exec(str(workdir), {"options": ["rw", "noexec"]}, os.getuid() + 1)
    assert any("noexec" in p for p in res["problems"]) and any("not by you" in p for p in res["problems"])
    assert not os.listdir(workdir)                              # probe file removed


def _mount_runner(root, fsck_rc=0, header=CLEAN):
    def mkdir(argv):
        assert argv[-1].startswith(root)
        os.makedirs(argv[-1], exist_ok=True)
        return 0, "", ""
    fm = json.dumps({"filesystems": [{"target": root + "/A.I.", "source": "/dev/sdx2", "fstype": "ext4",
                                      "options": "rw,nosuid,nodev,relatime"}]})
    return MockRunner(responses={"mkdir": (mkdir, None, None), "e2fsck -p": (fsck_rc, "", ""),
                                 "e2fsck -f -n": (4, "report", ""), "dumpe2fs": (0, header, ""),
                                 "findmnt": (0, fm, "")})


def test_mount_dry_run_and_collision(workdir):
    root = str(workdir / "USB")
    rep = LinuxMounter(MockRunner(), root).mount("/dev/sdx1", "/dev/sdx2")
    assert not rep.executed and rep.log[0].startswith("DRY RUN") and any("e2fsck -p" in l for l in rep.log)
    os.makedirs(root)
    open(os.path.join(root, "keep.txt"), "w").close()
    r = MockRunner()
    rep = LinuxMounter(r, root).mount("/dev/sdx1", "/dev/sdx2", execute=True)
    assert not rep.ok and "not empty" in rep.log[0] and r.calls == []


def test_mount_sequence_mocked(workdir):
    root = str(workdir / "USB")
    r = _mount_runner(root)
    rep = LinuxMounter(r, root).mount("/dev/sdx1", "/dev/sdx2", execute=True, mounted_sources=set(),
                                      after_fat=lambda d: [f"copied into {d}/AI"])
    assert rep.ok and rep.ext4_mode == "rw", rep.log
    seq = [" ".join(c) for c in r.calls]
    i_fsck = next(i for i, c in enumerate(seq) if c.startswith("e2fsck -p"))
    i_vfat = next(i for i, c in enumerate(seq) if c.startswith("mount -t vfat"))
    i_ext4 = next(i for i, c in enumerate(seq) if c.startswith("mount -t ext4"))
    assert i_fsck < i_vfat < i_ext4
    assert f"/dev/sdx1 {root}" in seq[i_vfat] and f"/dev/sdx2 {root}/A.I." in seq[i_ext4]
    assert "exec" in seq[i_ext4] and "noexec" not in seq[i_ext4]


def test_fsck_unresolved_blocks_rw_mount(workdir):
    root = str(workdir / "USB")
    r = _mount_runner(root, fsck_rc=4)
    rep = LinuxMounter(r, root).mount("/dev/sdx1", "/dev/sdx2", execute=True, mounted_sources=set())
    assert not rep.ok and any("BLOCKED" in l for l in rep.log)
    assert not any(c[0] == "mount" for c in r.calls)


def test_unclean_header_refused_or_ro_fallback(workdir):
    root = str(workdir / "USB")
    r = _mount_runner(root, header=DIRTY)
    rep = LinuxMounter(r, root).mount("/dev/sdx1", "/dev/sdx2", execute=True, mounted_sources=set())
    assert not rep.ok and not any(c[0] == "mount" for c in r.calls)
    r = _mount_runner(root, header=DIRTY)
    rep = LinuxMounter(r, root).mount("/dev/sdx1", "/dev/sdx2", execute=True, mounted_sources=set(),
                                      allow_ro_fallback=True)
    assert rep.ext4_mode == "ro-fallback"
    assert any(c[:3] == ["mount", "-t", "ext4"] and EXT4_RO_OPTS in c for c in r.calls)


def test_unmount_stops_before_poweroff_on_failure(workdir):
    m = LinuxMounter(MockRunner(), str(workdir / "USB"))
    dry = m.unmount(eject_dev="/dev/sdx")
    assert any("power-off" in l for l in dry.log)
    r = MockRunner(responses={"umount": (1, "", "busy")})
    m = LinuxMounter(r, "/proc")       # /proc is a real mount point => umount attempted (mocked)
    rep = m.unmount(execute=True, eject_dev="/dev/sdx")
    assert not rep.ok and not any(c[0] == "udisksctl" for c in r.calls)


def test_handoff_marker_blocks_host_mount():
    s, r = session_with([prepared_lsblk_device()])
    dev = s.select("/dev/sdx")
    os.makedirs(vmhandoff.state_dir(), exist_ok=True)
    with open(vmhandoff.marker_path(dev), "w") as f:
        json.dump({"time": "now", "vm": "TestVM"}, f)
    with pytest.raises(SafetyError, match="handed to a VM"):
        s.mount_and_install(dev, execute=True, mounted_sources=set())
    assert not any(c[0] in ("mount", "e2fsck") for c in r.calls)
