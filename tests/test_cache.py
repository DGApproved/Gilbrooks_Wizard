import hashlib
import json
import os
import shutil
import subprocess
import tarfile

import pytest

from usbprep import cache as K
from usbprep.appcopy import APP_ROOT

ARCH = K.normalize_arch()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(ai, os_name, name, data, version="1.8.0_514", arch=ARCH, type_=None):
    d = os.path.join(os.path.dirname(ai), "dl"); os.makedirs(d, exist_ok=True)
    src = os.path.join(d, name)
    with open(src, "wb") as f:
        f.write(data)
    return K.add(ai, src, os_name, arch, version, sha(data), type_=type_)


def test_java_major_and_arch():
    assert K.java_major("1.8.0_514") == 8 and K.java_major("17.0.2") == 17 and K.java_major("21") == 21
    assert K.java_major("1.7.0_80") == 7 and K.java_major("junk") == 0
    assert K.normalize_arch("x86_64") == "x64" and K.normalize_arch("arm64") == "aarch64"


def test_missing_cache(workdir):
    st = K.status(str(workdir / "AI"), "linux", ARCH)
    assert st["usable"] == [] and st["rejected"] == [] and st["unlisted_ignored"] == []


def test_cached_usable_and_tamper_detected(workdir):
    ai = str(workdir / "AI")
    e = put(ai, "linux", "OpenJDK8U-jre.tar.gz", b"archive-bytes")
    assert e["type"] == "tar.gz"
    assert [u["file"] for u in K.status(ai, "linux", ARCH)["usable"]] == ["OpenJDK8U-jre.tar.gz"]
    with open(os.path.join(K.os_dir(ai, "linux"), "OpenJDK8U-jre.tar.gz"), "wb") as f:
        f.write(b"archive-bytez")             # same size, different content
    st = K.status(ai, "linux", ARCH)
    assert st["usable"] == [] and "SHA-256" in st["rejected"][0]["reason"]


@pytest.mark.parametrize("field,value,reason", [("arch", "riscv64", "architecture"), ("os", "darwin", "built for"),
                                                ("version", "1.7.0_80", "older"), ("type", "deb", "not supported"),
                                                ("size", "5000000000", "4 GiB"), ("sha256", "abc", "SHA-256")])
def test_incompatible_entries_rejected(workdir, field, value, reason):
    ai = str(workdir / "AI")
    put(ai, "linux", "jre.tar.gz", b"data")
    d = K.os_dir(ai, "linux")
    rows = K.read_manifest(d)
    rows[0][field] = value
    K.write_manifest(d, rows)
    st = K.status(ai, "linux", ARCH)
    assert st["usable"] == [] and reason in st["rejected"][0]["reason"]


def test_unlisted_file_never_used(workdir):
    ai = str(workdir / "AI")
    d = K.os_dir(ai, "windows")
    os.makedirs(d)
    open(os.path.join(d, "setup.exe"), "wb").close()
    open(os.path.join(d, "README.txt"), "w").close()
    st = K.status(ai, "windows", "x64")
    assert st["usable"] == [] and st["unlisted_ignored"] == ["setup.exe"]


def test_add_refusals(workdir):
    ai = str(workdir / "AI")
    src = str(workdir / "jre.tar.gz")
    open(src, "wb").write(b"abc")
    with pytest.raises(K.CacheError, match="does not match"):
        K.add(ai, src, "linux", ARCH, "8", "0" * 64)
    with pytest.raises(K.CacheError, match="64-hex"):
        K.add(ai, src, "linux", ARCH, "8", "not-a-hash")
    deb = str(workdir / "jre.deb")
    open(deb, "wb").write(b"abc")
    with pytest.raises(K.CacheError, match="not supported offline"):
        K.add(ai, deb, "linux", ARCH, "8", sha(b"abc"))
    with pytest.raises(K.CacheError, match="free space"):
        K.add(ai, src, "linux", ARCH, "8", sha(b"abc"), disk_usage=lambda p: type("U", (), {"free": 10})())
    assert not os.path.exists(os.path.join(K.os_dir(ai, "linux"), "jre.tar.gz"))
    assert K.read_manifest(K.os_dir(ai, "linux")) == []
    msi = str(workdir / "jre.msi")
    open(msi, "wb").write(b"m")
    assert K.add(ai, msi, "windows", "x64", "1.8.0_512", sha(b"m"))["type"] == "msi"


def test_fetch_java_with_fake_http(workdir):
    payload = b"temurin-archive"
    meta = [{"binary": {"package": {"name": "OpenJDK8U-jre_x64_linux_hotspot_8u514b01.tar.gz", "link": "https://x/y",
                                    "checksum": sha(payload), "size": len(payload)}},
             "version": {"openjdk_version": "1.8.0_514-b01"}}]

    def http(url, dest=None):
        if dest is None:
            assert "api.adoptium.net" in url and "image_type=jre" in url
            return json.dumps(meta).encode()
        open(dest, "wb").write(payload)
        return dest
    e = K.fetch_java(str(workdir / "AI"), "linux", "x64", http=http, workdir=str(workdir))
    assert e["version"] == "1.8.0_514-b01" and e["source"] == "https://x/y"
    bad = [dict(meta[0], binary={"package": dict(meta[0]["binary"]["package"], checksum="1" * 64)})]

    def http_bad(url, dest=None):
        return json.dumps(bad).encode() if dest is None else http(url, dest)
    with pytest.raises(K.CacheError, match="does not match"):
        K.fetch_java(str(workdir / "AI2"), "linux", "x64", http=http_bad, workdir=str(workdir))


