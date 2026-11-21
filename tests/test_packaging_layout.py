"""FAT32 layout: the whole project lives in /AI at the FAT32 root; /EFI holds only firmware boot files.
Start folder scripts delegate to the launchers (paths with spaces), Driver hook folders are carried."""
import os
import re
import shutil
import subprocess

import pytest

from usbprep import bootcontent, constants as C
from usbprep.appcopy import APP_ROOT, CARRY, INCLUDE, DirBackend, install_app

ROOT = APP_ROOT


def test_constants_put_project_under_AI_and_boot_under_EFI():
    assert C.APP_DIR_NAME == "AI"
    assert bootcontent.EFI_FALLBACK == "EFI/BOOT/BOOTX64.EFI"
    assert "Start" in INCLUDE and "Driver" in INCLUDE
    for keep in ("Driver", "Scripts", "scr", "src"):
        assert keep in CARRY


def test_start_and_driver_files_present():
    for rel in ("Start/Linux/start.sh", "Start/MacOS/start.command", "Start/Windows/START.bat", "Start/README.txt",
                "Driver/README.txt", "Driver/Windows/README.txt", "Driver/MacOS/README.txt"):
        assert os.path.isfile(os.path.join(ROOT, rel)), rel
    assert not os.path.exists(os.path.join(ROOT, "Driver", "Windows", "ext4.sh"))   # no shipped custom script
    assert not os.path.exists(os.path.join(ROOT, "Driver", "MacOS", "ext4.sh"))
    bat = open(os.path.join(ROOT, "Start/Windows/START.bat"), "rb").read()
    assert b"\r\n" in bat and b'call "%~dp0..\\..\\launchers\\Windows\\START_HERE.bat" %*' in bat
    assert b"wsl" not in bat.lower().replace(b"wsl checkbox", b"").replace(b"wsl only", b"")
    assert "launchers/Linux/run.sh" in open(os.path.join(ROOT, "Start/Linux/start.sh")).read()
    assert "launchers/macOS/START_HERE.command" in open(os.path.join(ROOT, "Start/MacOS/start.command")).read()


def test_install_puts_everything_under_AI_only(tmp_path):
    fat = tmp_path / "fat"
    (fat / "EFI" / "BOOT").mkdir(parents=True)
    (fat / "EFI" / "BOOT" / "BOOTX64.EFI").write_bytes(b"boot")
    install_app(DirBackend(str(fat)), src_root=ROOT)
    assert sorted(os.listdir(fat)) == ["AI", "EFI"]
    assert os.listdir(fat / "EFI") == ["BOOT"] and os.listdir(fat / "EFI" / "BOOT") == ["BOOTX64.EFI"]
    ai = fat / "AI"
    for rel in ("Start/Windows/START.bat", "Start/MacOS/start.command", "Start/Linux/start.sh",
                "Driver/MacOS/README.txt", "Driver/Windows/README.txt", "usbprep/backends.py",
                "usbprep/customdriver.py", C.MANIFEST_NAME):
        assert (ai / rel).is_file(), rel


def test_user_custom_driver_script_survives_app_update(tmp_path):
    fat = tmp_path / "fat"
    fat.mkdir()
    install_app(DirBackend(str(fat)), src_root=ROOT)
    user = fat / "AI" / "Driver" / "MacOS" / "ext4.sh"
    user.write_text("#!/bin/sh\necho user hook\n")
    # a changed app version forces an update; the user's hook must be carried, not replaced
    v2 = tmp_path / "v2"
    shutil.copytree(ROOT, v2, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "docs", "gui-java/build"))
    (v2 / "START_HERE.txt").write_text(open(os.path.join(ROOT, "START_HERE.txt")).read() + "\nv2\n")
    install_app(DirBackend(str(fat)), allow_update=True, src_root=str(v2))
    assert user.read_text() == "#!/bin/sh\necho user hook\n"
    assert sorted(x for x in os.listdir(fat) if not x.startswith("AI.")) == ["AI"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX sh")
@pytest.mark.parametrize("start,launcher", [("Start/Linux/start.sh", "launchers/Linux/run.sh"),
                                            ("Start/MacOS/start.command", "launchers/macOS/START_HERE.command")])
def test_start_scripts_delegate_with_spaces(tmp_path, start, launcher):
    ai = tmp_path / "my USB drive" / "A I"
    (ai / os.path.dirname(start)).mkdir(parents=True)
    (ai / os.path.dirname(launcher)).mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, start), ai / start)
    (ai / launcher).write_text('#!/bin/sh\nprintf "%s|" "$0" "$@"\n')
    out = subprocess.run(["sh", str(ai / start), "arg with space", "x"], capture_output=True, text=True,
                         timeout=30).stdout
    parts = out.split("|")
    assert os.path.realpath(parts[0]) == os.path.realpath(str(ai / launcher))
    assert parts[1:3] == ["arg with space", "x"]


def test_windows_setup_uses_wsl_only_behind_explicit_choice():
    ps = open(os.path.join(ROOT, "setup", "windows_setup.ps1"), encoding="utf-8").read()
    funcs = re.split(r"(?m)^function ", ps)
    for body in funcs[1:]:
        name = body.split("{", 1)[0].strip()
        code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
        calls_wsl = re.search(r'Run "(wsl|usbipd)"', code)
        if calls_wsl:
            assert name.startswith("Invoke-Wsl") and "Assert-WslChosen" in code, name
    # dispatcher: native is the else-branch default
    for act in ("Setup", "MountExt4", "UnmountExt4"):
        m = re.search(r'"%s" \{\s*if \(\$WslChosen\) \{ Invoke-Wsl\w+ \} else \{ Invoke-Native\w+ \}' % act, ps)
        assert m, act
    assert re.search(r"Get-SavedWslChoice", ps)


@pytest.mark.skipif(os.name != "posix", reason="POSIX sh")
def test_macos_readonly_mount_refuses_when_saved_backend_not_readonly(tmp_path):
    env = dict(os.environ, USBPREP_CONFIG_DIR=str(tmp_path / "cfg"))
    sh = os.path.join(ROOT, "setup", "macos_setup.sh")
    r = subprocess.run(["sh", sh, "--dry-run", "readonly-mount", "disk4s2"], capture_output=True, text=True, env=env,
                       timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "not 'readonly' - refusing" in r.stdout
    r = subprocess.run(["sh", sh, "--dry-run", "readonly-mount", "disk4; reboot"], capture_output=True, text=True,
                       env=env, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "disk4s2" in r.stdout
    r = subprocess.run(["sh", sh, "--dry-run", "homebrew-mount", "disk4s2"], capture_output=True, text=True, env=env,
                       timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "refusing" in r.stdout
