"""High-level flow shared by CLI and GUI: detect -> select -> inspect -> (plan/confirm/prepare) ->
mount FAT -> copy app -> mount ext4 -> verify RW/exec -> open file manager."""
import os
import shutil
import tempfile
from typing import List, Optional

from . import constants as C
from . import bootcontent as B
from .appcopy import APP_ROOT, CopyError, DirBackend, MtoolsBackend, install_app
from .detect import Device, list_linux_devices, selectable, usb_candidates
from .executor import ExecResult, LinuxExecutor, privilege_prefix
from .layout import check_layout, read_device_layout, read_image_layout
from .mounting import LinuxMounter, invoking_user, open_file_manager
from .fsck import Ext4Checker
from .planner import build_plan
from .runner import DEFAULT_RUNNER
from .safety import SafetyError, check_image_target, device_busy, revalidate


class WorkflowError(RuntimeError):
    pass


class LinuxSession:
    def __init__(self, runner=DEFAULT_RUNNER, mount_root: str = C.MOUNT_FAT, lister=None, source_finder=None):
        self.runner = runner
        self.lister = lister or (lambda: list_linux_devices(runner))
        self.mount_root = mount_root
        from .source import find_source_disk
        self._source_finder = source_finder or (lambda: find_source_disk(runner=runner))
        self._source = False  # not yet resolved
        self.source_ai = None  # tests may set the running USB's AI folder explicitly
        self._full = False     # full-format choice for the current duplicate_to() call

    @property
    def boot_source(self) -> Optional[str]:
        """AI folder of the RUNNING verified source USB (read only), else None (stock only)."""
        return self.source_ai or (APP_ROOT if self.source_disk else None)

    def _install_boot(self, target: Device, backend, content, privilege=None) -> List[str]:
        """Boot example onto a NEW target only; identity re-checked before every write; explicit target."""
        guard = self._ext_guard(target)
        self._refuse_source(target, "install boot content onto")
        log = list(content.log)
        guard()
        log += B.deploy(backend, content)
        if content.install.get("EFI"):
            guard()
            log += B.install_efi(backend, content.install["EFI"], B.stock_efi_hashes())
        if content.install.get("BIOS"):
            guard()
            cmd = B.privileged_patch_cmd(target.path, content.install["BIOS"], mbr_backup_dir(),
                                         privilege_prefix(privilege))
            B.guard_command(cmd)
            r = self.runner.run(cmd, check=False)
            log.append((r.stdout or "").strip() if r.returncode == 0 else
                       f"BIOS boot code NOT installed: {(r.stderr or '').strip()}")
        return log

    @property
    def source_disk(self) -> Optional[str]:
        """Verified physical USB the app runs from, or None (cached)."""
        if self._source is False:
            self._source = self._source_finder()
        return self._source

    def default_selection(self, candidates: List[Device]) -> Optional[str]:
        """Preselect ONLY the verified prepared source drive; otherwise no selection."""
        src = self.source_disk
        return src if src and any(d.path == src for d in candidates) else None

    def _refuse_source(self, dev: Device, what: str):
        if self.source_disk and dev.path == self.source_disk:
            raise SafetyError(f"refusing to {what} {dev.path}: this app is running from that drive")

    # ---- detection ----
    def candidates(self) -> List[Device]:
        return usb_candidates(self.lister())

    def select(self, path: str, allow_ambiguous: bool = False) -> Device:
        for d in self.lister():
            if d.path == path:
                why = selectable(d, allow_ambiguous)
                if why:
                    raise WorkflowError(f"{path}: {why}")
                return d
        raise WorkflowError(f"{path} not found among detected disks")

    # ---- layout ----
    def inspect(self, dev: Device):
        lay = read_device_layout(dev.path, self.runner)
        return lay, check_layout(lay)

    def plan(self, dev: Device, force: bool = False, full: bool = False):
        lay, chk = self.inspect(dev)
        if chk.prepared and not force:
            return None, lay, chk
        return build_plan(dev.path, dev.size, dev.sector_size, "device", owner=invoking_user(), full=full), lay, chk

    def prepare(self, dev: Device, execute=False, confirmation=None, privilege=None,
                allow_ambiguous=False, mounted_sources=None, force=False, reformat=False, full=False) -> ExecResult:
        """force/reformat are used ONLY by the explicit Extras > Reformat workflow.
        full=True: zero-fill every partition extent before creating filesystems (not a secure erase)."""
        plan, lay, chk = self.plan(dev, force, full)
        if plan is None:
            return ExecResult(False, ["Drive already has the exact target layout - preserved, nothing formatted."])
        if not reformat:
            self._refuse_source(dev, "prepare/erase")
        busy = device_busy(dev, mounted_sources)
        if busy:
            raise SafetyError(f"refusing: {dev.path} is in use ({', '.join(busy)}); unmount it first")
        fp = lay.fingerprint()

        def guard(step):
            revalidate(dev, fp if step == 1 else None, allow_ambiguous, self.runner, self.lister, mounted_sources)

        res = LinuxExecutor(self.runner).run_plan(plan, execute=execute, confirmation=confirmation,
                                                  expected_token=dev.confirm_token(), guard=guard,
                                                  privilege=privilege)
        if res.executed and res.ok:
            self.runner.run(["udevadm", "settle"], check=False)
            _, chk2 = self.inspect(dev)
            res.log.append("post-check: " + chk2.summary())
            res.ok = chk2.prepared
        return _mark_partial(res, full)

    # ---- mount + install ----
    def _ext_guard(self, dev, mounted_sources=None):
        """Identity + presence re-check (layout fingerprint not needed: we do not repartition here)."""
        def guard():
            cur = {d.path: d for d in self.lister()}.get(dev.path)
            if cur is None or cur.identity() != dev.identity():
                raise SafetyError(f"{dev.path} changed or disappeared; aborting")
        return guard

    def _parts(self, dev):
        lay, chk = self.inspect(dev)
        if not chk.prepared:
            raise WorkflowError("drive is not prepared with the exact layout; prepare it first:\n" + chk.summary())
        return sorted(lay.partitions, key=lambda p: p.number)

    def mount_and_install(self, dev: Device, execute=False, privilege=None, allow_update=False,
                          allow_ro_fallback=False, fix_owner=False, open_fm=True, fsck_mode="preen",
                          full_repair_consent=False, mounted_sources=None, boot_content=None, build_consent=False):
        from .vmhandoff import assert_host_owned
        assert_host_owned(dev)          # never mount on the host while the drive is handed to a VM
        parts = self._parts(dev)
        m = LinuxMounter(self.runner, self.mount_root)

        def copy_app(root):
            be = DirBackend(root)
            fresh = not be.exists(C.APP_DIR_NAME)
            try:
                log = install_app(be, allow_update)
            except CopyError as e:  # app copy problems never block access to the user's ext4 data
                return [f"WARNING: app copy skipped: {e}"]
            if not fresh:          # prepared drive: its boot content is never touched by mount/launch
                return log
            work = tempfile.mkdtemp(prefix="usbprep-boot-")
            try:
                content = B.stage(work, self.boot_source, boot_content, build_consent)
                return log + self._install_boot(dev, be, content, privilege)
            except B.BootError as e:
                return log + [f"WARNING: boot example not deployed: {e}"]
            finally:
                shutil.rmtree(work, ignore_errors=True)

        rep = m.mount(parts[0].path, parts[1].path, execute=execute, privilege=privilege,
                      allow_ro_fallback=allow_ro_fallback, fix_owner=fix_owner, after_fat=copy_app,
                      fsck_mode=fsck_mode, full_repair_consent=full_repair_consent,
                      guard=self._ext_guard(dev), mounted_sources=mounted_sources)
        if not execute:
            rep.log.append(f"  then: copy app into {self.mount_root}/{C.APP_DIR_NAME} with SHA-256 verification")
        elif rep.ok and open_fm:
            rep.log += open_file_manager(m.ai, self.runner)
        return rep

    def check_ext4(self, dev: Device, privilege=None, full_repair_consent=False, scan_bad_blocks=False,
                   mark_consent=False, mounted_sources=None) -> List[str]:
        """Standalone check (CLI `check`): preen, optional consented full repair, optional RO bad-block scan."""
        ext = self._parts(dev)[1]
        chk = Ext4Checker(self.runner, privilege, self._ext_guard(dev), mounted_sources)
        r = chk.auto_check(ext.path)
        log = list(r.log)
        if r.status == "needs-consent":
            log.append(r.report[-2000:])
            r = chk.full_repair(ext.path, full_repair_consent)
            log += r.log
        if scan_bad_blocks and r.status in ("clean", "repaired"):
            lst, blocks = chk.scan_bad_blocks(ext.path, ext.size // 4096)
            log.append(f"read-only scan finished: {len(blocks)} unreadable blocks")
            log += chk.mark_bad_blocks(ext.path, lst, blocks, mark_consent).log
        return log

    def duplicate_to(self, target: Device, execute=False, confirmation=None, privilege=None,
                     allow_ambiguous=False, allow_update=False, mounted_sources=None, boot_content=None,
                     build_consent=False, full=False) -> List[str]:
        """Replicate the layout + setup app + boot example (NOT ext4 user content) onto another drive.
        The preserve/fresh boot choice is resolved BEFORE any target write (BootChoiceRequired otherwise)."""
        self._refuse_source(target, "duplicate onto")
        self._full = full
        work = tempfile.mkdtemp(prefix="usbprep-boot-")
        try:
            return self._duplicate(target, execute, confirmation, privilege, allow_ambiguous, allow_update,
                                   mounted_sources, B.stage(work, self.boot_source, boot_content, build_consent))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _duplicate(self, target, execute, confirmation, privilege, allow_ambiguous, allow_update, mounted_sources,
                   content) -> List[str]:
        log = [f"Duplicate setup to {target.path} (source drive: {self.source_disk or 'not running from a USB'})"]
        r = self.prepare(target, execute, confirmation, privilege, allow_ambiguous, mounted_sources, full=getattr(self, "_full", False))
        log += r.log
        if not r.ok or not r.executed:
            if r.ok and not execute:
                return log + ["  then: udisksctl mount -b <target>1 (temporary, not /USB), copy setup app to AI "
                              "with SHA-256 verification, " + content.log[0] + ", EFI/BOOT/BOOTX64.EFI and MBR "
                              "boot-code bytes 0-439 on the target only, udisksctl unmount -b <target>1"]
            return log + ["duplication stopped: target was not freshly prepared (existing drives keep their content)"
                          if r.ok else "duplication stopped: target preparation failed"]
        p1 = self._parts(target)[0].path
        out = self.runner.run(["udisksctl", "mount", "-b", p1]).stdout
        mnt = out.strip().split(" at ", 1)[-1].rstrip(".") if " at " in out else None
        if not mnt or not os.path.isdir(mnt):
            return log + [f"could not determine temporary mount point from: {out.strip()}"]
        try:
            log += install_app(DirBackend(mnt), allow_update)
            log += self._install_boot(target, DirBackend(mnt), content, privilege)
        except (CopyError, B.BootError) as e:
            log.append(f"app/boot copy failed: {e}")
        finally:
            self.runner.run(["udisksctl", "unmount", "-b", p1], check=False)
            log.append(f"unmounted temporary mount {mnt}")
        return log

    # ---- Extras: check / repair (ext4 never checked while mounted) ----
    def _ext4_released(self, ext_path, privilege, mounted_sources=None):
        """Unmount ext4 if WE mounted it at /USB/A.I.; refuse if mounted anywhere else. Returns remount flag."""
        from .safety import proc_mounted_sources
        src = proc_mounted_sources() if mounted_sources is None else mounted_sources
        if os.path.realpath(ext_path) not in src:
            return False
        ai = os.path.join(self.mount_root, C.MOUNT_EXT4_NAME)
        fm = self.runner.run(["findmnt", "-n", "-o", "SOURCE", ai], check=False).stdout.strip()
        if os.path.realpath(fm or "/nonexistent") != os.path.realpath(ext_path):
            raise SafetyError(f"{ext_path} is mounted somewhere other than {ai}; unmount it first")
        self.runner.run(privilege_prefix(privilege) + ["umount", ai])
        return True

    def _remount_ext4(self, ext_path, privilege):
        from .mounting import EXT4_RW_OPTS
        ai = os.path.join(self.mount_root, C.MOUNT_EXT4_NAME)
        self.runner.run(privilege_prefix(privilege) + ["mount", "-t", "ext4", "-o", EXT4_RW_OPTS, ext_path, ai])
        return f"remounted {ext_path} at {ai}"

    def check_drive(self, dev: Device, privilege=None, mounted_sources=None) -> List[str]:
        """Extras > Check drive for errors: NON-MUTATING (e2fsck -fn, fsck.vfat -n)."""
        parts = self._parts(dev)
        fat, ext = parts[0].path, parts[1].path
        remount = self._ext4_released(ext, privilege, mounted_sources)
        log = []
        try:
            chk = Ext4Checker(self.runner, privilege, self._ext_guard(dev), mounted_sources)
            rc, out = chk._e2fsck(ext, "-f", "-n")
            verdict = {0: "no errors"}.get(rc, "ERRORS FOUND (use 'Auto repair detected errors')" if rc == 4
                                           else f"check failed (exit {rc})")
            log += [f"ext4 {ext}: e2fsck -fn exit {rc}: {verdict}"] + out.splitlines()[-4:]
        except SafetyError as e:
            log.append(f"ext4 check refused: {e}")
            rc = None
        from .safety import proc_mounted_sources
        src = proc_mounted_sources() if mounted_sources is None else mounted_sources
        if os.path.realpath(fat) in src:
            log.append(f"FAT32 {fat} is mounted (app may run from it) - read-only FAT check skipped")
        else:
            r = self.runner.run(privilege_prefix(privilege) + ["fsck.vfat", "-n", fat], check=False)
            log.append(f"FAT32 {fat}: fsck.vfat -n exit {r.returncode}: "
                       + ("no errors" if r.returncode == 0 else "errors reported (not repaired automatically)"))
        if remount:
            log.append(self._remount_ext4(ext, privilege) if rc == 0 else
                       "ext4 left UNMOUNTED because the check did not pass cleanly")
        return log

    def repair_drive(self, dev: Device, privilege=None, full_repair_consent=False, mounted_sources=None) -> List[str]:
        """Extras > Auto repair detected errors: e2fsck -p; full -fy only with explicit consent."""
        ext = self._parts(dev)[1].path
        remount = self._ext4_released(ext, privilege, mounted_sources)
        chk = Ext4Checker(self.runner, privilege, self._ext_guard(dev), mounted_sources)
        r = chk.auto_check(ext)
        log = list(r.log)
        if r.status == "needs-consent":
            r = chk.full_repair(ext, full_repair_consent)
            log += r.log
        if remount:
            log.append(self._remount_ext4(ext, privilege) if r.mount_ok else
                       "ext4 left UNMOUNTED: unresolved errors block read/write mounting")
        return log

    # ---- Extras: reformat (phase A; phase B runs from the /tmp copy, see reformat.run_job) ----
    def start_reformat(self, dev: Device, token: str, privilege=None, allow_ambiguous=False, gui=False,
                       base="/tmp", popen=None, src_ai_dir: Optional[str] = None, wait_pids=(), full=False):
        from . import reformat as R
        from .appcopy import APP_ROOT
        why = selectable(dev, allow_ambiguous)
        if why:
            raise WorkflowError(f"{dev.path}: {why}")
        if token != dev.confirm_token():
            raise WorkflowError(f"typed confirmation does not match '{dev.confirm_token()}'; nothing done")
        lay = read_device_layout(dev.path, self.runner)
        if src_ai_dir is None:
            src_ai_dir = APP_ROOT
            if dev.path != self.source_disk and check_layout(lay).prepared:
                p1 = sorted(lay.partitions, key=lambda p: p.number)[0]
                mp = next((c.get("mountpoints") or [None])[0] for c in dev.raw.get("children") or []
                          if c.get("path") == p1.path) if dev.raw.get("children") else None
                if mp and os.path.isdir(os.path.join(mp, C.APP_DIR_NAME)):
                    src_ai_dir = os.path.join(mp, C.APP_DIR_NAME)
        stage = R.stage_backup(src_ai_dir, base)            # raises StageError => nothing formatted
        job = R.write_job(stage, dev, lay.fingerprint(), token, privilege, allow_ambiguous, self.mount_root,
                          wait_pids=list(wait_pids), full_format=full)
        kw = {"popen": popen} if popen else {}
        try:
            pid = R.relaunch_from_stage(job, gui=gui, **kw)
        except R.HandoffError:
            import shutil
            shutil.rmtree(stage["work"], ignore_errors=True)  # nothing was formatted; backup not needed
            raise
        return job, pid, stage

    def unmount(self, dev: Optional[Device], execute=False, privilege=None, eject=False):
        if dev is not None:
            self._refuse_source(dev, "unmount/eject")
        elif self.source_disk:
            raise SafetyError("refusing to unmount: this app is running from the USB mounted there")
        m = LinuxMounter(self.runner, self.mount_root)
        return m.unmount(privilege=privilege, execute=execute, remove_root=True,
                         eject_dev=dev.path if (eject and dev) else None)


# ---- image mode (safe demonstration / tests; regular files only) ----
def image_token(path: str) -> str:
    return f"ERASE {os.path.splitext(os.path.basename(path))[0][-4:]}"


def create_image(path: str, size: int) -> None:
    if os.path.exists(path):
        raise WorkflowError(f"{path} already exists; refusing to overwrite")
    with open(path, "wb") as f:
        f.truncate(size)  # sparse


def _mark_partial(res: ExecResult, full: bool) -> ExecResult:
    if full and res.executed and not res.ok:
        res.log.append("FULL FORMAT INTERRUPTED/FAILED: the drive is PARTIALLY formatted and NOT usable; nothing was "
                       "restored or mounted. Run prepare/reformat again.")
    return res


def prepare_image(path: str, execute=False, confirmation=None, runner=DEFAULT_RUNNER, full=False) -> ExecResult:
    check_image_target(path)
    lay = read_image_layout(path, runner)
    if check_layout(lay).prepared:
        return ExecResult(False, ["Image already has the exact target layout - preserved."])
    plan = build_plan(path, os.path.getsize(path), 512, "image", owner=invoking_user(), full=full)
    res = LinuxExecutor(runner).run_plan(plan, execute=execute, confirmation=confirmation,
                                         expected_token=image_token(path))
    if res.executed and res.ok:
        chk = check_layout(read_image_layout(path, runner))
        res.log.append("post-check: " + chk.summary())
        res.ok = chk.prepared
    return _mark_partial(res, full)


def install_app_image(path: str, allow_update=False, runner=DEFAULT_RUNNER) -> List[str]:
    check_image_target(path)
    lay = read_image_layout(path, runner)
    chk = check_layout(lay)
    if not chk.prepared:
        raise WorkflowError("image is not prepared:\n" + chk.summary())
    p1 = sorted(lay.partitions, key=lambda p: p.number)[0]
    return install_app(MtoolsBackend(path, p1.start, runner), allow_update)


def mbr_backup_dir() -> str:
    d = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "usbprep", "mbr-backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def fresh_install_image(path: str, execute=False, confirmation=None, boot_content=None, source_ai=None,
                        build_consent=False, runner=DEFAULT_RUNNER, full=False) -> ExecResult:
    """Fresh preparation of a blank IMAGE: layout -> app -> boot example (stock unless the user chose to
    preserve a modified source). The boot choice is resolved before the image is touched."""
    check_image_target(path)
    work = tempfile.mkdtemp(prefix="usbprep-boot-")
    try:
        content = B.stage(work, source_ai, boot_content, build_consent)
        res = prepare_image(path, execute, confirmation, runner, full=full)
        if not (res.executed and res.ok):
            if res.ok and not res.executed and execute is False:
                res.log.append("  then: copy app to AI; " + content.log[0] + "; EFI/BOOT/BOOTX64.EFI; MBR bytes 0-439")
            return res
        res.log += install_app_image(path, runner=runner)
        p1 = sorted(read_image_layout(path, runner).partitions, key=lambda p: p.number)[0]
        be = MtoolsBackend(path, p1.start, runner)
        res.log += content.log + B.deploy(be, content)
        if content.install.get("EFI"):
            res.log += B.install_efi(be, content.install["EFI"], B.stock_efi_hashes())
        if content.install.get("BIOS"):
            res.log += B.patch_mbr_image(path, content.install["BIOS"], mbr_backup_dir(), B.stock_codes())
        return res
    except B.BootError as e:
        if isinstance(e, B.BootChoiceRequired):
            raise
        raise WorkflowError(str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
