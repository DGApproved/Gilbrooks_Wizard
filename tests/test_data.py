"""Data menu: fixture homes + fixture 'ext4' dirs under tmp_path only (never a real home or USB)."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from usbprep import data as D

RSYNC = shutil.which("rsync")
need_rsync = pytest.mark.skipif(not RSYNC, reason="rsync not installed")


def mk_home(tmp_path):
    h = tmp_path / "home" / "alice"
    (h / "Documents" / "proj").mkdir(parents=True)
    (h / "Documents" / "proj" / "a.txt").write_text("hello")
    (h / ".bashrc").write_text("x")
    (h / ".ssh").mkdir(mode=0o700)
    (h / ".ssh" / "id_test").write_text("FAKE-NOT-A-KEY")
    os.chmod(h / ".ssh" / "id_test", 0o600)
    os.chmod(h / ".ssh", 0o700)
    (h / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(h / "run.sh", 0o750)
    os.symlink("/etc/passwd", h / "link_out")
    os.mkfifo(h / "fifo")
    return h


def user(h, elevated=False):
    return {"name": "alice", "uid": os.getuid(), "gid": os.getgid(), "home": str(h), "elevated": elevated}


def snapshot(root):
    r = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            c = hashlib.sha256(open(p, "rb").read()).hexdigest() if stat.S_ISREG(st.st_mode) else None
            r[os.path.relpath(p, root)] = (st.st_mode, c)
    return r


def ext4(tmp_path):
    e = tmp_path / "USB" / "A.I."
    e.mkdir(parents=True)
    return e


def test_rsync_flags_exact_no_delete():
    a = D.linux_backup_argv("/home/alice", "/USB/A.I./Data/Linux/m/alice/home", [])
    assert a[:2] == ["rsync", "-axHAX"] and not any("delete" in x for x in a)
    assert a[-2:] == ["/home/alice/", "/USB/A.I./Data/Linux/m/alice/home/"]
    for mode in D.CONFLICT_MODES:
        r = D.restore_argv("/s", "/h", mode, dry=False)
        assert not any("delete" in x for x in r) and "-p" not in r[1]


def test_conflict_flags():
    assert "--update" in D.restore_argv("/s", "/h", "skip-newer", True)
    assert "--ignore-existing" in D.restore_argv("/s", "/h", "skip-existing", True)
    assert "--dry-run" in D.restore_argv("/s", "/h", "overwrite", True)
    with pytest.raises(D.DataError):
        D.restore_argv("/s", "/h", "mirror", True)


def test_disabled_on_windows_and_macos(tmp_path):
    for o in ("windows", "darwin"):
        with pytest.raises(D.DataDisabled, match="BLOCKED"):
            D.backup(str(tmp_path), o, execute=True, consent=True)
        with pytest.raises(D.DataDisabled):
            D.restore(str(tmp_path), "Linux/x/alice", o)


def test_cli_disabled_on_windows(monkeypatch, capsys):
    from usbprep import cli
    monkeypatch.setattr(cli, "current_os", lambda: "windows")
    assert cli.main(["data", "backup", "--device", "X", "--execute", "--i-understand-777"]) == 2
    assert "disabled" in capsys.readouterr().err


def test_real_user_resolution():
    pw = SimpleNamespace(getpwnam=lambda n: SimpleNamespace(pw_name=n, pw_uid=1000, pw_gid=1000, pw_dir="/"),
                         getpwuid=lambda u: SimpleNamespace(pw_name="root" if u == 0 else "bob", pw_uid=u, pw_gid=u,
                                                            pw_dir="/"))
    assert D.real_user({"SUDO_USER": "alice"}, 0, pw)["name"] == "alice"
    assert D.real_user({"PKEXEC_UID": "1001"}, 0, pw)["uid"] == 1001
    with pytest.raises(D.DataError, match="root"):
        D.real_user({}, 0, pw)
    with pytest.raises(D.DataError):
        D.real_user({"SUDO_USER": "root"}, 0, pw)
    assert D.real_user({}, 1001, pw)["elevated"] is False


def test_machine_id_hashed_not_raw():
    m = D.machine_id("linux", lambda p: "0123456789abcdef0123456789abcdef")
    assert len(m) == 16 and "0123456789abcdef" not in m
    assert m == D.machine_id("linux", lambda p: "0123456789abcdef0123456789abcdef")


def test_dest_guards(tmp_path):
    e = ext4(tmp_path)
    with pytest.raises(D.DataError, match="outside"):
        D.check_dest(str(e), str(tmp_path / "elsewhere"))
    (e / "Data").symlink_to(tmp_path)
    with pytest.raises(D.DataError, match="symlink"):
        D.backup_dir(str(e), "linux", "m1", "alice")
    with pytest.raises(D.DataError, match="unsafe"):
        D.backup_dir(str(e), "linux", "..", "alice")
    with pytest.raises(D.DataError, match="no verified ext4"):
        D.check_dest(str(tmp_path / "missing"), str(tmp_path / "missing" / "x"))


def test_recursion_into_plain_home_folder_refused(tmp_path):
    h = mk_home(tmp_path)
    fake = h / "USB_in_home"
    fake.mkdir()
    with pytest.raises(D.DataError, match="recursion"):
        D.backup(str(fake), user=user(h), mid="m1", du=lambda p: 1, free=lambda p: 10 ** 9)


def test_space_check(tmp_path):
    h, e = mk_home(tmp_path), ext4(tmp_path)
    with pytest.raises(D.DataError, match="not enough space"):
        D.backup(str(e), user=user(h), mid="m1", du=lambda p: 10 ** 12, free=lambda p: 10 ** 6)


def test_dry_run_default_writes_nothing(tmp_path):
    h, e = mk_home(tmp_path), ext4(tmp_path)
    plan = D.backup(str(e), user=user(h), mid="m1", du=lambda p: 1, free=lambda p: 10 ** 9)
    assert plan["executed"] is False and "777" in plan["warning"]
    assert plan["shared_counts"]["Documents"] == 1 and plan["os_counts"]["linux"] > 0
    assert not (e / "Data").exists()
    with pytest.raises(D.DataError, match="consent"):
        D.backup(str(e), execute=True, user=user(h), mid="m1", du=lambda p: 1, free=lambda p: 10 ** 9)


@need_rsync
def test_backup_real_rsync_split_manifest_chmod_source_untouched(tmp_path):
    h, e = mk_home(tmp_path), ext4(tmp_path)
    (h / "Music").mkdir()
    (h / "Music" / "Song.MP3").write_bytes(b"ID3fake")
    (h / "Downloads").mkdir()
    (h / "Downloads" / "setup.exe").write_bytes(b"MZ")
    (h / "Downloads" / "tool.AppImage").write_bytes(b"\x7fELF")
    (h / "Apps" / "Foo.app" / "Contents" / "Resources").mkdir(parents=True)
    (h / "Apps" / "Foo.app" / "Contents" / "Resources" / "icon.png").write_bytes(b"png")
    (h / ".cache").mkdir()
    (h / ".cache" / "thumb.png").write_bytes(b"png")
    (h / "notes.unknownext").write_text("?")
    before = snapshot(h)
    calls = []
    def runner(argv):
        calls.append(argv)
        p = subprocess.run(argv, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    r = D.backup(str(e), execute=True, consent=True, user=user(h), mid="m1", du=lambda p: 1,
                 free=lambda p: 10 ** 9, runner=runner)
    assert r["status"] == "complete", r
    assert all(c[:2] == ["rsync", "-axHAX"] and not any("delete" in x for x in c) for c in calls)
    assert snapshot(h) == before                                   # source untouched (content + modes)
    data = e / "Data"
    sh, lin = data / "Shared", data / "Linux" / "m1" / "alice" / "home"
    assert (sh / "Music" / "Music" / "Song.MP3").exists()          # structure kept, case-insensitive ext
    assert (sh / "Documents" / "Documents" / "proj" / "a.txt").exists()
    assert (data / "Windows" / "m1" / "alice" / "home" / "Downloads" / "setup.exe").exists()
    assert (lin / "Downloads" / "tool.AppImage").exists()
    bundle = data / "MacOS" / "m1" / "alice" / "home" / "Apps" / "Foo.app" / "Contents" / "Resources" / "icon.png"
    assert bundle.exists() and not (sh / "Pictures" / "Apps").exists()   # bundle kept whole, not split
    assert (lin / ".cache" / "thumb.png").exists()                 # hidden -> OS folder, not shared
    assert (lin / "notes.unknownext").exists() and (lin / ".bashrc").exists()
    assert (lin / ".ssh" / "id_test").exists() and not (sh / "Documents" / "Music").exists()
    assert stat.S_IMODE(os.lstat(lin / ".ssh" / "id_test").st_mode) == 0o777
    assert stat.S_IMODE(os.lstat(sh / "Music" / "Music" / "Song.MP3").st_mode) == 0o777
    assert os.path.islink(lin / "link_out") and os.readlink(lin / "link_out") == "/etc/passwd"
    assert stat.S_IMODE(os.stat("/etc/passwd").st_mode) != 0o777
    man = json.load(open(lin.parent / D.MANIFEST))["entries"]
    assert man[".ssh/id_test"]["mode"] == 0o600 and man["run.sh"]["mode"] == 0o750 and man["link_out"]["t"] == "l"
    assert stat.S_IMODE(os.lstat(lin.parent / D.MANIFEST).st_mode) == 0o600
    idx = json.load(open(sh / D.INDEX))["entries"]
    assert idx["Music/Music/Song.MP3"][0]["user"] == "alice" and idx["Music/Music/Song.MP3"][0]["machine"] == "m1"
    ids = {b["id"] for b in D.list_backups(str(e))}
    assert {"Linux/m1/alice", "Windows/m1/alice", "MacOS/m1/alice", "Shared:m1/alice"} <= ids
    assert not (data / ".usbprep_work").exists()


@need_rsync
def test_shared_dedup_and_versions_across_accounts(tmp_path):
    e = ext4(tmp_path)
    homes = {}
    for name, content in (("alice", b"same"), ("bob", b"same"), ("carol", b"DIFFERENT")):
        h = tmp_path / "homes" / name
        (h / "Music").mkdir(parents=True)
        (h / "Music" / "song.mp3").write_bytes(content)
        homes[name] = h
        u = {"name": name, "uid": os.getuid(), "gid": os.getgid(), "home": str(h), "elevated": False}
        r = D.backup(str(e), execute=True, consent=True, user=u, mid="m" + name, du=lambda p: 1,
                     free=lambda p: 10 ** 9)
        assert r["status"] == "complete"
    sh = e / "Data" / "Shared" / "Music"
    assert (sh / "Music" / "song.mp3").read_bytes() == b"same"     # never overwritten
    assert (sh / "_versions" / "mcarol-carol" / "Music" / "song.mp3").read_bytes() == b"DIFFERENT"
    assert not (sh / "_versions" / "mbob-bob").exists()             # identical -> dedup, provenance only
    ents = json.load(open(sh.parent / D.INDEX))["entries"]["Music/Music/song.mp3"]
    assert {x["user"] for x in ents} == {"alice", "bob", "carol"}
    # restore carol's version (explicit source) into a new home; alice's copy is not used
    h2 = tmp_path / "new" / "carol"
    h2.mkdir(parents=True)
    u2 = {"name": "carol", "uid": os.getuid(), "gid": os.getgid(), "home": str(h2), "elevated": False}
    prev = D.restore_shared(str(e), "mcarol", "carol", user=u2)
    assert prev["executed"] is False and prev["would_create"] == 1 and not (h2 / "Music").exists()
    r = D.restore_shared(str(e), "mcarol", "carol", user=u2, execute=True, confirm="RESTORE carol")
    assert r["status"] == "complete" and (h2 / "Music" / "song.mp3").read_bytes() == b"DIFFERENT"
    assert stat.S_IMODE(os.lstat(h2 / "Music" / "song.mp3").st_mode) != 0o777


@need_rsync
def test_restore_shared_localized_known_folder(tmp_path):
    e = ext4(tmp_path)
    h = tmp_path / "h" / "ana"
    (h / "Music").mkdir(parents=True)
    (h / "Music" / "a.flac").write_bytes(b"f")
    u = {"name": "ana", "uid": os.getuid(), "gid": os.getgid(), "home": str(h), "elevated": False}
    D.backup(str(e), execute=True, consent=True, user=u, mid="m1", du=lambda p: 1, free=lambda p: 10 ** 9)
    h2 = tmp_path / "h2" / "ana"
    (h2 / ".config").mkdir(parents=True)
    (h2 / ".config" / "user-dirs.dirs").write_text('XDG_MUSIC_DIR="$HOME/Musik"\n')
    u2 = dict(u, home=str(h2))
    r = D.restore_shared(str(e), "m1", "ana", user=u2, execute=True, confirm="RESTORE ana")
    assert r["known_folder_map"] == {"Music": "Musik"} and (h2 / "Musik" / "a.flac").exists()


def test_classify_rules(tmp_path):
    h = mk_home(tmp_path)
    (h / "Pics").mkdir()
    (h / "Pics" / "x.JPG").write_bytes(b"j")
    (h / "Pics" / "y.dmg").write_bytes(b"d")
    (h / "Pics" / "z.deb").write_bytes(b"d")
    (h / "Pics" / "noext").write_bytes(b"?")
    os.symlink(h / "Pics" / "x.JPG", h / "Pics" / "alias.jpg")
    c = D.classify(str(h), "linux")
    assert "Pics/x.JPG" in c["shared"]["Pictures"]
    assert "Pics/alias.jpg" in c["os"]["linux"]                      # symlink never "shared"
    assert "Pics/y.dmg" in c["os"]["darwin"] and "Pics/z.deb" in c["os"]["linux"]
    assert "Pics/noext" in c["os"]["linux"] and ".ssh/id_test" in c["os"]["linux"]
    assert "fifo" in c["os"]["linux"]
    shared_all = sum(c["shared"].values(), [])
    assert not any(x.startswith(".") for x in shared_all)


def test_foreign_os_restore_needs_explicit_flag(tmp_path):
    e = ext4(tmp_path)
    (e / "Data" / "Windows" / "m1" / "alice" / "home").mkdir(parents=True)
    with pytest.raises(D.DataError, match="allow-foreign-os"):
        D.restore(str(e), "Windows/m1/alice", user=user(tmp_path))


def test_chmod_skips_special_and_outside(tmp_path):
    e = ext4(tmp_path)
    t = e / "Data" / "t"
    t.mkdir(parents=True)
    (t / "f").write_text("x")
    os.mkfifo(t / "p")
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    os.chmod(outside, 0o600)
    os.symlink(outside, t / "l")
    n = D.chmod_777_subtree(str(t), str(e))
    assert n["skipped_special"] == 2 and n["errors"] == 0
    assert stat.S_IMODE(os.lstat(t / "p").st_mode) != 0o777
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o600
    with pytest.raises(D.DataError):
        D.chmod_777_subtree(str(tmp_path), str(e))


def test_rsync_outcomes_and_failed_paths(tmp_path):
    assert D.rsync_outcome(0) == "complete" and D.rsync_outcome(23) == "partial"
    assert D.rsync_outcome(24) == "partial" and D.rsync_outcome(12) == "failed"
    err = 'rsync: [sender] send_files failed to open "/home/alice/locked.db": Permission denied (13)\n'
    assert D.failed_paths(err) == ["/home/alice/locked.db"]
    h, e = mk_home(tmp_path), ext4(tmp_path)
    r = D.backup(str(e), execute=True, consent=True, user=user(h), mid="m1", du=lambda p: 1,
                 free=lambda p: 10 ** 9, runner=lambda a: (12, "", "rsync error: protocol"))
    assert r["status"] == "failed" and r["chmod"]["changed"] == 0          # no chmod after a failed copy
    assert json.load(open(e / "Data" / "Linux" / "m1" / "alice" / D.STATE))["status"] == "failed"
    r = D.backup(str(e), execute=True, consent=True, user=user(h), mid="m1", du=lambda p: 1,
                 free=lambda p: 10 ** 9, runner=lambda a: (23, "", err))
    assert r["status"] == "partial" and r["failed_paths"] == ["/home/alice/locked.db"]


@need_rsync
def test_restore_preview_nondeleting_modes_and_conflicts(tmp_path):
    h, e = mk_home(tmp_path), ext4(tmp_path)
    (h / "notes.cfg").write_text("from backup")
    os.utime(h / "notes.cfg", (1000, 1000))                        # backup copy is OLD
    D.backup(str(e), execute=True, consent=True, user=user(h), mid="m1", du=lambda p: 1, free=lambda p: 10 ** 9)
    h2 = tmp_path / "home2" / "alice"
    h2.mkdir(parents=True)
    (h2 / "only_here.txt").write_text("keep me")
    (h2 / "notes.cfg").write_text("NEWER LOCAL")
    u2 = user(h2)
    prev = D.restore(str(e), "Linux/m1/alice", user=u2)
    assert prev["executed"] is False and prev["deletes"] == 0 and prev["would_create"] >= 3
    assert not (h2 / ".bashrc").exists()                             # preview writes nothing
    with pytest.raises(D.DataError, match="RESTORE alice"):
        D.restore(str(e), "Linux/m1/alice", execute=True, confirm="yes", user=u2)
    r = D.restore(str(e), "Linux/m1/alice", execute=True, confirm="RESTORE alice", user=u2)
    assert r["status"] == "complete", r
    assert (h2 / "only_here.txt").read_text() == "keep me"            # never deleted
    assert (h2 / "notes.cfg").read_text() == "NEWER LOCAL"            # skip-newer default
    assert stat.S_IMODE(os.lstat(h2 / ".ssh" / "id_test").st_mode) == 0o600     # original, not 777
    assert stat.S_IMODE(os.lstat(h2 / ".ssh").st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(h2 / "run.sh").st_mode) == 0o750
    assert os.lstat(h2 / ".bashrc").st_uid == os.getuid()
    prev = D.restore(str(e), "Linux/m1/alice", conflict="overwrite", user=u2)
    assert "notes.cfg" in prev["overwrite_paths"]
    D.restore(str(e), "Linux/m1/alice", conflict="overwrite", execute=True, confirm="RESTORE alice", user=u2)
    assert (h2 / "notes.cfg").read_text() == "from backup" and (h2 / "only_here.txt").exists()


def test_restore_bad_id(tmp_path):
    e = ext4(tmp_path)
    for bad in ("../x/y", "Linux/m1", "Linux/../alice"):
        with pytest.raises(D.DataError):
            D.restore(str(e), bad, user=user(tmp_path))


def test_restore_modes_defaults_without_manifest(tmp_path):
    h = tmp_path / "h"
    (h / ".gnupg").mkdir(parents=True)
    (h / ".gnupg" / "k").write_text("x")
    (h / "doc").write_text("x")
    for p in (h / ".gnupg", h / ".gnupg" / "k", h / "doc"):
        os.chmod(p, 0o777)
    D.restore_modes(str(h), [".gnupg", ".gnupg/k", "doc", "../evil"], {"entries": {}}, 0o022)
    assert stat.S_IMODE(os.lstat(h / ".gnupg").st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(h / ".gnupg" / "k").st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(h / "doc").st_mode) == 0o644


def test_itemize_parse():
    out = ">f+++++++++ new.txt\n>f.st...... changed.txt\ncd+++++++++ dir/\ncL+++++++++ lnk -> x\n"
    r = D.parse_itemized(out)
    assert r["create"] == ["new.txt"] and r["overwrite"] == ["changed.txt"] and r["dirs"] == ["dir"]


def test_cross_os_names():
    bad = D.name_incompatibilities(["a/b:c.txt", "CON.txt", "x/End.", "Readme", "README"], "windows")
    probs = {b["path"] for b in bad}
    assert {"a/b:c.txt", "CON.txt", "x/End.", "README"} <= probs
    assert D.name_incompatibilities(["a:b"], "linux") == []
    assert D.name_incompatibilities(["a", "A"], "darwin")[0]["path"] == "A"


def test_robocopy_builders_and_exit_codes():
    b = D.robocopy_backup_argv(r"C:\Users\alice", r"X:\Data\Windows\m\alice\home", r"C:\t\log.txt")
    r = D.robocopy_restore_argv(r"X:\b", r"C:\Users\alice", "skip-newer", "l")
    for a in (b, r):
        assert "/E" in a and "/XJ" in a and not ({"/MIR", "/PURGE"} & set(a))
    assert "/XO" in r
    assert [D.robocopy_outcome(c) for c in (0, 1, 3, 7, 8, 16)] == \
        ["complete", "complete", "complete", "complete", "partial", "failed"]
    acl = D.windows_backup_acl_argv(r"X:\Data\Windows\m\alice")
    assert "*S-1-5-32-545:(OI)(CI)F" in acl and not any("Everyone" in x or "S-1-1-0" in x for x in acl)


def test_macos_rsync_capabilities():
    assert D.macos_rsync_flags("openrsync: protocol version 29") == (["-axH"], ["ACLs", "extended attributes"])
    assert D.macos_rsync_flags("rsync  version 2.6.9  protocol version 29") == (["-axHE"], ["ACLs"])
    f, lost = D.macos_rsync_flags("rsync  version 3.2.7  protocol version 31\n    ACLs, xattrs, iconv")
    assert f == ["-axHAX"] and lost == []
