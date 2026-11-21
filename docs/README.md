# USB Prep (Project 1) - usage and build

This desktop tool prepares a USB flash drive with this GPT layout (minimum size 5365 MiB):
- p1: FAT32, label "EFI", 4096 MiB, ESP-typed
- p2: ext4, label "AI", rest of the disk
- p3: swap, label "SWAP", 243 MiB, last

It copies the app to `EFI:/AI`. On Linux it mounts FAT32 at `/USB` and ext4 at `/USB/A.I.`.

The core is Python 3 stdlib (`usbprep/`). The GUI is Java 8+ Swing (`gui-java/`) and calls the Python backend. The CLI is headless.

## Run
- **CLI dry run** (the default; never writes): `python3 -m usbprep list`, `python3 -m usbprep prepare /dev/sdX` (shows the plan only).
- **Execute:** add `--execute --confirm "ERASE <last4 of serial>"`. In the GUI: typed token plus a checkbox. Root comes from sudo/pkexec.
- **Mount/unmount:** `python3 -m usbprep mount|unmount ...`
- **Capabilities:** `python3 -m usbprep capabilities`
- **GUI:** `launchers/launch_gui.sh`. It detects Java; if Java is missing it shows https://adoptium.net/temurin/releases/ and a Retry button.
- **GUI "Extras" menu:**
  - check drive for errors
  - auto repair
  - reformat (with backup/restore)
  - bad-block scan (off by default)
  - prepare VMDK file for VirtualBox
  - hand drive to VM
  - return drive from VM
- **VMDK:** a raw-disk descriptor goes in `EFI:/AI/Cache/Virtual`, together with `<name>.README.txt`. The README has the path, official VirtualBox links, VM setup steps and the BIOS/EFI firmware choice.
  - The descriptor points to the physical USB; it is not a copy of it.
  - The USB must stay connected.
  - Regenerate it on another host.

## Hello World boot example (stock, preserve/fresh)
- **Stock example** (read-only, bundled in `boot_stock/`, SHA-256 baseline in `boot_stock/STOCK_MANIFEST.json`):
  - BIOS: `scr/BIOS/hello.asm` (NASM, 96-byte MBR boot code), `build.sh`, `hello_bios.bin`, `BUILD_INFO.json`.
  - EFI: `src/EFI/hello.c` (self-contained x86-64 UEFI app, mingw-w64), `build.sh`, `BOOTX64.EFI` (2560 B, unsigned), `BUILD_INFO.json`.
  - Both print "Hello World" and "Press Enter to reboot", wait for Enter, then reset.
- **Fresh preparation of a blank drive:** the stock example goes to `EFI:/AI/scr/BIOS` and `EFI:/AI/src/EFI`. The EFI app goes only to `EFI/BOOT/BOOTX64.EFI` on the USB's ESP. The BIOS code patches only bytes 0-439 of the USB's MBR; the disk signature, partition entries, 0x55AA and the GPT are kept.
- **User changes:** if the running USB's `AI/scr/BIOS` (or the alias `AI/src/BIOS`) or `AI/src/EFI` differ from the stock manifest, duplicating to a new drive asks BEFORE any write: **Preserve user updates** / **Create a fresh installation** / **Cancel** (GUI dialog; CLI `--boot-content preserve|fresh`; without it a non-interactive run stops with exit 3 and writes nothing).
  - Preserve copies your sources and scripts, plus artifacts only if `BUILD_INFO.json` matches the current sources. A stale or missing build is either rebuilt with `--build-consent` (fixed approved toolchain in a private temp folder; your `build.sh` is never executed) or reported as stale/unbuilt, and the boot code is NOT installed. The stock binary is never substituted.
  - Fresh uses the stock content only. The source USB is never modified; Cancel writes nothing.
