<#
Project 1 USB prep - Windows helper (consent-gated). UNTESTED: written on Linux, never run on Windows.

  START_HERE.bat [-Action <name>] [-DryRun]
    Info            (default) what works on Windows
    Setup           DEFAULT (native): Python 3 via winget + opens the Ext4Fsd download page (manual driver install)
                    with -UseWSL / WSL box ticked: Python 3, WSL2 + Ubuntu, usbipd-win
    MountExt4       DEFAULT (native): needs the Ext4Fsd driver; opens Ext2Mgr to assign a drive letter. No WSL.
                    with -UseWSL: attach the USB to WSL2 (usbipd), mount /USB and /USB/A.I. INSIDE WSL
    UnmountExt4     native: instructions; with -UseWSL: unmount inside WSL and detach the USB
    InstallAutorun  OPT-IN scheduled task that offers the helper when a prepared USB is inserted
    Uninstall       remove the scheduled task and host copy (rollback). winget packages are left installed.

Windows path mapping limits: there is no literal /USB on Windows. FAT32 is read/write natively at a drive
letter (e.g. E:\). ext4 is reachable only through WSL2; while the USB is attached to WSL it disappears
from Windows entirely (including the FAT32 drive letter). Linux programs on ext4 run only inside WSL.
Passwords/UAC are handled by Windows itself; this script never reads or stores credentials.
#>
param([string]$Action = "Info", [switch]$DryRun, [string]$Distro = "Ubuntu", [switch]$UseWSL)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # ...\AI
$HostCopy = Join-Path $env:LOCALAPPDATA "usbprep\app"
$TaskName = "Project1-USBPrep-OnInsert"

function Run([string]$exe, [string[]]$argv) {
  if ($DryRun) { Write-Host "DRY-RUN: $exe $($argv -join ' ')"; return }
  Write-Host "+ $exe $($argv -join ' ')"; & $exe @argv
  if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}
