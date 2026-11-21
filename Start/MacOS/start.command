#!/bin/sh
# FAT32 /AI/Start/MacOS/start.command - double-click in Finder. Delegates to launchers/macOS/START_HERE.command.
HERE=$(cd "$(dirname "$0")/../.." && pwd)
exec sh "$HERE/launchers/macOS/START_HERE.command" "$@"
