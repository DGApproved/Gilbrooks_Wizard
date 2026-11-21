# Called by the opt-in scheduled task (installed by windows_setup.ps1 -Action InstallAutorun).
# Shows a prompt only if a FAT32 volume labelled EFI contains AI\USBPREP_MANIFEST.json. UNTESTED.
Add-Type -AssemblyName PresentationFramework
$vol = Get-Volume | Where-Object { $_.FileSystemLabel -eq 'EFI' -and $_.DriveLetter } |
       Where-Object { Test-Path ("{0}:\AI\USBPREP_MANIFEST.json" -f $_.DriveLetter) } | Select-Object -First 1
if ($vol) {
  $p = "{0}:\AI\launchers\Windows\START_HERE.bat" -f $vol.DriveLetter
  if ([System.Windows.MessageBox]::Show("Project 1 USB detected at $($vol.DriveLetter):. Open the USB helper?",
      "Project 1 USB", 'YesNo') -eq 'Yes') { Start-Process -FilePath $p }
}
