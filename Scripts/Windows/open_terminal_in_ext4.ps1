# Default usbprep script (Windows): open a terminal in the verified ext4 (A.I.) mount.
# Windows cannot mount the ext4 partition natively (BLOCKED in the support matrix); this script does NOT start
# WSL or a VM. usbprep normally passes no mount path here, so it explains that and exits.
$m = $env:USBPREP_EXT4_MOUNT
if (-not $m -or -not (Test-Path -LiteralPath $m -PathType Container)) {
    Write-Error "The A.I. (ext4) partition is not mounted on Windows - no native ext4 support (see SUPPORT_MATRIX). Nothing opened."
    exit 3
}
if (Get-Command wt.exe -ErrorAction SilentlyContinue) { Start-Process wt.exe -ArgumentList @('-d', $m) }
else { Start-Process powershell.exe -WorkingDirectory $m -ArgumentList '-NoExit' }
