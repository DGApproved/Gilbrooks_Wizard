import contextlib
import json
import os
import signal
import stat
import time

import pytest

from usbprep import constants as C, executor, reformat as R, safety
from usbprep.appcopy import APP_ROOT, CopyError, DirBackend, build_staging, install_app, verify_tree
from usbprep.detect import parse_lsblk

from helpers import prepared_lsblk_device, session_with


def fake_app(root, version_text="v1"):
    os.makedirs(os.path.join(root, "usbprep"))
    with open(os.path.join(root, "usbprep", "__init__.py"), "w") as f:
        f.write(f"# {version_text}\n")
    with open(os.path.join(root, "START_HERE.txt"), "w") as f:
        f.write("start\n")
    os.makedirs(os.path.join(root, "Cache", "Linux"))
    with open(os.path.join(root, "Cache", "Linux", "README.txt"), "w") as f:
        f.write("cache readme\n")
    return root


def test_install_update_and_preserve(workdir):
    fat = str(workdir / "fat")
    os.makedirs(os.path.join(fat, "EFI", "BOOT"))
    boot = os.path.join(fat, "EFI", "BOOT", "BOOTX64.EFI")
    with open(boot, "wb") as f:
        f.write(b"user bootloader")
    v1 = fake_app(str(workdir / "v1"))
    log = install_app(DirBackend(fat), src_root=v1)
    assert any("SHA-256 verified" in l for l in log)
    man = json.load(open(os.path.join(fat, "AI", C.MANIFEST_NAME)))
    assert verify_tree(os.path.join(fat, "AI"), man) == []
    assert "unchanged" in install_app(DirBackend(fat), src_root=v1)[0]
    # user puts a (verified elsewhere) installer into the cache on the drive
    cached = os.path.join(fat, "AI", "Cache", "Linux", "jre.tar.gz")
    with open(cached, "wb") as f:
        f.write(b"\x1f\x8b fake archive")
    v2 = fake_app(str(workdir / "v2"), "v2")
    with pytest.raises(CopyError, match="update"):
        install_app(DirBackend(fat), src_root=v2)
    log = install_app(DirBackend(fat), allow_update=True, src_root=v2)
    assert any("kept 1 cached installer" in l for l in log), log
    assert open(cached, "rb").read() == b"\x1f\x8b fake archive"            # cache carried over
    assert os.path.isdir(os.path.join(fat, "AI.prev"))
    assert open(boot, "rb").read() == b"user bootloader"                      # /EFI never touched


def test_foreign_ai_folder_refused(workdir):
    fat = str(workdir / "fat")
    os.makedirs(os.path.join(fat, "AI"))
    with pytest.raises(CopyError, match="not created by usbprep"):
        install_app(DirBackend(fat), src_root=fake_app(str(workdir / "v1")))


def test_real_app_file_set():
    from usbprep.appcopy import collect_app_files
    files = collect_app_files(APP_ROOT)
    assert "usbprep/cli.py" in files and "launchers/java_bootstrap.sh" in files
    assert not any("__pycache__" in f or f.startswith("tests/") or f.startswith(".git") for f in files)


def _ai(workdir):
    ai = str(workdir / "AI")
    build_staging(ai, fake_app(str(workdir / "src")))
    with open(os.path.join(ai, "config.json"), "w") as f:
        f.write("{}")
    for secret in (".env", "id_rsa", "api_token.txt", "cert.pem"):
        with open(os.path.join(ai, secret), "w") as f:
            f.write("s3cret")
    with open(os.path.join(ai, "Cache", "Linux", "jre.tar.gz"), "wb") as f:
        f.write(b"x" * 1000)
    return ai


def test_stage_backup_excludes_secrets(workdir):
    st = R.stage_backup(_ai(workdir), base=str(workdir))
    assert stat.S_IMODE(os.stat(st["work"]).st_mode) == 0o700
    assert sorted(st["excluded"]) == sorted([".env", "id_rsa", "api_token.txt", "cert.pem"])
    assert "config.json" in st["hashes"] and "Cache/Linux/jre.tar.gz" in st["hashes"]
    assert not os.path.exists(os.path.join(st["staged"], ".env"))


