"""Headless CLI. Every destructive or privileged command is a DRY RUN unless --execute is given."""
import argparse
import json
import os
import sys

from . import __version__, constants as C
from .detect import human
from .platforms import CAPABILITIES, UnsupportedOperation, current_os, fail_closed, matrix_markdown
from .safety import SafetyError
from .executor import ConfirmationError
from .cache import CacheError
from .vmdk import VmdkError
from .vmhandoff import HandoffRefused
from .data import DataError
from .customdriver import CustomDriverError as _CDErr
from .backends import ReadOnlyBackend as _ROErr


def _session():
    from .workflow import LinuxSession
    return LinuxSession()


def _print_devices(devs, as_json):
    if as_json:
        print(json.dumps([d.to_dict() for d in devs], indent=1))
        return
    if not devs:
        print("No USB storage devices detected. A dedicated USB flash drive "
              f"(>= {human(C.MIN_DEVICE_SIZE)}) is required.")
        return
    for d in devs:
        mark = {"saved": "  ★", "session": "  ★ (this session)"}.get(d.starred, "")
        note = "" if d.detected_status == "eligible" or d.starred else "  (not detected as dedicated USB)"
        print(f"[{d.status.upper():9}] {d.path}  {d.model or '?'}  serial={d.serial or '?'}  "
              f"{d.size} B ({d.size_human}){mark}{note}")
        for r in d.reasons:
            print(f"             - {r}")


