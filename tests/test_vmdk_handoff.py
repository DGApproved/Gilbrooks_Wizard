"""VirtualBox descriptor + host<->VM handoff. VBoxManage is MOCKED (not installed here); no raw disk is opened."""
import json
import os

import pytest

from usbprep import vmdk, vmhandoff as H
from usbprep.safety import SafetyError

from helpers import blank_lsblk_device, prepared_lsblk_device, session_with

VBOX = "/opt/fake/VBoxManage"


def vbox_responses(version="7.1.4r165100", big=False, bad=False):
    def create(argv):
        out = argv[argv.index("--filename" if "--filename" in argv else "-filename") + 1]
        dev = next((a.split("=", 1)[1] for a in argv if a.startswith("RawDrive=")), argv[-1])
        with open(out, "w") as f:
            f.write("garbage" if bad else f'# Disk DescriptorFile\nversion=1\ncreateType="fullDevice"\n'
                                          f'RW 31277232 FLAT "{dev}" 0\n')
            if big:
                f.write("x" * (vmdk.MAX_DESCRIPTOR + 1))
        return 0, "", ""
    return {f"{VBOX} --version": (0, version + "\n", ""), f"{VBOX} createmedium": (create, None, None),
            f"{VBOX} internalcommands": (create, None, None), f"{VBOX} closemedium": (0, "", "")}


def setup(extra=None, dev=None):
    holder = [dev or prepared_lsblk_device()]
    s, r = session_with(holder, extra)
    return s, r, s.select("/dev/sdx", allow_ambiguous=True), holder


def call(s, dev, out, **kw):
    kw.setdefault("access", lambda p, m: True)
    return vmdk.prepare_vmdk(dev, out, runner=s.runner, lister=s.lister, vbox=VBOX, **kw)


def test_dry_run_v7_and_v6_commands(workdir):
    s, r, dev, _ = setup(vbox_responses())
    log = call(s, dev, str(workdir))
    cmd = next(l for l in log if l.startswith("command:"))
    assert "createmedium disk" in cmd and "--variant RawDisk" in cmd and "RawDrive=/dev/sdx" in cmd
    assert "DRY RUN" in log[-1] and os.listdir(workdir) == []
    s, r, dev, _ = setup(vbox_responses("6.1.50r161033"))
    assert "internalcommands createrawvmdk -filename" in next(l for l in call(s, dev, str(workdir))
                                                               if l.startswith("command:"))


def test_create_confirm_readme_and_current(workdir):
    s, r, dev, _ = setup(vbox_responses())
    with pytest.raises(vmdk.VmdkError, match="informed confirmation"):
        call(s, dev, str(workdir), execute=True)
    log = call(s, dev, str(workdir), execute=True, confirmed=True)
    vm, side = vmdk.names(dev)
    assert os.path.isfile(workdir / vm) and os.path.getsize(workdir / vm) < 4096
    assert json.load(open(workdir / side))["device"] == dev.identity()
    readme = (workdir / vm.replace(".vmdk", ".README.txt")).read_text()
    assert str(workdir / vm) in readme and "https://www.virtualbox.org/wiki/Downloads" in readme
    assert "NOT a\n  self-contained" in readme or "NOT a" in readme
    assert "Save the machine state" in readme and "--firmware efi" in readme and "BIOS" in readme
    assert "does NOT unmount" in readme and "regenerate" in readme
    assert any(l.startswith("step-by-step VirtualBox instructions:") for l in log)
    assert [c for c in r.calls if "closemedium" in c]                      # unregistered, not attached
    assert not any(c[1:2] in (["createvm"], ["startvm"], ["storageattach"], ["registervm"]) for c in r.calls)
    again = call(s, dev, str(workdir), execute=True, confirmed=True)
    assert any("nothing to do" in l for l in again)


