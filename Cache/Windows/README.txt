Offline installer cache for Windows (EFI:/AI/Cache/Windows)

You may place a Windows Java 8+ JRE .zip (extracted per user into %LOCALAPPDATA%\usbprep\java, no admin rights) or a signed .msi installer (installed through the normal Windows UAC prompt after its Authenticode signature and checksum are checked). Pick x64 or aarch64 to match your PC.

A file placed here is NOT used until it is registered with its vendor SHA-256 checksum
(for Temurin, the .sha256.txt file published next to each download on https://adoptium.net/temurin/releases/):
  python3 -m usbprep cache add <file> --os <linux|darwin|windows> --arch <x64|aarch64> --version <e.g. 1.8.0_514> --sha256 <vendor checksum>
or let setup download and register it:  python3 -m usbprep cache fetch-java --os ... --arch ... --execute
Registered files are listed in cache_manifest.tsv and re-checked (size + SHA-256) before every use.
Nothing is installed if a compatible Java 8 or newer is already present.
FAT32 limits: max 4 GiB per file; the EFI partition is 4096 MiB in total.
Never put passwords, keys or licence secrets here.
