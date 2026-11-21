"""Exact target layout and shared constants.

"4GB / 4096MB" in the requirements is interpreted as 4096 MiB (4 GiB) and
"243MB" as 243 MiB. All partitions are aligned to 1 MiB.
"""
MiB = 1024 * 1024
GiB = 1024 * MiB

ALIGN_BYTES = 1 * MiB
EFI_SIZE = 4096 * MiB
SWAP_SIZE = 243 * MiB
MIN_AI_SIZE = 1 * GiB
# 1 MiB before p1 (GPT header + alignment) + 1 MiB tail (backup GPT + alignment)
OVERHEAD = 2 * MiB
MIN_DEVICE_SIZE = EFI_SIZE + MIN_AI_SIZE + SWAP_SIZE + OVERHEAD  # 5365 MiB
# Upper sanity cap for "flash drive" heuristics (bigger => not eligible)
MAX_FLASH_SIZE = 2 * 1024 * GiB

GUID_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GUID_LINUX_FS = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
GUID_LINUX_SWAP = "0657FD6D-A4AB-43C4-84E5-0933C84B4F4F"

LABEL_EFI = "EFI"
LABEL_AI = "AI"
LABEL_SWAP = "SWAP"

# Linux literal mount points
MOUNT_FAT = "/USB"
MOUNT_EXT4_NAME = "A.I."
APP_DIR_NAME = "AI"  # folder inside the FAT32 (EFI) partition
MANIFEST_NAME = "USBPREP_MANIFEST.json"

# Tests set nothing; real-device writes from tests are refused unless this is "1".
REAL_DEVICE_ENV = "USBPREP_ALLOW_REAL_DEVICE"

# Capability status vocabulary
WORKING_TESTED = "WORKING+TESTED"
IMPLEMENTED_UNTESTED = "IMPLEMENTED-UNTESTED"
BLOCKED = "BLOCKED"
ALTERNATIVE = "ALTERNATIVE-PROPOSED"
