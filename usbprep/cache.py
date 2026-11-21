"""Offline prerequisite installer cache in EFI:/AI/Cache/{Windows,MacOS,Linux} (FAT32, readable on every OS).

A cached file is only ever USED if it is listed in that folder's cache_manifest.tsv with a SHA-256 that was
checked against the vendor's published checksum when it was added (`usbprep cache add` / `cache fetch-java`)
and the file still matches it. Files simply dropped into the folder are reported, never executed.

Manifest format (tab-separated, one header line; parsed by Python, POSIX sh and PowerShell alike):
    kind version os arch sha256 size file type license source
type: tar.gz / zip = per-user archive (no admin rights); msi (Windows) / pkg (macOS) = system installer.
Linux: only self-contained archives (e.g. Temurin JRE .tar.gz). Distro packages (.deb/.rpm) need a complete,
distro/version/arch-specific dependency closure, so they are NOT offered for offline install.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

CACHE_DIR = "Cache"
OS_DIRS = {"linux": "Linux", "windows": "Windows", "darwin": "MacOS"}
FIELDS = ["kind", "version", "os", "arch", "sha256", "size", "file", "type", "license", "source"]
MANIFEST = "cache_manifest.tsv"
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1        # FAT32 single-file limit
SPACE_MARGIN = 16 * 1024 ** 2
TYPES = {"linux": ("tar.gz",), "darwin": ("tar.gz", "pkg"), "windows": ("zip", "msi")}
ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot?os={os}&architecture={arch}&image_type=jre&vendor=eclipse"
ADOPTIUM_OS = {"linux": "linux", "windows": "windows", "darwin": "mac"}
JAVA_DOWNLOAD_PAGE = "https://adoptium.net/temurin/releases/"


class CacheError(RuntimeError):
    pass


def normalize_arch(machine: Optional[str] = None) -> str:
    m = (machine or platform.machine()).lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m, m)


def java_major(version: str) -> int:
    """'1.8.0_514' -> 8, '17.0.2' -> 17, '21' -> 21; 0 if unparsable."""
    m = re.match(r"(\d+)(?:\.(\d+))?", version or "")
    if not m:
        return 0
    return int(m.group(2) or 0) if m.group(1) == "1" else int(m.group(1))


def os_dir(ai_root: str, os_name: str) -> str:
    return os.path.join(ai_root, CACHE_DIR, OS_DIRS[os_name])


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(d: str) -> List[Dict[str, str]]:
    p = os.path.join(d, MANIFEST)
    if not os.path.isfile(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f):
            parts = line.rstrip("\r\n").split("\t")
            if i == 0 or not line.strip() or line.startswith("#"):
                continue
            if len(parts) == len(FIELDS):
                rows.append(dict(zip(FIELDS, parts)))
    return rows


def write_manifest(d: str, rows: List[Dict[str, str]]) -> None:
    """Atomic replace: a failed write never leaves a half-written manifest."""
    tmp = os.path.join(d, MANIFEST + ".partial")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(FIELDS) + "\n")
        for r in rows:
            f.write("\t".join(str(r[k]).replace("\t", " ") for k in FIELDS) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(d, MANIFEST))


def check_entry(d: str, e: Dict[str, str], os_name: str, arch: str, min_major: int = 8,
                kind: str = "java", verify_hash: bool = True) -> Optional[str]:
    """None if usable, else the reason it is rejected."""
    if e["kind"] != kind:
        return f"kind {e['kind']} != {kind}"
    if e["os"] != os_name:
        return f"built for {e['os']}, not {os_name}"
    if e["arch"] != arch:
        return f"architecture {e['arch']} does not match this machine ({arch})"
    if kind == "java" and java_major(e["version"]) < min_major:
        return f"Java {e['version']} is older than required {min_major}"
    if e["type"] not in TYPES[os_name]:
        return f"installer type {e['type']} is not supported offline on {os_name}"
    if not e["size"].isdigit() or int(e["size"]) > FAT32_MAX_FILE:
        return "size exceeds the FAT32 4 GiB single-file limit or is invalid"
    if not re.fullmatch(r"[0-9a-f]{64}", e["sha256"]):
        return "manifest has no valid SHA-256"
    p = os.path.join(d, e["file"])
    if os.path.basename(e["file"]) != e["file"] or not os.path.isfile(p):
        return f"file {e['file']} is missing"
    if os.path.getsize(p) != int(e["size"]):
        return "file size does not match the manifest (incomplete copy?)"
    if verify_hash and sha256(p) != e["sha256"]:
        return "SHA-256 does not match the verified vendor checksum - file NOT used"
    return None


def status(ai_root: str, os_name: str, arch: str, min_major: int = 8) -> dict:
    d = os_dir(ai_root, os_name)
    rows = read_manifest(d)
    usable, rejected = [], []
    for e in rows:
        why = check_entry(d, e, os_name, arch, min_major)
        (rejected.append({"file": e["file"], "reason": why}) if why else usable.append(e))
    listed = {e["file"] for e in rows} | {MANIFEST, MANIFEST + ".partial"}
    unlisted = sorted(f for f in (os.listdir(d) if os.path.isdir(d) else [])
                      if f not in listed and not f.lower().startswith("readme") and not f.endswith(".partial"))
    return {"dir": d, "os": os_name, "arch": arch, "usable": usable, "rejected": rejected,
            "unlisted_ignored": unlisted}


def add(ai_root: str, src: str, os_name: str, arch: str, version: str, expected_sha256: str,
        source: str = "manual", license_: str = "see vendor", kind: str = "java", type_: Optional[str] = None,
        disk_usage: Callable = shutil.disk_usage) -> Dict[str, str]:
    """Verify src against the vendor checksum, check FAT32 limits/space, copy atomically, record in manifest."""
    d = os_dir(ai_root, os_name)
    os.makedirs(d, exist_ok=True)
    name = os.path.basename(src)
    type_ = type_ or next((t for t in ("tar.gz", "zip", "msi", "pkg") if name.lower().endswith("." + t)), "")
    if type_ not in TYPES[os_name]:
        raise CacheError(f"{name}: type '{type_}' is not supported offline on {os_name} (supported: {TYPES[os_name]})")
    size = os.path.getsize(src)
    if size > FAT32_MAX_FILE:
        raise CacheError(f"{name} is {size} B: larger than the FAT32 4 GiB single-file limit; not cached")
    expected = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CacheError("a 64-hex-digit vendor SHA-256 checksum is required")
    got = sha256(src)
    if got != expected:
        raise CacheError(f"{name}: SHA-256 {got} does not match the vendor checksum {expected}; not cached")
    dst = os.path.join(d, name)
    existing = os.path.getsize(dst) if os.path.exists(dst) else 0
    if size - existing + SPACE_MARGIN > disk_usage(d).free:
        raise CacheError(f"not enough free space on the EFI partition for {name} ({size} B); cache unchanged")
    if os.path.abspath(src) != os.path.abspath(dst):
        tmp = dst + ".partial"
        try:
            shutil.copyfile(src, tmp)
            if sha256(tmp) != expected:
                raise CacheError("copy verification failed; cache unchanged")
            os.replace(tmp, dst)
        except (OSError, CacheError):
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
    entry = {"kind": kind, "version": version, "os": os_name, "arch": arch, "sha256": expected, "size": str(size),
             "file": name, "type": type_, "license": license_, "source": source}
    rows = [r for r in read_manifest(d) if r["file"] != name] + [entry]
    write_manifest(d, rows)
    return entry


def _http_get(url: str, dest: Optional[str] = None, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "usbprep-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    return dest


def adoptium_jre(os_name: str, arch: str, major: int = 8, http: Callable = _http_get) -> dict:
    """Metadata of the latest Eclipse Temurin JRE archive (GPLv2 + Classpath Exception) from the Adoptium API."""
    data = json.loads(http(ADOPTIUM_API.format(major=major, os=ADOPTIUM_OS[os_name], arch=arch)))
    if not data:
        raise CacheError(f"Adoptium has no Temurin {major} JRE for {os_name}/{arch}")
    pkg = data[0]["binary"]["package"]
    return {"name": pkg["name"], "link": pkg["link"], "sha256": pkg["checksum"], "size": int(pkg["size"]),
            "version": data[0]["version"]["openjdk_version"]}


def fetch_java(ai_root: str, os_name: str, arch: str, major: int = 8, http: Callable = _http_get,
               workdir: Optional[str] = None) -> Dict[str, str]:
    """Download (approved setup step) and cache a Temurin JRE archive, verified against the API checksum."""
    meta = adoptium_jre(os_name, arch, major, http)
    if meta["size"] > FAT32_MAX_FILE:
        raise CacheError("installer exceeds the FAT32 4 GiB limit")
    import tempfile
    with tempfile.TemporaryDirectory(dir=workdir) as td:
        tmp = http(meta["link"], os.path.join(td, meta["name"]))
        return add(ai_root, tmp, os_name, arch, meta["version"], meta["sha256"], source=meta["link"],
                   license_="GPL-2.0-with-classpath-exception (Eclipse Temurin)")
