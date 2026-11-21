"""Clone menu: ext4-only USB -> USB copy. Mocked device commands + a real rsync copy/verify between folders."""
import json
import os
import subprocess

import pytest

from helpers import blank_lsblk_device, prepared_lsblk_device, session_with
from usbprep import clone as CL
from usbprep.clone import CloneError, CloneJob
from usbprep.safety import SafetyError

SRC, DST = "sdb", "sdc"


def two(dst_mounts=(None, None, None), dst=None, src_serial="SRC00001"):
    holder = [prepared_lsblk_device(SRC, serial=src_serial, model="SrcDisk"),
              dst or prepared_lsblk_device(DST, serial="DST00002", model="FriendDisk", mounts=dst_mounts)]
    return holder


def mocked(holder, used=1000, size=10 ** 9, diff="", e2fsck_rc=0, source=None):
    def findmnt(argv):
        mnt = argv[-1]
        mounts = [c[1:] if c[0] == "sudo" else c for c in r.calls]
        mounts = [c for c in mounts if c[0] == "mount"]
        part = next(c[-2] for c in mounts if c[-1] == mnt)
        return 0, json.dumps({"filesystems": [{"source": part, "fstype": "ext4"}]}), ""
    extra = {"findmnt": (findmnt, None, None),
             "df -B1 --output=used": (0, f"Used\n{used}\n", ""),
             "df -B1 --output=size": (0, f"Size\n{size}\n", ""),
             "e2fsck -n": (e2fsck_rc, "", ""),
             "rsync -aHAXn": (0, diff, "")}
    s, r = session_with(holder, extra, source=source)
    return s, r


def job(s, holder, mounted=frozenset()):
    return CloneJob(s, s.select("/dev/" + SRC), s.select("/dev/" + DST), mounted_sources=set(mounted))


def assert_no_writes(r):
    for c in r.calls:
        assert c[0] not in ("mount", "rsync", "dd", "sgdisk", "wipefs", "tune2fs", "resize2fs", "mkswap"), c
        assert not c[0].startswith("mkfs"), c


def test_dry_run_writes_nothing_and_states_replacement():
    h = two()
    s, r = mocked(h)
    out = "\n".join(job(s, h).run(execute=False))
    assert "DRY RUN" in out and "REPLACED" in out and "REPLACE 0002" in out and "UUID" in out
    assert_no_writes(r)


@pytest.mark.parametrize("case", ["same", "serial", "dest_is_source", "mounted_lsblk", "mounted_proc", "unprepared", "vm"])
def test_refusals_before_any_write(case, tmp_path, monkeypatch):
    h = two()
    src_dev = "/dev/" + DST if case == "dest_is_source" else None
    if case == "serial":
        h[1]["serial"], h[1]["model"] = h[0]["serial"], h[0]["model"]
    if case == "mounted_lsblk":
        h = two(dst_mounts=(None, "/USB/A.I.", None))
    if case == "unprepared":
        h[1] = blank_lsblk_device(DST, serial="DST00002")
    s, r = mocked(h, source=src_dev)
    a, b = s.select("/dev/" + SRC), s.select("/dev/" + (SRC if case == "same" else DST))
    mounted = {"/dev/sdc2"} if case == "mounted_proc" else set()
    if case == "vm":
        from usbprep import vmhandoff
        os.makedirs(os.path.dirname(vmhandoff.marker_path(b)), exist_ok=True)
        with open(vmhandoff.marker_path(b), "w") as f:
            json.dump({"time": "now", "vm": "x"}, f)
    from usbprep.workflow import WorkflowError
    with pytest.raises((CloneError, SafetyError, WorkflowError)) as e:
        CloneJob(s, a, b, mounted_sources=mounted).run(execute=True, confirmation=CL.token_for(b))
    print(case, e.value)
    assert_no_writes(r)


def test_wrong_or_missing_token_refused():
    h = two()
    s, r = mocked(h)
    for tok in (None, "", "REPLACE 0001", "ERASE 0002"):
        with pytest.raises(CloneError):
            job(s, h).run(execute=True, confirmation=tok)
    assert_no_writes(r)


