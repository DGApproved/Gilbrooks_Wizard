Custom driver hook (FAT32 /AI/Driver) - OFF by default; the app NEVER runs this on discovery, refresh or insertion.
Put YOUR script at exactly:  /AI/Driver/Windows/ext4.sh   or   /AI/Driver/MacOS/ext4.sh
Enable the "Custom driver" checkbox, then press an explicit action; you are shown the exact path and a
trust warning and must confirm. The script runs as YOU (never with inherited admin; use your OS's own prompt).
Contract (argv, no shell parsing):
  ext4.sh <mount|unmount|status> --device <dev> --mount-point <path> --fat-root <FAT32 root>
  env: USBPREP_OPERATION, USBPREP_DEVICE, USBPREP_MOUNT_POINT, USBPREP_FAT_ROOT
  exit 0 = success, non-zero = failure. Exit 0 does NOT prove a mount: the app checks the mount point itself
  (is it mounted, is it ext4/nfs, can it write) before enabling read/write features.
Windows runs it with Git Bash or MSYS2 bash (never WSL); paths are converted to /c/... form.
macOS runs it with /bin/sh. Output is logged with secrets redacted (custom-driver.log).
This app cannot sandbox or audit your script - only add code you trust.
