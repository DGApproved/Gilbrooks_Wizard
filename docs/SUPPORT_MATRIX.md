| Capability | Linux | Windows | macOS |
|---|---|---|---|
| detect USB flash drives | WORKING+TESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| detect prepared layout | WORKING+TESTED | BLOCKED | BLOCKED |
| prepare/format (image file) | WORKING+TESTED | n/a | n/a |
| prepare/format (physical USB) | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| copy app to EFI:/AI | WORKING+TESTED | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED |
| FAT32 RW mount at literal /USB | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| ext4 RW+exec mount at literal /USB/A.I. | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| open file manager | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| unmount/eject | IMPLEMENTED-UNTESTED | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED |
| launch on insertion | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED |
| Java 8+ Swing GUI | WORKING+TESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| Java 8+ bootstrap + offline installer cache | WORKING+TESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| ESP-typed FAT32 partition (ESP-ready, not bootable by itself) | WORKING+TESTED | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED |
| VirtualBox raw-disk VMDK descriptor | IMPLEMENTED-UNTESTED | ALTERNATIVE-PROPOSED | ALTERNATIVE-PROPOSED |
| host <-> VM drive handoff (VMDK edit/test loop) | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| Hello World boot example (stock + preserve/fresh) | WORKING+TESTED | BLOCKED | BLOCKED |
| Clone ext4 partition USB -> USB | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| Scripts menu (AI/Scripts/<OS>) | WORKING+TESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| Full format option (zero-fill each partition, then create filesystems) | WORKING+TESTED | BLOCKED | BLOCKED |
| Data backup/restore (home <-> ext4 /Data) | IMPLEMENTED-UNTESTED | BLOCKED | BLOCKED |
| Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star) | WORKING+TESTED | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |
| ext4 backend: native Ext4Fsd (default) | n/a | IMPLEMENTED-UNTESTED | n/a |
| ext4 backend: WSL checkbox (opt-in) | n/a | IMPLEMENTED-UNTESTED | n/a |
| ext4 backend: Custom driver checkbox | n/a | IMPLEMENTED-UNTESTED | IMPLEMENTED-UNTESTED |

**Linux notes**

