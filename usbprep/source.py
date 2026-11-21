"""Identify the physical USB this app is running from (Linux), verified - never guessed."""
import json
import os
from typing import Optional

from . import constants as C
from .appcopy import APP_ROOT
from .layout import check_layout, read_device_layout
from .runner import DEFAULT_RUNNER, CommandError


def find_source_disk(app_root: str = APP_ROOT, runner=DEFAULT_RUNNER) -> Optional[str]:
    """Return '/dev/sdX' only if ALL hold: app_root is on a mounted partition (findmnt), that partition's
    parent disk (lsblk PKNAME) has the exact prepared layout, the partition is p1 (EFI), and the app
    folder carries our manifest. Otherwise None (caller leaves selection explicit)."""
    try:
        if not os.path.isfile(os.path.join(app_root, C.MANIFEST_NAME)):
            return None
        fm = json.loads(runner.run(["findmnt", "-J", "-T", app_root]).stdout)["filesystems"][0]
        part = fm.get("source", "")
        if not part.startswith("/dev/") or fm.get("fstype") != "vfat":
            return None
        pk = json.loads(runner.run(["lsblk", "-J", "-o", "PKNAME", part]).stdout)["blockdevices"][0]["pkname"]
        if not pk:
            return None
        disk = f"/dev/{pk}"
        lay = read_device_layout(disk, runner)
        if not check_layout(lay).prepared:
            return None
        p1 = sorted(lay.partitions, key=lambda p: p.number)[0]
        if os.path.realpath(p1.path) != os.path.realpath(part):
            return None
        return disk
    except (CommandError, ValueError, KeyError, IndexError, TypeError, OSError):
        return None