def _list_other_os(osname):
    import subprocess
    if osname == "windows":
        from .platforms import WINDOWS_DETECT_PS, parse_windows_disks
        out = subprocess.run(["powershell", "-NoProfile", "-Command", WINDOWS_DETECT_PS],
                             capture_output=True, text=True).stdout
        from .detect import usb_candidates
        return usb_candidates(parse_windows_disks(out or "[]"))
    if osname == "darwin":
        import plistlib
        from .platforms import parse_macos_diskinfo
        lst = plistlib.loads(subprocess.run(["diskutil", "list", "-plist", "external", "physical"],
                                            capture_output=True).stdout)
        from .detect import usb_candidates
        return usb_candidates([parse_macos_diskinfo(subprocess.run(["diskutil", "info", "-plist", d],
                                                                   capture_output=True).stdout)
                               for d in lst.get("WholeDisks", [])])
    raise UnsupportedOperation(f"unsupported OS {osname}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="usbprep", description="Project 1 USB preparation and mounting tool "
                                 "(dry-run by default; real changes need --execute).")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="list USB storage candidates")
    p.add_argument("--json", action="store_true")
    sub.add_parser("capabilities", help="print the platform support matrix")
    sub.add_parser("gui", help="start the Java 8+ Swing desktop GUI (gui-java/dist/usbprep-gui.jar)")
    sub.add_parser("gui-state", help="JSON for the GUI: OS, candidates, verified source drive, preselection")
    p = sub.add_parser("star", help="mark a USB drive that detection missed as a dedicated USB drive (saved by "
                       "serial/model/vendor/size, never by /dev path)")
    p.add_argument("device", help="whole-disk path, e.g. /dev/sdb")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--session", action="store_true",
                   help="drive without a serial: print a session-only key (USBPREP_SESSION_STARS); nothing is saved")
    p = sub.add_parser("unstar", help="remove a saved star (by device path, identity key, or serial)")
    p.add_argument("target")
    p = sub.add_parser("stars", help="list saved star marks")
    p.add_argument("--json", action="store_true")

    def dev_cmd(name, hlp):
        q = sub.add_parser(name, help=hlp)
        q.add_argument("device", help="whole-disk path, e.g. /dev/sdb (never auto-selected)")
        q.add_argument("--allow-ambiguous", action="store_true",
                       help="I acknowledge the device type is uncertain and it is a dedicated flash drive")
        q.add_argument("--execute", action="store_true", help="really perform the operation (default: dry run)")
        q.add_argument("--privilege", choices=["sudo", "pkexec"], help="approved elevation method for root steps")
        return q
    def full_flag(q):
        q.add_argument("--full-format", action="store_true",
                       help="Full format (slower; writes the entire partition): zero-fill every partition before "
                            "creating filesystems. Default = quick format. Not a secure erase; no media verification")

    def boot_flags(q):
        q.add_argument("--boot-content", choices=["preserve", "fresh"],
                       help="required when the running USB's boot example (AI/scr/BIOS, AI/src/EFI) has user changes: "
                            "preserve = copy user updates, fresh = stock Hello World")
        q.add_argument("--build-consent", action="store_true",
                       help="I consent to rebuilding stale/unbuilt edited boot sources with the approved toolchain "
                            "(nasm / x86_64-w64-mingw32-gcc fixed commands; edited build.sh is never executed)")
    dev_cmd("inspect", "show layout and whether the drive is already prepared")
    q = dev_cmd("prepare", "partition+format (ALL DATA DELETED) unless already prepared")
    q.add_argument("--confirm", help="typed confirmation token shown in the dry run, e.g. 'ERASE 1A2B'")
    full_flag(q)
    for name in ("mount", "run"):
        q = dev_cmd(name, "mount FAT32 at /USB, copy app to /USB/AI, mount ext4 at /USB/A.I., open file manager"
                    if name == "mount" else "prepare if needed, then mount (same flags as prepare+mount)")
        q.add_argument("--update-app", action="store_true", help="replace an older app copy in EFI:/AI")
        q.add_argument("--allow-ro-fallback", action="store_true", help="mount an unclean ext4 read-only")
        q.add_argument("--fix-owner", action="store_true",
                       help="chown the ext4 top-level directory (only) to you if owned by another uid")
        q.add_argument("--no-open", action="store_true")
        q.add_argument("--no-fsck", action="store_true", help="skip the automatic pre-mount e2fsck -p (not recommended)")
        q.add_argument("--full-repair", action="store_true",
                       help="I consent to a full e2fsck -f -y repair if the safe preen pass cannot fix errors")
        if name == "run":
            q.add_argument("--confirm")
            full_flag(q)
    q = dev_cmd("duplicate", "prepare ANOTHER drive and copy this setup app to it (no ext4 content is cloned)")
    q.add_argument("--confirm")
    boot_flags(q)
    full_flag(q)
    q.add_argument("--update-app", action="store_true")
    q = dev_cmd("check", "pre-mount ext4 check/repair (unmounted only); optional read-only bad-block scan")
    q.add_argument("--full-repair", action="store_true", help="consent to e2fsck -f -y if preen cannot fix errors")
    q.add_argument("--scan-bad-blocks", action="store_true", help="opt-in READ-ONLY badblocks scan (slow, wears nothing)")
    q.add_argument("--mark-bad-blocks", action="store_true",
                   help="consent to record unreadable blocks in the ext4 bad-block inode (does not fix failing flash)")
    dev_cmd("check-drive", "Extras > Check drive for errors: NON-mutating e2fsck -fn + fsck.vfat -n")
    q = dev_cmd("repair", "Extras > Auto repair detected errors: e2fsck -p; full -f -y only with --full-repair")
    q.add_argument("--full-repair", action="store_true", help="consent to e2fsck -f -y if preen cannot fix errors")
    q = dev_cmd("reformat", "Extras > Reformat: back up EFI:/AI to /tmp, relaunch from there, re-prepare the "
                "drive (ext4 user data IS ERASED, not backed up), restore the software")
    q.add_argument("--confirm")
    q.add_argument("--wait-pid", type=int, action="append", default=[],
                   help="extra process (e.g. the Java GUI) that must exit before formatting starts")
    q.add_argument("--monitor", action="store_true", help="show a progress window run from the /tmp copy")
    full_flag(q)
    q = sub.add_parser("clone", help="Clone: copy ONLY the ext4 (A.I.) partition contents from one prepared USB to "
                       "another (destination A.I. data is replaced; FAT32/swap/UUIDs untouched)")
    q.add_argument("source", help="prepared source USB, e.g. /dev/sdb (read only)")
    q.add_argument("destination", help="prepared destination USB, e.g. /dev/sdc")
    q.add_argument("--allow-ambiguous", action="store_true")
    q.add_argument("--execute", action="store_true")
    q.add_argument("--confirm", help="typed token shown in the dry run, e.g. 'REPLACE 1A2B'")
    q.add_argument("--privilege", choices=["sudo", "pkexec"])
    da = sub.add_parser("data", help="Data menu: back up the signed-in user's home to <ext4>/Data and restore it "
                        "(Linux only; disabled on Windows/macOS - ext4 blocked)").add_subparsers(dest="dcmd", required=True)
    for n in ("list", "backup", "restore", "restore-shared"):
        q = da.add_parser(n)
        q.add_argument("--device", required=True, help="selected USB; its ext4 must be mounted at /USB/A.I.")
        q.add_argument("--json", action="store_true")
        if n == "restore-shared":
            q.add_argument("source", help="machine/user of a Shared source as shown by 'data list' (Shared:<machine>/<user>)")
            q.add_argument("--category", action="append", choices=["Music", "Pictures", "Videos", "Documents"])
        if n == "restore":
            q.add_argument("backup_id", help="OS/machine/user as shown by 'data list'")
            q.add_argument("--allow-foreign-os", action="store_true",
                           help="restore another OS's specific files (copied as files only, not made runnable)")
        if n.startswith("restore"):
            q.add_argument("--conflict", choices=["skip-newer", "skip-existing", "overwrite"], default="skip-newer",
                           help="existing files in your home: keep newer (default), keep all, or overwrite")
        if n.startswith("restore"):
            q.add_argument("--confirm", help="typed token shown in the preview, e.g. 'RESTORE alice'")
        if n != "list":
            q.add_argument("--execute", action="store_true")
        if n == "backup":
            q.add_argument("--i-understand-777", dest="consent", action="store_true",
                           help="I read the warning: backup copies get mode 777 and may contain secrets")
    bk = sub.add_parser("backend", help="ext4 access backend. Windows: native Ext4Fsd (default) | wsl | custom; "
                        "macOS: native=blocked (default) | homebrew (anylinuxfs RW) | readonly (ext4fuse) | custom. "
                        "Mutually exclusive; selecting never installs, runs or remounts").add_subparsers(dest="bcmd", required=True)
    q = bk.add_parser("status", help="which backend would be used and whether it is usable (never falls back)")
    q.add_argument("--choice"); q.add_argument("--fat-root")
    q.add_argument("--os", dest="target_os", choices=("linux", "windows", "darwin"), help=argparse.SUPPRESS)
    q.add_argument("--json", action="store_true")
    q = bk.add_parser("set", help="save the explicit choice for this OS (one of the choices above)")
    q.add_argument("choice")
    q.add_argument("--os", dest="target_os", choices=("windows", "darwin"), help=argparse.SUPPRESS)
    q = bk.add_parser("custom-driver", help="run /AI/Driver/<OS>/ext4.sh ONCE after explicit trust consent, then "
                      "verify the mount point independently")
    q.add_argument("operation", choices=("mount", "unmount", "status"))
    q.add_argument("--device", required=True); q.add_argument("--mount-point", required=True)
    q.add_argument("--fat-root"); q.add_argument("--trust-script", metavar="EXACT_PATH",
                   help="required: the exact script path shown by 'backend status', as consent to run it")
    q.add_argument("--os", dest="target_os", choices=("windows", "darwin"), help=argparse.SUPPRESS)
    sc = sub.add_parser("scripts", help="Scripts menu: AI/Scripts/<this OS>").add_subparsers(dest="scmd", required=True)
    q = sc.add_parser("list"); q.add_argument("--ai"); q.add_argument("--json", action="store_true")
    q = sc.add_parser("run", help="run ONE explicitly chosen script as the normal user")
    q.add_argument("name", help="exact file name as listed"); q.add_argument("--ai")
    q.add_argument("--device", help="selected USB (to pass its verified ext4 mount path)")
    q.add_argument("--i-trust-this-script", dest="consent", action="store_true",
                   help="required: I trust this script (usbprep does not check what it does)")
    q = dev_cmd("vmdk", "Extras > Prepare VMDK file for VirtualBox: raw-disk descriptor in EFI:/AI/Cache/Virtual "
                "(a pointer to the USB, not a copy)")
    q.add_argument("--confirm-raw-disk", action="store_true",
                   help="I read the raw-disk warning (no simultaneous host/VM use; USB must stay connected)")
    q.add_argument("--replace", action="store_true", help="regenerate a stale/current usbprep descriptor")
    q = dev_cmd("vm-handoff", "VirtualBox loop: release the drive from the host (unmount FAT32+ext4, swapoff, flush) "
                "before YOU start the VM that uses the raw-disk .vmdk")
    q.add_argument("--vm", help="VM name; if given it must be powered off now (checked with VBoxManage)")
    q = dev_cmd("vm-return", "VirtualBox loop: take the drive back after the VM is fully powered off, then fsck + mount")
    q.add_argument("--vm", help="VM name to verify it is powered off (poweroff/aborted, not saved)")
    q.add_argument("--vm-powered-off", action="store_true", help="I confirm the VM is fully powered off")
    q.add_argument("--no-open", action="store_true")
    sub.add_parser("host-copy", help="copy this app to a private /tmp folder and print how to run it from there "
                   "(needed before handing over the drive the app runs from)")
    q = sub.add_parser("reformat-resume", help=argparse.SUPPRESS)
    q.add_argument("job")
    q.add_argument("--monitor", action="store_true")
    q = sub.add_parser("unmount", help="unmount /USB/A.I. and /USB (optionally power off the drive)")
    q.add_argument("device", nargs="?")
    q.add_argument("--eject", action="store_true")
    q.add_argument("--execute", action="store_true")
    q.add_argument("--privilege", choices=["sudo", "pkexec"])
    ca = sub.add_parser("cache", help="offline prerequisite installer cache in EFI:/AI/Cache/<OS>").add_subparsers(
        dest="ccmd", required=True)
    for n in ("status", "add", "fetch-java"):
        q = ca.add_parser(n)
        q.add_argument("--ai", help="AI folder (default: the folder this app runs from)")
        q.add_argument("--os", choices=["linux", "darwin", "windows"])
        q.add_argument("--arch", help="x64 / aarch64 (default: this machine)")
        if n == "add":
            q.add_argument("file")
            q.add_argument("--version", required=True)
            q.add_argument("--sha256", required=True, help="the VENDOR's published checksum (not one you computed)")
            q.add_argument("--source", default="manual")
            q.add_argument("--license", default="see vendor")
        if n == "fetch-java":
            q.add_argument("--major", type=int, default=8)
            q.add_argument("--execute", action="store_true", help="really download (default: show what would be fetched)")
    im = sub.add_parser("image", help="safe practice mode on a regular image file").add_subparsers(dest="icmd", required=True)
    q = im.add_parser("create"); q.add_argument("path"); q.add_argument("--size-mib", type=int, default=6144)
    q = im.add_parser("inspect"); q.add_argument("path")
    q = im.add_parser("prepare"); q.add_argument("path"); q.add_argument("--execute", action="store_true"); q.add_argument("--confirm")
    boot_flags(q); full_flag(q); q.add_argument("--source-ai", help="treat this folder as the running USB's AI folder (practice mode)")
    q = sub.add_parser("boot-status", help="boot example on the running USB vs the stock baseline (JSON)")
    q.add_argument("--ai", help="AI folder to inspect (default: the running verified source USB)")
    q = im.add_parser("install-app"); q.add_argument("path"); q.add_argument("--update-app", action="store_true")
    a = ap.parse_args(argv)

    try:
        return _dispatch(a)
    except (SafetyError, ConfirmationError, UnsupportedOperation, CacheError, VmdkError, HandoffRefused, DataError, ValueError,
            _CDErr, _ROErr) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # report clearly, never a silent partial success
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _dispatch(a):
    osname = current_os()
    if a.cmd == "capabilities":
        print(matrix_markdown())
        return 0
    if a.cmd == "gui":
        return _launch_java_gui()
    if a.cmd == "gui-state":
        print(json.dumps(_gui_state(osname), indent=1))
        return 0
    if a.cmd in ("star", "unstar", "stars"):
        return _stars_cmd(a, osname)
    if a.cmd == "cache":
        return _cache(a, osname)
    if a.cmd == "scripts":
        return _scripts(a, osname)
    if a.cmd == "backend":
        return _backend(a, osname)
    if a.cmd == "data":
        return _data(a, osname)
    if a.cmd == "boot-status":
        from . import bootcontent as B
        ai = a.ai or (_session().boot_source if osname == "linux" else None)
        rep = B.detect(ai)
        for comp, r in rep.items():
            if r["dirs"]:
                r["build"] = B.build_state(os.path.join(ai, *r["dirs"][0].split("/")), comp)
        print(json.dumps({"ai": ai, "modified": B.is_modified(rep), "components": rep,
                          "stock_integrity": B.verify_stock() or "ok"}, indent=1))
        return 0
    if a.cmd == "image":
        from . import workflow as w
        from .layout import check_layout, read_image_layout
        if a.icmd == "create":
            w.create_image(a.path, a.size_mib * C.MiB)
            print(f"created sparse image {a.path} ({a.size_mib} MiB)")
            return 0
        if a.icmd == "inspect":
            print(check_layout(read_image_layout(a.path)).summary())
            return 0
        if a.icmd == "prepare":
            from .bootcontent import BootChoiceRequired
            try:
                r = w.fresh_install_image(a.path, a.execute, a.confirm, _boot_choice(a, a.source_ai), a.source_ai,
                                          a.build_consent, full=a.full_format)
            except BootChoiceRequired as e:
                print(f"STOPPED before any write: {e}", file=sys.stderr)
                return 3
            print("\n".join(r.log))
            if not a.execute and not r.log[0].startswith("Image already"):
                print(f"\nTo execute type: --execute --confirm '{w.image_token(a.path)}'")
            return 0 if r.ok else 1
        print("\n".join(w.install_app_image(a.path, a.update_app)))
        return 0
    if osname != "linux":
        if a.cmd == "list":
            _print_devices(_list_other_os(osname), a.json)
            return 0
        fail_closed(osname, {"inspect": "detect prepared layout", "check": "detect prepared layout",
                             "check-drive": "detect prepared layout", "vmdk": "detect prepared layout",
                             "vm-handoff": "VirtualBox raw-disk VMDK descriptor", "vm-return": "VirtualBox raw-disk VMDK descriptor",
                             "host-copy": "copy app to EFI:/AI", "repair": "detect prepared layout",
                             "reformat": "prepare/format (physical USB)", "reformat-resume": "prepare/format (physical USB)",
                             "duplicate": "prepare/format (physical USB)", "prepare": "prepare/format (physical USB)",
                             "mount": "ext4 RW+exec mount at literal /USB/A.I.", "run": "prepare/format (physical USB)",
                             "unmount": "unmount/eject", "clone": "Clone ext4 partition USB -> USB"}[a.cmd])
    if a.cmd == "host-copy":
        import tempfile
        from .appcopy import build_staging, collect_app_files, APP_ROOT
        d = os.path.join(tempfile.mkdtemp(prefix="usbprep-host-", dir="/tmp"), "AI")
        build_staging(d, APP_ROOT)
        print(f"copied {len(collect_app_files(APP_ROOT))} files to {d}\nClose the app on the USB, then run:\n  sh {d}/launchers/Linux/run.sh")
        return 0
    if a.cmd == "reformat-resume":
        return _reformat_resume(a)
    s = _session()
    if a.cmd == "list":
        _print_devices(s.candidates(), a.json)
        return 0
    if a.cmd == "unmount":
        dev = s.select(a.device, True) if a.device else None
        r = s.unmount(dev, a.execute, a.privilege, a.eject)
        print("\n".join(r.log))
        return 0 if r.ok else 1
    if a.cmd == "clone":
        from .clone import CloneJob, CloneError
        src, dst = s.select(a.source, a.allow_ambiguous), s.select(a.destination, a.allow_ambiguous)
        try:
            print("\n".join(CloneJob(s, src, dst).run(a.execute, a.confirm, a.privilege)))
        except (CloneError, RuntimeError) as e:
            print(f"CLONE REFUSED/FAILED: {e}", file=sys.stderr)
            return 1
        return 0
    dev = s.select(a.device, a.allow_ambiguous)
    print(f"Selected: {dev.path} {dev.model} serial={dev.serial or '?'} {dev.size} B ({dev.size_human})")
    if a.cmd == "inspect":
        lay, chk = s.inspect(dev)
        print(chk.summary())
        return 0
    if a.cmd == "duplicate":
        print(f"WARNING: ALL DATA ON {dev.path} ({dev.model}, {dev.size_human}) WILL BE DELETED unless already prepared.")
        from .bootcontent import BootChoiceRequired
        try:
            print("\n".join(s.duplicate_to(dev, a.execute, a.confirm, a.privilege, a.allow_ambiguous, a.update_app,
                                           boot_content=_boot_choice(a, s.boot_source), build_consent=a.build_consent,
                                           full=a.full_format)))
        except BootChoiceRequired as e:
            print(f"STOPPED before any write: {e}", file=sys.stderr)
            return 3
        if not a.execute:
            print(f"\nTo execute: add --execute --confirm '{dev.confirm_token()}' --privilege sudo")
        return 0
    if a.cmd == "check":
        if not a.execute:
            print("DRY RUN - would run: e2fsck -p on the AI partition (unmounted only)"
                  + ("; e2fsck -f -y if needed" if a.full_repair else "")
                  + ("; badblocks read-only scan" if a.scan_bad_blocks else "")
                  + ("; e2fsck -l <list> to mark unreadable blocks" if a.mark_bad_blocks else ""))
            return 0
        print("\n".join(s.check_ext4(dev, a.privilege, a.full_repair, a.scan_bad_blocks, a.mark_bad_blocks)))
        return 0
    if a.cmd in ("check-drive", "repair"):
        if not a.execute:
            print("DRY RUN - would run (unmounted ext4 only; ext4 at /USB/A.I. is unmounted and remounted only if clean): "
                  + ("e2fsck -f -n (no changes) + fsck.vfat -n on the FAT32 partition if it is not mounted"
                     if a.cmd == "check-drive" else
                     "e2fsck -p (safe automatic repair)" + ("; e2fsck -f -y if needed (consented)" if a.full_repair
                                                            else "; stops and asks if a full repair is needed")))
            print("Add --execute --privilege pkexec|sudo to run it.")
            return 0
        log = (s.check_drive(dev, a.privilege) if a.cmd == "check-drive"
               else s.repair_drive(dev, a.privilege, a.full_repair))
        print("\n".join(log))
        return 0
    if a.cmd == "reformat":
        return _reformat(a, s, dev)
    if a.cmd in ("vm-handoff", "vm-return"):
        from . import vmhandoff as H
        from .appcopy import APP_ROOT
        if a.cmd == "vm-handoff":
            print("\n".join(H.handoff(dev, s.runner, s.lister, a.privilege, a.execute, a.vm, APP_ROOT)))
            return 0
        if not a.execute:
            print("DRY RUN - would check: VM powered off (not saved/paused), raw device not open, identity and layout "
                  "unchanged; then run the normal pre-mount e2fsck and mount at /USB and /USB/A.I.")
            return 0
        print("\n".join(H.give_back(dev, s.runner, s.lister, a.vm, a.vm_powered_off)))
        r = s.mount_and_install(dev, True, a.privilege, open_fm=not a.no_open)
        print("\n".join(r.log))
        return 0 if r.ok else 1
    if a.cmd == "vmdk":
        from .appcopy import APP_ROOT
        from .vmdk import find_ai_dir, prepare_vmdk
        ai = find_ai_dir(dev, s.source_disk, APP_ROOT)
        out = os.path.join(ai, "Cache", "Virtual") if ai else None
        print("\n".join(prepare_vmdk(dev, out, a.execute, a.confirm_raw_disk, a.replace, a.allow_ambiguous,
                                     s.runner, s.lister)))
        return 0
    if a.cmd in ("prepare", "run"):
        plan, lay, chk = s.plan(dev, full=a.full_format)
        if plan is not None:
            print("Format mode: " + (FULL_FORMAT_NOTE if a.full_format else "quick (filesystem creation only)"))
            print(f"\n!!! WARNING: ALL DATA ON {dev.path} ({dev.model}, {dev.size_human}, serial "
                  f"{dev.serial or '?'}) WILL BE DELETED !!!\n{chk.summary()}\n")
        r = s.prepare(dev, a.execute, a.confirm, a.privilege, a.allow_ambiguous, full=a.full_format)
        print("\n".join(r.log))
        if plan is not None and not a.execute:
            print(f"\nTo execute: add --execute --confirm '{dev.confirm_token()}' --privilege sudo")
        if not r.ok or a.cmd == "prepare" or (plan is not None and not a.execute):
            return 0 if r.ok else 1
    r = s.mount_and_install(dev, a.execute, a.privilege, a.update_app, a.allow_ro_fallback,
                            a.fix_owner, not a.no_open, "off" if a.no_fsck else "preen", a.full_repair)
    print("\n".join(r.log))
    if r.checks:
        print("ext4 checks:", json.dumps(r.checks))
    return 0 if r.ok else 1


