"""Copy the app into <FAT32>/AI with SHA-256 manifest verification and safe update.

Backends: DirBackend (mounted FAT, e.g. /USB) and MtoolsBackend (FAT inside an image file at an
offset - no mount/root needed; used by tests and image mode).
"""
import hashlib
import json
import os
import shutil
import tempfile
from typing import Dict, List

from . import __version__, constants as C
from .runner import DEFAULT_RUNNER, CommandError

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ("usbprep", "boot_stock", "launchers", "setup", "host_helpers", "gui-java/dist", "Cache", "Start", "Driver", "START_HERE.txt", "README.md",
           "LICENSE", "pyproject.toml")
PRESERVE = ("Cache",)   # user/setup-populated content carried over on app updates (never part of the app manifest)
CARRY = PRESERVE + ("scr", "src", "Scripts", "Driver")   # + user boot example / script folders: never replaced by app updates
DEFAULT_SCRIPTS = "Scripts"   # default scripts are seeded only where that file name does not exist yet
SKIP = ("__pycache__", ".pyc", ".pytest_cache")


class CopyError(RuntimeError):
    pass


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_app_files(root: str = APP_ROOT) -> Dict[str, str]:
    """relative path -> absolute path for files that make up the portable app."""
    files = {}
    for item in INCLUDE:
        if item in PRESERVE:   # READMEs + manifest-listed cached installers (so duplication carries the cache)
            from .cache import MANIFEST, read_manifest
            for dp, _, fns in os.walk(os.path.join(root, item)):
                listed = {MANIFEST} | {e["file"] for e in read_manifest(dp)}
                for fn in fns:
                    if fn.lower().startswith("readme") or fn in listed:
                        full = os.path.join(dp, fn)
                        files[os.path.relpath(full, root)] = full
            continue
        p = os.path.join(root, item)
        if os.path.isfile(p):
            files[item] = p
        for dp, dns, fns in os.walk(p) if os.path.isdir(p) else []:
            dns[:] = [d for d in dns if d not in SKIP]
            for fn in fns:
                if not fn.endswith(SKIP):
                    full = os.path.join(dp, fn)
                    files[os.path.relpath(full, root)] = full
    return files


