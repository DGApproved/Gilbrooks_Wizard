#!/bin/sh
# Build the x86-64 UEFI app with the MinGW-w64 cross compiler (as the normal user; never writes any disk).
# Output: BOOTX64.EFI (PE32+ EFI application) + BUILD_INFO.json (hash of the sources it was built from).
set -eu
cd "$(dirname "$0")"
CC=x86_64-w64-mingw32-gcc
command -v $CC >/dev/null 2>&1 || { echo "$CC not found - install it (e.g. sudo apt install gcc-mingw-w64-x86-64)" >&2; exit 2; }
$CC -Os -ffreestanding -fno-stack-protector -fno-asynchronous-unwind-tables -fshort-wchar -mno-red-zone \
    -nostdlib -Wl,--subsystem,10 -Wl,--no-insert-timestamp -e efi_main -s -o BOOTX64.EFI.tmp hello.c
mv BOOTX64.EFI.tmp BOOTX64.EFI
src=$(sha256sum hello.c build.sh | sha256sum | cut -d' ' -f1)
art=$(sha256sum BOOTX64.EFI | cut -d' ' -f1)
printf '{"component": "EFI", "artifact": "BOOTX64.EFI", "artifact_sha256": "%s", "sources": ["hello.c", "build.sh"], "source_sha256": "%s", "tool": "%s"}\n' \
  "$art" "$src" "$($CC --version | head -1)" > BUILD_INFO.json
echo "built BOOTX64.EFI ($(wc -c < BOOTX64.EFI) bytes)"