REFORMAT_WARNING = ("Reformat erases the WHOLE drive. The setup software in EFI:/AI (and its non-secret config) is "
                    "backed up to a private /tmp folder and restored afterwards. ext4 (AI partition) user data WILL BE "
                    "ERASED and is NOT backed up - copy your files elsewhere first. Anything else on the FAT32 (EFI) "
                    "partition outside the AI folder, e.g. a bootloader you installed under EFI/, is also erased.")


def _reformat(a, s, dev):
    print(f"WARNING: {REFORMAT_WARNING}")
    print(f"Target: {dev.path} {dev.model} serial={dev.serial or '?'} {dev.size_human}"
          + ("  (this is the drive the app runs from)" if dev.path == s.source_disk else ""))
    if not a.execute:
        print("DRY RUN - steps that --execute would perform:\n"
              "  1. copy EFI:/AI to a new private (0700) /tmp/usbprep-reformat-* folder, excluding secret-looking files; verify SHA-256\n"
              "  2. relaunch this tool from that /tmp copy; confirm the handoff; wait for the original process(es) to exit\n"
              "  3. re-check device identity/eligibility, refuse if any process still uses the drive\n"
              "  4. unmount all partitions, swapoff, re-check identity\n"
              "  5. re-create GPT: EFI FAT32 4096 MiB / AI ext4 rest / SWAP 243 MiB (same plan as prepare)"
              + (" with FULL format (zero-fill every partition first)\n" if a.full_format else " with quick format\n") +
              "  6. restore the software into EFI:/AI and verify SHA-256; delete the /tmp backup only after a verified restore\n"
              "Any failure before step 5 formats nothing; any failure after it keeps the /tmp backup and prints its path.")
        print(f"\nTo execute: add --execute --confirm '{dev.confirm_token()}' --privilege pkexec|sudo")
        return 0
    job, pid, stage = s.start_reformat(dev, a.confirm or "", a.privilege, a.allow_ambiguous, gui=a.monitor,
                                       wait_pids=a.wait_pid, full=a.full_format)
    print(f"backup verified: {stage['work']} ({len(stage['hashes'])} files"
          + (f"; secrets excluded: {', '.join(stage['excluded'])}" if stage["excluded"] else "") + ")")
    print(f"HANDOFF-OK pid={pid} log={os.path.join(stage['work'], 'reformat.log')}")
    print("This process now exits; the /tmp copy continues the reformat.")
    return 0