def build_staging(dst: str, root: str = APP_ROOT) -> dict:
    files = collect_app_files(root)
    manifest = {"app": "usbprep", "version": __version__, "files": {}}
    for rel, src in sorted(files.items()):
        out = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copyfile(src, out)
        manifest["files"][rel.replace(os.sep, "/")] = sha256(out)
    with open(os.path.join(dst, C.MANIFEST_NAME), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest


def verify_tree(local_dir: str, manifest: dict) -> List[str]:
    bad = []
    for rel, digest in manifest["files"].items():
        p = os.path.join(local_dir, *rel.split("/"))
        if not os.path.isfile(p):
            bad.append(f"missing {rel}")
        elif sha256(p) != digest:
            bad.append(f"checksum mismatch {rel}")
    return bad


class DirBackend:
    def __init__(self, fat_root: str):
        self.root = fat_root

    def _p(self, name):
        return os.path.join(self.root, name)

    def exists(self, name):
        return os.path.exists(self._p(name))

    def read_manifest(self, name):
        try:
            with open(os.path.join(self._p(name), C.MANIFEST_NAME)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put_tree(self, local, name):
        shutil.copytree(local, self._p(name))
        os.sync()

    def get_tree(self, name, local):
        shutil.copytree(self._p(name), local)

    def rename(self, a, b):
        os.rename(self._p(a), self._p(b))

    def rmtree(self, name):
        shutil.rmtree(self._p(name))

    def exists_file(self, rel):
        return os.path.isfile(self._p(rel))

    def file_sha256(self, rel):
        return sha256(self._p(rel))

    def put_file(self, local, rel):
        os.makedirs(os.path.dirname(self._p(rel)), exist_ok=True)
        shutil.copyfile(local, self._p(rel))
        os.sync()


class MtoolsBackend:
    """FAT filesystem inside an image file at byte offset (mtools `image@@offset`)."""

    def __init__(self, image: str, offset: int, runner=DEFAULT_RUNNER):
        self.img, self.runner = f"{image}@@{offset}", runner
        self.env_ok = True

    def _run(self, argv, check=True):
        return self.runner.run(argv[:1] + ["-i", self.img] + argv[1:], check=check)

    def exists(self, name):
        return self._run(["mdir", "-b", f"::/{name}"], check=False).returncode == 0

    def read_manifest(self, name):
        r = self._run(["mtype", f"::/{name}/{C.MANIFEST_NAME}"], check=False)
        try:
            return json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            return None

    def put_tree(self, local, name):
        self._run(["mmd", f"::/{name}"])
        entries = [os.path.join(local, e) for e in sorted(os.listdir(local))]
        self._run(["mcopy", "-s", "-Q", "-n"] + entries + [f"::/{name}/"])

    def get_tree(self, name, local):
        os.makedirs(local)
        self._run(["mcopy", "-s", "-Q", "-n", f"::/{name}/*", local + "/"])

    def rename(self, a, b):
        self._run(["mmove", f"::/{a}", f"::/{b}"])

    def rmtree(self, name):
        self._run(["mdeltree", f"::/{name}"])

    def exists_file(self, rel):
        return self._run(["mdir", "-b", f"::/{rel}"], check=False).returncode == 0

    def file_sha256(self, rel):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "f")
            self._run(["mcopy", "-n", f"::/{rel}", out])
            return sha256(out)

    def put_file(self, local, rel):
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self._run(["mmd", "::/" + "/".join(parts[:i])], check=False)
        self._run(["mcopy", "-o", "-n", local, f"::/{rel}"])


def install_app(backend, allow_update: bool = False, src_root: str = APP_ROOT) -> List[str]:
    """Stage -> copy to AI.new -> verify by checksum -> swap into AI (old kept as AI.prev)."""
    name, new, prev = C.APP_DIR_NAME, C.APP_DIR_NAME + ".new", C.APP_DIR_NAME + ".prev"
    log = []
    with tempfile.TemporaryDirectory() as td:
        stage = os.path.join(td, "stage")
        manifest = build_staging(stage, src_root)
        if backend.exists(name):
            old = backend.read_manifest(name)
            if old is None:
                raise CopyError(f"folder {name} already exists on the EFI partition and was not created by "
                                f"usbprep (no {C.MANIFEST_NAME}); refusing to overwrite it")
            if old.get("files") == manifest["files"]:
                return [f"app {old.get('version')} already present in {name} with identical checksums - unchanged"]
            if not allow_update:
                raise CopyError(f"{name} contains usbprep {old.get('version')}; new {manifest['version']} differs. "
                                f"Re-run with update enabled to replace it (old copy is kept as {prev}).")
        for leftover in (new,):
            if backend.exists(leftover):
                if backend.read_manifest(leftover) is None and backend.exists(leftover):
                    raise CopyError(f"unexpected folder {leftover} on the EFI partition; refusing")
                backend.rmtree(leftover)
        carried = {}
        for keep in CARRY:   # carry the existing offline installer cache into the new copy
            src = f"{name}/{keep}"
            if backend.exists(name) and backend.exists(src):
                tmp = os.path.join(td, "carry-" + keep)
                backend.get_tree(src, tmp)
                for rel, digest in hash_tree(tmp).items():
                    dst = os.path.join(stage, keep, *rel.split("/"))
                    if not os.path.exists(dst):
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copyfile(os.path.join(tmp, *rel.split("/")), dst)
                        carried[f"{keep}/{rel}"] = digest
        seed = os.path.join(src_root, DEFAULT_SCRIPTS)
        for rel, digest in (hash_tree(seed).items() if os.path.isdir(seed) else ()):
            dst = os.path.join(stage, DEFAULT_SCRIPTS, *rel.split("/"))
            if not os.path.exists(dst):            # never overwrite a user's (possibly edited) script
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(seed, *rel.split("/")), dst)
                carried[f"{DEFAULT_SCRIPTS}/{rel}"] = digest
        backend.put_tree(stage, new)
        back = os.path.join(td, "verify")
        backend.get_tree(new, back)
        bad = verify_tree(back, {"files": dict(manifest["files"], **carried)})
        if bad:
            backend.rmtree(new)
            raise CopyError("copy verification failed: " + "; ".join(bad[:5]))
        log.append(f"copied {len(manifest['files'])} files, all SHA-256 verified"
                   + (f"; kept {len(carried)} cached installer file(s) from {name}/Cache" if carried else ""))
        if backend.exists(name):
            if backend.exists(prev):
                if backend.read_manifest(prev) is None:
                    raise CopyError(f"{prev} exists but is not a usbprep copy; refusing")
                backend.rmtree(prev)
            backend.rename(name, prev)
            log.append(f"previous version kept in {prev}")
        backend.rename(new, name)
        log.append(f"app {manifest['version']} installed in EFI:/{name} (manifest {C.MANIFEST_NAME})")
    return log



def hash_tree(root: str) -> Dict[str, str]:
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP]
        for fn in fns:
            if not fn.endswith(SKIP):
                full = os.path.join(dp, fn)
                out[os.path.relpath(full, root).replace(os.sep, "/")] = sha256(full)
    return out


def restore_tree(backend, staged_dir: str, hashes: Dict[str, str]) -> List[str]:
    """Restore a verified backup of the whole AI folder onto a FRESH EFI partition (reformat path)."""
    name, new = C.APP_DIR_NAME, C.APP_DIR_NAME + ".new"
    if backend.exists(name) or backend.exists(new):
        raise CopyError(f"{name} already exists on the freshly formatted EFI partition; refusing to overwrite")
    backend.put_tree(staged_dir, new)
    with tempfile.TemporaryDirectory() as td:
        back = os.path.join(td, "verify")
        backend.get_tree(new, back)
        bad = verify_tree(back, {"files": hashes})
    if bad:
        raise CopyError("restore verification failed: " + "; ".join(bad[:5]))
    backend.rename(new, name)
    return [f"restored {len(hashes)} files to EFI:/{name}, all SHA-256 verified against the backup"]