function Consent([string]$text) {
  Write-Host "==== Project 1 USB prep (Windows): $Action ====`n$text`n"
  if ((Read-Host "Type AGREE to continue (anything else aborts)") -ne "AGREE") { Write-Host "Aborted - no changes made."; exit 1 }
}
function Test-Admin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
function Elevate {
  if (-not (Test-Admin) -and -not $DryRun) {
    $a = @("-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", "`"$($MyInvocation.PSCommandPath)`"", "-Action", $Action, "-Distro", $Distro)
    if ($UseWSL) { $a += "-UseWSL" }
    Start-Process powershell -Verb RunAs -ArgumentList $a -Wait   # Windows UAC prompt
    exit 0
  }
}

# ---- backend choice: native Windows (Ext4Fsd) by DEFAULT; WSL only when explicitly chosen ----
# Explicit choice = the -UseWSL switch, or windows_wsl:true saved by the GUI 'WSL' checkbox / `usbprep backend set
# --wsl on` in %APPDATA%\usbprep\backend.json. Anything else (missing, corrupt, false) means native. No fallback:
# if the native driver is missing we explain and stop; WSL is never installed or used in its place.
function Get-SavedWslChoice {
  $f = if ($env:USBPREP_CONFIG_DIR) { Join-Path $env:USBPREP_CONFIG_DIR "backend.json" } else { Join-Path $env:APPDATA "usbprep\backend.json" }
  try { $j = Get-Content -Raw -LiteralPath $f -ErrorAction Stop | ConvertFrom-Json; return (($j.windows_backend -eq "wsl") -or (($j.windows_wsl -eq $true) -and -not $j.windows_backend)) } catch { return $false }
}
$WslChosen = [bool]$UseWSL -or (Get-SavedWslChoice)
function Assert-WslChosen { if (-not $WslChosen) { throw "WSL was not selected (WSL checkbox unticked). Refusing to use WSL." } }
$Ext4FsdUrl = "https://github.com/bobranten/Ext4Fsd/releases"
function Test-Ext4Fsd { [bool](Get-Service -Name "Ext2Fsd" -ErrorAction SilentlyContinue) }
function Show-NativeManual {
  Write-Host @"
Native Windows ext4 backend (default; WSL not used): Ext4Fsd by bobranten, GPL-2.0, free, signed for Windows 10/11.
  1. Download v0.71 or newer yourself: $Ext4FsdUrl
  2. Install it (Administrator / UAC). usbprep never bundles or silently installs kernel drivers.
  3. Open Ext2Mgr, select the 'AI' ext4 partition, enable write, assign a drive letter or folder (e.g. C:\USB\A.I.).
  Limits: volumes with bigalloc/quota/project/verity/orphan_present stay read-only; ea_inode/mmp/inline_data/
  encrypt/casefold/largedir cannot be mounted ('EXT4+' in Ext2Mgr). Kernel drivers can crash Windows: there are
  2026 reports of blue screens with Memory Integrity (HVCI) on Windows 11 - mount read-only first and keep backups.
  Read-only alternative without a driver: DiskInternals Linux Reader (freeware, browse/export only).
  Rollback: uninstall Ext4Fsd from Settings > Apps (or its uninstaller), reboot.
"@
}
function Invoke-NativeSetup {
  Consent @"
Native setup (WSL is NOT installed - tick the WSL box to use WSL instead):
  - Python 3.12  winget Python.Python.3.12 (PSF licence; UAC prompt)
  - Ext4Fsd driver: NOT installed automatically. The download page will be opened for you to install it yourself.
Rollback: winget uninstall Python.Python.3.12 ; uninstall Ext4Fsd from Settings > Apps.
"@
  Elevate
  Run "winget" @("install", "-e", "--id", "Python.Python.3.12", "--accept-package-agreements")
  Show-NativeManual
  if (-not (Test-Ext4Fsd)) { Run "explorer.exe" @($Ext4FsdUrl) }
}
function Invoke-NativeMount {
  if (-not (Test-Ext4Fsd)) {
    Write-Host "Native backend selected (default) but the Ext4Fsd driver is not installed. WSL is NOT used as a fallback."
    Show-NativeManual; exit 2
  }
  Write-Host "Ext4Fsd is installed. Assign the AI partition a drive letter / folder in Ext2Mgr (write enabled)."
  $mgr = @("$env:ProgramFiles\Ext2Fsd\Ext2Mgr.exe", "${env:ProgramFiles(x86)}\Ext2Fsd\Ext2Mgr.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
  if ($mgr) { Run $mgr @() } else { Show-NativeManual }
}
function Invoke-NativeUnmount {
  Write-Host "Native backend: remove the drive letter in Ext2Mgr (or use 'Safely Remove Hardware'), then unplug."
}
function Invoke-WslSetup {
  Assert-WslChosen
    Consent @"
Will install (Administrator / UAC prompt; downloads come from winget / Microsoft):
  - Python 3.12           winget Python.Python.3.12 (PSF licence)
  - WSL2 + Ubuntu         wsl --install -d $Distro (Microsoft; may require a reboot, then create a Linux user)
  - usbipd-win            winget dorssel.usbipd-win (MIT licence; installs a service + driver for USB/IP)
  - inside WSL: sudo apt-get install python3 e2fsprogs dosfstools util-linux mtools gdisk (asks your Linux password)
Rollback: winget uninstall Python.Python.3.12 / dorssel.usbipd-win ; wsl --unregister $Distro (deletes that distro).
"@
    Elevate
    Run "winget" @("install", "-e", "--id", "Python.Python.3.12", "--accept-package-agreements")
    Run "winget" @("install", "-e", "--id", "dorssel.usbipd-win", "--accept-package-agreements")
    Run "wsl" @("--install", "-d", $Distro)
    Write-Host "If WSL asked for a reboot, reboot, open '$Distro' once to create your user, then run Setup again."
    Run "wsl" @("-d", $Distro, "--", "sudo", "apt-get", "install", "-y", "python3", "e2fsprogs", "dosfstools", "util-linux", "mtools", "gdisk")
}
function Invoke-WslMount {
  Assert-WslChosen
    Consent @"
1. Copies this app into WSL (~/.local/share/usbprep/app) while the drive letter is still visible.
2. Shows 'usbipd list'; YOU type the BUSID of the Project 1 USB. It is then bound (admin) and attached to WSL.
   The USB disappears from Windows while attached.
3. Inside WSL runs: python3 -m usbprep mount <disk> --execute --privilege sudo --no-open
   (same safety checks as Linux: exact layout, collision checks, e2fsck -p, rw,exec,nosuid,nodev).
4. Opens \\wsl.localhost\$Distro\USB\A.I. in Explorer.
"@
    Elevate
    $src = ($Here -replace '\\', '/'); $drv = $src.Substring(0,1).ToLower()
    Run "wsl" @("-d", $Distro, "--", "sh", "-c", "mkdir -p ~/.local/share/usbprep && rm -rf ~/.local/share/usbprep/app && cp -r '/mnt/$drv$($src.Substring(2))' ~/.local/share/usbprep/app")
    Run "usbipd" @("list")
    $bus = Read-Host "BUSID of the Project 1 USB drive (e.g. 2-3)"
    if ($bus -notmatch '^\d+-\d+(\.\d+)*$') { throw "invalid BUSID" }
    Run "usbipd" @("bind", "--busid", $bus)
    Run "usbipd" @("attach", "--wsl", "--busid", $bus)
    Start-Sleep -Seconds 3
    Run "wsl" @("-d", $Distro, "--", "sh", "-c", "cd ~/.local/share/usbprep/app && python3 -m usbprep list")
    $disk = Read-Host "Disk path shown above inside WSL (e.g. /dev/sdd)"
    if ($disk -notmatch '^/dev/sd[a-z]+$') { throw "invalid disk path" }
    Run "wsl" @("-d", $Distro, "--", "sh", "-c", "cd ~/.local/share/usbprep/app && python3 -m usbprep mount $disk --execute --privilege sudo --no-open")
    Run "explorer.exe" @("\\wsl.localhost\$Distro\USB\A.I.")
}
function Invoke-WslUnmount {
  Assert-WslChosen
    Consent "Unmounts /USB/A.I. and /USB inside WSL, then detaches the USB (usbipd detach) so Windows sees it again."
    Run "wsl" @("-d", $Distro, "--", "sh", "-c", "cd ~/.local/share/usbprep/app && python3 -m usbprep unmount --execute --privilege sudo")
    $bus = Read-Host "BUSID to detach"
    if ($bus -notmatch '^\d+-\d+(\.\d+)*$') { throw "invalid BUSID" }
    Run "usbipd" @("detach", "--busid", $bus)
}

switch ($Action) {
  "Info" {
    Write-Host @"
Windows status:
  FAT32 'EFI' partition : read/write natively at a drive letter. Literal /USB: BLOCKED (not a Windows path).
                          Proposed alternative: drive letter, or an NTFS folder mount point such as C:\USB
                          (Disk Management > Change Drive Letter and Paths) - not done automatically.
  ext4  'AI'  partition : DEFAULT native backend = Ext4Fsd driver (installed by YOU; Setup opens the page).
                          WSL selected now: $WslChosen  (only -UseWSL or the GUI 'WSL' checkbox selects WSL;
                          WSL2 + usbipd-win then attaches the whole USB to WSL). No automatic fallback.
  Preparing/formatting  : BLOCKED on Windows - prepare the drive on Linux (or inside WSL with this app).
"@
  }
  "Setup" {
    if ($WslChosen) { Invoke-WslSetup } else { Invoke-NativeSetup }
  }
  "MountExt4" {
    if ($WslChosen) { Invoke-WslMount } else { Invoke-NativeMount }
  }
  "UnmountExt4" {
    if ($WslChosen) { Invoke-WslUnmount } else { Invoke-NativeUnmount }
  }
  "InstallAutorun" {
    Consent @"
OPT-IN. Copies this app to $HostCopy and registers the scheduled task '$TaskName' (current user) that runs
host_helpers\windows\on_insert.ps1 on event Microsoft-Windows-Partition/Diagnostic ID 1006 (disk arrival).
The script only shows a Yes/No prompt when a volume labelled EFI contains AI\USBPREP_MANIFEST.json.
Nothing runs from the USB without your click. Undo: -Action Uninstall
"@
    Run "robocopy" @($Here, $HostCopy, "/E", "/NFL", "/NDL")
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><EventTrigger><Enabled>true</Enabled>
    <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="Microsoft-Windows-Partition/Diagnostic"&gt;&lt;Select Path="Microsoft-Windows-Partition/Diagnostic"&gt;*[System[(EventID=1006)]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
  </EventTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries></Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command>
    <Arguments>-NoProfile -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "$HostCopy\host_helpers\windows\on_insert.ps1"</Arguments></Exec></Actions>
</Task>
"@
    if ($DryRun) { Write-Host "DRY-RUN: Register-ScheduledTask -TaskName $TaskName -Xml <event trigger 1006>" }
    else { Register-ScheduledTask -TaskName $TaskName -Xml $xml | Out-Null; Write-Host "Registered $TaskName" }
  }
  "Uninstall" {
    Consent "Removes scheduled task '$TaskName' and $HostCopy. winget packages and WSL are left installed (see Setup for rollback commands)."
    if ($DryRun) { Write-Host "DRY-RUN: Unregister-ScheduledTask $TaskName; Remove-Item $HostCopy" }
    else {
      Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
      Remove-Item -Recurse -Force $HostCopy -ErrorAction SilentlyContinue
      Write-Host "Rollback complete."
    }
  }
  default { Write-Host "Unknown action $Action. Use: Info, Setup, MountExt4, UnmountExt4, InstallAutorun, Uninstall" }
}
