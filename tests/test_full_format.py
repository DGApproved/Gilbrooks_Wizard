"""Quick vs full format: plan argv (device, mocked), real image boundaries, reformat job, partial-state reporting."""
import contextlib
import json
import os
import shutil
import subprocess

import pytest

from helpers import blank_lsblk_device, session_with
from usbprep import constants as C, executor, reformat as R, safety, workflow
from usbprep.appcopy import DirBackend
from usbprep.executor import ExecResult
from usbprep.planner import build_plan, compute_geometry
from usbprep.runner import Runner

MiB = C.MiB
SIZE = C.MIN_DEVICE_SIZE + 64 * MiB
TOOLS = ["sgdisk", "mkswap", "dd", "mcopy"]
needs_tools = pytest.mark.skipif(any(not shutil.which(t) for t in TOOLS), reason="image tools missing")


def dd_steps(plan):
    return [s.argv for s in plan.steps if s.argv[0] == "dd" and s.argv[1] == "if=/dev/zero"]


def kv(argv):
    return dict(a.split("=", 1) for a in argv if "=" in a)


def test_quick_plan_has_no_zero_fill():
    for kind in ("device", "image"):
        assert dd_steps(build_plan("/dev/sdx" if kind == "device" else "/home/ubuntu/x.img", SIZE, kind=kind)) == []


def test_device_full_plan_targets_partition_nodes_only_exact_counts():
    g = compute_geometry(SIZE, 512)
    plan = build_plan("/dev/sdx", SIZE, kind="device", full=True)
    dds = dd_steps(plan)
    assert [kv(a)["of"] for a in dds] == ["/dev/sdx1", "/dev/sdx2", "/dev/sdx3"]          # never the whole disk
    assert [int(kv(a)["count"]) for a in dds] == [g.bytes_of(g.p1), g.bytes_of(g.p2), g.bytes_of(g.p3)]
    assert all("iflag=count_bytes" in a and "seek" not in kv(a) for a in dds)
    names = [s.argv[0] for s in plan.steps]
    first_dd, last_dd = names.index("dd"), len(names) - 1 - names[::-1].index("dd")
    assert names.index("sgdisk") < max(i for i, n in enumerate(names) if n == "wipefs") < first_dd
    assert all(not n.startswith(("mkfs", "mkswap")) for n in names[:last_dd])         # zero-fill BEFORE filesystems
    nvme = dd_steps(build_plan("/dev/nvme0n1", SIZE, kind="device", full=True))
    assert [kv(a)["of"] for a in nvme] == ["/dev/nvme0n1p1", "/dev/nvme0n1p2", "/dev/nvme0n1p3"]


def test_image_full_plan_inside_partition_boundaries():
    g = compute_geometry(SIZE, 512)
    dds = dd_steps(build_plan("/home/ubuntu/x.img", SIZE, kind="image", full=True))
    first_usable, last_usable_end = 34 * 512, SIZE - 33 * 512       # primary / backup GPT areas
    for a, p in zip(dds, (g.p1, g.p2, g.p3)):
        seek, count = int(kv(a)["seek"]), int(kv(a)["count"])
        assert (seek, count) == (g.offset_of(p), g.bytes_of(p))
        assert first_usable <= seek and seek + count <= last_usable_end
        assert "conv=notrunc,fsync" in a and "oflag=seek_bytes" in a


@pytest.fixture
def fake_nodes(monkeypatch):
    monkeypatch.setattr(safety, "check_device_target", lambda p: None)
    monkeypatch.setattr(executor, "check_device_target", lambda p: None)


def test_mocked_device_full_prepare_argv(fake_nodes):
    holder = [blank_lsblk_device("sdx", size=SIZE)]
    s, r = session_with(holder)
    dev = s.select("/dev/sdx")
    res = s.prepare(dev, execute=True, confirmation=dev.confirm_token(), full=True)
    dds = [c for c in r.calls if c and c[0] == "dd"]
    assert [kv(c)["of"] for c in dds] == ["/dev/sdx1", "/dev/sdx2", "/dev/sdx3"], res.log
    s2, r2 = session_with([blank_lsblk_device("sdx", size=SIZE)])
    d2 = s2.select("/dev/sdx")
    s2.prepare(d2, execute=True, confirmation=d2.confirm_token(), full=False)
    assert not [c for c in r2.calls if c and c[0] == "dd"]                           # quick: no zero-fill


def test_partial_state_reported_on_failure():
    res = workflow._mark_partial(ExecResult(True, ["step failed"], ok=False), True)
    assert "PARTIALLY formatted" in res.log[-1] and "nothing was restored or mounted" in res.log[-1]
    assert workflow._mark_partial(ExecResult(True, [], ok=False), False).log == []
    assert workflow._mark_partial(ExecResult(True, [], ok=True), True).log == []


