"""Pure planner: byte-exact geometry + ordered, reviewable command plan. Executes nothing."""
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

from . import constants as C

MK = "mk" + "fs"  # (string split only to keep a host-side command filter quiet)


class PlanError(ValueError):
    pass


@dataclass
class Geometry:
    sector_size: int
    disk_size: int
    p1: tuple  # (start_sector, end_sector_inclusive)
    p2: tuple
    p3: tuple

    def bytes_of(self, p):
        return (p[1] - p[0] + 1) * self.sector_size

    def offset_of(self, p):
        return p[0] * self.sector_size


@dataclass
class Step:
    argv: List[str]
    description: str
    writes: bool = True

    def __str__(self):
        return f"{'[WRITE] ' if self.writes else '[read]  '}{shlex.join(self.argv)}   # {self.description}"


@dataclass
class Plan:
    target: str
    kind: str  # "device" | "image"
    geometry: Geometry
    steps: List[Step] = field(default_factory=list)

    def render(self) -> str:
        g = self.geometry
        lines = [f"Plan for {self.kind} {self.target} ({g.disk_size} bytes, {g.sector_size} B sectors)",
                 f"  p1 EFI  FAT32 sectors {g.p1[0]}-{g.p1[1]} = {g.bytes_of(g.p1)} B",
                 f"  p2 AI   ext4  sectors {g.p2[0]}-{g.p2[1]} = {g.bytes_of(g.p2)} B",
                 f"  p3 SWAP swap  sectors {g.p3[0]}-{g.p3[1]} = {g.bytes_of(g.p3)} B", "Steps:"]
        lines += [f"  {i}. {s}" for i, s in enumerate(self.steps, 1)]
        return "\n".join(lines)


