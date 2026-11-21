"""Star = user's explicit 'dedicated USB drive' mark (overrides flash detection only, never system/internal refusals)."""
import io
import json
import os
import plistlib
import stat
from contextlib import redirect_stdout, redirect_stderr

import pytest

from usbprep import cli, stars
from usbprep.detect import parse_lsblk, selectable, usb_candidates
from usbprep.platforms import parse_macos_diskinfo, parse_windows_disks
from usbprep.safety import SafetyError, revalidate
from tests.helpers import GiB, blank_lsblk_device, prepared_lsblk_device, session_with


def usb_hdd(name="sdh", serial="HDD0001", prepared=False):
    d = (prepared_lsblk_device if prepared else blank_lsblk_device)(name, 1000 * GiB, serial, "Elements 25A2")
    d.update(rota=True, rm=False, vendor="WD")
    return d


def lsblk(*devs):
    return json.dumps({"blockdevices": list(devs)})


def system_usb():
    d = prepared_lsblk_device("sdr", 32 * GiB, "ROOT1", "Cruzer", mounts=("/boot/efi", "/", None))
    return d


def internal(name, tran):
    d = blank_lsblk_device(name, 64 * GiB, "INT" + name, "Disk " + name)
    d.update(tran=tran, subsystems="block:" + (tran or "virtio"))
    return d


