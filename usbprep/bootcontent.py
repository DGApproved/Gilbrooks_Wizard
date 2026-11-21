"""Hello World boot example: stock baseline, user-modification detection, preserve/fresh choice,
build-state tracking, and target-only installation (MBR boot-code bytes 0-439, ESP removable path).

Isolation: every write here targets ONLY an explicit, already-revalidated USB/image path given by the
caller. Nothing here registers firmware boot entries, touches NVRAM, or writes host disks; the
command guard below refuses host boot tools outright.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

from .appcopy import APP_ROOT

STOCK_ROOT = os.path.join(APP_ROOT, "boot_stock")
STOCK_MANIFEST = "STOCK_MANIFEST.json"
BUILD_INFO = "BUILD_INFO.json"
EFI_FALLBACK = "EFI/BOOT/BOOTX64.EFI"      # removable-media path; no NVRAM registration
MBR_CODE_LEN = 440
FORBIDDEN_TOOLS = ("efibootmgr", "bcdedit", "bless", "grub-install", "grub2-install", "update-grub",
                   "grub-mkconfig", "bootctl", "kernelstall")

# component -> (primary dir under AI, aliases honoured in place, sources, artifact)
COMPONENTS = {
    "BIOS": ("scr/BIOS", ("src/BIOS",), ("hello.asm", "build.sh"), "hello_bios.bin"),
    "EFI": ("src/EFI", (), ("hello.c", "build.sh"), "BOOTX64.EFI"),
}


class BootError(RuntimeError):
    pass


class BootChoiceRequired(BootError):
    """Source boot example is user-modified and no explicit preserve/fresh choice was given."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__("the boot example on the running USB has user changes (" + ", ".join(
            f"{c}: {r['status']}" for c, r in report.items()) + "). Choose 'preserve' (keep user updates) or "
            "'fresh' (stock Hello World) - nothing has been written")


