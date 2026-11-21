#!/bin/sh
# Project 1 USB prep - macOS helper (consent-gated). UNTESTED: written on Linux, never run on macOS.
#   sh macos_setup.sh [--dry-run] info        what works on macOS (default; read-only)
#   sh macos_setup.sh [--dry-run] mount-fat   mount the FAT32 'EFI' partition at /Volumes/USB (admin prompt)
#   sh macos_setup.sh [--dry-run] ext4-help   supported MANUAL options for ext4 access + licence/cost notes
#   sh macos_setup.sh [--dry-run] autorun     OPT-IN launchd agent that offers the helper when the USB mounts
#   sh macos_setup.sh [--dry-run] uninstall   remove the launchd agent + host copy (rollback)
#   sh macos_setup.sh [--dry-run] readonly-install   OPT-IN: macFUSE + ext4fuse via Homebrew (native prompts); saves backend=readonly
#   sh macos_setup.sh [--dry-run] readonly-mount  diskNsM   mount ext4 READ-ONLY with ext4fuse at /Volumes/AI, verify RO
#   sh macos_setup.sh [--dry-run] homebrew-install   OPT-IN (Apple Silicon only): anylinuxfs RW via Homebrew; saves backend=homebrew
#   sh macos_setup.sh [--dry-run] homebrew-mount  diskNsM   mount ext4 read/write with anylinuxfs (only if backend=homebrew)
set -eu
DRY=0
[ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
ACTION="${1:-info}"
HERE=$(cd "$(dirname "$0")/.." && pwd)
APPDIR="$HOME/Library/Application Support/usbprep/app"
AGENT="$HOME/Library/LaunchAgents/com.project1.usbprep.plist"
run() { if [ "$DRY" = 1 ]; then echo "DRY-RUN: $*"; else echo "+ $*"; "$@"; fi; }
consent() {
  printf '%s\n%s\nType AGREE to continue (anything else aborts):\n' "== Project 1 USB prep (macOS): $ACTION ==" "$1"
  read -r ans || ans=""
  [ "$ans" = "AGREE" ] || { echo "Aborted - no changes made."; exit 1; }
}

py() { (cd "$HERE" && python3 -m usbprep "$@"); }
saved_backend() { py backend status --os darwin --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("choice",""))' 2>/dev/null || echo native; }
check_disk() { case "${1:-}" in disk[0-9]*s[0-9]*) : ;; *) echo "Give the ext4 partition identifier, e.g. disk4s2 (see: diskutil list)"; exit 2;; esac; }

case "$ACTION" in
  readonly-install)
    command -v brew >/dev/null 2>&1 || { echo "BLOCKED: Homebrew not found (https://brew.sh). Install it yourself, then re-run. Nothing was changed."; exit 2; }
    consent "Installs, via Homebrew (the installation mechanism; NOT a driver):
  - macFUSE (cask; closed source, free for personal use - see https://macfuse.github.io licence)
  - ext4fuse (GPL-2.0, READ-ONLY ext4; Homebrew formula is DEPRECATED, disable date 2027-06-21)
macOS will ask for your password and to approve the macFUSE system extension (Apple Silicon may need
Recovery-mode 'Reduced Security'). This app never sees your password.
Then saves your choice: macOS backend = readonly (ext4fuse). No other driver is substituted.
Undo: brew uninstall ext4fuse; brew uninstall --cask macfuse; sh $0 ... and choose another backend."
    run brew install --cask macfuse
    run brew install ext4fuse || { echo "BLOCKED: ext4fuse could not be installed on this Mac (no fallback driver used)."; exit 2; }
    if [ "$DRY" = 1 ]; then echo "DRY-RUN: python3 -m usbprep backend set readonly --os darwin"; else py backend set readonly --os darwin; fi ;;
  readonly-mount)
    check_disk "${2:-}"
    B=$(saved_backend); [ "$B" = "readonly" ] || { echo "Saved macOS backend is '$B', not 'readonly' - refusing (no silent switch)."; exit 2; }
    command -v ext4fuse >/dev/null 2>&1 || { echo "BLOCKED: ext4fuse not installed. Run: readonly-install"; exit 2; }
    [ -e /Volumes/AI ] && [ -n "$(ls -A /Volumes/AI 2>/dev/null)" ] && { echo "/Volumes/AI exists and is not empty - refusing."; exit 1; }
    run sudo mkdir -p /Volumes/AI
    run sudo ext4fuse "/dev/$2" /Volumes/AI -o allow_other
    if [ "$DRY" = 1 ]; then echo "DRY-RUN: verify read-only"; exit 0; fi
    if (cd "$HERE" && python3 -c 'import sys; from usbprep.backends import verify_readonly_mount as v; r=v("/Volumes/AI"); print(r["reason"]); sys.exit(0 if r["verified"] else 1)'); then
      run open /Volumes/AI
    else echo "Read-only verification FAILED - unmounting."; run sudo umount /Volumes/AI; exit 1; fi ;;
  homebrew-install)
    [ "$(uname -m)" = "arm64" ] || { echo "BLOCKED: anylinuxfs supports Apple Silicon only. Intel Macs: use readonly-install (read-only)."; exit 2; }
    command -v brew >/dev/null 2>&1 || { echo "BLOCKED: Homebrew not found (https://brew.sh)."; exit 2; }
    consent "Installs anylinuxfs (GPL-3.0, https://github.com/nohajc/anylinuxfs) via Homebrew: read/write ext4
