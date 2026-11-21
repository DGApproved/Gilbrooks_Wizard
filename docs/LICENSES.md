# Licenses / redistribution
## Bundled
Only this project's source (MIT, assumed), the jar/zipapp built from it, and the stock boot example binaries built from this project's own sources (`boot_stock/`: 96-byte MBR code, unsigned BOOTX64.EFI; no third-party code linked). No drivers or Java runtime are bundled. rsync (GPLv3) is a host tool, not bundled. The Linux ext4/vfat drivers are in the kernel, so there is nothing to bundle.

## Host tools (installed by the user)
- Python: PSF
- OpenJDK/Temurin: GPLv2 + Classpath Exception. The user may cache it from adoptium.net, checked against the vendor SHA-256.
- Oracle Java: OTN licence; do not bundle.
- util-linux: GPL/LGPL
- gdisk: GPLv2
- e2fsprogs: GPLv2
- dosfstools: GPLv3
- mtools: GPLv3
- udisks2: GPL/LGPL
- VirtualBox: GPLv3. The Extension Pack (PUEL) is not needed.
- usbipd-win: MIT
- macFUSE: closed source, not redistributable; not used.

## Test-only (not bundled)
- QEMU: GPLv2
- OVMF/EDK2: BSD-2-Clause-Patent
- NASM: BSD-2
- mingw-w64
