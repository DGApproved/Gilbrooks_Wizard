"""Scripts menu: listing (no execution), safe argv launch, consent, elevation refusal, locking, defaults."""
import io
import json
import os
import subprocess
import sys

import pytest

from helpers import prepared_lsblk_device, session_with
from usbprep import scripts as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_ai(tmp_path):
    ai = tmp_path / "AI"
    for f in ("Linux", "MacOS", "Windows"):
        (ai / "Scripts" / f).mkdir(parents=True)
    lin = ai / "Scripts" / "Linux"
    (lin / "my tool \u00e9.sh").write_text("#!/bin/sh\necho ran-$1\n")
    (lin / "b.py").write_text("print('py')\n")
    (lin / "notes.txt").write_text("x")
    (lin / ".hidden.sh").write_text("x")
    (lin / "folder.sh").mkdir()
    (tmp_path / "outside.sh").write_text("echo outside\n")
    os.symlink(tmp_path / "outside.sh", lin / "escape.sh")
    (ai / "Scripts" / "Windows" / "w.ps1").write_text("Write-Host hi")
    (ai / "Scripts" / "Windows" / "bad&name.bat").write_text("echo")
    (ai / "Scripts" / "MacOS" / "m.command").write_text("echo")
    return str(ai)


class FakePopen:
    calls = []

    def __init__(self, argv, **kw):
        FakePopen.calls.append((argv, kw))
        self.stdout = io.BytesIO(b"hello\n")

    def wait(self):
        return 0


def test_lists_only_current_os_exact_names_and_skips_unsafe(tmp_path):
    ai = make_ai(tmp_path)
    r = S.list_scripts(ai, "linux")
    assert [x["name"] for x in r["scripts"]] == ["b.py", "my tool \u00e9.sh"]
    skipped = {x["name"]: x["reason"] for x in r["skipped"]}
    assert "symlink" in skipped["escape.sh"] and "regular" in skipped["folder.sh"]
    assert "unsupported" in skipped["notes.txt"] and "hidden" in skipped[".hidden.sh"]
    assert [x["name"] for x in S.list_scripts(ai, "windows")["scripts"]] == ["w.ps1"]
    assert "cmd.exe" in S.list_scripts(ai, "windows")["skipped"][0]["reason"]
    assert [x["name"] for x in S.list_scripts(ai, "darwin")["scripts"]] == ["m.command"]
    assert S.list_scripts(str(tmp_path / "none"), "linux")["scripts"] == []


def test_listing_never_executes(tmp_path, monkeypatch):
    ai = make_ai(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("executed during listing"))
    S.list_scripts(ai, "linux")


def test_requires_consent_and_refuses_elevated(tmp_path):
    ai = make_ai(tmp_path)
    FakePopen.calls = []
    with pytest.raises(S.ScriptError, match="consent"):
        S.run(ai, "b.py", "linux", consent=False, popen=FakePopen, elevated=False)
    with pytest.raises(S.ScriptError, match="administrator/root"):
        S.run(ai, "b.py", "linux", consent=True, popen=FakePopen, elevated=True)
    assert FakePopen.calls == []


@pytest.mark.parametrize("name", ["escape.sh", "notes.txt", "../outside.sh", "folder.sh", "missing.sh"])
def test_refuses_unsafe_names(tmp_path, name):
    ai = make_ai(tmp_path)
    FakePopen.calls = []
    with pytest.raises(S.ScriptError):
        S.run(ai, name, "linux", consent=True, popen=FakePopen, elevated=False)
    assert FakePopen.calls == []


def test_argv_list_no_shell_and_env(tmp_path):
    ai = make_ai(tmp_path)
    FakePopen.calls = []
    out = io.StringIO()
    rc = S.run(ai, "my tool \u00e9.sh", "linux", True, ext4_mount="/USB/A.I.", device="/dev/sdb",
               popen=FakePopen, elevated=False, out=out)
    argv, kw = FakePopen.calls[0]
    assert rc == 0 and isinstance(argv, list) and os.path.basename(argv[0]) == "sh"
    assert argv[-1] == os.path.join(ai, "Scripts", "Linux", "my tool \u00e9.sh")
    assert "shell" not in kw and kw["cwd"].endswith(os.path.join("Scripts", "Linux"))
    assert kw["env"]["USBPREP_EXT4_MOUNT"] == "/USB/A.I." and kw["env"]["USBPREP_DEVICE"] == "/dev/sdb"
    assert "hello" in out.getvalue()


def test_real_launch_with_spaces_unicode(tmp_path):
    ai = make_ai(tmp_path)
    out = io.StringIO()
    assert S.run(ai, "my tool \u00e9.sh", "linux", True, elevated=False, out=out) == 0
    assert "ran-" in out.getvalue()