- *detect USB flash drives* - WORKING+TESTED: lsblk heuristics; fixture-tested + real lsblk run on build host (no USB attached there)
- *detect prepared layout* - WORKING+TESTED: tested against real GPT images built with sgdisk/mkfs tools
- *prepare/format (image file)* - WORKING+TESTED: real sgdisk/mkfs.vfat/mkfs.ext4/mkswap on sparse image files
- *prepare/format (physical USB)* - IMPLEMENTED-UNTESTED: same plan + TOCTOU guards; never run on a real device during development
- *copy app to EFI:/AI* - WORKING+TESTED: tested via mtools on image + directory backend; SHA-256 verified
- *FAT32 RW mount at literal /USB* - IMPLEMENTED-UNTESTED: kernel vfat; needs root (sudo/pkexec); mock-tested only - no loop devices on build host
- *ext4 RW+exec mount at literal /USB/A.I.* - IMPLEMENTED-UNTESTED: kernel ext4, rw,exec,nosuid,nodev; user-owned root; probe verified only on tmp dirs
- *open file manager* - IMPLEMENTED-UNTESTED: xdg-open /USB/A.I.; mock-tested
- *unmount/eject* - IMPLEMENTED-UNTESTED: umount + udisksctl power-off; mock-tested
- *launch on insertion* - ALTERNATIVE-PROPOSED: opt-in udev rule + systemd user service installed by setup/linux_setup.sh after consent
- *Java 8+ Swing GUI* - WORKING+TESTED: jar built with javac --release 8 (class major 52); self-test run under Xvfb on Temurin 1.8.0_514 and OpenJDK 17.0.21 (Linux x64 only)
- *Java 8+ bootstrap + offline installer cache* - WORKING+TESTED: launchers/java_bootstrap.sh: detect/skip, verified cached Temurin .tar.gz per-user install, refusal cases - tested in a sandboxed HOME
- *ESP-typed FAT32 partition (ESP-ready, not bootable by itself)* - WORKING+TESTED: GPT type C12A7328-... set by sgdisk; verified on images
- *VirtualBox raw-disk VMDK descriptor* - IMPLEMENTED-UNTESTED: VBoxManage createmedium --variant RawDisk; mock-tested only (VirtualBox not installed on the build host, no physical USB)
- *host <-> VM drive handoff (VMDK edit/test loop)* - IMPLEMENTED-UNTESTED: unmount FAT32+ext4, swapoff, flush, marker blocks host mount; return needs VM poweroff/aborted; guardrails mock-tested only
- *Hello World boot example (stock + preserve/fresh)* - WORKING+TESTED: stock NASM MBR code + x86-64 UEFI app built here; deployed on fresh image preparation; booted in QEMU 7.2 SeaBIOS 1.16.2 and OVMF 2022.11 (image, snapshot) - greeting, waits for Enter, resets. Physical PCs/USB untested; hybrid MBR NOT implemented (protective MBR kept)
- *Clone ext4 partition USB -> USB* - IMPLEMENTED-UNTESTED: rsync -aHAX --numeric-ids between private mounts (source ro,noload); rsync copy/verify tested on directories, device flow mock-tested only
- *Scripts menu (AI/Scripts/<OS>)* - WORKING+TESTED: discovery/filtering/argv/no-root checks tested; default terminal script refuses without a verified /USB/A.I. mount; real terminal launch untested
- *Full format option (zero-fill each partition, then create filesystems)* - WORKING+TESTED: image: real zero-fill of exactly each partition extent, GPT/gap untouched (tested); physical USB: same plan on partition nodes, mock-tested only. Not a secure erase, no media verification, no bad-block scan
- *Data backup/restore (home <-> ext4 /Data)* - IMPLEMENTED-UNTESTED: rsync -axHAX + chmod 777 on the backup subtree, manifest of original modes, non-deleting restore with preview/typed confirmation; tested with real rsync on fixture homes and temp dirs only - never on a real home or physical USB
- *Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star)* - WORKING+TESTED: per-user stars.json (0600, atomic) keyed by serial+model+vendor+size, never /dev path; no serial -> session-only; overrides flash detection only (system/internal/no-medium/too-small refusals stay); tested on lsblk fixtures, mocks and the headless GUI self-test - never on a physical USB HDD

**Windows notes**

