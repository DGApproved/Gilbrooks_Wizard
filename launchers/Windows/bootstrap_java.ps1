# Native (pre-Java) Java 8+ bootstrap for Windows. UNTESTED (no Windows host was available).
#   bootstrap_java.ps1 [-Launch]                      find Java 8+, else offer the verified cached installer, else instructions + Retry
#   bootstrap_java.ps1 -Register FILE -Version V -Sha256 VENDOR_SHA256 [-Arch x64|aarch64]
# A cached file is used only if listed in AI\Cache\Windows\cache_manifest.tsv AND its size + SHA-256 match
# AND (msi) its Authenticode signature is Valid. Nothing is installed when a compatible Java is present.
param([switch]$Launch, [string]$Register, [string]$Version, [string]$Sha256, [string]$Arch)
$ErrorActionPreference = 'Stop'
$AI = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CDir = Join-Path $AI 'Cache\Windows'
$Man = Join-Path $CDir 'cache_manifest.tsv'
$Dest = Join-Path $env:LOCALAPPDATA 'usbprep\java'
$MaxF = 4294967295
$Link = 'https://adoptium.net/temurin/releases/'
if (-not $Arch) { $Arch = @{ 'AMD64' = 'x64'; 'ARM64' = 'aarch64' }[$env:PROCESSOR_ARCHITECTURE] }

