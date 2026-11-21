#!/bin/sh
# Called by the opt-in launchd agent on every volume mount. Opens Terminal with the helper only
# when a prepared Project 1 USB (FAT32 label EFI containing AI/USBPREP_MANIFEST.json) is present.
for v in /Volumes/EFI*; do
  if [ -f "$v/AI/USBPREP_MANIFEST.json" ]; then
    open -a Terminal "$v/AI/launchers/macOS/START_HERE.command"
    exit 0
  fi
done
exit 0
