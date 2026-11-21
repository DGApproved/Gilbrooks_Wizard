import os
import stat

import pytest

from usbprep import executor, safety
from usbprep.executor import ConfirmationError, LinuxExecutor, MockExecutor, privilege_prefix
from usbprep.planner import build_plan
from usbprep.runner import MockRunner
from usbprep.safety import SafetyError, check_device_target, revalidate
from usbprep.workflow import WorkflowError

from helpers import GiB, blank_lsblk_device, clone, prepared_lsblk_device, session_with


@pytest.fixture
def fake_nodes(monkeypatch):
    """Pretend /dev/sdx exists as a block node (only ever used with MockRunner: nothing real runs)."""
    monkeypatch.setattr(safety, "check_device_target", lambda p: None)
    monkeypatch.setattr(executor, "check_device_target", lambda p: None)


def test_real_block_device_refused_in_test_mode():
    blk = next((os.path.join("/dev", n) for n in sorted(os.listdir("/dev"))
                if stat.S_ISBLK(os.stat(os.path.join("/dev", n)).st_mode)), None) if os.path.isdir("/dev") else None
    if blk is None:
        pytest.skip("no block device node visible in this container to prove the refusal on")
    with pytest.raises(SafetyError, match="refused in test/safe mode"):
        check_device_target(blk)


def test_missing_device_and_char_device():
    with pytest.raises(SafetyError, match="not present"):
        check_device_target("/dev/sdzz-not-here")
    with pytest.raises(SafetyError, match="not a block device"):
        check_device_target("/dev/null")


def test_executor_dry_run_is_default():
    r = MockRunner()
    res = LinuxExecutor(r).run_plan(build_plan("/dev/sdx", 16 * GiB))
    assert not res.executed and res.log[0].startswith("DRY RUN") and r.calls == []


def test_executor_requires_exact_confirmation():
    r = MockRunner()
    with pytest.raises(ConfirmationError):
        LinuxExecutor(r).run_plan(build_plan("/dev/sdx", 16 * GiB), execute=True, confirmation="erase 2345",
                                  expected_token="ERASE 2345")
    assert r.calls == []


def test_executor_device_needs_guard(fake_nodes):
    with pytest.raises(SafetyError, match="guard"):
        LinuxExecutor(MockRunner()).run_plan(build_plan("/dev/sdx", 16 * GiB), execute=True,
                                             confirmation="T", expected_token="T")


def test_mock_executor_abort_and_failure():
    plan = build_plan("/dev/sdx", 16 * GiB)
    m = MockExecutor(fail_at=2)
    res = m.run_plan(plan)
    assert not res.ok and len(m.ran) == 1

    def guard(i):
        if i == 3:
            raise SafetyError("unplugged")
    res = MockExecutor().run_plan(plan, guard=guard)
    assert not res.ok and "ABORT before step 3" in res.log[-1]


def test_privilege_prefix():
    if os.geteuid() == 0:
        assert privilege_prefix("sudo") == []
    else:
        assert privilege_prefix("sudo") == ["sudo"] and privilege_prefix("pkexec") == ["pkexec"]
    assert privilege_prefix(None) == []
    if os.geteuid() != 0:
        with pytest.raises(ValueError):
            privilege_prefix("doas")


# ---- session-level flows (mocked lsblk; MockRunner never runs anything) ----
def test_prepared_drive_preserved():
    s, r = session_with([prepared_lsblk_device()])
    res = s.prepare(s.select("/dev/sdx"), execute=True, confirmation="ERASE 2345", mounted_sources=set())
    assert not res.executed and "preserved" in res.log[0]
    assert not any(c[0] in ("sgdisk", "wipefs") for c in r.calls)


def test_blank_dry_run_and_wrong_token(fake_nodes):
    s, r = session_with([blank_lsblk_device()])
    dev = s.select("/dev/sdx")
    res = s.prepare(dev, mounted_sources=set())
    assert not res.executed and any("sgdisk" in l for l in res.log)
    assert not any(c[0] == "sgdisk" for c in r.calls)
    with pytest.raises(ConfirmationError):
        s.prepare(dev, execute=True, confirmation="ERASE 0000", mounted_sources=set())
    assert dev.confirm_token() == "ERASE 2345"


def _becomes_prepared(holder):
    def fn(argv):
        holder[0] = prepared_lsblk_device()
        return 0, "", ""
    return fn


