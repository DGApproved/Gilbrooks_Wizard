"""USB flash-drive detection (Linux lsblk) with fail-closed heuristics.

Status values:
  eligible  - looks like a USB flash drive; may be selected
  ambiguous - USB storage but type uncertain; selectable ONLY with explicit override acknowledgement
  excluded  - never selectable (internal, system, HDD, SSD, empty reader, too small, ...)
"""
import json
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from . import constants as C
from .runner import DEFAULT_RUNNER

SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "/opt", "/srv"}
SWAP_MARK = "[SWAP]"
USER_MOUNT_PREFIXES = ("/media/", "/run/media/", "/mnt/", C.MOUNT_FAT)
HDD_SSD_WORDS = re.compile(r"\b(SSD|NVME|HDD|PASSPORT|ELEMENTS|EXPANSION|BACKUP\s*PLUS|PORTABLE\s*DRIVE|"
                           r"EXTREME\s*PRO\s*SSD|T5|T7|T9|X5|X9|SATA|HARD\s*DISK|ST\d{3,}|WDC|TOSHIBA\s*EXT)\b", re.I)
BRIDGE_WORDS = re.compile(r"(JMS5\d\d|ASM1\d\d\d|ASM2\d\d\d|RTL92\d\d|JMICRON|ASMEDIA|USB3\.0\s*TO|"
                          r"ENCLOSURE|NVME\s*BRIDGE)", re.I)
READER_WORDS = re.compile(r"(CARD\s*READER|SD/MMC|MULTI[- ]?CARD|MULTI-READER|MMC/SD|CF/SD|"
                          r"SDXC|MICROSD|STORAGE\s*DEVICE|FLASH\s*READER|CARD\s*RDR)", re.I)


def human(n: Optional[int]) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


@dataclass
class Device:
    path: str
    name: str = ""
    model: str = ""
    vendor: str = ""
    serial: str = ""
    size: int = 0
    transport: str = ""
    removable: Optional[bool] = None
    rotational: Optional[bool] = None
    wwn: str = ""
    sector_size: int = 512
    mountpoints: List[str] = field(default_factory=list)
    status: str = "excluded"
    reasons: List[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)
    detected_status: str = ""          # what detection alone said (before any star)
    starred: str = ""                  # "" | "saved" | "session"
    hard_reasons: List[str] = field(default_factory=list)    # never overridable (system/internal/no medium...)
    soft_reasons: List[str] = field(default_factory=list, repr=False)    # flash-vs-not exclusions (star overrides)
    amb_reasons: List[str] = field(default_factory=list, repr=False)     # flash-vs-not doubts (star overrides)
    sticky_reasons: List[str] = field(default_factory=list, repr=False)  # doubts a star does NOT clear (active swap)

    @property
    def size_human(self):
        return human(self.size)

    def identity(self) -> dict:
        """Stable identity used for TOCTOU revalidation."""
        return {"path": self.path, "model": self.model, "serial": self.serial,
                "size": self.size, "wwn": self.wwn, "transport": self.transport}

    def confirm_token(self) -> str:
        tail = (self.serial or self.name or self.path.rsplit("/", 1)[-1])[-4:]
        return f"ERASE {tail}"

    def to_dict(self):
        from . import stars
        d = asdict(self)
        for k in ("raw", "soft_reasons", "amb_reasons", "sticky_reasons"):
            d.pop(k, None)
        d["size_human"] = self.size_human
        d["confirm_token"] = self.confirm_token()
        why = stars.star_refusal(self)
        d["star_allowed"] = why is None
        d["star_refusal"] = why or ""
        d["detected_dedicated"] = self.detected_status == "eligible"
        d["serial_present"] = bool(self.serial.strip())
        d["star_session_key"] = stars.session_key(self)
        return d


def finish(d: "Device", hard, soft, amb, sticky=(), ok="USB flash drive") -> "Device":
    """Common status decision for every platform: detection result first, then the user's star (if any)."""
    d.hard_reasons, d.soft_reasons, d.amb_reasons, d.sticky_reasons = list(hard), list(soft), list(amb), list(sticky)
    if hard or soft:
        d.detected_status, d.reasons = "excluded", list(hard) + list(soft) + list(amb) + list(sticky)
    elif amb or sticky:
        d.detected_status, d.reasons = "ambiguous", list(amb) + list(sticky)
    else:
        d.detected_status, d.reasons = "eligible", [ok]
    d.status = d.detected_status
    from . import stars
    return stars.apply(d)


