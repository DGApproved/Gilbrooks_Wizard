"""Read a disk/image partition layout and check it against the exact target layout."""
import json
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from . import constants as C
from .runner import DEFAULT_RUNNER, CommandError


@dataclass
class Partition:
    number: int
    start: int          # bytes
    size: int           # bytes
    type_guid: str = ""
    fstype: str = ""    # vfat / ext4 / swap
    fsver: str = ""     # FAT32 / 1.0 ...
    label: str = ""
    path: str = ""      # /dev/sdb1 (devices only)

    @property
    def end(self) -> int:  # exclusive
        return self.start + self.size


@dataclass
class DiskLayout:
    disk_size: int
    sector_size: int = 512
    table: Optional[str] = None   # "gpt" | "dos" | None
    ptuuid: str = ""
    partitions: List[Partition] = field(default_factory=list)

    def last_usable_end(self) -> int:
        """Exclusive byte offset after the last GPT-usable sector."""
        ss = self.sector_size
        entry_sectors = -(-16384 // ss)
        return self.disk_size - (entry_sectors + 1) * ss

    def fingerprint(self) -> dict:
        """Stable summary used for TOCTOU comparison."""
        return {"table": self.table, "ptuuid": self.ptuuid, "size": self.disk_size,
                "parts": [(p.number, p.start, p.size, p.type_guid.upper(), p.fstype, p.label)
                          for p in self.partitions]}

    def to_dict(self):
        return asdict(self)


@dataclass
class LayoutCheck:
    prepared: bool
    problems: List[str]

    def summary(self) -> str:
        if self.prepared:
            return "PREPARED: exact target layout found (will be preserved, no formatting)."
        return "NOT PREPARED:\n  - " + "\n  - ".join(self.problems)


def _children_parts(dev: dict) -> List[Partition]:
    parts = []
    for i, ch in enumerate(dev.get("children") or [], 1):
        num = ch.get("partn") or i
        parts.append(Partition(
            number=int(num), start=int(ch.get("start") or 0) * 512, size=int(ch.get("size") or 0),
            type_guid=(ch.get("parttype") or "").upper(), fstype=ch.get("fstype") or "",
            fsver=ch.get("fsver") or "", label=ch.get("label") or "", path=ch.get("path") or ""))
    return parts


def from_lsblk_device(dev: dict) -> DiskLayout:
    """Build a layout from one lsblk -J -b -O device entry (lsblk START is in 512 B units)."""
    return DiskLayout(disk_size=int(dev.get("size") or 0), sector_size=int(dev.get("log-sec") or 512),
                      table=dev.get("pttype"), ptuuid=(dev.get("ptuuid") or "").upper(),
                      partitions=_children_parts(dev))


def read_device_layout(path: str, runner=DEFAULT_RUNNER) -> DiskLayout:
    out = runner.run(["lsblk", "-J", "-b", "-O", path]).stdout
    return from_lsblk_device(json.loads(out)["blockdevices"][0])


def read_image_layout(image: str, runner=DEFAULT_RUNNER) -> DiskLayout:
    """Read layout from a regular image file using sfdisk --json + blkid probe at offsets (no loop)."""
    import os
    size = os.path.getsize(image)
    lay = DiskLayout(disk_size=size)
    try:
        js = json.loads(runner.run(["sfdisk", "--json", image]).stdout)["partitiontable"]
    except (CommandError, ValueError, KeyError):
        return lay  # no partition table
    lay.table = js.get("label")
    lay.ptuuid = (js.get("id") or "").upper()
    lay.sector_size = int(js.get("sectorsize", 512))
    ss = lay.sector_size
    for p in js.get("partitions", []):
        node = p["node"]
        num = int("".join(ch for ch in node[len(image):] if ch.isdigit()) or 0)
        part = Partition(number=num, start=int(p["start"]) * ss, size=int(p["size"]) * ss,
                         type_guid=str(p.get("type", "")).upper())
        r = runner.run(["blkid", "-p", "-o", "export", "-O", str(part.start), "-S",
                        str(part.size), image], check=False)
        tags = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        part.fstype, part.label, part.fsver = tags.get("TYPE", ""), tags.get("LABEL", ""), tags.get("VERSION", "")
        lay.partitions.append(part)
    return lay


def check_layout(lay: DiskLayout) -> LayoutCheck:
    """Exact-match check. Any deviation => not prepared (drive would need re-preparation)."""
    pr: List[str] = []
    A = C.ALIGN_BYTES
    if lay.table != "gpt":
        pr.append(f"partition table is {lay.table or 'missing'}, expected GPT")
        return LayoutCheck(False, pr)
    parts = sorted(lay.partitions, key=lambda p: p.number)
    if [p.number for p in parts] != [1, 2, 3]:
        pr.append(f"expected exactly partitions 1,2,3; found {[p.number for p in parts]}")
        return LayoutCheck(False, pr)
    p1, p2, p3 = parts
    if not (p1.start < p2.start < p3.start):
        pr.append("partitions are not in on-disk order EFI, AI, SWAP")
    for p in parts:
        if p.start % A:
            pr.append(f"partition {p.number} start {p.start} not 1 MiB aligned")
    exp = [(p1, C.GUID_ESP, "vfat", C.LABEL_EFI), (p2, C.GUID_LINUX_FS, "ext4", C.LABEL_AI),
           (p3, C.GUID_LINUX_SWAP, "swap", C.LABEL_SWAP)]
    for p, guid, fs, label in exp:
        if p.type_guid.upper() != guid:
            pr.append(f"partition {p.number} type GUID {p.type_guid or '?'} != {guid}")
        if p.fstype != fs:
            pr.append(f"partition {p.number} filesystem {p.fstype or 'none'} != {fs}")
        if p.label != label:
            pr.append(f"partition {p.number} label '{p.label}' != '{label}'")
    if p1.fstype == "vfat" and p1.fsver and p1.fsver != "FAT32":
        pr.append(f"partition 1 is {p1.fsver}, expected FAT32")
    if p1.start != A:
        pr.append(f"partition 1 starts at {p1.start}, expected {A} (1 MiB)")
    if p1.size != C.EFI_SIZE:
        pr.append(f"partition 1 size {p1.size} != {C.EFI_SIZE} (4096 MiB)")
    if p3.size != C.SWAP_SIZE:
        pr.append(f"partition 3 size {p3.size} != {C.SWAP_SIZE} (243 MiB)")
    if p2.start - p1.end >= A or p2.start < p1.end:
        pr.append("partition 2 does not immediately follow partition 1")
    if p3.start - p2.end >= A or p3.start < p2.end:
        pr.append("partition 2 does not extend to partition 3 (unused gap in the middle)")
    tail = lay.last_usable_end() - p3.end
    if tail < 0 or tail >= A:
        pr.append(f"partition 3 is not at the end of the disk ({tail} bytes unused after it)")
    if p2.size < C.MIN_AI_SIZE:
        pr.append(f"partition 2 size {p2.size} below minimum {C.MIN_AI_SIZE}")
    return LayoutCheck(not pr, pr)
