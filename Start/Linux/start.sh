#!/bin/sh
# FAT32 /AI/Start/Linux/start.sh - start Project 1 USB prep on Linux. Delegates to launchers/Linux/run.sh.
HERE=$(cd "$(dirname "$0")/../.." && pwd)
exec sh "$HERE/launchers/Linux/run.sh" "$@"