def test_failed_full_device_prepare_reports_partial(fake_nodes):
    holder = [blank_lsblk_device("sdx", size=SIZE)]
    s, r = session_with(holder, {"dd if=/dev/zero of=/dev/sdx2": (1, "", "No space left / I/O error")})
    dev = s.select("/dev/sdx")
    res = s.prepare(dev, execute=True, confirmation=dev.confirm_token(), full=True)
    assert not res.ok and any("PARTIALLY formatted" in l for l in res.log)
    assert not [c for c in r.calls if c[0].startswith("mkfs") or c[0] == "mount"]     # stopped; no mount


def test_reformat_job_carries_full_format(workdir, fake_nodes):
    from test_appcopy_reformat import _ai, _job, _reformat_session
    job_path, st = _job(workdir, _ai(workdir))
    job = json.load(open(job_path))
    assert job["full_format"] is False
    job["full_format"] = True
    s, r = _reformat_session(workdir)
    fresh = workdir / "fresh_fat"
    fresh.mkdir()
    ok = R.run_job(job, s, log=lambda m: None, restore_backend=lambda p: contextlib.nullcontext(DirBackend(str(fresh))),
                   busy_scanner=lambda mps: [], mounted_sources=set())
    assert ok
    assert [kv(c)["of"] for c in r.calls if c[0] == "dd"] == ["/dev/sdx1", "/dev/sdx2", "/dev/sdx3"]


def test_cli_dry_run_states_mode(capsys, monkeypatch):
    from usbprep import cli
    holder = [blank_lsblk_device("sdx", size=SIZE)]
    s, r = session_with(holder)
    monkeypatch.setattr(cli, "_session", lambda: s)
    cli.main(["prepare", "/dev/sdx", "--full-format"])
    out = capsys.readouterr().out
    assert "Format mode: FULL" in out and "NOT guaranteed" in out and "Not a secure erase" in out
    assert "FULL FORMAT: zero-fill" in out
    cli.main(["prepare", "/dev/sdx"])
    out = capsys.readouterr().out
    assert "Format mode: quick" in out and "zero-fill" not in out
    assert not [c for c in r.calls if c[0] in ("dd", "sgdisk", "wipefs")]           # dry run: no writes


# ---- real tools on sparse image files ----
MARK = b"\xaa" * MiB


def _markers(g):
    """(offset, kind) samples: deep inside each partition + the unpartitioned gap before p1.
    kind 'ext4': the ext4 creator discards (hole-punches) its range on a regular file even in quick mode,
    so quick-mode content there is not asserted (on USB sticks without TRIM support it usually stays)."""
    return [(g.offset_of(g.p1) + 2048 * MiB, "in"), (g.offset_of(g.p2) + g.bytes_of(g.p2) // 2, "ext4"),
            (g.offset_of(g.p2) + g.bytes_of(g.p2) - MiB, "ext4"), (g.offset_of(g.p3) + g.bytes_of(g.p3) - MiB, "in"),
            (64 * 512, "out")]       # gap marker: sectors 64..2047 only (p1 starts at 1 MiB)


def _len(kind):
    return MiB - 64 * 512 if kind == "out" else MiB


@needs_tools
@pytest.mark.parametrize("full", [False, True], ids=["quick", "full"])
def test_image_quick_vs_full(full, workdir):
    g = compute_geometry(SIZE, 512)
    img = str(workdir / "ff7777.img")
    workflow.create_image(img, SIZE)
    with open(img, "r+b") as f:
        for off, kind in _markers(g):
            f.seek(off)
            f.write(b"\xaa" * _len(kind))
    res = workflow.prepare_image(img, execute=True, confirmation="ERASE 7777", runner=Runner(), full=full)
    assert res.ok, "\n".join(res.log)
    with open(img, "rb") as f:
        for off, kind in _markers(g):
            f.seek(off)
            blk = f.read(_len(kind))
            if kind == "out":
                assert blk == b"\xaa" * _len(kind), "bytes outside partitions must never be written"
            elif full:
                assert blk == b"\0" * MiB, f"full format left old data at {off}"
            elif kind == "in":
                assert blk == MARK, f"quick format unexpectedly overwrote {off}"
    v = subprocess.run(["sgdisk", "-v", img], capture_output=True, text=True)
    assert "No problems found" in v.stdout                    # primary + backup GPT intact
    assert os.path.getsize(img) == SIZE
    os.remove(img)
