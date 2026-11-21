#!/bin/sh
# Builds gui-java/dist/usbprep-gui.jar with Java 8 bytecode AND Java 8 API checking (--release 8).
# Needs a JDK 9+ (for --release); a JDK 8 works too (falls back to -source/-target 8).
set -e
cd "$(dirname "$0")"
rm -rf build && mkdir -p build dist
if javac --release 8 -version >/dev/null 2>&1; then REL="--release 8"; else REL="-source 8 -target 8"; fi
javac $REL -Xlint:all -encoding UTF-8 -d build src/usbprep/gui/*.java
printf 'Main-Class: usbprep.gui.UsbPrepGui\n' > build/MANIFEST.MF
jar cfm dist/usbprep-gui.jar build/MANIFEST.MF -C build usbprep
echo "built dist/usbprep-gui.jar ($REL)"
