VirtualBox raw-disk descriptors (EFI:/AI/Cache/Virtual)

Created by Extras > Prepare VMDK file for VirtualBox (CLI: python3 -m usbprep vmdk <device>).
A .vmdk here is a small text POINTER to the whole USB drive, NOT a copy of its contents:
  * the USB drive must stay connected; the device path (e.g. /dev/sdb) and permissions are specific to the host
    that created it - regenerate it on another computer or if the drive gets a different device name;
  * never use the drive on the host and inside the VM at the same time (unmount it on the host first);
  * the VM boots only if YOU installed a compatible bootloader in the EFI partition and enabled UEFI in the VM.
    The partition is ESP-typed (ESP-ready), which is not the same as bootable; no legacy BIOS boot is provided.
Each .vmdk has a .usbprep.json sidecar with the device/partition identity; a mismatch is reported as stale.
No VM is created, registered, started or attached by this tool.
