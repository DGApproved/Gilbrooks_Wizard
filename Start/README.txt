Start scripts (FAT32 /AI/Start). Pick your OS folder:
  Windows: Start\Windows\START.bat      (native backend by default; WSL only when the "WSL" checkbox is ticked)
  macOS:   Start/MacOS/start.command    (native = ext4 BLOCKED; opt-in "Read-only driver" = ext4fuse, "Homebrew" = anylinuxfs RW, Apple Silicon)
  Linux:   sh Start/Linux/start.sh
Each one only forwards to launchers/<OS>/; if Java 8+ is missing the launcher offers the verified cached
installer or the official download link, exactly as before. Add "setup ..." to reach the host setup helper.
