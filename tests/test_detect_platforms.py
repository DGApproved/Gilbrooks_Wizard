import json
import plistlib

import pytest

from usbprep import detect
from usbprep.platforms import CAPABILITIES, UnsupportedOperation, fail_closed, matrix_markdown, \
    parse_macos_diskinfo, parse_windows_disks


@pytest.fixture
def devs(fixture_text):
    return {d.name: d for d in detect.parse_lsblk(fixture_text("lsblk_mixed.json"))}


@pytest.mark.parametrize("name,status", [
    ("nvme0n1", "excluded"),  # internal + system
    ("sda", "eligible"),      # USB flash drive
    ("sdb", "excluded"),      # USB HDD (rotational)
    ("sdc", "excluded"),      # USB SSD by model
    ("sdd", "excluded"),      # card reader without media
    ("sde", "ambiguous"),     # card reader with media
    ("sdf", "ambiguous"),     # SATA bridge
    ("sdg", "excluded"),      # too small
    ("sdh", "eligible"),      # flash, mounted by desktop (formatting still refused by safety layer)
    ("sdi", "excluded"),      # USB stick hosting /
    ("sdj", "ambiguous"),     # unknown rotational + no serial
])
def test_classification(devs, name, status):
    assert devs[name].status == status, devs[name].reasons


def test_non_disks_ignored(devs):
    assert "loop0" not in devs and "sr0" not in devs


def test_reasons_present_for_excluded(devs):
    assert any("rotational" in r for r in devs["sdb"].reasons)
    assert any("too small" in r for r in devs["sdg"].reasons)
    assert any("no medium" in r for r in devs["sdd"].reasons)


def test_selectable_requires_override_for_ambiguous(devs):
    assert detect.selectable(devs["sda"]) is None
    assert "override" in detect.selectable(devs["sde"])
    assert detect.selectable(devs["sde"], allow_ambiguous=True) is None
    assert detect.selectable(devs["sdb"], allow_ambiguous=True).startswith("not eligible")


def test_candidates_are_usb_only_and_no_autopick(devs):
    c = detect.usb_candidates(list(devs.values()))
    assert "nvme0n1" not in [d.name for d in c]
    assert len([d for d in c if d.status == "eligible"]) == 2  # multiple listed; user must pick


def test_identity_and_token(devs):
    d = devs["sda"]
    assert d.identity()["serial"] == "4C530001"
    assert d.confirm_token() == "ERASE 0001"
    assert d.to_dict()["size_human"] == "32.0 GiB"


def test_no_devices_message_path():
    assert detect.parse_lsblk(json.dumps({"blockdevices": []})) == []


def test_windows_parser_mocked():
    data = [{"Number": 0, "FriendlyName": "NVMe Samsung", "SerialNumber": "X", "Size": 512 << 30, "BusType": "NVMe",
             "IsBoot": True, "IsSystem": True, "MediaType": "SSD"},
            {"Number": 2, "FriendlyName": "SanDisk Cruzer", "SerialNumber": "AB12", "Size": 32 << 30, "BusType": "USB",
             "IsBoot": False, "IsSystem": False, "MediaType": "Unspecified"},
            {"Number": 3, "FriendlyName": "WD Elements", "SerialNumber": "W", "Size": 2 << 40, "BusType": "USB",
             "IsBoot": False, "IsSystem": False, "MediaType": "HDD"}]
    out = {d.name: d for d in parse_windows_disks(json.dumps(data))}
    assert out["disk0"].status == "excluded"
    assert out["disk2"].status == "ambiguous"  # media type unspecified => fail closed
    assert out["disk3"].status == "excluded"


def test_macos_parser_mocked():
    info = {"DeviceIdentifier": "disk4", "MediaName": "SanDisk Ultra", "TotalSize": 32 << 30, "BusProtocol": "USB",
            "RemovableMedia": True, "Internal": False, "SolidState": True}
    assert parse_macos_diskinfo(plistlib.dumps(info)).status == "eligible"
    info.update(Internal=True, BusProtocol="PCI-Express")
    assert parse_macos_diskinfo(plistlib.dumps(info)).status == "excluded"


def test_capability_matrix_honest():
    assert CAPABILITIES["windows"]["ext4 RW+exec mount at literal /USB/A.I."][0] == "BLOCKED"
    assert CAPABILITIES["windows"]["FAT32 RW mount at literal /USB"][0] == "BLOCKED"
    assert CAPABILITIES["darwin"]["ext4 RW+exec mount at literal /USB/A.I."][0] == "BLOCKED"
    assert CAPABILITIES["darwin"]["FAT32 RW mount at literal /USB"][0] == "BLOCKED"
    for osn in ("windows", "darwin"):
        assert all(st != "WORKING+TESTED" for st, _ in CAPABILITIES[osn].values())
    assert "| Capability | Linux | Windows | macOS |" in matrix_markdown()
    with pytest.raises(UnsupportedOperation):
        fail_closed("windows", "prepare/format (physical USB)")


def test_active_swap_needs_override():
    from helpers import prepared_lsblk_device
    d = detect.parse_lsblk(json.dumps({"blockdevices": [prepared_lsblk_device(mounts=(None, None, "[SWAP]"))]}))[0]
    assert d.status == "ambiguous" and any("swap" in r for r in d.reasons)