def _b(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip() in ("1", "true", "True")


def _mounts(node: dict) -> List[str]:
    """All mount points of a node and its descendants (partitions, LVM, crypt), de-duplicated."""
    mps = node.get("mountpoints")
    if mps is None and node.get("mountpoint") is not None:
        mps = [node.get("mountpoint")]
    out = [m for m in (mps or []) if m]
    for ch in node.get("children") or []:
        out += _mounts(ch)
    return list(dict.fromkeys(out))


def classify(dev: dict, root_disks: Optional[set] = None) -> Optional[Device]:
    """Classify one top-level lsblk entry. Returns None for non-disk entries (loop, rom...)."""
    if dev.get("type") != "disk":
        return None
    d = Device(path=dev.get("path") or f"/dev/{dev.get('name')}", name=dev.get("name") or "",
               model=(dev.get("model") or "").strip(), vendor=(dev.get("vendor") or "").strip(),
               serial=(dev.get("serial") or "").strip(), size=int(dev.get("size") or 0),
               transport=(dev.get("tran") or ""), removable=_b(dev.get("rm")),
               rotational=_b(dev.get("rota")), wwn=dev.get("wwn") or "",
               sector_size=int(dev.get("log-sec") or 512), mountpoints=_mounts(dev), raw=dev)
    text = f"{d.vendor} {d.model}"
    is_usb = d.transport == "usb" or ":usb" in (dev.get("subsystems") or "")
    hard, excl, amb, sticky = [], [], [], []
    if not is_usb:
        hard.append(f"not a USB device (transport={d.transport or 'none'}) - internal disk or unknown bus")
    if root_disks and d.name in root_disks:
        hard.append("hosts the running system (root/boot disk)")
    sys_mounts = [m for m in d.mountpoints
                  if m != SWAP_MARK and (m in SYSTEM_MOUNTS or not m.startswith(USER_MOUNT_PREFIXES))]
    if sys_mounts:
        hard.append(f"system mount points present: {', '.join(sys_mounts)}")
    if d.size == 0:
        hard.append("no medium (empty card reader or no media)")
    if d.rotational:
        excl.append("rotational device (USB hard disk)")
    if HDD_SSD_WORDS.search(text):
        excl.append(f"model '{text.strip()}' identifies an external SSD/HDD")
    if d.size > C.MAX_FLASH_SIZE:
        excl.append(f"size {d.size_human} exceeds flash-drive sanity cap {human(C.MAX_FLASH_SIZE)}")
    if 0 < d.size < C.MIN_DEVICE_SIZE:
        hard.append(f"too small: {d.size_human} < required {human(C.MIN_DEVICE_SIZE)} ({C.MIN_DEVICE_SIZE} B)")
    if BRIDGE_WORDS.search(text):
        amb.append(f"model '{text.strip()}' looks like a SATA/NVMe bridge (external drive likely)")
    if READER_WORDS.search(text) and d.size > 0:
        amb.append("card reader with media (SD/microSD card, not a flash drive)")
    if d.removable is False:
        amb.append("removable flag not set (some flash drives do this, but so do SSDs)")
    if d.rotational is None:
        amb.append("rotational flag unknown")
    if d.size > 512 * C.GiB:
        amb.append(f"large capacity {d.size_human} for a flash drive")
    if SWAP_MARK in d.mountpoints:
        sticky.append("swap on this drive is active (the host may rely on it): explicit override required; "
                      "only Reformat / Hand drive to VM turn it off (swapoff) before continuing")
    if is_usb and not d.serial:
        amb.append("no serial number reported (identity cannot be pinned strongly)")
    finish(d, hard, excl, amb, sticky, "USB, removable, non-rotational, plausible flash size")
    other = [m for m in d.mountpoints if m not in sys_mounts and m != SWAP_MARK]
    if other and d.status != "excluded":
        d.reasons.append(f"currently mounted at {', '.join(other)} (must be unmounted before formatting)")
    return d


def root_disk_names(lsblk_json: dict) -> set:
    names = set()
    for dev in lsblk_json.get("blockdevices", []):
        if any(m in SYSTEM_MOUNTS for m in _mounts(dev)):
            names.add(dev.get("name"))
    return names


def parse_lsblk(text: str) -> List[Device]:
    js = json.loads(text)
    roots = root_disk_names(js)
    return [d for d in (classify(x, roots) for x in js.get("blockdevices", [])) if d]


def list_linux_devices(runner=DEFAULT_RUNNER) -> List[Device]:
    return parse_lsblk(runner.run(["lsblk", "-J", "-b", "-O"]).stdout)


def usb_candidates(devices: List[Device]) -> List[Device]:
    """USB devices worth showing to the user: eligible, ambiguous, starred, or USB drives that detection did not
    classify as flash (so they can be starred). Never the system disk, a disk with mounted host system
    partitions, or a non-USB / unknown-bus disk."""
    def never_listed(d):  # every hard refusal except 'no medium' / 'too small' (shown greyed, star disabled)
        return any(not r.startswith(("no medium", "too small")) for r in d.hard_reasons)
    return [d for d in devices if (d.transport == "usb" or d.status != "excluded") and not never_listed(d)]


def selectable(dev: Device, allow_ambiguous: bool = False) -> Optional[str]:
    """Return None if the device may be selected, else the refusal reason."""
    if dev.status == "eligible":
        return None
    if dev.status == "ambiguous":
        return None if allow_ambiguous else ("ambiguous device type - explicit override acknowledgement "
                                             "required: " + "; ".join(dev.reasons))
    return "not eligible: " + "; ".join(dev.reasons)
