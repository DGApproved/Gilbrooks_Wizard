@echo off
REM Windows entry point (double-click).
REM   START_HERE.bat          -> find Java 8+ (or offer the verified cached installer, or show the download link + Retry), start the GUI
REM   START_HERE.bat setup .. -> consent-gated setup / ext4 access via WSL2 + usbipd-win (setup\windows_setup.ps1)
REM -ExecutionPolicy RemoteSigned applies to this one PowerShell process only; no system policy is changed.
if /I "%~1"=="setup" (
  shift
  powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0..\..\setup\windows_setup.ps1" %2 %3 %4 %5 %6
  pause
  exit /b
)
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0bootstrap_java.ps1" -Launch
if errorlevel 1 pause