- **Mount/launch** never overwrites an already-prepared drive's boot content.
- **Hybrid MBR is NOT implemented.** The protective MBR is kept; the BIOS stage only replaces boot-code bytes.
- **Tested:** QEMU 7.2 (SeaBIOS 1.16.2 and OVMF 2022.11) booting an image file in snapshot mode shows the greeting, waits for Enter and resets. `sgdisk -v` reports no problems after the patch. **Not tested:** physical PCs, real USB sticks, Secure Boot (the EFI binary is unsigned).

## FAT32 layout, Start folder and ext4 backends (Windows/macOS)
- `/AI` sits at the root of the FAT32 partition and contains the entire project (GUI, core, `Start`, `Cache`, `Driver`, `Scripts`, `scr`, `src`, setup, docs). `/EFI` holds only firmware boot files (`EFI/BOOT/BOOTX64.EFI`), so it stays uncluttered. `EFI:/AI` elsewhere in this file means the `/AI` folder on the partition labelled EFI.
- `/AI/Start/<Windows|MacOS|Linux>` holds the start scripts; they forward to `launchers/` (paths quoted, missing Java handled as before).
- Backend choice is saved per user in `backend.json` (mode 0600; `python3 -m usbprep backend set <choice>`), shown with `backend status`. Choices are mutually exclusive; default is `native`. Selecting a choice never installs, runs or remounts anything; there is no automatic fallback.
  - Windows: `native` (default; Ext4Fsd signed driver, manual install, BSOD/HVCI risk noted; app reports MISSING-DEPENDENCY if absent), `wsl` (WSL2 + usbipd-win, only when the **WSL** checkbox is ticked), `custom` (Custom driver hook).
  - macOS: `native` (BLOCKED), `readonly` (**Read-only driver** = ext4fuse via macFUSE; installed with `setup readonly-install`, mounted with `setup readonly-mount diskNsM`; the mount is checked to refuse writes and any app write through it is refused), `homebrew` (**Homebrew** = installation mechanism for anylinuxfs, read/write, Apple Silicon only, Intel BLOCKED), `custom`.
- Custom driver hook: see `Driver/README.txt`. Off by default, exact path `/AI/Driver/<Windows|MacOS>/ext4.sh`, explicit consent, no inherited admin, exit 0 is not trusted without an independent mount check.
- None of the Windows/macOS backends were run on a real Windows or Mac by this project.

## Star (manual dedicated-USB mark)
- A **star button** (☆ off / ★ on) sits next to the drive dropdown. Click it to mark a USB drive that detection did not classify as a USB flash drive as your **dedicated USB drive**, for example a USB HDD/SSD enclosure, a drive reported as non-removable, an odd vendor or a card reader.
- **What is listed:**
  - USB drives that were not detected as flash drives are now listed with the label "(not detected as dedicated USB)".
  - Starred drives show ★. A session-only star shows "★ (this session)".
  - Never listed and never starrable:
    - the running system/boot/root disk, and any disk with a mounted host system partition;
    - non-USB internal disks (NVMe/SATA/virtio...) and disks whose bus cannot be determined;
    - loop, ram, zram and optical devices.
- **When the star is available:**
  - It is enabled only for a starrable drive. It stays disabled for a USB drive without medium or one that is too small for the layout, and the tooltip gives the reason.
  - Turning the star ON shows a confirmation dialog. It warns that Initialize/formatting erases everything on the drive and that the project was designed for USB flash drives. The dialog itself runs nothing.
  - Turning the star OFF needs no confirmation.
