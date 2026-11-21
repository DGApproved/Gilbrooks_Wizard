"""Java GUI self-test (headless via xvfb-run): menus, full-format default, Clone/Scripts argv builders."""
import glob
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAR = os.path.join(ROOT, "gui-java", "dist", "usbprep-gui.jar")
def _has_gui_awt(java):
    """Skip headless-only JREs (no libawt_xawt), which cannot create Swing frames even under Xvfb."""
    home = os.path.dirname(os.path.dirname(os.path.realpath(java)))
    return bool(glob.glob(os.path.join(home, "lib", "**", "libawt_xawt.*"), recursive=True))


JAVAS = [j for j in dict.fromkeys([shutil.which("java")] + glob.glob("/home/ubuntu/.tmp/j8/*/bin/java")
                                  + glob.glob("/home/ubuntu/.tmp/jdk*/bin/java")) if j and _has_gui_awt(j)]
_XVFB = {}


def _have_display():
    return bool(shutil.which("xvfb-run") and shutil.which("xauth")) or bool(shutil.which("Xvfb"))


def _gui(java, state_file):
    """Run the GUI self-test headless: xvfb-run when usable, else a private Xvfb display."""
    if shutil.which("xvfb-run") and shutil.which("xauth"):
        cmd, env = ["xvfb-run", "-a", java], None
    else:
        if "p" not in _XVFB:
            import time
            _XVFB["p"] = subprocess.Popen(["Xvfb", ":93", "-screen", "0", "1280x1024x24"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        cmd, env = [java], dict(os.environ, DISPLAY=":93")
    out = subprocess.run(cmd + ["-jar", JAR, "--self-test", str(state_file)], capture_output=True, text=True,
                         timeout=120, env=env).stdout
    return json.loads(out.strip().splitlines()[-1])

STATE = {"os": "linux", "source_disk": "/dev/sdb", "default_selection": "/dev/sdb", "error": None, "devices": [
    {"path": "/dev/sdb", "model": "SrcDisk", "serial": "AAA1", "size_human": "58.6 GiB", "status": "prepared",
     "confirm_token": "ERASE AAA1"},
    {"path": "/dev/sdc", "model": "FriendDisk", "serial": "BBB2", "size_human": "58.6 GiB", "status": "prepared",
     "confirm_token": "ERASE BBB2"}]}


@pytest.mark.skipif(not (JAVAS and _have_display() and os.path.exists(JAR)), reason="java/xvfb-run/jar missing")
@pytest.mark.parametrize("java", JAVAS)
def test_gui_selftest(java, tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(STATE))
    r = _gui(java, st)
    assert r["menus"] == ["Extras", "Clone", "Scripts", "Data"]
    assert r["data_items"][0] == "Backup current user to /Data..." and not any("[disabled]" in x for x in r["data_items"])
    assert r["data_backup_dry"] == "data backup --device /dev/sdb --json"
    assert r["data_backup_exec"].endswith("--execute --i-understand-777")
    assert "--allow-foreign-os" in r["data_restore_exec"] and "--confirm RESTORE alice" in r["data_restore_exec"]
    assert r["full_format_label"] == "Full format (slower; writes the entire partition)"
    assert r["dry_run_default"] is True and r["erase_ack_default"] is False
    assert "--full-format" not in r["exec_ok"]                     # unchecked = quick
    assert r["exec_ok_full"].endswith("--full-format")
    assert r["dry_prepare"] == "prepare /dev/sdb"
    # Scripts: exact names, nothing runs on population
    assert "my script \u00e9.sh" in r["script_items"] and "open_terminal_in_ext4.sh" in r["script_items"]
    assert r["scripts_running_after_populate"] == 0
    assert r["script_args"] == "scripts|run|my script \u00e9.sh|--device|/dev/sdb|--i-trust-this-script"
    # Clone
    assert r["clone_token"] == "REPLACE BBB2"
    assert r["clone_dry"] == "clone /dev/sdb /dev/sdc"
    assert r["clone_exec"].startswith("clone /dev/sdb /dev/sdc --execute --confirm REPLACE BBB2")
    assert r["clone_same"].startswith("REFUSED") and r["clone_to_source"].startswith("REFUSED")
    assert r["clone_wrong_token"].startswith("REFUSED")


@pytest.mark.skipif(not (JAVAS and _have_display() and os.path.exists(JAR)), reason="java/xvfb-run/jar missing")
def test_gui_data_menu_disabled_on_windows(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(dict(STATE, os="windows")))
    r = _gui(JAVAS[0], st)
    assert all(x.endswith("[disabled]") for x in r["data_items"])


GUI_OK = pytest.mark.skipif(not (JAVAS and _have_display() and os.path.exists(JAR)), reason="java/display/jar missing")


@GUI_OK
def test_gui_mount_initialize_linux(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(STATE))
    r = _gui(JAVAS[0], st)
    # Linux: no backend checkboxes visible or ticked
    assert all(v == [False, False] for v in r["backend_boxes"].values())
    # default selection /dev/sdb is prepared, kernel backend READY -> Mount enabled, button reads Re-initialize
    assert r["mount_enabled"] is True and r["init_label"] == "Re-initialize" and r["init_enabled"] is True
    assert r["control_table"] == [
        "true|Re-initialize|true",      # linux, prepared, READY
        "false|Initialize|true",        # linux, unprepared -> cannot mount, can initialize
        "false|Initialize|false",       # busy -> everything disabled
        "false|Re-initialize|false",    # windows, prepared, backend MISSING -> no mount, no init
        "true|Re-initialize|false",     # darwin, prepared, READY backend -> mount only
        "false|Initialize|false",       # darwin unprepared
        "false|Initialize|false"]       # no drive selected
    assert r["exclusive"] == ["wsl", "native"]
    assert r["revalidate"][0] == "OK"
    assert "stale" in r["revalidate"][1] and "SWAPPED" in r["revalidate"][1]
    assert "no longer connected" in r["revalidate"][2]


@GUI_OK
def test_gui_windows_defaults_native_and_mount_disabled_when_missing(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(dict(STATE, os="windows", backend={"choice": "native", "state": "MISSING-DEPENDENCY",
                                                                 "read_write": None, "message": "Ext4Fsd not found"})))
    r = _gui(JAVAS[0], st)
    b = r["backend_boxes"]
    assert b["wsl"] == [True, False] and b["custom_win"] == [True, False]     # visible, unticked by default
    assert b["readonly"][0] is False and b["homebrew"][0] is False and b["custom_mac"][0] is False
    assert r["mount_enabled"] is False and r["init_enabled"] is False
    assert "MISSING" in r["backend_label"] and "untested" in r["backend_label"]


@GUI_OK
def test_gui_macos_readonly_choice_shown(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(dict(STATE, os="darwin", backend={"choice": "readonly", "state": "READY",
                                                                "read_write": False, "message": "ext4fuse"})))
    r = _gui(JAVAS[0], st)
    b = r["backend_boxes"]
    assert b["readonly"] == [True, True] and b["homebrew"] == [True, False] and b["custom_mac"] == [True, False]
    assert b["wsl"][0] is False
    assert "READ-ONLY" in r["backend_label"]
    assert r["mount_enabled"] is True and r["init_enabled"] is False   # Initialize is Linux-only


def _star_dev(path, serial, status, starred="", allowed=True, refusal="", detected=False, layout="blank", serial_ok=True):
    return {"path": path, "model": "Elements", "serial": serial, "size_human": "931 GiB", "status": status,
            "confirm_token": "ERASE " + serial[-4:], "starred": starred, "star_allowed": allowed, "star_refusal": refusal,
            "detected_dedicated": detected, "serial_present": serial_ok, "layout": layout,
            "star_session_key": "session|" + path + "|Elements||1"}


STAR_BEFORE = {"os": "linux", "source_disk": None, "default_selection": None, "error": None, "devices": [
    _star_dev("/dev/sdh", "HDD1", "excluded", layout="unknown"),
    _star_dev("/dev/sdt", "TINY", "excluded", allowed=False, refusal="cannot be starred: too small: 1.0 GiB"),
    _star_dev("/dev/sdf", "FLSH", "eligible", detected=True)]}
STAR_AFTER = {"os": "linux", "source_disk": None, "default_selection": None, "error": None, "devices": [
    _star_dev("/dev/sdh", "HDD1", "eligible", starred="saved", layout="prepared"),
    _star_dev("/dev/sdk", "", "eligible", starred="session", serial_ok=False)]}


@GUI_OK
def test_gui_star_button_states_and_labels(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(STAR_BEFORE))
    r = {x["path"]: x for x in _gui(JAVAS[0], st)["stars"]}
    h, t, f = r["/dev/sdh"], r["/dev/sdt"], r["/dev/sdf"]
    assert h["enabled"] is True and h["text"] == "\u2606" and "(not detected as dedicated USB)" in h["label"]
    assert h["mount"] is False and h["init"] is False          # excluded until starred
    assert t["enabled"] is False and "too small" in t["tooltip"]  # refused drive: star disabled + reason
    assert f["enabled"] is True and "not detected" not in f["label"] and "\u2605" not in f["label"]
    assert f["init"] is True and f["init_label"] == "Initialize"
    assert h["args_on"] == "star /dev/sdh --yes" and h["args_off"] == "unstar /dev/sdh"


@GUI_OK
def test_gui_star_toggled_enables_mount_and_initialize(tmp_path):
    st = tmp_path / "state.json"
    st.write_text(json.dumps(STAR_AFTER))
    out = _gui(JAVAS[0], st)
    r = {x["path"]: x for x in out["stars"]}
    h, k = r["/dev/sdh"], r["/dev/sdk"]
    assert h["text"] == "\u2605" and h["enabled"] is True and h["label"].rstrip().endswith("\u2605")
    assert h["mount"] is True and h["init"] is True and h["init_label"] == "Re-initialize"   # starred prepared drive
    assert k["text"] == "\u2605" and "\u2605 (this session)" in k["label"] and "session only" in k["tooltip"]
    assert k["mount"] is False and k["init"] is True and k["init_label"] == "Initialize"     # starred blank drive
    assert "ERASE EVERYTHING" in out["star_warning"] and "flash drives" in out["star_warning"]
    assert out["star_none"].startswith("false|")
    assert json.loads(out["session_env"]) == ["session|/dev/sdq|x|y|1"]