def _reformat_resume(a):
    from . import reformat as R
    from .workflow import LinuxSession
    with open(a.job) as f:
        job = json.load(f)
    R.child_handshake(job, int(os.environ.get("USBPREP_PARENT_PID") or 0))
    if a.monitor:
        R.start_monitor(job)
    ok = R.run_job(job, LinuxSession(mount_root=job["mount_root"]), log=lambda m: print(m, flush=True))
    print("REFORMAT-DONE" if ok else "REFORMAT-FAILED", flush=True)
    return 0 if ok else 1


def _cache(a, osname):
    from . import cache as K
    from .appcopy import APP_ROOT
    ai = a.ai or APP_ROOT
    tos, arch = a.os or osname, a.arch or K.normalize_arch()
    if a.ccmd == "status":
        print(json.dumps(K.status(ai, tos, arch), indent=1))
        return 0
    if a.ccmd == "add":
        e = K.add(ai, a.file, tos, arch, a.version, a.sha256, a.source, a.license)
        print(f"cached and verified: {e['file']} ({e['size']} B, sha256 {e['sha256']}) in {K.os_dir(ai, tos)}")
        return 0
    meta = K.adoptium_jre(tos, arch, a.major)
    print(f"Eclipse Temurin {meta['version']} JRE for {tos}/{arch}: {meta['name']} ({meta['size']} B)\n"
          f"  from {meta['link']}\n  vendor sha256 {meta['sha256']}\n  license GPL-2.0 with Classpath Exception")
    if not a.execute:
        print("DRY RUN - add --execute to download, verify and cache it.")
        return 0
    e = K.fetch_java(ai, tos, arch, a.major)
    print(f"cached and verified: {e['file']} in {K.os_dir(ai, tos)}")
    return 0


