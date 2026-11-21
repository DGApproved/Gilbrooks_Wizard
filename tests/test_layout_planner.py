import copy
import os
import shutil
import subprocess

import pytest

from usbprep import constants as C, workflow
from usbprep.detect import parse_lsblk
from usbprep.layout import check_layout, from_lsblk_device, read_image_layout
from usbprep.planner import MK, PlanError, build_plan, compute_geometry
from usbprep.runner import Runner

from helpers import GiB, prepared_lsblk_device

MiB = C.MiB
IMAGE_TOOLS = ["sgdisk", "sfdisk", "blkid", MK + ".vfat", MK + ".ext4", "mkswap", "dd", "mcopy", "mdir"]
missing = [t for t in IMAGE_TOOLS if not shutil.which(t)]
needs_tools = pytest.mark.skipif(bool(missing), reason=f"image tools missing: {missing}")


@pytest.mark.parametrize("size,ss", [(16 * GiB, 512), (C.MIN_DEVICE_SIZE, 512), (64 * GiB + 12345, 512),
                                     (32 * GiB, 4096)])
def test_geometry_exact(size, ss):
    g = compute_geometry(size, ss)
    assert g.offset_of(g.p1) == MiB and g.bytes_of(g.p1) == C.EFI_SIZE
    assert g.bytes_of(g.p3) == C.SWAP_SIZE
    assert g.p2[0] == g.p1[1] + 1 and g.p3[0] == g.p2[1] + 1          # contiguous, in order
    for p in (g.p1, g.p2, g.p3):
        assert g.offset_of(p) % MiB == 0                                 # 1 MiB aligned
    last_usable_excl = size - (16384 // ss + 1) * ss
    assert 0 <= last_usable_excl - (g.p3[1] + 1) * ss < MiB              # swap is last, at the end
    assert g.bytes_of(g.p2) >= C.MIN_AI_SIZE


def test_min_size_constant():
    assert C.MIN_DEVICE_SIZE == 5365 * MiB


@pytest.mark.parametrize("size", [C.MIN_DEVICE_SIZE - 1, 4 * GiB, 0])
def test_too_small_refused(size):
    with pytest.raises(PlanError, match="too small"):
        compute_geometry(size)


def test_plan_device_steps_and_esp_type():
    p = build_plan("/dev/sdx", 16 * GiB)
    text = p.render()
    assert f"1:{C.GUID_ESP}" in text and f"2:{C.GUID_LINUX_FS}" in text and f"3:{C.GUID_LINUX_SWAP}" in text
    cmds = [s.argv for s in p.steps]
    assert cmds[0] == ["sgdisk", "--zap-all", "/dev/sdx"]
    assert [MK + ".vfat", "-F", "32", "-n", "EFI", "/dev/sdx1"] in cmds
    assert any(c[0] == MK + ".ext4" and "AI" in c and c[-1] == "/dev/sdx2" for c in cmds)
    assert ["mkswap", "-L", "SWAP", "/dev/sdx3"] in cmds
    assert build_plan("/dev/nvme0n1", 16 * GiB).steps[-1].argv[-1] == "/dev/nvme0n1p3"


def test_prepared_device_exact_match():
    assert check_layout(from_lsblk_device(prepared_lsblk_device())).prepared


def _lay():
    return from_lsblk_device(prepared_lsblk_device())


def _mut(fn):
    lay = _lay()
    fn(lay)
    return check_layout(lay)


@pytest.mark.parametrize("name,fn,expect", [
    ("wrong label", lambda l: setattr(l.partitions[1], "label", "DATA"), "label"),
    ("wrong EFI size", lambda l: setattr(l.partitions[0], "size", 4000 * MiB), "4096 MiB"),
    ("wrong swap size", lambda l: setattr(l.partitions[2], "size", 256 * MiB), "243 MiB"),
    ("MBR table", lambda l: setattr(l, "table", "dos"), "expected GPT"),
    ("no table", lambda l: setattr(l, "table", None), "missing"),
    ("bad ESP GUID (basic data)", lambda l: setattr(l.partitions[0], "type_guid",
                                                    "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"), "type GUID"),
    ("wrong fs", lambda l: setattr(l.partitions[1], "fstype", "btrfs"), "filesystem"),
    ("FAT16", lambda l: setattr(l.partitions[0], "fsver", "FAT16"), "FAT32"),
    ("extra partition", lambda l: l.partitions.append(copy.copy(l.partitions[2])), "exactly partitions"),
    ("unaligned", lambda l: setattr(l.partitions[0], "start", MiB + 512), "aligned"),
])
def test_near_misses(name, fn, expect):
    chk = _mut(fn)
    assert not chk.prepared
    assert any(expect in p for p in chk.problems), chk.problems


def test_wrong_order():
    def swap_order(l):
        p1, p2, p3 = l.partitions
        p2.start, p3.start = p3.start, p2.start
    chk = _mut(swap_order)
    assert not chk.prepared and any("order" in p for p in chk.problems)


def test_swap_not_at_end():
    lay = from_lsblk_device(prepared_lsblk_device())
    lay.disk_size += 2 * GiB      # same partitions on a bigger disk => unused tail
    chk = check_layout(lay)
    assert not chk.prepared and any("end of the disk" in p for p in chk.problems)


# ---- real tools on a sparse image file (no loop device, no root) ----
@pytest.fixture(scope="module")
def prepared_image(tmp_path_factory):
    if missing:
        pytest.skip(f"image tools missing: {missing}")
    d = tmp_path_factory.mktemp("img")
    assert str(d).startswith("/home/ubuntu/")
    path = str(d / "usb7777.img")
    workflow.create_image(path, C.MIN_DEVICE_SIZE + 64 * MiB)
    dry = workflow.prepare_image(path, runner=Runner())
    assert not dry.executed and dry.log[0].startswith("DRY RUN")
    assert read_image_layout(path, Runner()).table is None          # dry run wrote nothing
    res = workflow.prepare_image(path, execute=True, confirmation="ERASE 7777", runner=Runner())
    assert res.ok, "\n".join(res.log)
    return path


@needs_tools
def test_image_prepare_real_tools(prepared_image):
    lay = read_image_layout(prepared_image, Runner())
    assert check_layout(lay).prepared
    p1, p2, p3 = sorted(lay.partitions, key=lambda p: p.number)
    assert p1.type_guid == C.GUID_ESP and p1.fstype == "vfat" and p1.fsver == "FAT32" and p1.label == "EFI"
    assert (p2.fstype, p2.label, p3.fstype, p3.label) == ("ext4", "AI", "swap", "SWAP")
    assert os.path.getsize(prepared_image) == C.MIN_DEVICE_SIZE + 64 * MiB


@needs_tools
def test_image_prepared_is_preserved(prepared_image):
    before = read_image_layout(prepared_image, Runner()).fingerprint()
    r = workflow.prepare_image(prepared_image, execute=True, confirmation="ERASE 7777", runner=Runner())
    assert not r.executed and "preserved" in r.log[0]
    assert read_image_layout(prepared_image, Runner()).fingerprint() == before


@needs_tools
def test_image_install_app_mtools(prepared_image):
    log = workflow.install_app_image(prepared_image, runner=Runner())
    assert any("SHA-256 verified" in l for l in log), log
    again = workflow.install_app_image(prepared_image, runner=Runner())
    assert "unchanged" in again[0]
    off = sorted(read_image_layout(prepared_image, Runner()).partitions, key=lambda p: p.number)[0].start
    out = subprocess.run(["mdir", "-b", "-i", f"{prepared_image}@@{off}", "::/AI"], capture_output=True, text=True)
    assert C.MANIFEST_NAME in out.stdout and "/EFI/" not in out.stdout   # app lives in /AI, not /EFI


@needs_tools
def test_image_confirmation_required(workdir):
    path = str(workdir / "blank0001.img")
    workflow.create_image(path, C.MIN_DEVICE_SIZE)
    from usbprep.executor import ConfirmationError
    with pytest.raises(ConfirmationError):
        workflow.prepare_image(path, execute=True, confirmation="ERASE 9999", runner=Runner())
    assert read_image_layout(path, Runner()).table is None
    with pytest.raises(workflow.WorkflowError):
        workflow.create_image(path, C.MIN_DEVICE_SIZE)                  # never overwrite


@needs_tools
def test_image_near_miss_bad_guid_real(prepared_image, workdir):
    copy_path = str(workdir / "copy5555.img")
    subprocess.run(["cp", "--sparse=always", prepared_image, copy_path], check=True)
    subprocess.run(["sgdisk", "-t", "1:EBD0A0A2-B9E5-4433-87C0-68B6B72699C7", copy_path], check=True,
                   capture_output=True)
    chk = check_layout(read_image_layout(copy_path, Runner()))
    assert not chk.prepared and any("type GUID" in p for p in chk.problems)
    r = workflow.prepare_image(copy_path, runner=Runner())           # not preserved => plan offered (dry run)
    assert not r.executed and any("sgdisk" in l for l in r.log)


def test_image_target_refuses_non_regular(workdir):
    from usbprep.safety import SafetyError
    link = workdir / "null.img"
    os.symlink("/dev/null", link)
    with pytest.raises(SafetyError):
        workflow.prepare_image(str(link))
    with pytest.raises(SafetyError):
        workflow.prepare_image(str(workdir))


def test_detected_prepared_device_roundtrip():
    import json
    dev = parse_lsblk(json.dumps({"blockdevices": [prepared_lsblk_device()]}))[0]
    assert dev.status == "eligible"
