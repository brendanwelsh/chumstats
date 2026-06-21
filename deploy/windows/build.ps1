# Build the Chumstats friend distributable.
#
# Run from the repo root:
#     ./deploy/windows/build.ps1
#
# Output: dist/Chumstats/Chumstats.exe + supporting files.
# Zip the dist/Chumstats/ folder to ship to friends.

$ErrorActionPreference = "Stop"

$Root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $Root

$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Error "Expected venv at $Venv. Create it first: python -m venv .venv"
}

Write-Host "==> ensuring build dependencies" -ForegroundColor Cyan
& $VenvPy -m pip install --quiet --upgrade pip pyinstaller
& $VenvPy -m pip install --quiet -e ".[server,tray]"

Write-Host "==> cleaning previous build" -ForegroundColor Cyan
if (Test-Path "$Root\dist\Chumstats") {
    Remove-Item -Recurse -Force "$Root\dist\Chumstats"
}
if (Test-Path "$Root\build") {
    Remove-Item -Recurse -Force "$Root\build"
}

Write-Host "==> running PyInstaller" -ForegroundColor Cyan
& $VenvPy -m PyInstaller "deploy\windows\Chumstats.spec" --noconfirm --clean

$ExePath = "$Root\dist\Chumstats\Chumstats.exe"
if (-not (Test-Path $ExePath)) {
    Write-Error "Build failed -- no exe at $ExePath"
}

$Size = (Get-ChildItem -Recurse "$Root\dist\Chumstats" | Measure-Object Length -Sum).Sum
$SizeMB = [math]::Round($Size / 1MB, 1)

Write-Host ""
Write-Host "==> SUCCESS" -ForegroundColor Green
Write-Host "    $ExePath"
Write-Host "    Bundle size: ${SizeMB} MB"
Write-Host ""
Write-Host "To ship to a friend:"
Write-Host "    Compress-Archive -Path dist\Chumstats\* -DestinationPath Chumstats.zip"
Write-Host ""
Write-Host "To test the bundled exe yourself first:"
Write-Host "    $ExePath"
