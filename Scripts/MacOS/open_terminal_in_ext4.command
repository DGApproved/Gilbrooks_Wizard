#!/bin/bash
# Default usbprep script (macOS): open Terminal.app in the verified ext4 (A.I.) mount.
# macOS cannot mount the ext4 partition natively (BLOCKED in the support matrix), so usbprep normally
# passes no mount path and this script explains that instead of opening some other folder.
M="${USBPREP_EXT4_MOUNT:-}"
if [ -z "$M" ] || [ ! -d "$M" ]; then
  echo "The A.I. (ext4) partition is not mounted on this Mac - macOS has no native ext4 support (see SUPPORT_MATRIX). Nothing opened." >&2
  exit 3
fi
exec open -a Terminal -- "$M"