def test_stale_and_foreign(workdir):
    s, r, dev, holder = setup(vbox_responses())
    call(s, dev, str(workdir), execute=True, confirmed=True)
    vm, side = vmdk.names(dev)
    rec = json.load(open(workdir / side))
    rec["layout"]["ptuuid"] = "OLD"
    json.dump(rec, open(workdir / side, "w"))
    with pytest.raises(vmdk.VmdkError, match="stale"):
        call(s, dev, str(workdir), execute=True, confirmed=True)
    call(s, dev, str(workdir), execute=True, confirmed=True, replace=True)
    assert os.path.exists(workdir / (vm + ".stale")) and vmdk.status(str(workdir), dev, s.inspect(dev)[0].fingerprint()) == "current"
    os.remove(workdir / side)
    with pytest.raises(vmdk.VmdkError, match="not created by usbprep"):
        call(s, dev, str(workdir), execute=True, confirmed=True, replace=True)


def test_refusals(workdir, monkeypatch):
    s, r, dev, _ = setup(vbox_responses(), dev=blank_lsblk_device())
    with pytest.raises(vmdk.VmdkError, match="prepared layout"):
        call(s, dev, str(workdir))
    s, r, dev, _ = setup(vbox_responses())
    with pytest.raises(vmdk.VmdkError, match="EFI:/AI"):
        call(s, dev, None)
    monkeypatch.setattr(vmdk.shutil, "which", lambda n: None)
    with pytest.raises(vmdk.VmdkError, match="virtualbox.org"):
        vmdk.prepare_vmdk(dev, str(workdir), runner=s.runner, lister=s.lister)
    log = call(s, dev, str(workdir), access=lambda p, m: False)
    assert any("'disk' group" in l for l in log)
    with pytest.raises(vmdk.VmdkError, match="no raw access"):
        call(s, dev, str(workdir), execute=True, confirmed=True, access=lambda p, m: False)
    s, r, dev, _ = setup(vbox_responses(big=True))
    with pytest.raises(vmdk.VmdkError, match="small descriptor"):
        call(s, dev, str(workdir / "b"), execute=True, confirmed=True)
    s, r, dev, _ = setup(vbox_responses(bad=True))
    with pytest.raises(vmdk.VmdkError, match="does not look like"):
        call(s, dev, str(workdir / "c"), execute=True, confirmed=True)


def test_find_ai_dir(workdir):
    import json as _j
    from usbprep.detect import parse_lsblk
    mnt = workdir / "efi"
    (mnt / "AI").mkdir(parents=True)
    (mnt / "AI" / "USBPREP_MANIFEST.json").write_text("{}")
    dev = parse_lsblk(_j.dumps({"blockdevices": [prepared_lsblk_device(mounts=(str(mnt), None, None))]}))[0]
    assert vmdk.find_ai_dir(dev, None, "/app") == str(mnt / "AI")
    assert vmdk.find_ai_dir(dev, "/dev/sdx", "/app") == "/app"


# ---- host <-> VM handoff ----
MOUNTED = prepared_lsblk_device(mounts=("/USB", "/USB/A.I.", "[SWAP]"))


def releasing(holder):
    def fn(argv):
        for ch in holder[0]["children"]:
            ch["mountpoints"] = [None]
        return 0, "", ""
    return fn


def test_handoff_dry_run_steps():
    s, r, dev, holder = setup(dev=json.loads(json.dumps(MOUNTED)))
    log = H.handoff(dev, s.runner, s.lister, busy=lambda m: [])
    text = "\n".join(log)
    assert text.index("umount /USB/A.I.") < text.index("umount /USB\n") or "umount /USB/A.I." in text
    assert "swapoff /dev/sdx3" in text and "blockdev --flushbufs /dev/sdx" in text and "DRY RUN" in text
    assert "USB device filter" in text
    assert not any(c[0] in ("umount", "swapoff") for c in r.calls) and H.read_marker(dev) is None