function Get-JavaMajor([string]$exe) {
    try { $out = (& $exe -version 2>&1 | Out-String) } catch { return 0 }
    if ($out -match 'version "([^"]+)"') { $v = $Matches[1] } else { return 0 }
    if ($v.StartsWith('1.')) { $v = $v.Substring(2) }
    if ($v -match '^(\d+)') { return [int]$Matches[1] } else { return 0 }
}
function Find-Java {
    $c = @()
    if ($env:USBPREP_JAVA) { $c += $env:USBPREP_JAVA }
    if (Test-Path $Dest) { $c += Get-ChildItem $Dest -Directory | ForEach-Object { Join-Path $_.FullName 'bin\java.exe' } }
    $w = Get-Command java.exe -ErrorAction SilentlyContinue
    if ($w) { $c += $w.Source }
    foreach ($j in $c) { if ((Test-Path $j) -and (Get-JavaMajor $j) -ge 8) { return $j } }
    return $null
}
function Major-Of([string]$v) { if ($v.StartsWith('1.')) { $v = $v.Substring(2) }; if ($v -match '^(\d+)') { [int]$Matches[1] } else { 0 } }
function Test-Row($r) {
    if ($r.kind -ne 'java') { return "kind $($r.kind)" }
    if ($r.os -ne 'windows') { return "built for $($r.os)" }
    if ($r.arch -ne $Arch) { return "architecture $($r.arch) does not match this PC ($Arch)" }
    if ((Major-Of $r.version) -lt 8) { return "Java $($r.version) is older than 8" }
    if ($r.type -notin @('zip', 'msi')) { return "type $($r.type) not supported offline on Windows" }
    if ([int64]$r.size -gt $MaxF) { return 'exceeds the FAT32 4 GiB file limit' }
    $f = Join-Path $CDir $r.file
    if ($r.file -match '[\\/]' -or -not (Test-Path $f)) { return "file $($r.file) is missing" }
    if ((Get-Item $f).Length -ne [int64]$r.size) { return 'size mismatch (incomplete copy?)' }
    if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $r.sha256) { return 'SHA-256 does not match the verified vendor checksum - NOT used' }
    if ($r.type -eq 'msi' -and (Get-AuthenticodeSignature $f).Status -ne 'Valid') { return 'Authenticode signature is not Valid - NOT used' }
    return $null
}
function Install-Row($r) {
    $f = Join-Path $CDir $r.file
    if ($r.type -eq 'msi') {
        $ok = Read-Host "Install Java $($r.version) from the offline cache ($($r.file))? Windows will show a UAC prompt. [y/N]"
        if ($ok -ne 'y') { return $null }
        # Standard msiexec switches; elevation via the normal UAC prompt.
        $p = Start-Process msiexec.exe -ArgumentList @('/i', "`"$f`"", '/passive', '/norestart') -Verb RunAs -Wait -PassThru
        if ($p.ExitCode -ne 0) { Write-Host "msiexec failed with exit code $($p.ExitCode)"; return $null }
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        return Find-Java
    }
    $ok = Read-Host "Install Java $($r.version) from the offline cache ($($r.file)) for this user into $Dest (no admin rights)? [y/N]"
    if ($ok -ne 'y') { return $null }
    New-Item -ItemType Directory -Force $Dest | Out-Null
    $tmp = Join-Path $Dest ('.install-' + [guid]::NewGuid())
    Expand-Archive -Path $f -DestinationPath $tmp
    $top = Get-ChildItem $tmp -Directory | Select-Object -First 1
    $j = Join-Path $top.FullName 'bin\java.exe'
    if (-not (Test-Path $j) -or (Get-JavaMajor $j) -lt 8 -or (Get-AuthenticodeSignature $j).Status -ne 'Valid') {
        Remove-Item -Recurse -Force $tmp; Write-Host 'Archive does not contain a working, validly signed Java 8+ (java.exe).'; return $null
    }
    $final = Join-Path $Dest $top.Name
    if (Test-Path $final) { Remove-Item -Recurse -Force $final }
    Move-Item $top.FullName $final; Remove-Item -Recurse -Force $tmp
    Write-Host "Installed Java $($r.version) for this user in $final (delete that folder to uninstall)."
    return Find-Java
}

if ($Register) {
    $name = Split-Path $Register -Leaf
    $type = if ($name -like '*.zip') { 'zip' } elseif ($name -like '*.msi') { 'msi' } else { '' }
    if (-not $type) { throw "REFUSED: only .zip or .msi are supported offline on Windows" }
    $len = (Get-Item $Register).Length
    if ($len -gt $MaxF) { throw "REFUSED: $name exceeds the FAT32 4 GiB file limit" }
    $got = (Get-FileHash $Register -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $Sha256.ToLower()) { throw "REFUSED: SHA-256 $got != vendor checksum $Sha256" }
    New-Item -ItemType Directory -Force $CDir | Out-Null
    $free = (Get-PSDrive -Name (Split-Path $CDir -Qualifier).TrimEnd(':')).Free
    if ($len + 16MB -gt $free) { throw 'REFUSED: not enough free space on the EFI partition; cache unchanged' }
    $dst = Join-Path $CDir $name
    if ((Resolve-Path $Register).Path -ne $dst) {
        Copy-Item $Register "$dst.partial"
        if ((Get-FileHash "$dst.partial" -Algorithm SHA256).Hash.ToLower() -ne $got) { Remove-Item "$dst.partial"; throw 'copy failed; cache unchanged' }
        Move-Item -Force "$dst.partial" $dst
    }
    $rows = @()
    if (Test-Path $Man) { $rows = Get-Content $Man | Select-Object -Skip 1 | Where-Object { ($_ -split "`t")[6] -ne $name } }
    $lines = @("kind`tversion`tos`tarch`tsha256`tsize`tfile`ttype`tlicense`tsource") + $rows + @("java`t$Version`twindows`t$Arch`t$got`t$len`t$name`t$type`tsee vendor`tmanual")
    [IO.File]::WriteAllText("$Man.partial", (($lines -join "`n") + "`n"))
    Move-Item -Force "$Man.partial" $Man
    Write-Host "Registered $name (sha256 verified) in $Man"; exit 0
}

while ($true) {
    $java = Find-Java
    if (-not $java -and (Test-Path $Man)) {
        Write-Host 'No Java 8 or newer found on this PC.'
        $rows = Import-Csv $Man -Delimiter "`t"
        foreach ($r in $rows) {
            $why = Test-Row $r
            if ($why) { Write-Host "  cache: skipping $($r.file): $why"; continue }
            $java = Install-Row $r; break
        }
    }
    if ($java) { break }
    Write-Host @"

Java 8 or newer is needed for the GUI and was not found.
  * Free OpenJDK build (Eclipse Temurin, GPLv2 + Classpath Exception): $Link
    choose Windows, $Arch, version 8 or newer, JRE (.msi or .zip).
  * Offline: put the installer in $CDir and register it (see README.txt there); it is offered automatically next time.
Setup and ext4 access via WSL2 work without Java:  START_HERE.bat setup
"@
    $k = Read-Host 'Type R to retry, O to open the download page in your browser, or Q to quit'
    if ($k -eq 'O') { Start-Process $Link; continue }
    if ($k -ne 'R') { exit 3 }
}
Write-Host "Using Java: $java"
if ($Launch) {
    $javaw = Join-Path (Split-Path $java) 'javaw.exe'
    if (-not (Test-Path $javaw)) { $javaw = $java }
    Start-Process -FilePath $javaw -ArgumentList @('-jar', "`"$(Join-Path $AI 'gui-java\dist\usbprep-gui.jar')`"") -WorkingDirectory $AI
}