def test_full_mock_prepare_succeeds(fake_nodes):
    holder = [blank_lsblk_device()]
    s, r = session_with(holder, {"mkswap": (_becomes_prepared(holder), None, None)})
    res = s.prepare(s.select("/dev/sdx"), execute=True, confirmation="ERASE 2345", mounted_sources=set())
    assert res.ok, res.log
    assert "post-check: PREPARED" in res.log[-1]
    ran = [c[0] for c in r.calls if c[0] != "lsblk"]
    assert ran.index("sgdisk") < ran.index("mkswap")


@pytest.mark.parametrize("change", ["serial", "unplug", "mount", "ineligible"])
def test_toctou_abort_mid_plan(fake_nodes, change):
    holder = [blank_lsblk_device()]

    def mutate(argv):
        if change == "serial":
            holder[0]["serial"] = "OTHER999"
        elif change == "unplug":
            holder.clear()
        elif change == "mount":
            holder[0]["mountpoints"] = ["/media/user/x"]
        else:
            holder[0]["rota"] = True
        return 0, "", ""
    s, r = session_with(holder, {"sgdisk --zap-all": (mutate, None, None)})
    res = s.prepare(s.select("/dev/sdx"), execute=True, confirmation="ERASE 2345", mounted_sources=set())
    assert not res.ok and "ABORTED at step 2" in "\n".join(res.log)
    assert sum(1 for c in r.calls if c[0] == "sgdisk") == 1                # nothing after the change


def test_toctou_layout_changed_before_first_write(fake_nodes):
    holder = [blank_lsblk_device()]
    s, _ = session_with(holder)
    dev = s.select("/dev/sdx")
    reviewed = s.inspect(dev)[0].fingerprint()
    holder[0] = prepared_lsblk_device()          # someone partitioned it meanwhile (same identity)
    with pytest.raises(SafetyError, match="layout changed"):
        revalidate(dev, reviewed, runner=s.runner, lister=s.lister, mounted_sources=set())


def test_busy_device_refused_via_proc_mounts(fake_nodes):
    s, r = session_with([blank_lsblk_device()])
    with pytest.raises(SafetyError, match="in use"):
        s.prepare(s.select("/dev/sdx"), execute=True, confirmation="ERASE 2345", mounted_sources={"/dev/sdx"})
    assert not any(c[0] == "sgdisk" for c in r.calls)


def test_source_drive_protected_and_preselected(fake_nodes):
    other = blank_lsblk_device("sdy", serial="OTHER0001")
    s, _ = session_with([blank_lsblk_device(), other], source="/dev/sdx")
    cands = s.candidates()
    assert s.default_selection(cands) == "/dev/sdx"
    with pytest.raises(SafetyError, match="running from that drive"):
        s.prepare(s.select("/dev/sdx"), mounted_sources=set())
    with pytest.raises(SafetyError, match="running from that drive"):
        s.duplicate_to(s.select("/dev/sdx"), mounted_sources=set())
    log = s.duplicate_to(s.select("/dev/sdy"), mounted_sources=set())       # dry run on another drive OK
    assert any("DRY RUN" in l for l in log)


def test_no_autoselect_without_source():
    s, _ = session_with([blank_lsblk_device(), blank_lsblk_device("sdy", serial="Y1")])
    assert s.default_selection(s.candidates()) is None


def test_select_ambiguous_needs_override():
    d = blank_lsblk_device()
    d["tran"] = "sata"
    d["subsystems"] = "block:scsi:pci"
    s, _ = session_with([d])
    dev_status = s.lister()[0].status
    if dev_status == "ambiguous":
        with pytest.raises(WorkflowError, match="override"):
            s.select("/dev/sdx")
        assert s.select("/dev/sdx", allow_ambiguous=True)
    else:
        with pytest.raises(WorkflowError):
            s.select("/dev/sdx", allow_ambiguous=True)
    with pytest.raises(WorkflowError, match="not found"):
        s.select("/dev/sdq")


def test_too_small_device_not_selectable():
    d = blank_lsblk_device(); d["size"] = 4 * GiB
    s, _ = session_with([d])
    with pytest.raises(WorkflowError):
        s.select("/dev/sdx", allow_ambiguous=True)


def test_clone_helper_independent():
    a = prepared_lsblk_device()
    b = clone(a)
    b["children"][0]["label"] = "X"
    assert a["children"][0]["label"] == "EFI"