def _backend(a, osname):
    from . import backends as BK
    target = a.target_os or osname
    if a.bcmd == "set":
        if target not in BK.KEY:
            print(f"no backend choice on {target} (Linux always uses the kernel ext4 driver)")
            return 2
        c = BK.save_choice(target, **{"windows" if target == "windows" else "macos": a.choice})
        print(f"saved {BK.KEY[target]}={c[BK.KEY[target]]} to {BK.config_path(target)}. Nothing was installed, run "
              "or remounted; release (unmount) any drive mounted with the previous backend before using the new one.")
        return 0
    if a.bcmd == "custom-driver":
        from . import customdriver as CD
        r = BK.status(target, choice=None, fat_root=a.fat_root)
        if r.get("choice") != "custom":
            print("Custom driver box is not ticked (backend set custom first). Nothing was run.")
            return 2
        d = r["custom"]
        if d["state"] == "FOUND" and a.trust_script != d["path"]:
            print(CD.trust_warning(d["path"]) + "\nRe-run with --trust-script \"" + d["path"] + "\" to approve.")
            return 2
        res = CD.run(target, d, a.operation, a.device, a.mount_point, a.fat_root or CD.default_fat_root() or "",
                     consent_path=a.trust_script, log_dir=BK.config_dir(target))
        print(json.dumps(res, indent=1))
        return 0 if res["script_succeeded"] and res["verified"] is not False else 1
    r = BK.status(target, choice=a.choice, fat_root=a.fat_root)
    print(json.dumps(r, indent=1) if a.json else BK.summary(r))
    return 0 if r["state"] == BK.READY else 3


