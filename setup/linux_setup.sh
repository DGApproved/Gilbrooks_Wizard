#!/bin/sh
# Project 1 USB prep - Linux host setup (consent-gated). POSIX sh.
#   sh linux_setup.sh [--dry-run] deps       install host tools (sudo password prompt)
#   sh linux_setup.sh [--dry-run] autorun    OPT-IN launch-on-insertion (udev rule + systemd user service)
#   sh linux_setup.sh [--dry-run] uninstall  remove everything 'autorun' installed (rollback)
#   sh linux_setup.sh status                 show what is installed (read-only)
# --dry-run prints every command and changes nothing. Passwords are typed into sudo only, never read here.
set -eu
DRY=0
[ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
ACTION="${1:-help}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
APPDIR="$HOME/.local/share/usbprep/app"
UNIT_DIR="$HOME/.config/systemd/user"
RULE=/etc/udev/rules.d/99-usbprep-launch.rules

run() { if [ "$DRY" = 1 ]; then echo "DRY-RUN: $*"; else echo "+ $*"; "$@"; fi; }

consent() {
  cat <<EOF
==================================================================
 Project 1 USB prep - host setup: $ACTION
==================================================================
$1
Nothing is formatted or mounted by this script. You can undo 'autorun' with: sh $0 uninstall
Type AGREE to continue (anything else aborts):
EOF
  read -r ans || ans=""
  [ "$ans" = "AGREE" ] || { echo "Aborted - no changes made."; exit 1; }
}

pkg_install() {
  if command -v apt-get >/dev/null 2>&1; then
    run sudo apt-get update
    run sudo apt-get install -y gdisk dosfstools e2fsprogs util-linux parted udisks2 mtools python3
    command -v java >/dev/null 2>&1 || run sudo apt-get install -y default-jre
    run sudo apt-get install -y pkexec || run sudo apt-get install -y policykit-1 || echo "note: pkexec unavailable; use sudo"
  elif command -v dnf >/dev/null 2>&1; then
    run sudo dnf install -y gdisk dosfstools e2fsprogs util-linux parted udisks2 mtools python3 polkit
    command -v java >/dev/null 2>&1 || run sudo dnf install -y java-latest-openjdk
  elif command -v pacman >/dev/null 2>&1; then
    run sudo pacman -S --needed --noconfirm gptfdisk dosfstools e2fsprogs util-linux parted udisks2 mtools python polkit
    command -v java >/dev/null 2>&1 || run sudo pacman -S --needed --noconfirm jre-openjdk
  elif command -v zypper >/dev/null 2>&1; then
    run sudo zypper --non-interactive install gptfdisk dosfstools e2fsprogs util-linux parted udisks2 mtools python3 polkit
    command -v java >/dev/null 2>&1 || run sudo zypper --non-interactive install java-17-openjdk
  else
    echo "Unknown package manager. Install manually: sgdisk(gdisk) dosfstools e2fsprogs util-linux parted udisks2 mtools python3, and a Java 8+ runtime (https://adoptium.net/temurin/releases/)"
    exit 1
  fi
}

case "$ACTION" in
  deps)
    consent "Installs distribution packages with your package manager (sudo will ask for YOUR password):
  gdisk (sgdisk), dosfstools, e2fsprogs, util-linux, parted, udisks2, mtools, python3, pkexec/polkit, and a distro OpenJDK runtime ONLY if no Java 8+
  is present (GPLv2 + Classpath Exception).
All are standard distro packages (GPL/LGPL/BSD-family licences); nothing is downloaded from elsewhere.
Rollback: remove them with your package manager if you wish (they are common system packages,
so this script does not remove them automatically)."
    pkg_install
    echo "Kernel drivers for vfat and ext4 are part of the Linux kernel - nothing to install for them." ;;
  autorun)
    consent "OPT-IN launch on insertion. Changes made:
  1. copies this app to $APPDIR (host copy, so it can start before the USB is mounted)
  2. writes $UNIT_DIR/usbprep-launch.service (systemd USER service, starts the GUI)
  3. writes $RULE (needs sudo) and reloads udev rules
The GUI only opens; mounting still needs your click + pkexec/sudo approval. Nothing autoruns from the USB itself."
    run mkdir -p "$APPDIR" "$UNIT_DIR"
    run cp -R "$HERE/usbprep" "$HERE/host_helpers" "$HERE/setup" "$APPDIR/"
    if [ "$DRY" = 1 ]; then echo "DRY-RUN: write $UNIT_DIR/usbprep-launch.service (WorkingDirectory=$APPDIR)"
    else sed "s#@APPDIR@#$APPDIR#" "$HERE/host_helpers/linux/usbprep-launch.service" > "$UNIT_DIR/usbprep-launch.service"; fi
    run sudo install -m 0644 "$HERE/host_helpers/linux/99-usbprep-launch.rules" "$RULE"
    run sudo udevadm control --reload-rules
    run systemctl --user daemon-reload
    echo "Done. Re-insert a prepared USB drive to test. Undo with: sh $0 uninstall" ;;
  uninstall)
    consent "Removes: $RULE (sudo), $UNIT_DIR/usbprep-launch.service, $APPDIR.
Packages installed by 'deps' are left in place (remove manually if desired)."
    run sudo rm -f "$RULE"
    run sudo udevadm control --reload-rules
    run rm -f "$UNIT_DIR/usbprep-launch.service"
    run systemctl --user daemon-reload
    run rm -rf "$APPDIR"
    echo "Rollback complete." ;;
  status)
    for f in "$RULE" "$UNIT_DIR/usbprep-launch.service" "$APPDIR"; do
      [ -e "$f" ] && echo "installed: $f" || echo "absent:    $f"; done ;;
  *)
    sed -n '2,8p' "$0" ;;
esac
