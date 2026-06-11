$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot\..

if (-not (Test-Path .venv)) {
    py -m venv .venv
}

. .\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

if (Test-Path build) {
    Remove-Item build -Recurse -Force
}

if (Test-Path dist\DeepSeekDesktopGateway) {
    Remove-Item dist\DeepSeekDesktopGateway -Recurse -Force
}

python -m PyInstaller -y --clean packaging\pyinstaller.spec

$releaseDir = Join-Path (Get-Location) 'dist\release'
if (-not (Test-Path $releaseDir)) {
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
}

$appDir = Join-Path $releaseDir 'DeepSeekDesktopGateway'
if (Test-Path $appDir) {
    Remove-Item $appDir -Recurse -Force
}

Copy-Item dist\DeepSeekDesktopGateway $appDir -Recurse
Copy-Item docs\*.md $releaseDir

$launcherScript = @"
@echo off
setlocal
cd /d "%~dp0DeepSeekDesktopGateway"
start "" "DeepSeekDesktopGateway.exe"
endlocal
"@

Set-Content -Path (Join-Path $releaseDir 'Launch-DeepSeekDesktopGateway.cmd') -Value $launcherScript -Encoding Ascii

Write-Host "Release package created: $releaseDir"
Write-Host "Double-click dist\release\Launch-DeepSeekDesktopGateway.cmd to start the GUI."