- *detect USB flash drives* - IMPLEMENTED-UNTESTED: PowerShell Get-Disk/Get-PhysicalDisk JSON parser; unit-tested with mocked JSON only
- *detect prepared layout* - BLOCKED: not implemented on Windows; inspect on Linux
- *prepare/format (physical USB)* - BLOCKED: no native ext4 formatter; prepare the drive on Linux
- *copy app to EFI:/AI* - ALTERNATIVE-PROPOSED: FAT32 gets a drive letter natively; copy the app folder manually or run on Linux
- *FAT32 RW mount at literal /USB* - BLOCKED: no literal /USB on Windows; FAT32 is RW natively at a drive letter (e.g. E:\); optional NTFS folder mount point such as C:\USB is ALTERNATIVE-PROPOSED
- *ext4 RW+exec mount at literal /USB/A.I.* - BLOCKED: no redistributable native Windows ext4 RW driver; ALTERNATIVE: WSL2 `wsl --mount --partition 2 --type ext4` (admin, Win11/Store WSL) - USB flash drives need usbipd-win attach first; appears under /mnt/wsl/... and \\wsl$\<distro>\...; Linux binaries run only inside WSL
- *ext4 backend: native Ext4Fsd (default)* - IMPLEMENTED-UNTESTED: signed GPLv2 kernel driver, MANUAL install by user; RW except bigalloc/quota/project/verity/orphan_present (forced RO); refuses ea_inode/mmp/inline_data/encrypt/casefold/largedir; HVCI BSOD reports (issue #107); never run here
- *ext4 backend: WSL checkbox (opt-in)* - IMPLEMENTED-UNTESTED: WSL2 + usbipd-win; used ONLY when the WSL checkbox / -UseWSL is chosen; no fallback
- *ext4 backend: Custom driver checkbox* - IMPLEMENTED-UNTESTED: user script /AI/Driver/Windows/ext4.sh via Git Bash/MSYS2 (never WSL); explicit consent; independent mount check; script not sandboxed
- *open file manager* - IMPLEMENTED-UNTESTED: explorer.exe <path>; untested
- *unmount/eject* - ALTERNATIVE-PROPOSED: use Windows 'Safely Remove Hardware' / `wsl --unmount`
- *launch on insertion* - ALTERNATIVE-PROPOSED: opt-in Task Scheduler task (event 2003, DriverFrameworks-UserMode) via setup/windows_setup.ps1; untested
- *Java 8+ Swing GUI* - IMPLEMENTED-UNTESTED: same jar; detection + support matrix work, drive operations fail closed; never run on Windows
- *Java 8+ bootstrap + offline installer cache* - IMPLEMENTED-UNTESTED: launchers/Windows/bootstrap_java.ps1 (.zip per user / signed .msi via UAC; SHA-256 + Authenticode); never run, PowerShell syntax not checked
- *ESP-typed FAT32 partition (ESP-ready, not bootable by itself)* - ALTERNATIVE-PROPOSED: visible as a FAT32 drive letter only if Windows assigns one
- *VirtualBox raw-disk VMDK descriptor* - ALTERNATIVE-PROPOSED: not implemented on Windows; manual: VBoxManage createmedium disk --variant RawDisk --property RawDrive=\\.\PhysicalDriveN (admin)
- *host <-> VM drive handoff (VMDK edit/test loop)* - BLOCKED: not implemented; manual: eject/offline the disk in Disk Management (admin) before starting the VM; online it after full power-off
- *Hello World boot example (stock + preserve/fresh)* - BLOCKED: drive preparation is Linux-only; the FAT32 sources are visible and editable, build/install on Linux
- *Clone ext4 partition USB -> USB* - BLOCKED: no native ext4 driver; clone on Linux (Clone menu) or manually `sudo rsync -aHAX --numeric-ids` between two mounted ext4 partitions
- *Scripts menu (AI/Scripts/<OS>)* - IMPLEMENTED-UNTESTED: PowerShell .ps1 / .bat / .cmd / .py; never run on this OS; default terminal script explains the ext4 blocker
- *Full format option (zero-fill each partition, then create filesystems)* - BLOCKED: formatting is Linux-only; the checkbox is passed to the backend, which fails closed on this OS
- *Data backup/restore (home <-> ext4 /Data)* - BLOCKED: DISABLED: needs ext4 RW, which is blocked on this OS. robocopy /E /COPY:DAT /XJ (no /MIR) + icacls builders exist, unit-tested only
- *Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star)* - IMPLEMENTED-UNTESTED: Get-Disk BusType/IsBoot/IsSystem parsing with the same star logic, mock-tested only; a star makes the drive listable/eligible, but drive operations stay Linux-only

**macOS notes**

