#!/bin/sh
# Linux entry point. Works from EFI:/AI on the USB or from the source tree.
# Usage: sh run.sh            -> Java 8+ Swing GUI (Java found or installed from the verified offline cache)
#        sh run.sh list       -> CLI (any usbprep subcommand; dry-run by default; needs only Python 3)
#        sh run.sh setup ...  -> consent-gated host setup (setup/linux_setup.sh)
HERE=$(cd "$(dirname "$0")/../.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
if [ "$1" = "setup" ]; then shift; exec sh "$HERE/setup/linux_setup.sh" "$@"; fi
if [ $# -eq 0 ]; then exec sh "$HERE/launchers/launch_gui.sh"; fi
cd "$HERE" && exec python3 -m usbprep "$@"