- **What the star overrides:** only the flash-vs-not classification (rotational, HDD/SSD model words, size cap, bridge, card reader, removable flag, large capacity). It never overrides the system-disk or internal-disk protections. Active swap still needs the ambiguity override.
- **What still applies:** all other safeguards (dry run by default, typed `ERASE`/`AGREE` consent, revalidation before every write, USB-only writes).
- **Where a starred drive counts as dedicated:** Mount, Initialize/Re-initialize, Clone, Data, `prepared` detection, `list` and `gui-state`.
- **Storage:**
  - Stars are saved per user in `<config dir>/stars.json` (same folder as `backend.json`, `USBPREP_CONFIG_DIR` honoured; mode 0600, atomic write; a corrupt file counts as empty).
  - A star is keyed by **serial + model + vendor + size** (+ the USB vendor:product id when one is reported; current lsblk output does not report it), **never by /dev path**. A different drive that later appears on the same /dev node does not inherit the star, and revalidation refuses the action.
  - A drive **without a serial number** cannot be pinned, so its star is never saved. It is session-only: the GUI passes it to the backend in `USBPREP_SESSION_STARS`, and it is lost when the GUI closes. Limitation: two serial-less drives with the same model/vendor/size on the same /dev node cannot be told apart during that session.
- **CLI:**
  - `python3 -m usbprep star /dev/sdX` asks you to type YES; `--yes` skips the prompt. Without a serial it refuses to save (exit 3); `--session` prints a session key to export.
  - `python3 -m usbprep unstar <device|key|serial>` removes a saved star.
  - `python3 -m usbprep stars [--json]` lists the saved stars.
- **Windows/macOS:** the same logic is in the Get-Disk/diskutil parsers but is mock-tested only. Drive operations stay Linux-only.
- **gui-state:** it now also reports a read-only `layout` (prepared/blank) for each non-excluded Linux drive. The GUI Mount / Re-initialize choice uses it.