# ---- native POSIX bootstrap (no Java, no Python) in an isolated sandbox: no privileged install ----
TOOLS = ["sh", "uname", "dirname", "printf", "sed", "head", "cut", "sha256sum", "wc", "tr", "awk", "mkdir", "mktemp",
         "tar", "gzip", "ls", "mv", "rm", "rmdir", "cp", "df", "grep", "basename", "cat", "env"]


def fake_java_script(path, version):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f'#!/bin/sh\necho \'openjdk version "{version}"\' >&2\n')
    os.chmod(path, 0o755)


@pytest.fixture
def sandbox(workdir):
    if any(shutil.which(t) is None for t in TOOLS):
        pytest.skip("POSIX tools for the bootstrap sandbox missing")
    ai = workdir / "AI"
    (ai / "launchers").mkdir(parents=True)
    shutil.copy(os.path.join(APP_ROOT, "launchers", "java_bootstrap.sh"), ai / "launchers")
    bin_ = workdir / "bin"
    bin_.mkdir()
    for t in TOOLS:
        os.symlink(shutil.which(t), bin_ / t)
    home = workdir / "home"
    home.mkdir()
    jre = workdir / "build" / "fakejre-8u999"
    fake_java_script(str(jre / "bin" / "java"), "1.8.0_999")
    tgz = workdir / "OpenJDK8U-jre_fake.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(jre, arcname="fakejre-8u999")

    def run(*args, stdin=subprocess.DEVNULL):
        env = {"HOME": str(home), "PATH": str(bin_)}
        p = subprocess.run(["sh", str(ai / "launchers" / "java_bootstrap.sh"), *args], env=env, stdin=stdin,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr
    return {"run": run, "ai": ai, "bin": bin_, "home": home, "tgz": tgz,
            "sha": sha(tgz.read_bytes())}


def test_bootstrap_no_java_no_cache(sandbox):
    rc, out, err = sandbox["run"]()
    assert rc == 3 and out == "" and "https://adoptium.net/temurin/releases/" in err and "Retry" in err


def test_bootstrap_ignores_unregistered_file(sandbox):
    d = sandbox["ai"] / "Cache" / "Linux"
    d.mkdir(parents=True)
    shutil.copy(sandbox["tgz"], d / "dropped.tar.gz")
    rc, _, err = sandbox["run"]("--yes")
    assert rc == 3 and "ignoring dropped.tar.gz" in err and not (sandbox["home"] / ".local").exists()


def test_bootstrap_register_install_and_skip(sandbox):
    run = sandbox["run"]
    assert run("--register", str(sandbox["tgz"]), "--version", "1.8.0_999", "--sha256", "0" * 64)[0] == 2
    rc, _, err = run("--register", str(sandbox["tgz"]), "--version", "1.8.0_999", "--sha256", sandbox["sha"])
    assert rc == 0, err
    man = (sandbox["ai"] / "Cache" / "Linux" / "cache_manifest.tsv").read_text().splitlines()
    assert man[0].startswith("kind\tversion") and "\t1.8.0_999\tlinux\t" in man[1]
    rows = K.read_manifest(str(sandbox["ai"] / "Cache" / "Linux"))       # same format for Python
    assert rows[0]["sha256"] == sandbox["sha"]
    rc, _, err = run()                                   # no terminal, no --yes => never assumed
    assert rc == 4 and "consent" in err
    rc, out, err = run("--yes")
    assert rc == 0 and out.endswith("/usbprep/java/fakejre-8u999/bin/java") and "Installed" in err
    assert out.startswith(str(sandbox["home"]))         # per-user, no privileged install
    rc, out2, err = run()                                # already installed => found, nothing installed
    assert rc == 0 and out2 == out and "Installed" not in err


def test_bootstrap_existing_java_skips_cache(sandbox):
    fake_java_script(str(sandbox["bin"] / "java"), "17.0.21")
    rc, out, err = sandbox["run"]("--yes")
    assert rc == 0 and out == str(sandbox["bin"] / "java") and "Installed" not in err


def test_bootstrap_too_old_java_and_tampered_cache(sandbox):
    fake_java_script(str(sandbox["bin"] / "java"), "1.7.0_80")
    run = sandbox["run"]
    assert run("--register", str(sandbox["tgz"]), "--version", "1.8.0_999", "--sha256", sandbox["sha"])[0] == 0
    f = sandbox["ai"] / "Cache" / "Linux" / sandbox["tgz"].name
    data = bytearray(f.read_bytes())
    data[-1] ^= 0xFF
    f.write_bytes(bytes(data))
    rc, _, err = run("--yes")
    assert rc == 3 and "SHA-256 does not match" in err


def test_bootstrap_wrong_arch_row_skipped(sandbox):
    run = sandbox["run"]
    assert run("--register", str(sandbox["tgz"]), "--version", "1.8.0_999", "--sha256", sandbox["sha"],
               "--arch", "riscv64")[0] == 0
    rc, _, err = run("--yes")
    assert rc == 3 and "architecture riscv64" in err


def test_windows_bootstrap_script_parses():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell (pwsh) not installed on this Linux host: Windows bootstrap syntax NOT verified")
    script = os.path.join(APP_ROOT, "launchers", "Windows", "bootstrap_java.ps1")
    cmd = f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$e); " \
          "if ($e.Count) { $e; exit 1 }"
    assert subprocess.run([pwsh, "-NoProfile", "-Command", cmd]).returncode == 0