def _gui_state(osname):
    st = {"os": osname, "devices": [], "source_disk": None, "default_selection": None, "error": None}
    try:
        if osname == "linux":
            s = _session()
            devs = s.candidates()
            st["source_disk"] = s.source_disk
            st["default_selection"] = s.default_selection(devs)
        else:
            devs = _list_other_os(osname)
        st["devices"] = [d.to_dict() for d in devs]
        if osname == "linux":  # read-only layout check so the GUI can offer Mount vs Initialize
            for d, js in zip(devs, st["devices"]):
                js["layout"] = "unknown"
                if d.status != "excluded":
                    try:
                        js["layout"] = "prepared" if s.inspect(d)[1].prepared else "blank"
                    except Exception:
                        pass
    except Exception as e:  # GUI shows the message; never guesses a selection
        st["error"] = f"{type(e).__name__}: {e}"
    try:
        from . import backends as BK
        st["backend"] = BK.status(osname)
    except Exception as e:
        st["backend"] = {"state": "BLOCKED", "error": f"{type(e).__name__}: {e}"}
    return st


STAR_WARNING = ("Mark this drive as a DEDICATED USB drive?\n"
                "- Detection did not classify it as a USB flash drive (it may be a USB hard disk/SSD, card reader, ...).\n"
                "- Initialize / formatting it will ERASE EVERYTHING on it.\n"
                "- This project was designed for USB flash drives; other drives are your responsibility.\n"
                "- The star never overrides the system-disk / internal-disk protections.")