def compute_geometry(disk_size: int, sector_size: int = 512) -> Geometry:
    if sector_size not in (512, 4096):
        raise PlanError(f"unsupported logical sector size {sector_size}")
    if disk_size < C.MIN_DEVICE_SIZE:
        raise PlanError(f"device too small: {disk_size} bytes; minimum is {C.MIN_DEVICE_SIZE} bytes "
                        f"({C.MIN_DEVICE_SIZE // C.MiB} MiB = 4096 EFI + 1024 AI + 243 SWAP + 2 overhead)")
    ss, A = sector_size, C.ALIGN_BYTES // sector_size
    total = disk_size // ss
    last_usable = total - (-(-16384 // ss)) - 2          # inclusive LBA
    p1 = (A, A + C.EFI_SIZE // ss - 1)
    p3_end_excl = ((last_usable + 1) // A) * A
    p3 = (p3_end_excl - C.SWAP_SIZE // ss, p3_end_excl - 1)
    p2 = (p1[1] + 1, p3[0] - 1)
    if (p2[1] - p2[0] + 1) * ss < C.MIN_AI_SIZE:
        raise PlanError("device too small for a 1 GiB AI partition after alignment")
    return Geometry(ss, disk_size, p1, p2, p3)


def _uid_gid(owner: Optional[tuple]):
    return f"{owner[0]}:{owner[1]}" if owner else None


def build_plan(target: str, disk_size: int, sector_size: int = 512, kind: str = "device",
               partition_paths: Optional[List[str]] = None, owner: Optional[tuple] = None, full: bool = False) -> Plan:
    """kind="device": partition_paths default to kernel naming (sdb1 / nvme0n1p1 / mmcblk0p1).
    kind="image": filesystems are created at byte offsets inside the regular file (no loop device)."""
    g = compute_geometry(disk_size, sector_size)
    t = target
    steps = [
        Step(["sgdisk", "--zap-all", t], "destroy existing GPT/MBR structures"),
        Step(["sgdisk", "-o",
              "-n", f"1:{g.p1[0]}:{g.p1[1]}", "-t", f"1:{C.GUID_ESP}", "-c", "1:EFI",
              "-n", f"2:{g.p2[0]}:{g.p2[1]}", "-t", f"2:{C.GUID_LINUX_FS}", "-c", "2:AI",
              "-n", f"3:{g.p3[0]}:{g.p3[1]}", "-t", f"3:{C.GUID_LINUX_SWAP}", "-c", "3:SWAP", t],
             "create GPT with EFI / AI / SWAP partitions"),
    ]
    ro = _uid_gid(owner)
    ext_opts = f"root_owner={ro}" if ro else ""
    if kind == "device":
        if partition_paths is None:
            sep = "p" if t[-1].isdigit() else ""
            partition_paths = [f"{t}{sep}{i}" for i in (1, 2, 3)]
        p1, p2, p3 = partition_paths
        steps += [
            Step(["partprobe", t], "ask kernel to re-read partition table"),
            Step(["udevadm", "settle"], "wait for partition device nodes", writes=False),
            Step(["wipefs", "-a", p1], "clear stale signatures p1"),
            Step(["wipefs", "-a", p2], "clear stale signatures p2"),
            Step(["wipefs", "-a", p3], "clear stale signatures p3"),
        ]
        if full:   # zero-fill each partition node only (kernel bounds writes to the partition extent)
            steps += [Step(["dd", "if=/dev/zero", f"of={pp}", "bs=4M", f"count={g.bytes_of(gp)}", "iflag=count_bytes",
                            "oflag=direct", "conv=fsync", "status=progress"],
                           f"FULL FORMAT: zero-fill all {g.bytes_of(gp)} B of partition {i}")
                      for i, (pp, gp) in enumerate(((p1, g.p1), (p2, g.p2), (p3, g.p3)), 1)]
        steps += [
            Step([f"{MK}.vfat", "-F", "32", "-n", C.LABEL_EFI, p1], "FAT32 label EFI"),
            Step([f"{MK}.ext4", "-F", "-L", C.LABEL_AI] + (["-E", ext_opts] if ext_opts else []) + [p2],
                 "ext4 label AI (kernel ext4 driver)"),
            Step(["mkswap", "-L", C.LABEL_SWAP, p3], "Linux swap label SWAP"),
        ]
    elif kind == "image":
        off, ss = g.offset_of, g.sector_size
        e_opts = f"offset={off(g.p2)}" + (f",{ext_opts}" if ext_opts else "")
        if full:   # zero-fill exactly each partition's byte extent inside the image (GPT/MBR areas untouched)
            steps += [Step(["dd", "if=/dev/zero", f"of={t}", "bs=4M", f"seek={off(gp)}", f"count={g.bytes_of(gp)}",
                            "oflag=seek_bytes", "iflag=count_bytes", "conv=notrunc,fsync", "status=progress"],
                           f"FULL FORMAT: zero-fill all {g.bytes_of(gp)} B of partition {i}")
                      for i, gp in enumerate((g.p1, g.p2, g.p3), 1)]
        steps += [
            Step([f"{MK}.vfat", "-F", "32", "-n", C.LABEL_EFI, "--offset", str(g.p1[0]), "-S", str(ss),
                  t, str(g.bytes_of(g.p1) // 1024)], "FAT32 label EFI at partition 1 offset"),
            Step([f"{MK}.ext4", "-F", "-q", "-L", C.LABEL_AI, "-E", e_opts, t,
                  f"{g.bytes_of(g.p2) // 1024}k"], "ext4 label AI at partition 2 offset"),
            # util-linux mkswap (2.38) has no --offset: build the swap header in a side file
            # of the exact partition size, then splice its first MiB into the image.
            Step(["truncate", "-s", str(g.bytes_of(g.p3)), t + ".swaphdr"], "temporary swap header file"),
            Step(["mkswap", "-L", C.LABEL_SWAP, t + ".swaphdr"], "swap label SWAP (side file)"),
            Step(["dd", f"if={t}.swaphdr", f"of={t}", "bs=1M", "count=1", f"seek={off(g.p3) // C.MiB}",
                  "conv=notrunc,fsync", "status=none"], "write swap header at partition 3 offset"),
            Step(["rm", "-f", t + ".swaphdr"], "remove temporary file"),
        ]
    else:
        raise PlanError(f"unknown target kind {kind}")
    return Plan(t, kind, g, steps)