def run_cli(argv, monkeypatch, holder=None):
    if holder is not None:
        monkeypatch.setattr(cli, "_session", lambda: session_with(holder)[0])
    monkeypatch.setattr(cli, "current_os", lambda: "linux")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_star_unstar_roundtrip_persisted_0600_atomic(tmp_path):
    dev = parse_lsblk(lsblk(usb_hdd()))[0]
    assert dev.status == "excluded" and dev.detected_status == "excluded" and dev.starred == ""
    path = stars.star(dev)
    assert path == str(tmp_path / "config" / "stars.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [f for f in os.listdir(tmp_path / "config") if f.startswith(".stars-")] == []   # no temp leftovers
    saved = json.load(open(path))["stars"]
    assert saved[0]["key"] == "HDD0001|Elements 25A2|WD|%d|" % (1000 * GiB) and "/dev/" not in saved[0]["key"]
    again = parse_lsblk(lsblk(usb_hdd()))[0]
    assert again.starred == "saved" and again.status == "eligible"
    assert again.reasons[0].startswith("manually starred") and any("rotational" in r for r in again.reasons)
    assert stars.star(dev) and len(stars.load()) == 1      # idempotent
    assert stars.unstar("/dev/sdh", [again])[0]["serial"] == "HDD0001"
    assert stars.load() == [] and parse_lsblk(lsblk(usb_hdd()))[0].status == "excluded"


@pytest.fixture
def node_present(monkeypatch):
    from usbprep import safety
    monkeypatch.setattr(safety, "check_device_target", lambda path: None)


def test_starred_usb_hdd_becomes_eligible_everywhere(node_present):
    holder = [usb_hdd(prepared=True)]
    s, _ = session_with(holder)
    d = s.candidates()[0]
    assert d.path == "/dev/sdh" and d.to_dict()["detected_dedicated"] is False and d.to_dict()["star_allowed"]
    with pytest.raises(Exception):
        s.select("/dev/sdh")
    stars.star(d)
    sel = s.select("/dev/sdh")                       # Mount / Initialize / Clone / Data all go through select()
    assert sel.status == "eligible" and selectable(sel) is None
    revalidate(sel, None, False, s.runner, s.lister, [])   # identity still matches -> accepted
    assert s.inspect(sel)[1].prepared                # 'prepared' detection works on the starred drive
    stars.unstar("/dev/sdh", [sel])                  # star removed after selection -> action refused
    with pytest.raises(SafetyError, match="no longer eligible"):
        revalidate(sel, None, False, s.runner, s.lister, [])


def test_system_disk_never_starred_or_listed(monkeypatch):
    devs = parse_lsblk(lsblk(system_usb(), usb_hdd()))
    sysd = devs[0]
    assert stars.star_refusal(sysd) and "running system" in stars.star_refusal(sysd)
    with pytest.raises(stars.StarError):
        stars.star(sysd)
    # even a hand-edited stars.json entry for the system disk changes nothing
    stars.save([{"key": stars.identity_key(sysd)}])
    again = parse_lsblk(lsblk(system_usb()))[0]
    assert again.status == "excluded" and again.starred == ""
    assert [d.path for d in usb_candidates(devs)] == ["/dev/sdh"]
    rc, _, err = run_cli(["star", "/dev/sdr", "--yes"], monkeypatch, [system_usb()])
    assert rc == 2 and "STAR REFUSED" in err


@pytest.mark.parametrize("name,tran", [("nvme0n1", "nvme"), ("sda", "sata"), ("vda", None)])
def test_internal_disks_refused(name, tran):
    d = parse_lsblk(lsblk(internal(name, tran)))[0]
    assert d.hard_reasons and "not a USB device" in stars.star_refusal(d)
    with pytest.raises(stars.StarError):
        stars.star(d)
    assert usb_candidates([d]) == [] and d.to_dict()["star_allowed"] is False


def test_no_serial_is_session_only(tmp_path, monkeypatch):
    raw = usb_hdd(serial="")
    d = parse_lsblk(lsblk(raw))[0]
    assert stars.identity_key(d) is None
    with pytest.raises(stars.StarError, match="NOT saved"):
        stars.star(d)
    assert not os.path.exists(tmp_path / "config" / "stars.json")
    rc, out, err = run_cli(["star", "/dev/sdh", "--yes"], monkeypatch, [raw])
    assert rc == 3 and "session-only" in err
    rc, out, err = run_cli(["star", "/dev/sdh", "--session"], monkeypatch, [raw])
    assert rc == 0 and not os.path.exists(tmp_path / "config" / "stars.json")
    monkeypatch.setenv(stars.ENV_SESSION, out.strip().splitlines()[0])
    d2 = parse_lsblk(lsblk(raw))[0]
    assert d2.starred == "session" and d2.status == "eligible" and "session only" in d2.reasons[0]


def test_same_dev_path_different_serial_does_not_inherit(node_present):
    holder = [usb_hdd(serial="HDD0001")]
    s, _ = session_with(holder)
    starred = s.lister()[0]
    stars.star(starred)
    sel = s.select("/dev/sdh")
    holder[0] = usb_hdd(serial="OTHER999")           # different drive on the same /dev node
    fresh = s.lister()[0]
    assert fresh.starred == "" and fresh.status == "excluded"
    with pytest.raises(SafetyError, match="(?i)identity|changed|different"):
        revalidate(sel, None, False, s.runner, s.lister, [])


def test_corrupt_config_treated_as_empty(tmp_path):
    p = tmp_path / "config"
    p.mkdir()
    for junk in ("{not json", "[1,2]", '{"stars": "x"}', '{"stars": [{"nokey": 1}, 7]}'):
        (p / "stars.json").write_text(junk)
        assert stars.load() == []
        assert parse_lsblk(lsblk(usb_hdd()))[0].status == "excluded"
    stars.star(parse_lsblk(lsblk(usb_hdd()))[0])     # rewritten cleanly
    assert len(stars.load()) == 1


def test_cli_star_unstar_stars_and_list(monkeypatch):
    holder = [usb_hdd(prepared=True), blank_lsblk_device("sdf", 16 * GiB, "FLASH1")]
    rc, out, _ = run_cli(["list"], monkeypatch, holder)
    assert "(not detected as dedicated USB)" in out and "/dev/sdf" in out
    rc, _, err = run_cli(["star", "/dev/sdh"], monkeypatch, holder)      # no TTY, no --yes -> not starred
    assert rc == 1 and stars.load() == []
    rc, out, _ = run_cli(["star", "/dev/sdh", "--yes"], monkeypatch, holder)
    assert rc == 0 and "starred /dev/sdh" in out
    rc, out, _ = run_cli(["list"], monkeypatch, holder)
    line = [x for x in out.splitlines() if "/dev/sdh" in x][0]
    assert line.startswith("[ELIGIBLE") and "★" in line and "not detected" not in line
    rc, out, _ = run_cli(["stars", "--json"], monkeypatch, holder)
    assert json.loads(out)[0]["serial"] == "HDD0001"
    rc, out, _ = run_cli(["gui-state"], monkeypatch, holder)
    st = {d["path"]: d for d in json.loads(out)["devices"]}
    assert st["/dev/sdh"]["starred"] == "saved" and st["/dev/sdh"]["layout"] == "prepared"
    assert st["/dev/sdf"]["layout"] == "blank" and st["/dev/sdf"]["detected_dedicated"] is True
    rc, out, _ = run_cli(["unstar", "HDD0001"], monkeypatch, holder)
    assert rc == 0 and stars.load() == []
    rc, out, _ = run_cli(["unstar", "/dev/sdh"], monkeypatch, holder)
    assert rc == 1


def test_windows_and_macos_parsers_same_logic():
    disks = [{"Number": 1, "FriendlyName": "WD Elements", "SerialNumber": "W1", "Size": 500 * GiB, "BusType": "USB",
              "MediaType": "HDD"},
             {"Number": 0, "FriendlyName": "Boot SSD", "SerialNumber": "S0", "Size": 500 * GiB, "BusType": "USB",
              "IsBoot": True, "MediaType": "SSD"},
             {"Number": 2, "FriendlyName": "Samsung", "SerialNumber": "N2", "Size": 500 * GiB, "BusType": "NVMe"}]
    w = parse_windows_disks(json.dumps(disks))
    assert w[0].status == "excluded" and stars.star_refusal(w[0]) is None
    assert stars.star_refusal(w[1]) and stars.star_refusal(w[2])
    assert [d.path for d in usb_candidates(w)] == [w[0].path]
    stars.star(w[0])
    w = parse_windows_disks(json.dumps(disks))
    assert w[0].status == "eligible" and w[0].starred == "saved" and w[1].status == "excluded"
    mac = plistlib.dumps({"DeviceIdentifier": "disk4", "MediaName": "Portable HDD", "IORegistryEntryName": "M4",
                          "TotalSize": 500 * GiB, "BusProtocol": "USB", "SolidState": False})
    m = parse_macos_diskinfo(mac)
    assert m.status == "excluded" and stars.star_refusal(m) is None
    stars.star(m)
    assert parse_macos_diskinfo(mac).status == "eligible"
    internal_mac = plistlib.dumps({"DeviceIdentifier": "disk0", "MediaName": "APPLE SSD", "IORegistryEntryName": "A",
                                   "TotalSize": 500 * GiB, "BusProtocol": "Apple Fabric", "Internal": True})
    assert stars.star_refusal(parse_macos_diskinfo(internal_mac))