using the real Linux driver in a small VM, exported over NFS (file ownership may show as the NFS user).
Not tested by this project on a real Mac. Saves macOS backend = homebrew. Undo: brew uninstall anylinuxfs"
    run brew tap nohajc/anylinuxfs
    run brew install anylinuxfs || { echo "BLOCKED: anylinuxfs install failed."; exit 2; }
    if [ "$DRY" = 1 ]; then echo "DRY-RUN: python3 -m usbprep backend set homebrew --os darwin"; else py backend set homebrew --os darwin; fi ;;
  homebrew-mount)
    check_disk "${2:-}"
    B=$(saved_backend); [ "$B" = "homebrew" ] || { echo "Saved macOS backend is '$B', not 'homebrew' - refusing."; exit 2; }
    command -v anylinuxfs >/dev/null 2>&1 || { echo "BLOCKED: anylinuxfs not installed. Run: homebrew-install"; exit 2; }
    run sudo anylinuxfs "/dev/$2" ;;
  info)
    cat <<'EOF'
macOS status (see SUPPORT_MATRIX in the instructions):
  - FAT32 'EFI' partition: read/write natively (auto-mounts at /Volumes/EFI). Literal /USB is BLOCKED:
    the system volume is read-only (SIP / sealed system volume); /etc/synthetic.conf is a privileged
    host change that only creates links/empty dirs at boot. Proposed alternative: /Volumes/USB (mount-fat).
  - ext4 'AI' partition: read/write is BLOCKED natively (macOS has no ext4 driver). See: ext4-help
  - Preparing/formatting the drive: BLOCKED on macOS - prepare it on Linux.
  - Linux programs stored on the ext4 partition do not run on macOS even if ext4 is mounted.
EOF
    ;;
  mount-fat)
    consent "Finds the USB partition labelled EFI (GPT ESP) and remounts it at /Volumes/USB using
diskutil (admin password prompt via sudo). Nothing is formatted. Undo: diskutil unmount /Volumes/USB"
    DEV=$(diskutil info -plist /Volumes/EFI 2>/dev/null | plutil -extract DeviceIdentifier raw - 2>/dev/null || true)
    [ -n "$DEV" ] || { echo "No volume mounted at /Volumes/EFI found."; exit 1; }
    [ -f /Volumes/EFI/AI/USBPREP_MANIFEST.json ] || { echo "/Volumes/EFI is not a Project 1 USB (no AI/USBPREP_MANIFEST.json)."; exit 1; }
    if [ -e /Volumes/USB ] && [ -n "$(ls -A /Volumes/USB 2>/dev/null)" ]; then echo "/Volumes/USB exists and is not empty - refusing."; exit 1; fi
    run diskutil unmount "/dev/$DEV"
    run sudo mkdir -p /Volumes/USB
    run sudo diskutil mount -mountPoint /Volumes/USB "/dev/$DEV"
    run open /Volumes/USB ;;
  ext4-help)
    cat <<'EOF'
ext4 access on macOS - NOTHING is installed automatically (licensing + kernel-extension approval):
 1. macFUSE (https://macfuse.github.io) + fuse-ext2 (https://github.com/alperakcan/fuse-ext2, GPL-2.0)
    - macFUSE is closed source since 3.x/4.x; free for non-commercial use, commercial bundling/redistribution
      needs a paid licence from its author. It must be downloaded by YOU from the official site.
    - Requires approving a system extension (Apple Silicon: Recovery-mode "Reduced Security" setting).
    - fuse-ext2 write mode is flagged by its author as risky (possible data loss). Read-only is safer:
        sudo fuse-ext2 /dev/diskNs2 /Volumes/AI -o ro
 2. Paragon extFS for Mac (commercial, paid) - vendor-supported read/write.
 3. A Linux VM (UTM/Parallels/VMware) with USB passthrough, then use this app's Linux mode inside it.
 Linux binaries on the ext4 partition still will not execute natively on macOS.
EOF
    ;;
  autorun)
    consent "OPT-IN: copies this app to '$APPDIR' and installs the launchd agent
$AGENT (StartOnMount). On every volume mount it checks for /Volumes/EFI*/AI/USBPREP_MANIFEST.json and,
if present, opens this helper in Terminal. It never formats or mounts anything. Undo: sh $0 uninstall"
    run mkdir -p "$APPDIR" "$HOME/Library/LaunchAgents"
    run cp -R "$HERE/usbprep" "$HERE/host_helpers" "$HERE/setup" "$APPDIR/"
    if [ "$DRY" = 1 ]; then echo "DRY-RUN: write $AGENT"
    else sed "s#@APPDIR@#$APPDIR#" "$HERE/host_helpers/macos/com.project1.usbprep.plist" > "$AGENT"; fi
    run launchctl load "$AGENT" ;;
  uninstall)
    consent "Removes $AGENT (launchctl unload) and '$APPDIR'."
    run launchctl unload "$AGENT" || true
    run rm -f "$AGENT"
    run rm -rf "$APPDIR"
    echo "Rollback complete." ;;
  *) sed -n '2,8p' "$0" ;;
esac
