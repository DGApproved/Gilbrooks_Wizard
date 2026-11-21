"""ext4 backend choice (Windows: native/WSL/custom; macOS: native/Homebrew/Read-only ext4fuse/custom).
Pure logic with injected probes: nothing is installed, mounted or executed."""
import json
import os
import stat
import subprocess
import sys

import pytest

from usbprep import backends as B

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTHING = {"which": lambda n: None, "win_service": lambda n: False, "machine": lambda: "arm64",
           "exists": lambda p: False}


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("USBPREP_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_default_is_native_everywhere():
    c = B.load_choice()
    assert c["windows_backend"] == "native" and c["macos_backend"] == "native"
    r = B.status("windows", probe=NOTHING)
    assert r["choice"] == "native" and r["wsl_selected"] is False and r["backend"] == "native-ext4fsd"


def test_save_load_roundtrip_and_mode(cfg):
    B.save_choice(windows="wsl")
    assert B.load_choice()["windows_backend"] == "wsl"
    B.save_choice(macos="readonly")
    c = B.load_choice()
    assert c["macos_backend"] == "readonly" and c["windows_backend"] == "wsl"
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(B.config_path()).st_mode) == 0o600
    assert not [f for f in os.listdir(cfg) if f.startswith(".backend.")]     # atomic write leaves no temp files


def test_single_key_per_os_is_exclusive():
    B.save_choice(windows="wsl")
    B.save_choice(windows="custom")
    data = json.load(open(B.config_path()))
    assert data["windows_backend"] == "custom" and B.status("windows", probe=NOTHING)["wsl_selected"] is False


def test_corrupt_or_unknown_config_means_native(cfg):
    os.makedirs(cfg, exist_ok=True)
    open(B.config_path(), "w").write("{not json")
    assert B.load_choice()["windows_backend"] == "native"
    open(B.config_path(), "w").write(json.dumps({"windows_backend": "wsl2", "windows_wsl": "yes"}))
    assert B.load_choice()["windows_backend"] == "native"          # only an exact known value / literal true counts


def test_invalid_choice_rejected():
    with pytest.raises(ValueError):
        B.save_choice(windows="homebrew")
    with pytest.raises(ValueError):
        B.status("darwin", choice="wsl", probe=NOTHING)


def test_windows_native_missing_has_no_wsl_fallback():
    probe = dict(NOTHING, which=lambda n: "/x/" + n)               # wsl + usbipd present on PATH
    r = B.status("windows", choice="native", probe=probe)
    assert r["state"] == B.MISSING and r["backend"] == "native-ext4fsd" and r["fallback"] is None
    assert r["read_write"] is False and "NOT used as a fallback" in r["message"]


def test_windows_wsl_only_when_selected_and_missing_reported():
    r = B.status("windows", choice="wsl", probe=NOTHING)
    assert r["backend"] == "wsl" and r["state"] == B.MISSING and "Nothing is installed" in r["message"]
    r = B.status("windows", choice="wsl", probe=dict(NOTHING, which=lambda n: "/x/" + n))
    assert r["state"] == B.READY and r["tested_here"] is False


def test_windows_native_ready_and_feature_blocks():
    probe = dict(NOTHING, win_service=lambda n: n == "Ext2Fsd")
    assert B.status("windows", choice="native", probe=probe)["state"] == B.READY
    r = B.status("windows", choice="native", probe=probe, features=["extent", "casefold"])
    assert r["state"] == B.BLOCKED and r["read_write"] is False


def test_macos_native_blocked_and_intel_homebrew_blocked():
    assert B.status("darwin", choice="native", probe=NOTHING)["state"] == B.BLOCKED
    r = B.status("darwin", choice="homebrew", probe=dict(NOTHING, machine=lambda: "x86_64"))
    assert r["state"] == B.BLOCKED and r["read_write"] is False and r["fallback"] is None


def test_macos_readonly_routes_to_ext4fuse_only():
    r = B.status("darwin", choice="readonly", probe=NOTHING)
    assert r["backend"] == "readonly-ext4fuse" and r["state"] == B.MISSING and r["fallback"] is None
    r = B.status("darwin", choice="readonly", probe=dict(NOTHING, which=lambda n: "/b/" + n))
    assert r["state"] == B.READY and r["read_write"] is False and r["backend"] == "readonly-ext4fuse"
    assert B.ext4fuse_mount_argv("disk4s2", "/Volumes/AI") == ["ext4fuse", "/dev/disk4s2", "/Volumes/AI", "-o",
                                                                "allow_other"]
    with pytest.raises(ValueError):
        B.ext4fuse_mount_argv("disk4; rm -rf /", "/Volumes/AI")


def test_saved_readonly_choice_used_for_later_status():
    B.save_choice(macos="readonly")
    assert B.status("darwin", probe=NOTHING)["backend"] == "readonly-ext4fuse"


def test_require_writable_refuses_readonly_and_unknown():
    for rw in (False, None):
        with pytest.raises(B.ReadOnlyBackend):
            B.require_writable({"read_write": rw, "backend": "readonly-ext4fuse"}, "Data restore to USB")
    B.require_writable({"read_write": True, "backend": "linux-kernel"}, "format")


def test_verify_readonly_mount():
    yes, no = (lambda p: True), (lambda p: False)
    assert B.verify_readonly_mount("/m", {"ismount": no})["verified"] is False
    r = B.verify_readonly_mount("/m", {"ismount": yes, "write_probe": yes})
    assert r["verified"] is False and r["read_write"] is True
    r = B.verify_readonly_mount("/m", {"ismount": yes, "write_probe": no})
    assert r["verified"] is True and r["read_write"] is False
    r = B.verify_readonly_mount("/m", {"ismount": yes, "write_probe": no, "device_of": lambda p: "/dev/disk9s2"},
                                expected_device="/dev/disk4s2")
    assert r["verified"] is False and "not the selected USB" in r["reason"]


def test_verify_readonly_mount_real_dirs(tmp_path):
    rw = B.verify_readonly_mount(str(tmp_path), {"ismount": lambda p: True})
    assert rw["verified"] is False and rw["read_write"] is True
    assert os.listdir(tmp_path) == []                                  # probe file removed


def _cli(*a, env):
    return subprocess.run([sys.executable, "-m", "usbprep", *a], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=60)


def test_cli_backend_set_and_status(cfg):
    env = dict(os.environ, USBPREP_CONFIG_DIR=str(cfg))
    r = _cli("backend", "status", "--os", "windows", "--json", env=env)
    j = json.loads(r.stdout)
    assert j["choice"] == "native" and j["wsl_selected"] is False
    assert _cli("backend", "set", "readonly", "--os", "darwin", env=env).returncode == 0
    j = json.loads(_cli("backend", "status", "--os", "darwin", "--json", env=env).stdout)
    assert j["choice"] == "readonly" and j["read_write"] is False
    assert _cli("backend", "set", "wsl", "--os", "darwin", env=env).returncode != 0
