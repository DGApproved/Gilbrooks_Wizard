#!/bin/sh
# Build the BIOS boot code with NASM (as the normal user; never writes any disk).
# Output: hello_bios.bin (<=440 bytes) + BUILD_INFO.json (hash of the sources it was built from).
set -eu
cd "$(dirname "$0")"
command -v nasm >/dev/null 2>&1 || { echo "nasm not found - install it (e.g. sudo apt install nasm)" >&2; exit 2; }
nasm -f bin -o hello_bios.bin.tmp hello.asm
size=$(wc -c < hello_bios.bin.tmp)
[ "$size" -le 440 ] || { rm -f hello_bios.bin.tmp; echo "boot code is $size bytes (> 440)" >&2; exit 3; }
mv hello_bios.bin.tmp hello_bios.bin
src=$(sha256sum hello.asm build.sh | sha256sum | cut -d' ' -f1)
art=$(sha256sum hello_bios.bin | cut -d' ' -f1)
printf '{"component": "BIOS", "artifact": "hello_bios.bin", "artifact_sha256": "%s", "sources": ["hello.asm", "build.sh"], "source_sha256": "%s", "tool": "%s"}\n' \
  "$art" "$src" "$(nasm -v | head -1)" > BUILD_INFO.json
echo "built hello_bios.bin ($size bytes)"