def _stars_cmd(a, osname):
    from . import stars as ST
    if a.cmd == "stars":
        items = ST.load()
        if a.json:
            print(json.dumps(items, indent=1))
        elif not items:
            print(f"No saved stars ({ST.stars_path()}).")
        else:
            for e in items:
                print(f"★ {e.get('model') or '?'}  serial={e.get('serial')}  {e.get('size')} B  key={e['key']}")
        return 0
    devs = _session().lister() if osname == "linux" else _list_other_os(osname)
    if a.cmd == "unstar":
        removed = ST.unstar(a.target, devs)
        print(f"removed {len(removed)} star(s)" if removed else f"no saved star matches {a.target}")
        return 0 if removed else 1
    dev = next((d for d in devs if d.path == a.device), None)
    if dev is None:
        print(f"{a.device} not found among detected disks", file=sys.stderr)
        return 1
    why = ST.star_refusal(dev)
    if why:
        print(f"STAR REFUSED: {dev.path}: {why}", file=sys.stderr)
        return 2
    if not ST.identity_key(dev):
        print(f"{dev.path}: {ST.NO_SERIAL}", file=sys.stderr)
        if not a.session:
            return 3
        print(json.dumps([ST.session_key(dev)]))
        print(f"Session-only: export {ST.ENV_SESSION}='<the JSON line above>' in this shell; nothing was saved.",
              file=sys.stderr)
        return 0
    if not a.yes:
        print(STAR_WARNING)
        if not sys.stdin.isatty() or input("Type YES to mark it: ").strip() != "YES":
            print("not starred (no confirmation)", file=sys.stderr)
            return 1
    path = ST.star(dev)
    print(f"★ starred {dev.path} ({dev.model or '?'}, serial {dev.serial}) - saved to {path}. All other safeguards "
          "(dry run, typed confirmation, revalidation) still apply.")
    return 0


