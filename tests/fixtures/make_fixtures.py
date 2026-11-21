"""Regenerates lsblk_mixed.json (fixture shaped like `lsblk -J -b -O` output). Run manually."""
import json
import os

GiB = 1024 ** 3


def disk(name, tran, rm, rota, size, model, serial, vendor="", children=None, mounts=None, subsystems=None, typ="disk"):
    return {"name": name, "path": f"/dev/{name}", "type": typ, "tran": tran, "rm": rm, "rota": rota, "size": size,
            "model": model, "serial": serial, "vendor": vendor, "wwn": None, "log-sec": 512, "pttype": None,
            "ptuuid": None, "mountpoints": mounts or [None], "children": children or [],
            "subsystems": subsystems or ("block:scsi:usb:pci" if tran == "usb" else "block:nvme:pci")}


def part(name, mounts=None, fstype="vfat"):
    return {"name": name, "path": f"/dev/{name}", "type": "part", "fstype": fstype, "mountpoints": mounts or [None]}


devs = [
    disk("nvme0n1", "nvme", False, False, 512 * GiB, "Samsung SSD 980", "S1", children=[
        part("nvme0n1p1", ["/boot/efi"]), part("nvme0n1p2", ["/"], "ext4")]),
    disk("sda", "usb", True, False, 32 * GiB, "Cruzer Blade", "4C530001", "SanDisk"),
    disk("sdb", "usb", False, True, 2000 * GiB, "Elements 25A2", "WX11", "WD"),
    disk("sdc", "usb", False, False, 1000 * GiB, "Portable SSD T7", "S5T7", "Samsung"),
    disk("sdd", "usb", True, False, 0, "SD/MMC Reader", "000000001", "Generic"),
    disk("sde", "usb", True, False, 64 * GiB, "SD/MMC Card Reader", "000000002", "Generic"),
    disk("sdf", "usb", False, False, 256 * GiB, "JMS578 USB3.0 bridge", "DD5641", "JMicron"),
    disk("sdg", "usb", True, False, 4 * GiB, "DataTraveler 2.0", "KT4G", "Kingston"),
    disk("sdh", "usb", True, False, 16 * GiB, "Flash Disk", "FD16", "Generic", children=[
        part("sdh1", ["/media/user/STICK"])]),
    disk("sdi", "usb", True, False, 16 * GiB, "Flash Disk", "FDROOT", "Generic", children=[
        part("sdi1", ["/"], "ext4")]),
    disk("sdj", "usb", True, None, 8 * GiB, "Ultra", "", "SanDisk"),
    disk("loop0", None, False, False, 30 * 1024 ** 2, None, None, typ="loop"),
    disk("sr0", "sata", True, False, 1024 ** 3, "DVD", "X", typ="rom"),
]
with open(os.path.join(os.path.dirname(__file__), "lsblk_mixed.json"), "w") as f:
    json.dump({"blockdevices": devs}, f, indent=1)