def test_no_mount_env_leak(tmp_path, monkeypatch):
    ai = make_ai(tmp_path)
    monkeypatch.setenv("USBPREP_EXT4_MOUNT", "/somewhere/else")
    FakePopen.calls = []
    S.run(ai, "b.py", "linux", True, popen=FakePopen, elevated=False, out=io.StringIO())
    assert "USBPREP_EXT4_MOUNT" not in FakePopen.calls[0][1]["env"]


def test_duplicate_launch_prevented(tmp_path):
    ai = make_ai(tmp_path)
    lock = S._lock_path(S.scripts_dir(ai, "linux"), "b.py")
    with open(lock, "w") as f:
        f.write(str(os.getpid()))                     # a live process holds it
    with pytest.raises(S.ScriptError, match="already running"):
        S.run(ai, "b.py", "linux", True, popen=FakePopen, elevated=False)
    with open(lock, "w") as f:
        f.write("999999999")                          # stale lock is recovered
    FakePopen.calls = []
    S.run(ai, "b.py", "linux", True, popen=FakePopen, elevated=False, out=io.StringIO())
    assert FakePopen.calls and not os.path.exists(lock)


def test_missing_interpreter_explained(tmp_path, monkeypatch):
    ai = make_ai(tmp_path)
    monkeypatch.setattr(S.shutil, "which", lambda x: None)
    with pytest.raises(S.ScriptError, match="not installed"):
        S.run(ai, "b.py", "linux", True, popen=FakePopen, elevated=False)
    assert S.list_scripts(ai, "linux")["scripts"][0]["available"] is False


def test_default_linux_script_needs_verified_mount(tmp_path):
    p = os.path.join(ROOT, "Scripts", "Linux", "open_terminal_in_ext4.sh")
    r = subprocess.run(["sh", p], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert r.returncode == 3 and "not mounted" in r.stderr
    r = subprocess.run(["sh", p], env={"PATH": "/nonexistent", "USBPREP_EXT4_MOUNT": str(tmp_path)},
                       capture_output=True, text=True, executable="/bin/sh")
    assert r.returncode == 4 and "No terminal emulator" in r.stderr     # no fallback, guidance only


def test_default_scripts_exist_per_os():
    for osn in ("linux", "darwin", "windows"):
        names = [x["name"] for x in S.list_scripts(ROOT, osn)["scripts"]]
        assert any(n.startswith("open_terminal_in_ext4") for n in names), osn


def test_resolve_ext4_mount_verified_only():
    h = [prepared_lsblk_device("sdb")]
    good = json.dumps({"filesystems": [{"source": "/dev/sdb2", "fstype": "ext4"}]})
    s, _ = session_with(h, {"findmnt": (0, good, "")})
    assert S.resolve_ext4_mount(s, s.select("/dev/sdb"), "/USB/A.I.") == "/USB/A.I."
    bad = json.dumps({"filesystems": [{"source": "/dev/sdz2", "fstype": "ext4"}]})
    s, _ = session_with(h, {"findmnt": (0, bad, "")})
    assert S.resolve_ext4_mount(s, s.select("/dev/sdb"), "/USB/A.I.") is None
    s, _ = session_with(h, {"findmnt": (1, "", "")})
    assert S.resolve_ext4_mount(s, s.select("/dev/sdb"), "/USB/A.I.") is None


def test_app_update_preserves_user_scripts_and_seeds_defaults(tmp_path):
    from usbprep.appcopy import DirBackend, install_app

    def app(root, ver):
        os.makedirs(os.path.join(root, "usbprep"))
        open(os.path.join(root, "usbprep", "__init__.py"), "w").write(f"# {ver}\n")
        os.makedirs(os.path.join(root, "Scripts", "Linux"))
        open(os.path.join(root, "Scripts", "Linux", "open_terminal_in_ext4.sh"), "w").write(f"# default {ver}\n")
        open(os.path.join(root, "Scripts", "Linux", f"new_in_{ver}.sh"), "w").write("# new default\n")
        return root

    fat = str(tmp_path / "fat")
    os.makedirs(fat)
    install_app(DirBackend(fat), src_root=app(str(tmp_path / "v1"), "v1"))
    lin = os.path.join(fat, "AI", "Scripts", "Linux")
    assert open(os.path.join(lin, "open_terminal_in_ext4.sh")).read() == "# default v1\n"   # seeded
    open(os.path.join(lin, "open_terminal_in_ext4.sh"), "w").write("# USER EDIT\n")
    open(os.path.join(lin, "mine.sh"), "w").write("# user script\n")
    install_app(DirBackend(fat), allow_update=True, src_root=app(str(tmp_path / "v2"), "v2"))
    assert open(os.path.join(lin, "open_terminal_in_ext4.sh")).read() == "# USER EDIT\n"    # never overwritten
    assert open(os.path.join(lin, "mine.sh")).read() == "# user script\n"
    assert os.path.exists(os.path.join(lin, "new_in_v2.sh"))                                 # new default added