## Clone menu (ext4 only, Linux)
- Copies ONLY the ext4 "A.I." partition contents from one prepared USB to another (e.g. to a friend's drive). The FAT32 (app, cache, boot) and swap partitions are not touched.
- Separate source and destination dropdowns show path, model, serial, size and status. It refuses the same disk, the same serial/model, the drive the app runs from as destination, unprepared, non-USB/system/ambiguous drives, drives handed to a VM and mounted ext4 partitions.
- Backend: `e2fsck -n` on both, then private mounts (source `ro,noload`, never written), a capacity check (needs 1.05x of the source's used space; no resizing), `rsync -aHAX --numeric-ids --delete`, then an `rsync --checksum` dry run that must show 0 differences, then both are unmounted. Not a raw `dd`; filesystem UUIDs are not copied.
- Typed token `REPLACE <last4>`; the dry run states exactly what is replaced. Projects that refer to the source filesystem UUID may need manual edits.
- CLI: `python3 -m usbprep clone /dev/sdX /dev/sdY [--execute --confirm "REPLACE xxxx" --privilege sudo]`.
- Windows/macOS: BLOCKED (no native ext4); clone on Linux.

## Scripts menu
- Lists files in `EFI:/AI/Scripts/<Linux|MacOS|Windows>` for the current OS only, by exact filename. Symlinks, folders, hidden files, path traversal and unsupported extensions are skipped (Linux `.sh .bash .py`; macOS `.sh .command .zsh .py`; Windows `.ps1 .bat .cmd .py`).
- Nothing runs on discovery, refresh or insertion. Selecting a script shows a trust warning; after consent it runs as an argv list (no shell string) as the normal user. The app refuses to run scripts while elevated; a script that needs admin rights must ask for it itself. A script cannot be started twice at once; a missing interpreter is explained.
- usbprep does not audit or sandbox scripts and cannot guarantee their behaviour.
- Default script `open_terminal_in_ext4.*`: opens a terminal in the verified ext4 mount (`/USB/A.I.` on Linux) passed in `USBPREP_EXT4_MOUNT`. No fallback: it exits if the drive is not mounted. On Windows/macOS it explains that ext4 is not readable there (no WSL/VM).
- User scripts and edited defaults are kept on app update, duplication and reformat; new default scripts are added only where that filename is absent.
- CLI: `python3 -m usbprep scripts list` / `scripts run "<name>" --device /dev/sdX --i-trust-this-script`.

## Quick / full format
- Checkbox **"Full format (slower; writes the entire partition)"** for prepare, duplicate and reformat (CLI `--full-format`). Unchecked = quick.
- Quick: the partition table and filesystems are created; old data blocks are not overwritten (the ext4 creator may discard/TRIM its range on devices that support it).
- Full: after partitioning, every byte of each partition (FAT32, ext4, swap) is zero-filled, strictly inside the partition boundaries, then the filesystems are created. Free space is zeroed, not "formatted as a type"; zero-fill plus correct filesystem creation is what "full format" means here. The MBR/GPT, backup GPT and gaps are not written; boot code is installed after formatting.
- Warning shown when ticked: it takes much longer and writes the whole partition, which adds flash wear and may reduce lifespan. Any later speed benefit depends on the device and workload and is not guaranteed. It is NOT a secure erase, does NOT verify the media and does NOT run the bad-block scan.
- If a full format is interrupted, the result says the drive is PARTIALLY formatted; nothing is restored or mounted as success.
- The GUI has no Cancel button for a running backend job. Closing the window does not stop an already started operation.

## Data menu (Linux; disabled on Windows/macOS)
- **Backup current user to /Data** (CLI `data backup --device /dev/sdX [--execute --i-understand-777]`): copies the signed-in user's whole home, hidden files included, to the **ext4 partition root `/Data`** (not FAT32). When run elevated, it backs up the real user (SUDO_USER/PKEXEC_UID), never `/root`.
  - `/Data/Shared/{Music,Pictures,Videos,Documents}/<home-relative path>`: only regular, non-hidden files with an allowlisted extension (case-insensitive, editable `SHARED_EXT` in `usbprep/data.py`, e.g. mp3/flac, png/jpg, mp4/mkv, pdf/docx/txt). Folder structure is kept, never flattened.
  - `/Data/<Windows|MacOS|Linux>/<machine-id>/<user>/home/...`: everything else. OS-specific extensions go to that OS's folder (.exe/.msi/.cab -> Windows; .app/.pkg/.dmg -> MacOS; .deb/.rpm/.AppImage -> Linux). Hidden/config files, unknown types, symlinks and special files go to the running OS's folder. macOS bundles (.app, .pkg, .framework...) are copied whole, never split.
  - Shared collisions: identical content is deduplicated (provenance only); a different file at the same path is kept under `_versions/<machine>-<user>/`. Nothing is overwritten (`--ignore-existing`). Provenance (OS, machine, user, original path, SHA-256, original mode) is kept in `/Data/Shared/usbprep_shared_index.json`.
  - The machine id is a truncated SHA-256 of `/etc/machine-id` (the raw id is not stored; hostname is the fallback).
  - Every copy uses `rsync -axHAX` with NUL-separated file lists. Other filesystems inside home and the USB itself are not entered. Hard links split across buckets are not preserved as links.
  - Original metadata goes to `usbprep_manifest.json` (mode 600) BEFORE the requested `chmod 777`. The chmod touches only regular files and dirs of the backup copies (never symlinks, devices, fifos, sockets or the source home).
  - Warning plus consent before any write: anyone can read and change 777 copies; a home contains secrets; a live account is not a consistent snapshot. Unreadable or locked files are listed by path. The status is complete, partial or failed and is never overstated; a space check runs first.
- **Restore shared data** (`data restore-shared --device D <machine>/<user> [--category ..]`) and **Restore OS-specific data** (`data restore --device D <OS>/<machine>/<user>`):
  - The source is chosen explicitly. Files go only into the signed-in user's own home.
  - A preview (rsync `--dry-run --itemize-changes`) lists creates and overwrites, then you type `RESTORE <user>`.
  - Restore never deletes (no `--delete`, no `/MIR`). Default `skip-newer`; `skip-existing` and `overwrite` are explicit choices.
  - Files belong to the signed-in user. Original modes come from the manifest/index (never 777); `.ssh`/`.gnupg` stay 0700/0600.
  - Top-level Music/Pictures/Videos/Documents map to localized XDG folders (`user-dirs.dirs`).
  - Another OS's specific files need `--allow-foreign-os`. They are copied as files and not made runnable. Name incompatibilities are reported.
- **Windows/macOS:** the Data menu is disabled because ext4 read/write is BLOCKED there. The robocopy (`/E /COPY:DAT /XJ`, no `/MIR`, exit codes >= 8 = failure), icacls (BUILTIN\Users on backup copies only, never Everyone, not an exact chmod mapping) and macOS rsync capability (Apple rsync/openrsync lack -A/-X) builders are unit-tested only.
- **Tests:** real rsync on fixture homes in temp dirs only. No real home was backed up or restored, and no USB was written.

## Build / test
- **Tests:** `~/.local/bin/pytest` (output saved in `docs/TEST_RESULTS.txt`). Tests use image files only and refuse real block devices; `USBPREP_ALLOW_REAL_DEVICE` is never set.
- **Jar:** `sh gui-java/build.sh` (javac --release 8).
- **Wheel/sdist:** `python3 -m build`. Zipapp: see `docs/BUILD_RESULTS.txt`.

## Scope boundary
- **Where the app writes:** the app and its helpers write only to the selected USB. Before each write they recheck it:
  - identity
  - whole-disk ancestry
  - not the system/root disk
  - not mounted
  - not handed to a VM

  There is no fallback disk.
- **No host boot changes:** the app never runs efibootmgr/bcdedit/bless/grub-install and never changes host boot config or NVRAM.
- **Host changes:** consented prerequisite installs (Java, tools, opt-in helpers) and reformat staging in host temp are the only ones.
- **What the app answers for:** only its own actions. Code or OSes that the user later installs or runs from the USB (e.g. an OS that installs itself to another disk) are outside its scope. They are not audited, sandboxed or contained.

## Working / Tested / Untested / Blocked
- **Working + tested (Linux, on image files and mocks):**
  - detection heuristics (fixtures)
  - plan geometry
  - real partitioning/formatting tools on sparse images
  - layout check (exact matches and near misses)
  - app copy with SHA-256 manifest (mtools)
  - safety, TOCTOU and consent guards
  - fsck flow (mock)
  - offline cache manifest and tamper detection
  - Java bootstrap (sandboxed HOME)
  - Java GUI self-test (Temurin 1.8.0_514, OpenJDK 17)
  - VMDK and handoff guardrails (mock)
  - wheel, sdist, zipapp and jar builds
  - stock boot example build, manifest, preserve/fresh/cancel, MBR boot-code patch on images, QEMU boot (SeaBIOS + OVMF)
  - Clone refusals and argv (mock), real rsync copy/verify between folders
  - Scripts listing, consent, no-root, argv launch, locking (real launch of a test script)
  - quick vs full format on real image files (partition-only zero-fill), device argv (mock)
  - Data backup/restore: classification, shared dedupe/versions, chmod scope, manifest, non-deleting restore (real rsync on fixture dirs)
- **Implemented, untested:**
  - anything on a physical USB (including device clone, full format and BIOS/EFI boot on real PCs)
  - real terminal launch by the default script
  - Data backup/restore on a real home and real mounted USB ext4
  - real `/USB` mounts (no loop devices on the build host)
  - real reformat
  - VBoxManage (not installed on the build host)
  - VM handoff
  - Windows/macOS adapters and scripts (never run; PowerShell syntax unverified)
- **Blocked:**
  - literal `/USB` and ext4 RW on Windows/macOS (alternatives in SUPPORT_MATRIX)
  - zero-install autorun on all OSes (opt-in host helper templates only)
  - Clone, full format and boot install on Windows/macOS (Linux only)
- **Not implemented:**
  - the hybrid MBR stage (the protective MBR is kept)
  - a GUI "Boot" menu (Recompile/Reinstall); boot content is handled during preparation/duplication only
  - a GUI cancel button for running backend jobs
