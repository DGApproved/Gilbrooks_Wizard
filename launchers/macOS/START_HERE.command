#!/bin/sh
# macOS entry point (double-click in Finder, or: sh START_HERE.command).
#   (no argument) -> Java 8+ GUI (detection/support matrix; ext4 read/write is BLOCKED natively on macOS)
#   setup ...     -> consent-gated macOS helper (FAT32 at /Volumes/USB, ext4 options, opt-in autorun)
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
if [ "$1" = "setup" ]; then shift; exec sh "$HERE/setup/macos_setup.sh" "$@"; fi
exec sh "$HERE/launchers/launch_gui.sh"
