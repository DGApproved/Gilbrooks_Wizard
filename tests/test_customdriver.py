"""Custom driver hook (/AI/Driver/{Windows,MacOS}/ext4.sh): discovery never executes; running needs exact-path
consent, a non-elevated process, a non-WSL interpreter, and an independent mount check. Runner is mocked."""
import os
import subprocess

import pytest

from usbprep import customdriver as CD

SH = {"which": lambda n: None, "exists": lambda p: p == "/bin/sh"}


def make_root(tmp_path, osdir="MacOS", script=True, name="ext4.sh"):
    root = tmp_path / "FAT root"
    d = root / "AI" / "Driver" / osdir
    d.mkdir(parents=True)
    if script:
        (d / name).write_text("#!/bin/sh\nexit 0\n")
    return str(root)


def test_disabled_by_default_looks_for_nothing(tmp_path):
    r = CD.discover("darwin", make_root(tmp_path), enabled=False, probe=SH)
    assert r["state"] == "DISABLED" and r["path"] is None and r["executed"] is False


def test_discover_exact_path_never_executes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))
    root = make_root(tmp_path)
    r = CD.discover("darwin", root, enabled=True, probe=SH)
    assert r["state"] == "FOUND" and r["trusted"] is False and r["executed"] is False
    assert r["path"] == os.path.join(root, "AI", "Driver", "MacOS", "ext4.sh")
    assert calls == []
    assert CD.script_relpath("windows") == "AI/Driver/Windows/ext4.sh"


def test_wrong_name_and_symlink_ignored(tmp_path):
    root = make_root(tmp_path, name="EXT4.sh.txt")
    assert CD.discover("darwin", root, True, SH)["state"] == "MISSING-SCRIPT"
    target = tmp_path / "evil.sh"
    target.write_text("x")
    os.symlink(target, os.path.join(root, "AI", "Driver", "MacOS", "ext4.sh"))
    assert CD.discover("darwin", root, True, SH)["state"] == "MISSING-SCRIPT"


def test_linux_not_offered(tmp_path):
    assert CD.discover("linux", make_root(tmp_path), True, SH)["state"] == "BLOCKED"


def test_windows_interpreter_never_wsl(monkeypatch):
    monkeypatch.setattr(CD, "_windows_bash_candidates", lambda: [r"C:\Program Files\Git\bin\bash.exe"])
    wsl = {"which": lambda n: r"C:\Windows\System32\bash.exe", "exists": lambda p: False}
    r = CD.find_interpreter("windows", wsl)
    assert r["path"] is None and "WSL" in r["error"]
    git = {"which": lambda n: r"C:\Windows\System32\bash.exe", "exists": lambda p: "Git" in p}
    assert CD.find_interpreter("windows", git)["path"] == r"C:\Program Files\Git\bin\bash.exe"
    assert CD.is_wsl_bash(r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\bash.exe")


def test_msys_path_and_argv_with_spaces():
    assert CD.to_msys_path(r"E:\My USB\AI\Driver\Windows\ext4.sh") == "/e/My USB/AI/Driver/Windows/ext4.sh"
    d = {"path": r"E:\AI\Driver\Windows\ext4.sh", "interpreter": r"C:\Program Files\Git\bin\bash.exe"}
    argv = CD.build_argv("windows", d, "mount", r"\\.\PhysicalDrive2", "F:\\", "E:\\")
    assert argv == [r"C:\Program Files\Git\bin\bash.exe", "/e/AI/Driver/Windows/ext4.sh", "mount", "--device",
                    r"\\.\PhysicalDrive2", "--mount-point", "/f/", "--fat-root", "/e/"]
    with pytest.raises(CD.CustomDriverError):
        CD.build_argv("darwin", {"path": "/x", "interpreter": "/bin/sh"}, "format", "/dev/disk4s2", "/m", "/r")
    with pytest.raises(CD.CustomDriverError):
        CD.build_argv("darwin", {"path": "/x", "interpreter": "/bin/sh"}, "mount", "/dev/disk4s2\nrm", "/m", "/r")


class Done:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _found(tmp_path):
    root = make_root(tmp_path)
    return root, CD.discover("darwin", root, True, SH)


def test_run_requires_exact_consent_and_non_elevated(tmp_path):
    root, d = _found(tmp_path)
    ran = []
    runner = lambda argv, **k: ran.append(argv) or Done(0)
    with pytest.raises(CD.CustomDriverError):
        CD.run("darwin", d, "mount", "/dev/disk4s2", "/Volumes/AI", root, consent_path=None, runner=runner,
               elevated=lambda o: False)
    with pytest.raises(CD.CustomDriverError):
        CD.run("darwin", d, "mount", "/dev/disk4s2", "/Volumes/AI", root, consent_path=d["path"] + "x",
               runner=runner, elevated=lambda o: False)
    with pytest.raises(CD.CustomDriverError):
        CD.run("darwin", d, "mount", "/dev/disk4s2", "/Volumes/AI", root, consent_path=d["path"], runner=runner,
               elevated=lambda o: True)
    assert ran == []


def test_exit_zero_is_not_proof_of_mount(tmp_path):
    root, d = _found(tmp_path)
    seen = {}

    def runner(argv, **k):
        seen.update(k)
        return Done(0, "ok password=hunter2")
    r = CD.run("darwin", d, "mount", "/dev/disk4s2", "/Volumes/AI", root, consent_path=d["path"], runner=runner,
               elevated=lambda o: False, verify=lambda mp: {"ok": False, "reason": "not a mount point"},
               log_dir=str(tmp_path / "logs"))
    assert seen["shell"] is False
    assert r["script_succeeded"] is True and r["verified"] is False and r["read_write"] is False
    assert "hunter2" not in r["output_tail"]
    assert "hunter2" not in open(tmp_path / "logs" / "custom-driver.log").read()
    r = CD.run("darwin", d, "mount", "/dev/disk4s2", "/Volumes/AI", root, consent_path=d["path"],
               runner=lambda a, **k: Done(3), elevated=lambda o: False, verify=lambda mp: {"ok": True, "reason": ""})
    assert r["verified"] is None and r["read_write"] is False and "failed" in r["message"]


def test_verify_mount_plain_folder_rejected(tmp_path):
    assert CD.verify_mount(str(tmp_path))["ok"] is False
    assert CD.verify_mount(str(tmp_path / "missing"))["ok"] is False
    v = CD.verify_mount(str(tmp_path), {"ismount": lambda p: True, "fstype": lambda p: "msdos"})
    assert v["ok"] is False and "not ext4" in v["reason"]


def test_redact():
    assert CD.redact("TOKEN: abc api_key=zz") == "TOKEN=<redacted> api_key=<redacted>"