def test_handoff_execute_and_return(workdir):
    holder = [json.loads(json.dumps(MOUNTED))]
    vmstate = {"v": "poweroff"}
    extra = {f"VBoxManage showvminfo": (lambda a: (0, f'name="T"\nVMState="{vmstate["v"]}"\n', ""), None, None)}
    s, r = session_with(holder, extra)
    s.runner.responses["umount"] = (releasing(holder), None, None)
    s.runner.responses["swapoff"] = (releasing(holder), None, None)
    dev = s.select("/dev/sdx", allow_ambiguous=True)   # active swap => explicit override
    log = H.handoff(dev, s.runner, s.lister, execute=True, vm="T", busy=lambda m: [])
    order = [c[0] for c in r.calls if c[0] in ("umount", "swapoff", "sync", "blockdev")]
    assert order == ["umount", "umount", "swapoff", "sync", "blockdev"]
    assert [c for c in r.calls if c[0] == "umount"][0][-1] == "/USB/A.I."   # inner mount first
    assert H.read_marker(dev)["vm"] == "T" and any("released" in l for l in log)
    with pytest.raises(SafetyError):
        H.assert_host_owned(dev)
    # return: running / saved VM refused
    for st in ("running", "saved", "paused"):
        vmstate["v"] = st
        with pytest.raises(H.HandoffRefused, match="shut the guest down"):
            H.give_back(dev, s.runner, s.lister, busy=lambda p: [])
    vmstate["v"] = "poweroff"
    with pytest.raises(H.HandoffRefused, match="still open"):
        H.give_back(dev, s.runner, s.lister, busy=lambda p: ["pid 9: /dev/sdx"])
    log = H.give_back(dev, s.runner, s.lister, busy=lambda p: [])
    assert "returned to the host" in log[-1] and H.read_marker(dev) is None
    H.assert_host_owned(dev)


def test_handoff_refusals():
    s, r, dev, holder = setup(dev=json.loads(json.dumps(MOUNTED)))
    with pytest.raises(H.HandoffRefused, match="host copy"):
        H.handoff(dev, s.runner, s.lister, execute=True, app_root="/USB/AI", busy=lambda m: [])
    with pytest.raises(H.HandoffRefused, match="close these programs"):
        H.handoff(dev, s.runner, s.lister, execute=True, busy=lambda m: ["pid 5: /USB/A.I./file"])
    # automount re-mounted it: still mounted after unmount => no marker
    with pytest.raises(H.HandoffRefused, match="still mounted"):
        H.handoff(dev, s.runner, s.lister, execute=True, busy=lambda m: [])
    assert H.read_marker(dev) is None
    s, r, dev, holder = setup(dev=blank_lsblk_device())
    with pytest.raises(H.HandoffRefused, match="prepared layout"):
        H.handoff(dev, s.runner, s.lister, busy=lambda m: [])


def test_return_requires_ack_and_detects_layout_change():
    holder = [prepared_lsblk_device()]
    s, r = session_with(holder)
    dev = s.select("/dev/sdx")
    with pytest.raises(H.HandoffRefused, match="not marked"):
        H.give_back(dev, s.runner, s.lister, busy=lambda p: [])
    H.handoff(dev, s.runner, s.lister, execute=True, busy=lambda m: [])
    with pytest.raises(H.HandoffRefused, match="confirm the VM"):
        H.give_back(dev, s.runner, s.lister, busy=lambda p: [])
    holder[0]["children"][1]["label"] = "CHANGED"       # guest reformatted p2
    with pytest.raises(H.HandoffRefused, match="layout changed"):
        H.give_back(dev, s.runner, s.lister, vm_powered_off_ack=True, busy=lambda p: [])
    assert H.read_marker(dev) is not None                 # still VM-owned => host mount stays blocked
    holder[0]["serial"] = "OTHER"
    with pytest.raises(H.HandoffRefused, match="identity"):
        H.give_back(dev, s.runner, s.lister, vm_powered_off_ack=True, busy=lambda p: [])


def test_parse_vbox_version_with_vboxdrv_warning():
    from usbprep.vmdk import parse_vbox_version
    out = ("WARNING: The vboxdrv kernel module is not loaded. Either there is no module\n"
           "         available for the current kernel (6.8.0-49-generic) or it failed to\n"
           "         load. Please recompile the kernel module and install it by\n\n"
           "           sudo /sbin/vboxconfig\n\n"
           "         You will not be able to start VMs until this problem is fixed.\n"
           "7.0.14_Ubuntur161095\n")
    v, warn = parse_vbox_version(out)
    assert v == "7.0.14_Ubuntur161095"
    assert "vboxdrv" in warn


def test_parse_vbox_version_plain_and_garbage():
    import pytest
    from usbprep.vmdk import parse_vbox_version, VmdkError
    assert parse_vbox_version("6.1.50r161033\n") == ("6.1.50r161033", "")
    for bad in ("", "WARNING: something broke\n"):
        with pytest.raises(VmdkError):
            parse_vbox_version(bad)
