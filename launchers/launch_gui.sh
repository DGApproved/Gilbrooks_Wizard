#!/bin/sh
# Shared Linux/macOS GUI launcher: find (or install from the verified offline cache) Java 8+, then run the jar.
# A missing Java is never fatal: instructions + Retry. The Swing GUI drives the Python backend (python3 -m usbprep).
HERE=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1   # never write __pycache__ onto the FAT32 partition
while :; do
  JAVA=$(sh "$HERE/launchers/java_bootstrap.sh") && break
  if [ -t 0 ]; then
    printf 'Press Enter to retry after installing Java (or put an installer in the cache), or type q to quit: '
    read -r ans; [ "$ans" = q ] && exit 1
  elif command -v zenity >/dev/null 2>&1 && [ -n "$DISPLAY$WAYLAND_DISPLAY" ]; then
    zenity --question --title="Java 8+ needed" --ok-label=Retry --cancel-label=Quit \
      --text="Java 8 or newer is needed. Get a free OpenJDK build at https://adoptium.net/temurin/releases/ , install it, then press Retry." || exit 1
  elif [ "$(uname -s)" = Darwin ]; then
    osascript -e 'display dialog "Java 8 or newer is needed. Download a free OpenJDK build from https://adoptium.net/temurin/releases/ , install it, then press Retry." buttons {"Quit","Retry"} default button "Retry"' 2>/dev/null | grep -q Retry || exit 1
  else
    exit 1
  fi
done
cd "$HERE" && exec "$JAVA" -jar "$HERE/gui-java/dist/usbprep-gui.jar"