def test_stage_backup_space_check(workdir, monkeypatch):
    ai = _ai(workdir)
    monkeypatch.setattr(R.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    base = workdir / "base"
    base.mkdir()
    with pytest.raises(R.StageError, match="nothing was formatted"):
        R.stage_backup(ai, base=str(base))
    assert os.listdir(base) == []


def test_stage_backup_refuses_non_usbprep(workdir):
    (workdir / "x").mkdir()
    with pytest.raises(R.StageError):
        R.stage_backup(str(workdir / "x"), base=str(workdir))


def _job(workdir, ai_dir):
    """allow_ambiguous=True: the simulated drive has its swap active, which needs the explicit override."""
    st = R.stage_backup(ai_dir, base=str(workdir))
    dev = parse_lsblk(json.dumps({"blockdevices": [prepared_lsblk_device()]}))[0]
    return R.write_job(st, dev, {}, "ERASE 2345", None, True, str(workdir / "USB")), st


def test_relaunch_handoff_real_process(workdir):
    """The staged copy really starts from /home/ubuntu/.tmp/..., confirms, then waits for us (killed here)."""
    ai = str(workdir / "AI")
    build_staging(ai, APP_ROOT)
    job, st = _job(workdir, ai)
    pid = R.relaunch_from_stage(job, timeout=60)
    try:
        info = json.load(open(os.path.join(st["work"], R.READY_FILE)))
        assert info["module_file"].startswith(os.path.realpath(st["staged"]) + os.sep)
        assert info["cwd"].startswith(os.path.realpath(st["staged"]))
        os.kill(pid, 0)                                          # still waiting for the parent (us)
    finally:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


def test_relaunch_rejects_wrong_location(workdir):
    job, st = _job(workdir, _ai(workdir))

    class FakeChild:
        pid, returncode = 4242, None

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    def popen(argv, **kw):
        with open(os.path.join(st["work"], R.READY_FILE), "w") as f:
            json.dump({"module_file": "/media/user/EFI/AI/usbprep/__init__.py"}, f)
        return FakeChild()
    with pytest.raises(R.HandoffError, match="not running from the backup"):
        R.relaunch_from_stage(job, popen=popen, timeout=5)


@pytest.fixture
def fake_nodes(monkeypatch):
    monkeypatch.setattr(safety, "check_device_target", lambda p: None)
    monkeypatch.setattr(executor, "check_device_target", lambda p: None)


def _reformat_session(workdir, fail_format=False):
    holder = [prepared_lsblk_device(mounts=("/media/u/EFI", "/media/u/AI", "[SWAP]"))]

    def release(argv):
        for ch in holder[0]["children"]:
            ch["mountpoints"] = [None]
        return 0, "", ""

    def formatted(argv):
        holder[0] = prepared_lsblk_device()
        return 0, "", ""
    extra = {"umount": (release, None, None), "swapoff": (release, None, None),
             "mkswap": (formatted, None, None)}
    if fail_format:
        extra["sgdisk -o"] = (1, "", "I/O error")
    return session_with(holder, extra)


def test_run_job_success_restores_and_removes_backup(workdir, fake_nodes):
    job_path, st = _job(workdir, _ai(workdir))
    job = json.load(open(job_path))
    s, r = _reformat_session(workdir)
    fresh = workdir / "fresh_fat"
    fresh.mkdir()
    logs = []
    ok = R.run_job(job, s, log=logs.append, restore_backend=lambda p: contextlib.nullcontext(DirBackend(str(fresh))),
                   busy_scanner=lambda mps: [], mounted_sources=set())
    assert ok, logs
    assert os.path.isfile(fresh / "AI" / "config.json") and os.path.isfile(fresh / "AI" / "Cache/Linux/jre.tar.gz")
    assert not os.path.exists(fresh / "AI" / ".env")
    assert not os.path.exists(st["work"])
    assert any(c[0] == "swapoff" for c in r.calls) and any(c[0] == "sgdisk" for c in r.calls)


def test_run_job_failure_keeps_backup(workdir, fake_nodes):
    job_path, st = _job(workdir, _ai(workdir))
    job = json.load(open(job_path))
    s, _ = _reformat_session(workdir, fail_format=True)
    logs = []
    ok = R.run_job(job, s, log=logs.append, restore_backend=lambda p: pytest.fail("no restore after failure"),
                   busy_scanner=lambda mps: [], mounted_sources=set())
    assert not ok and os.path.isdir(st["work"]) and "Backup kept" in logs[-1]


def test_run_job_refuses_busy_drive(workdir, fake_nodes):
    job_path, st = _job(workdir, _ai(workdir))
    s, r = _reformat_session(workdir)
    logs = []
    ok = R.run_job(json.load(open(job_path)), s, log=logs.append, busy_scanner=lambda mps: ["pid 1: /media/u/AI"])
    assert not ok and "nothing formatted" in logs[-1] and not any(c[0] in ("umount", "sgdisk") for c in r.calls)


def test_run_job_identity_change(workdir, fake_nodes):
    job_path, _ = _job(workdir, _ai(workdir))
    job = json.load(open(job_path))
    job["device"]["serial"] = "SWAPPED1"
    s, r = _reformat_session(workdir)
    logs = []
    assert not R.run_job(job, s, log=logs.append, busy_scanner=lambda m: [])
    assert "device changed" in logs[-1] and not any(c[0] == "sgdisk" for c in r.calls)


def test_processes_using_detects_own_cwd(workdir):
    import subprocess
    p = subprocess.Popen(["sleep", "30"], cwd=str(workdir))
    try:
        time.sleep(0.2)
        assert any(f"pid {p.pid}" in h for h in R.processes_using([str(workdir)]))
    finally:
        p.kill()
        p.wait()
