Offline installer cache for Linux (EFI:/AI/Cache/Linux)

You may place a Linux Java 8+ JRE .tar.gz archive for your CPU (x64 or aarch64), e.g. Eclipse Temurin OpenJDK8U-jre_x64_linux_hotspot_*.tar.gz. It is extracted per user into ~/.local/share/usbprep/java (no admin rights). Distro packages (.deb/.rpm) are NOT supported offline: they need a complete distro/version-specific dependency set.

A file placed here is NOT used until it is registered with its vendor SHA-256 checksum
(for Temurin, the .sha256.txt file published next to each download on https://adoptium.net/temurin/releases/):
  python3 -m usbprep cache add <file> --os <linux|darwin|windows> --arch <x64|aarch64> --version <e.g. 1.8.0_514> --sha256 <vendor checksum>
or let setup download and register it:  python3 -m usbprep cache fetch-java --os ... --arch ... --execute
Registered files are listed in cache_manifest.tsv and re-checked (size + SHA-256) before every use.
Nothing is installed if a compatible Java 8 or newer is already present.
FAT32 limits: max 4 GiB per file; the EFI partition is 4096 MiB in total.
Never put passwords, keys or licence secrets here.