def test_execute_argv_source_read_only_and_no_fat_or_swap():
    h = two()
    s, r = mocked(h)
    log = "\n".join(job(s, h).run(execute=True, confirmation="REPLACE 0002", privilege="sudo"))
    calls = [c[1:] if c[0] == "sudo" else c for c in r.calls]
    mounts = [c for c in calls if c[0] == "mount"]
    assert mounts[0][:4] == ["mount", "-t", "ext4", "-o"] and mounts[0][4].startswith("ro,noload") and mounts[0][5] == "/dev/sdb2"
    assert mounts[1][5] == "/dev/sdc2" and "ro" not in mounts[1][4].split(",")
    rs = [c for c in calls if c[0] == "rsync"]
    assert rs[0][1:5] == ["-aHAX", "--numeric-ids", "--delete", "--one-file-system"]
    assert rs[1][1] == "-aHAXn" and "--checksum" in rs[1]
    assert rs[0][-2].startswith(mounts[0][-1]) and rs[0][-1].startswith(mounts[1][-1])   # direction src -> dst
    flat = " ".join(" ".join(c) for c in calls)
    for bad in ("sdb1", "sdc1", "sdb3", "sdc3", "dd ", "tune2fs", "mkfs", "sgdisk", "wipefs", "resize2fs"):
        assert bad not in flat
    assert [c[0] for c in calls].count("umount") == 2
    order = [c[0] for c in calls if c[0] in ("e2fsck", "mount", "rsync", "umount")]
    assert order == ["e2fsck", "e2fsck", "mount", "mount", "rsync", "rsync", "umount", "umount"]
    assert "0 differences" in log


def test_capacity_abort_before_copy():
    h = two()
    s, r = mocked(h, used=10 ** 9, size=10 ** 9)
    with pytest.raises(CloneError, match="too small"):
        job(s, h).run(execute=True, confirmation="REPLACE 0002")
    assert not [c for c in r.calls if c[0] == "rsync"]
    assert [c[0] for c in r.calls].count("umount") == 2           # cleaned up


def test_dirty_fs_aborts_before_mount():
    h = two()
    s, r = mocked(h, e2fsck_rc=4)
    with pytest.raises(CloneError, match="not clean"):
        job(s, h).run(execute=True, confirmation="REPLACE 0002")
    assert_no_writes(r)


def test_verify_differences_is_error():
    h = two()
    s, r = mocked(h, diff=">fcsT...... a.txt\n")
    with pytest.raises(CloneError, match="differences"):
        job(s, h).run(execute=True, confirmation="REPLACE 0002")
    assert [c[0] for c in r.calls].count("umount") == 2


def test_device_change_aborts():
    h = two()
    s, r = mocked(h)
    j = job(s, h)
    h[1] = prepared_lsblk_device(DST, serial="OTHER999", model="FriendDisk")   # replugged a different disk
    with pytest.raises(SafetyError):
        j.run(execute=True, confirmation="REPLACE 0002")
    assert_no_writes(r)


def _tree(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in sorted(dn + fn):
            p = os.path.join(dp, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            out[rel] = (st.st_mode, st.st_nlink, os.readlink(p) if os.path.islink(p) else
                        (open(p, "rb").read() if os.path.isfile(p) else None))
    return out


def test_real_rsync_copy_and_verify(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "proj" / "sub").mkdir(parents=True)
    dst.mkdir()
    (dst / "old_file_on_friend_drive").write_text("will be replaced")
    (src / "proj" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(src / "proj" / "run.sh", 0o750)
    (src / "proj" / "sub" / "data.bin").write_bytes(os.urandom(4096))
    os.link(src / "proj" / "sub" / "data.bin", src / "proj" / "hard.bin")
    os.symlink("sub/data.bin", src / "proj" / "link")
    (src / "proj" / "\u00fcnicode name.txt").write_text("x")
    try:
        os.setxattr(src / "proj" / "run.sh", "user.usbprep", b"1")
        xattr = True
    except OSError:
        xattr = False
    before = _tree(src)
    subprocess.run(CL.rsync_copy_argv(str(src), str(dst)), check=True)
    v = subprocess.run(CL.rsync_verify_argv(str(src), str(dst)), check=True, capture_output=True, text=True)
    assert CL.differences(v.stdout) == []
    assert _tree(src) == before                                     # source untouched
    assert _tree(dst) == before                                     # identical incl. modes, symlink, hardlink count
    assert not (dst / "old_file_on_friend_drive").exists()
    assert os.stat(dst / "proj" / "hard.bin").st_ino == os.stat(dst / "proj" / "sub" / "data.bin").st_ino
    if xattr:
        assert os.getxattr(dst / "proj" / "run.sh", "user.usbprep") == b"1"
    (dst / "proj" / "run.sh").write_text("changed")
    v = subprocess.run(CL.rsync_verify_argv(str(src), str(dst)), check=True, capture_output=True, text=True)
    assert CL.differences(v.stdout)                                  # verification detects a mismatch
