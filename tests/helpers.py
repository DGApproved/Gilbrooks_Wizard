import copy
import json

from usbprep import constants as C
from usbprep.detect import parse_lsblk
from usbprep.planner import compute_geometry
from usbprep.runner import MockRunner

GiB = C.GiB


def prepared_lsblk_device(name="sdx", size=16 * GiB, serial="SER12345", model="Cruzer", mounts=(None, None, None)):
    g = compute_geometry(size, 512)
    parts = []
    for i, (p, guid, fs, ver, label) in enumerate(
            [(g.p1, C.GUID_ESP, "vfat", "FAT32", "EFI"), (g.p2, C.GUID_LINUX_FS, "ext4", "1.0", "AI"),
             (g.p3, C.GUID_LINUX_SWAP, "swap", "1", "SWAP")], 1):
        parts.append({"name": f"{name}{i}", "path": f"/dev/{name}{i}", "type": "part", "partn": i,
                      "start": p[0], "size": (p[1] - p[0] + 1) * 512, "parttype": guid.lower(),
                      "fstype": fs, "fsver": ver, "label": label, "mountpoints": [mounts[i - 1]]})
    return {"name": name, "path": f"/dev/{name}", "type": "disk", "tran": "usb", "rm": True, "rota": False,
            "size": size, "model": model, "serial": serial, "vendor": "SanDisk", "wwn": None, "log-sec": 512,
            "pttype": "gpt", "ptuuid": "ABCD-1", "mountpoints": [None], "children": parts,
            "subsystems": "block:scsi:usb:pci"}


def blank_lsblk_device(name="sdx", size=16 * GiB, serial="SER12345", model="Cruzer"):
    d = prepared_lsblk_device(name, size, serial, model)
    d.update(pttype=None, ptuuid=None, children=[])
    return d


def session_with(devs_holder, extra=None, source=None):
    """LinuxSession backed by a mutable list of lsblk device dicts (simulates replug/changes)."""
    from usbprep.workflow import LinuxSession

    def lsblk(argv):
        if argv[-1].startswith("/dev/"):
            for d in devs_holder:
                if d["path"] == argv[-1]:
                    return 0, json.dumps({"blockdevices": [d]}), ""
            return 1, "", "not found"
        return 0, json.dumps({"blockdevices": devs_holder}), ""

    r = MockRunner(responses={"lsblk": (lsblk, None, None), **(extra or {})})
    s = LinuxSession(runner=r, lister=lambda: parse_lsblk(json.dumps({"blockdevices": devs_holder})),
                     source_finder=lambda: source)
    return s, r


def clone(d):
    return copy.deepcopy(d)
