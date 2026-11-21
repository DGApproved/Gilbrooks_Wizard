#!/bin/sh
# Default usbprep script: open a terminal whose working directory is the verified ext4 (A.I.) mount.
# usbprep passes the VERIFIED mount path in USBPREP_EXT4_MOUNT (it is never interpolated into a shell string).
# No fallback: if the drive is not mounted, nothing is opened.
M="${USBPREP_EXT4_MOUNT:-}"
if [ -z "$M" ] || [ ! -d "$M" ]; then
  echo "The A.I. (ext4) partition of the selected USB is not mounted. Use Mount first, then run this again." >&2
  exit 3
fi
for t in x-terminal-emulator gnome-terminal konsole xfce4-terminal mate-terminal tilix kitty alacritty xterm; do
  command -v "$t" >/dev/null 2>&1 || continue
  case "$t" in
    gnome-terminal|xfce4-terminal|mate-terminal|tilix) exec "$t" --working-directory="$M" ;;
    konsole) exec konsole --workdir "$M" ;;
    *) cd -- "$M" && exec "$t" ;;
  esac
done
echo "No terminal emulator found (tried x-terminal-emulator, gnome-terminal, konsole, xfce4-terminal, mate-terminal, tilix, kitty, alacritty, xterm). Install one, e.g. 'sudo apt install xterm'. The A.I. folder is: $M" >&2
exit 4