- *detect USB flash drives* - IMPLEMENTED-UNTESTED: diskutil -plist parser; unit-tested with mocked plist only
- *detect prepared layout* - BLOCKED: not implemented on macOS; inspect on Linux
- *prepare/format (physical USB)* - BLOCKED: no native ext4 formatter; prepare the drive on Linux
- *copy app to EFI:/AI* - ALTERNATIVE-PROPOSED: FAT32 auto-mounts at /Volumes/EFI; copy manually or run on Linux
- *FAT32 RW mount at literal /USB* - BLOCKED: root volume is read-only (SIP/SSV); /etc/synthetic.conf is a privileged host change and only creates links; ALTERNATIVE: `diskutil mount -mountPoint /Volumes/USB diskNs1`
- *ext4 RW+exec mount at literal /USB/A.I.* - BLOCKED: no native ext4; macFUSE is closed-source, not freely redistributable (commercial bundling needs a paid license), needs kernel-extension approval; fuse-ext2 write support is flagged risky by its author; Linux ELF binaries would not execute on macOS anyway
- *ext4 backend: Read-only driver (ext4fuse)* - IMPLEMENTED-UNTESTED: ext4fuse + macFUSE via Homebrew (setup readonly-install, consent); READ-ONLY, verified by refused write; app writes refused; formula deprecated (disable 2027-06-21); feature compatibility unverified; never run here
- *ext4 backend: Homebrew checkbox (anylinuxfs RW)* - IMPLEMENTED-UNTESTED: Apple Silicon only (Intel BLOCKED); Linux ext4 driver in libkrun microVM over NFS; GPL-3.0; installed via Homebrew with consent; never run here
- *ext4 backend: Custom driver checkbox* - IMPLEMENTED-UNTESTED: user script /AI/Driver/MacOS/ext4.sh via /bin/sh; explicit consent; independent mount check; script not sandboxed
- *open file manager* - IMPLEMENTED-UNTESTED: `open <path>`; untested
- *unmount/eject* - ALTERNATIVE-PROPOSED: `diskutil eject diskN`
- *launch on insertion* - ALTERNATIVE-PROPOSED: opt-in launchd agent (StartOnMount) via setup/macos_setup.sh; untested
- *Java 8+ Swing GUI* - IMPLEMENTED-UNTESTED: same jar; detection + support matrix work, drive operations fail closed; never run on macOS
- *Java 8+ bootstrap + offline installer cache* - IMPLEMENTED-UNTESTED: same java_bootstrap.sh (.tar.gz per user / signed .pkg); never run on macOS
- *ESP-typed FAT32 partition (ESP-ready, not bootable by itself)* - ALTERNATIVE-PROPOSED: auto-mounts as FAT32; ESP may be hidden by Finder
- *VirtualBox raw-disk VMDK descriptor* - ALTERNATIVE-PROPOSED: not implemented on macOS; manual: VBoxManage ... RawDrive=/dev/diskN after diskutil unmountDisk
- *host <-> VM drive handoff (VMDK edit/test loop)* - BLOCKED: not implemented; manual: quit apps using it, `diskutil unmountDisk /dev/diskN`, start the VM; after full VM power-off `diskutil mountDisk`
- *Hello World boot example (stock + preserve/fresh)* - BLOCKED: drive preparation is Linux-only; the FAT32 sources are visible and editable, build/install on Linux
- *Clone ext4 partition USB -> USB* - BLOCKED: no native ext4 driver; clone on Linux (Clone menu) or manually `sudo rsync -aHAX --numeric-ids` between two mounted ext4 partitions
- *Scripts menu (AI/Scripts/<OS>)* - IMPLEMENTED-UNTESTED: .sh / .command / .zsh / .py; never run on this OS; default terminal script explains the ext4 blocker
- *Full format option (zero-fill each partition, then create filesystems)* - BLOCKED: formatting is Linux-only; the checkbox is passed to the backend, which fails closed on this OS
- *Data backup/restore (home <-> ext4 /Data)* - BLOCKED: DISABLED: needs ext4 RW, which is blocked on this OS. rsync capability detection (Apple rsync/openrsync lack -A/-X) builders exist, unit-tested only
- *Star: mark a missed USB drive as dedicated USB (GUI star / usbprep star)* - IMPLEMENTED-UNTESTED: diskutil Internal/SystemImage/BusProtocol parsing with the same star logic, mock-tested only; a star makes the drive listable/eligible, but drive operations stay Linux-only