def guard_command(argv: List[str]) -> None:
    """Refuse host boot-configuration tools (firmware entries, host bootloaders) - never used by this app."""
    if argv and os.path.basename(argv[0]) in FORBIDDEN_TOOLS:
        raise BootError(f"refusing host boot tool {argv[0]}: usbprep only writes the selected USB")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_hash(d: str, sources) -> Optional[str]:
    """sha256 of `sha256sum <sources>` output (identical to what build.sh records)."""
    lines = []
    for s in sources:
        p = os.path.join(d, s)
        if not os.path.isfile(p):
            return None
        lines.append(f"{sha256_file(p)}  {s}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def tree_hashes(root: str) -> Dict[str, str]:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            out[os.path.relpath(full, root).replace(os.sep, "/")] = sha256_file(full)
    return out


# ---------- stock baseline ----------
def write_stock_manifest(root: str = STOCK_ROOT) -> dict:
    files = {k: v for k, v in tree_hashes(root).items() if k != STOCK_MANIFEST}
    m = {"name": "usbprep stock Hello World boot example", "files": files}
    with open(os.path.join(root, STOCK_MANIFEST), "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    return m


def load_stock_manifest(root: str = STOCK_ROOT) -> dict:
    with open(os.path.join(root, STOCK_MANIFEST)) as f:
        return json.load(f)


def verify_stock(root: str = STOCK_ROOT) -> List[str]:
    """Problems with the bundled stock tree (empty list = intact)."""
    try:
        m = load_stock_manifest(root)
    except (OSError, ValueError) as e:
        return [f"stock manifest unreadable: {e}"]
    cur = {k: v for k, v in tree_hashes(root).items() if k != STOCK_MANIFEST}
    bad = [f"missing {k}" for k in m["files"] if k not in cur]
    bad += [f"modified {k}" for k in m["files"] if k in cur and cur[k] != m["files"][k]]
    bad += [f"unexpected {k}" for k in cur if k not in m["files"]]
    return bad


def stock_dir(component: str, root: str = STOCK_ROOT) -> str:
    return os.path.join(root, *COMPONENTS[component][0].split("/"))


# ---------- build state ----------
def build_state(d: str, component: str) -> dict:
    """valid  : artifact exists and BUILD_INFO matches current sources + artifact
       stale  : artifact/BUILD_INFO present but sources (or artifact) changed since that build
       unbuilt: no artifact"""
    _, _, sources, art = COMPONENTS[component]
    ap, ip = os.path.join(d, art), os.path.join(d, BUILD_INFO)
    cur = source_hash(d, sources)
    if not os.path.isfile(ap):
        return {"state": "unbuilt", "reason": f"{art} missing", "source_sha256": cur}
    try:
        with open(ip) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return {"state": "stale", "reason": f"{BUILD_INFO} missing/unreadable - cannot prove {art} matches the sources",
                "source_sha256": cur}
    if info.get("artifact_sha256") != sha256_file(ap):
        return {"state": "stale", "reason": f"{art} differs from the build recorded in {BUILD_INFO}", "source_sha256": cur}
    if cur is None or info.get("source_sha256") != cur:
        return {"state": "stale", "reason": "sources changed after the last build", "source_sha256": cur}
    return {"state": "valid", "reason": "artifact matches the recorded build of the current sources", "source_sha256": cur}


def validate_artifact(component: str, path: str) -> None:
    with open(path, "rb") as f:
        data = f.read()
    if component == "BIOS":
        if not 0 < len(data) <= MBR_CODE_LEN:
            raise BootError(f"BIOS boot code must be 1..{MBR_CODE_LEN} bytes (got {len(data)})")
    else:
        pe = int.from_bytes(data[0x3C:0x40], "little") if len(data) > 0x40 else 0
        ok = data[:2] == b"MZ" and data[pe:pe + 4] == b"PE\0\0" and data[pe + 4:pe + 6] == b"\x64\x86"
        if not ok or int.from_bytes(data[pe + 24:pe + 26], "little") != 0x20B or \
                int.from_bytes(data[pe + 24 + 68:pe + 24 + 70], "little") != 10:
            raise BootError("EFI artifact is not a PE32+ x86-64 EFI application")


def approved_build(d: str, component: str, out_dir: str) -> str:
    """Build with the APPROVED fixed toolchain command (never the user's build.sh) in a private temp dir.
    Returns the path of the verified artifact in out_dir (with a fresh BUILD_INFO next to it)."""
    _, _, sources, art = COMPONENTS[component]
    with tempfile.TemporaryDirectory(prefix="usbprep-build-") as td:
        for s in sources:
            shutil.copy2(os.path.join(d, s), td)
        if component == "BIOS":
            cmd = ["nasm", "-f", "bin", "-o", art, "hello.asm"]
        else:
            cmd = ["x86_64-w64-mingw32-gcc", "-Os", "-ffreestanding", "-fno-stack-protector",
                   "-fno-asynchronous-unwind-tables", "-fshort-wchar", "-mno-red-zone", "-nostdlib",
                   "-Wl,--subsystem,10", "-Wl,--no-insert-timestamp", "-e", "efi_main", "-s", "-o", art, "hello.c"]
        if not shutil.which(cmd[0]):
            raise BootError(f"{cmd[0]} not installed - install the toolchain first (see docs/PRIVILEGES.md); not built")
        p = subprocess.run(cmd, cwd=td, capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise BootError(f"build failed ({cmd[0]}): {p.stderr.strip()[-800:]}")
        built = os.path.join(td, art)
        validate_artifact(component, built)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, art)
        shutil.copyfile(built, dst)
    info = {"component": component, "artifact": art, "artifact_sha256": sha256_file(dst), "sources": list(sources),
            "source_sha256": source_hash(d, sources), "tool": cmd[0] + " (usbprep approved build)"}
    with open(os.path.join(out_dir, BUILD_INFO), "w") as f:
        json.dump(info, f)
    return dst


# ---------- source detection ----------
def detect(ai_root: Optional[str], stock_root: str = STOCK_ROOT) -> dict:
    """Compare the running USB's AI/scr|src boot dirs with the stock manifest.
    status: stock | modified | missing (missing = no folder at all -> treated as nothing to preserve)."""
    stock = load_stock_manifest(stock_root)["files"]
    rep = {}
    for comp, (primary, aliases, _, _) in COMPONENTS.items():
        base = {k[len(primary) + 1:]: v for k, v in stock.items() if k.startswith(primary + "/")}
        dirs = [x for x in (primary,) + aliases if ai_root and os.path.isdir(os.path.join(ai_root, *x.split("/")))]
        if not dirs:
            rep[comp] = {"status": "missing", "dirs": [], "changes": []}
            continue
        changes = []
        for rel in dirs:
            cur = tree_hashes(os.path.join(ai_root, *rel.split("/")))
            if rel != primary:            # alias folder present at all = user content
                changes += [f"{rel}/{k}" for k in cur]
                continue
            changes += [f"{rel}/{k} (changed)" for k in cur if k in base and cur[k] != base[k]]
            changes += [f"{rel}/{k} (added)" for k in cur if k not in base]
            changes += [f"{rel}/{k} (deleted)" for k in base if k not in cur]
        rep[comp] = {"status": "modified" if changes else "stock", "dirs": dirs, "changes": changes}
    return rep


def is_modified(rep: dict) -> bool:
    return any(r["status"] == "modified" for r in rep.values())


# ---------- staging the content for a NEW target ----------
class BootContent:
    def __init__(self, mode: str, tree: str, install: Dict[str, Optional[str]], log: List[str]):
        self.mode, self.tree, self.install, self.log = mode, tree, install, log


def stage(work: str, source_ai: Optional[str], choice: Optional[str] = None, build_consent: bool = False,
          stock_root: str = STOCK_ROOT) -> BootContent:
    """Decide + stage boot content in host temp `work` BEFORE any target write.
    Raises BootChoiceRequired when the source is modified and choice is None. Never writes source_ai."""
    bad = verify_stock(stock_root)
    if bad:
        raise BootError("bundled stock boot example failed its integrity check: " + "; ".join(bad[:5]))
    if choice not in (None, "preserve", "fresh"):
        raise BootError("boot content choice must be 'preserve' or 'fresh'")
    rep = detect(source_ai, stock_root)
    tree = os.path.join(work, "boot-tree")
    os.makedirs(tree)
    if not is_modified(rep) or choice == "fresh":
        if not is_modified(rep) and choice == "preserve":
            choice = "fresh"
        for comp, (primary, _, _, art) in COMPONENTS.items():
            shutil.copytree(stock_dir(comp, stock_root), os.path.join(tree, *primary.split("/")))
        install = {c: os.path.join(tree, *COMPONENTS[c][0].split("/"), COMPONENTS[c][3]) for c in COMPONENTS}
        why = "user chose fresh installation" if choice == "fresh" else "source boot example unmodified/absent"
        return BootContent("fresh", tree, install, [f"boot example: stock Hello World ({why})"])
    if choice is None:
        raise BootChoiceRequired(rep)
    log, install = ["boot example: preserving user updates from the running USB (source is read only)"], {}
    for comp, (primary, aliases, sources, art) in COMPONENTS.items():
        r = rep[comp]
        if r["status"] == "missing":
            log.append(f"  {comp}: no folder on the source - nothing copied, no {comp} boot code installed")
            install[comp] = None
            continue
        for rel in r["dirs"]:
            shutil.copytree(os.path.join(source_ai, *rel.split("/")), os.path.join(tree, *rel.split("/")))
        d = os.path.join(tree, *r["dirs"][0].split("/"))
        st = build_state(d, comp)
        if st["state"] == "valid":
            try:
                validate_artifact(comp, os.path.join(d, art))
                install[comp] = os.path.join(d, art)
                log.append(f"  {comp}: copied {', '.join(r['dirs'])}; artifact valid for the edited sources")
                continue
            except BootError as e:
                st = {"state": "stale", "reason": str(e)}
        for junk in (art, BUILD_INFO):          # never carry an artifact that does not match the sources
            if os.path.exists(os.path.join(d, junk)):
                os.rename(os.path.join(d, junk), os.path.join(d, junk + ".stale"))
        if build_consent:
            try:
                install[comp] = approved_build(d, comp, d)
                log.append(f"  {comp}: artifact was {st['state']} ({st['reason']}); rebuilt with the approved "
                           f"toolchain (consented), verified")
                continue
            except BootError as e:
                log.append(f"  {comp}: approved build failed: {e}")
        install[comp] = None
        log.append(f"  {comp}: sources copied, but output is {st['state'].upper()} ({st['reason']}); "
                   f"{comp} boot code NOT installed (the stock binary is never substituted for edited code). "
                   f"Rebuild with consent or run build.sh, then use Boot > Reinstall")
    return BootContent("preserve", tree, install, log)


# ---------- target writers (explicit target only) ----------
def check_mbr_patchable(sector: bytes, code: bytes, known_codes=(), overwrite_unknown=False) -> None:
    if len(sector) < 512 or sector[510:512] != b"\x55\xAA":
        raise BootError("target sector 0 has no 0x55AA MBR signature; refusing to write boot code")
    if sector[446 + 4] != 0xEE:
        raise BootError("target MBR is not a GPT protective MBR (first entry type != 0xEE); refusing")
    cur = sector[:MBR_CODE_LEN]
    if cur.strip(b"\0") and not overwrite_unknown and \
            not any(cur == k.ljust(MBR_CODE_LEN, b"\0") for k in tuple(known_codes) + (code,)):
        raise BootError("target MBR already contains unknown boot code; refusing without explicit consent")


def patch_mbr_image(path: str, code_path: str, backup_dir: str, known_codes=(), overwrite_unknown=False) -> List[str]:
    """Write ONLY bytes 0-439 of sector 0 of a regular IMAGE file; verify 440-511 and LBA1+ unchanged."""
    if not os.path.isfile(path) or os.path.islink(path):
        raise BootError(f"{path} is not a regular image file")
    with open(code_path, "rb") as f:
        code = f.read()
    if not 0 < len(code) <= MBR_CODE_LEN:
        raise BootError(f"boot code must be 1..{MBR_CODE_LEN} bytes")
    with open(path, "r+b") as f:
        before = f.read(1024)
        check_mbr_patchable(before, code, known_codes, overwrite_unknown)
        os.makedirs(backup_dir, exist_ok=True)
        bk = os.path.join(backup_dir, "mbr-sector0.bak")
        with open(bk, "wb") as b:
            b.write(before[:512])
        f.seek(0)
        f.write(code.ljust(MBR_CODE_LEN, b"\0"))
        f.flush()
        os.fsync(f.fileno())
        f.seek(0)
        after = f.read(1024)
    if after[MBR_CODE_LEN:] != before[MBR_CODE_LEN:] or after[:len(code)] != code:
        with open(path, "r+b") as f:
            f.write(before[:512])
        raise BootError("post-write verification failed; original sector 0 restored")
    return [f"BIOS boot code ({len(code)} bytes) written to bytes 0-439 of {path}; signature, partition "
            f"entries, 0x55AA and GPT verified unchanged (backup {bk})"]


def install_efi(backend, efi_path: str, known=()) -> List[str]:
    """Copy to EFI/BOOT/BOOTX64.EFI on the target ESP (backend = that ESP only). No NVRAM entry."""
    h = sha256_file(efi_path)
    if backend.exists_file(EFI_FALLBACK):
        cur = backend.file_sha256(EFI_FALLBACK)
        if cur == h:
            return [f"{EFI_FALLBACK} already identical - unchanged"]
        if cur not in known:
            raise BootError(f"{EFI_FALLBACK} on the target is not ours; refusing to overwrite")
    backend.put_file(efi_path, EFI_FALLBACK)
    if backend.file_sha256(EFI_FALLBACK) != h:
        raise BootError(f"{EFI_FALLBACK} verification failed")
    return [f"EFI app installed at {EFI_FALLBACK} on the target ESP (removable-media fallback; no firmware "
            f"boot entry or NVRAM change), SHA-256 verified"]


def deploy(backend, content: BootContent, app_dir: str = "AI") -> List[str]:
    """Copy staged scr/src folders into <ESP>/AI on a target that has NO boot folders yet."""
    log = []
    for top in sorted(os.listdir(content.tree)):
        rel = f"{app_dir}/{top}"
        if backend.exists(rel):
            raise BootError(f"{rel} already exists on the target; refusing to overwrite boot content")
        backend.put_tree(os.path.join(content.tree, top), rel)
    got = {}
    with tempfile.TemporaryDirectory() as td:
        for top in sorted(os.listdir(content.tree)):
            backend.get_tree(f"{app_dir}/{top}", os.path.join(td, top))
        got = tree_hashes(td)
    want = tree_hashes(content.tree)
    if got != want:
        raise BootError("boot content verification on the target failed")
    log.append(f"boot example ({content.mode}) copied to EFI:/{app_dir}/{{scr,src}}: {len(want)} files SHA-256 verified")
    return log


def patch_mbr_device(dev: str, code_path: str, backup_dir: str, known_codes=(), overwrite_unknown=False) -> List[str]:
    """Same byte-exact patch on a WHOLE-DISK block device (run as root via the privileged helper only
    after the caller revalidated the selected USB's identity). Partitions are refused."""
    import stat
    st = os.stat(dev)
    base = os.path.basename(dev)
    if not stat.S_ISBLK(st.st_mode) or not os.path.exists(f"/sys/block/{base}"):
        raise BootError(f"{dev} is not a whole-disk block device; refusing")
    with open(code_path, "rb") as f:
        code = f.read()
    fd = os.open(dev, os.O_RDWR | os.O_SYNC)
    try:
        before = os.pread(fd, 1024, 0)
        check_mbr_patchable(before, code, known_codes, overwrite_unknown)
        os.makedirs(backup_dir, exist_ok=True)
        with open(os.path.join(backup_dir, f"mbr-{base}-sector0.bak"), "wb") as b:
            b.write(before[:512])
        os.pwrite(fd, code.ljust(MBR_CODE_LEN, b"\0"), 0)
        os.fsync(fd)
        after = os.pread(fd, 1024, 0)
        if after[MBR_CODE_LEN:] != before[MBR_CODE_LEN:] or after[:len(code)] != code:
            os.pwrite(fd, before[:512], 0)
            raise BootError("post-write verification failed; original sector 0 restored")
    finally:
        os.close(fd)
    return [f"BIOS boot code ({len(code)} bytes) written to bytes 0-439 of {dev}; table/signature verified unchanged"]


def stock_codes(root: str = STOCK_ROOT):
    with open(os.path.join(stock_dir("BIOS", root), COMPONENTS["BIOS"][3]), "rb") as f:
        return (f.read(),)


def stock_efi_hashes(root: str = STOCK_ROOT):
    return (sha256_file(os.path.join(stock_dir("EFI", root), COMPONENTS["EFI"][3])),)


def privileged_patch_cmd(dev: str, code_path: str, backup_dir: str, prefix: List[str]) -> List[str]:
    import sys
    return prefix + ["env", f"PYTHONPATH={APP_ROOT}", sys.executable, "-m", "usbprep.bootcontent",
                     "patch-mbr", dev, code_path, backup_dir]


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "patch-mbr":
        try:
            print("\n".join(patch_mbr_device(sys.argv[2], sys.argv[3], sys.argv[4], stock_codes())))
        except (BootError, OSError) as e:
            print(f"refused: {e}", file=sys.stderr)
            sys.exit(1)
    elif len(sys.argv) == 2 and sys.argv[1] == "write-stock-manifest":
        print(len(write_stock_manifest()["files"]), "files")
    else:
        print("usage: python3 -m usbprep.bootcontent patch-mbr <whole-disk-dev> <code.bin> <backup-dir>", file=sys.stderr)
        sys.exit(2)