def _launch_java_gui():
    import shutil
    import subprocess
    from .appcopy import APP_ROOT
    jar = os.path.join(APP_ROOT, "gui-java", "dist", "usbprep-gui.jar")
    java = shutil.which("java")
    if not java:
        print("Java 8 or newer was not found. Install a Java runtime (free OpenJDK build: "
              "https://adoptium.net/temurin/releases/ ), then retry. CLI commands keep working without Java.")
        return 1
    if not os.path.isfile(jar):
        print(f"GUI jar missing: {jar} (build it with gui-java/build.sh)")
        return 1
    return subprocess.call([java, "-jar", jar])


if __name__ == "__main__":
    sys.exit(main())


def _boot_choice(a, source_ai):
    """Explicit flag wins; else ask interactively ONLY when the source is modified and stdin is a TTY.
    No silent default: without a TTY the caller gets BootChoiceRequired."""
    if a.boot_content or not sys.stdin.isatty():
        return a.boot_content
    from . import bootcontent as B
    rep = B.detect(source_ai)
    if not B.is_modified(rep):
        return None
    print("The boot example on this USB has user changes:")
    for comp, r in rep.items():
        for c in r["changes"][:10]:
            print(f"  {comp}: {c}")
    ans = input("[p] Preserve user updates  [f] Create a fresh installation  [c] Cancel: ").strip().lower()
    if ans.startswith("p"):
        return "preserve"
    if ans.startswith("f"):
        return "fresh"
    raise SystemExit("cancelled - nothing was written")


FULL_FORMAT_NOTE = ("FULL - every byte of each partition (FAT32, ext4, swap) is zero-filled inside its boundaries, then "
                    "the filesystems are created. Slow; writes the whole partition (adds flash wear, may reduce "
                    "lifespan). Free space is zeroed, not 'formatted as a type'. Any later speed benefit is device/"
                    "workload dependent and NOT guaranteed. Not a secure erase, no media verification, no bad-block "
                    "scan; the partition table is not overwritten.")


def _scripts(a, osname):
    from . import scripts as S
    if a.scmd == "list":
        r = S.list_scripts(a.ai, osname)
        if a.json:
            print(json.dumps(r, indent=1))
        else:
            print(f"Scripts in {r['dir']}:")
            for x in r["scripts"]:
                print(f"  {x['name']}" + ("" if x["available"] else f"   (needs {x['interpreter']} - not installed)"))
            for x in r["skipped"]:
                print(f"  [skipped] {x['name']}: {x['reason']}")
        return 0
    mount = None
    if a.device and osname == "linux":
        s = _session()
        mount = S.resolve_ext4_mount(s, s.select(a.device, True), os.path.join(s.mount_root, C.MOUNT_EXT4_NAME))
    try:
        return S.run(a.ai, a.name, osname, a.consent, mount, a.device)
    except S.ScriptError as e:
        print(f"NOT RUN: {e}", file=sys.stderr)
        return 2


def _data(a, osname):
    from . import data as D
    D.require_supported(osname)          # Windows/macOS: disabled (ext4 blocked) - raises DataDisabled
    from . import scripts as S
    s = _session()
    ext4 = S.resolve_ext4_mount(s, s.select(a.device, True), os.path.join(s.mount_root, C.MOUNT_EXT4_NAME))
    if not ext4:
        raise D.DataError(f"the ext4 partition of {a.device} is not mounted at {s.mount_root}/{C.MOUNT_EXT4_NAME} "
                          "(use Mount first); nothing done")
    if a.dcmd == "list":
        r = D.list_backups(ext4)
    elif a.dcmd == "backup":
        r = D.backup(ext4, osname, a.execute, a.consent)
    elif a.dcmd == "restore-shared":
        src = a.source.split(":", 1)[-1].split("/")
        if len(src) != 2:
            raise D.DataError("source must be <machine>/<user> (see 'data list')")
        r = D.restore_shared(ext4, src[0], src[1], osname, a.category, a.conflict, a.execute, a.confirm)
    else:
        r = D.restore(ext4, a.backup_id, osname, a.conflict, a.execute, a.confirm,
                      allow_foreign_os=a.allow_foreign_os)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(json.dumps(r, indent=1) if not isinstance(r, list) else
              "\n".join(f"{b['id']}  [{b['status']}]  {b['time'] or ''}" for b in r) or "No backups in /Data.")
    if isinstance(r, dict) and r.get("executed"):
        return 0 if r.get("status") == "complete" else 1
    return 0
