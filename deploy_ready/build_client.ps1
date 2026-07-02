# =============================================================================
# P&ID client build - ONE COMMAND (Windows + Astra Linux).
#
#   powershell -ExecutionPolicy Bypass -File deploy_ready\build_client.ps1
#
# Steps:
#   1) version = today's date (YYYY-MM-DD);
#   2) Windows: create/update a build venv, install requirements/ui.txt +
#      PyInstaller fresh from PyPI, build PID-Client.exe, pack into a zip;
#   3) Astra: build the binary in Docker (manylinux_2_28, glibc 2.28 -> runs on
#      Astra 1.7 and 1.8), pack into a tar.gz;
#   4) put both archives into dist\release\ (date in the name), open the folder.
#
# Flags:
#   -WindowsOnly     Windows client only
#   -AstraOnly       Astra client only (Docker Desktop required)
#   -Version <str>   override version (default: date)
#   -Clean           recreate the build venv from scratch (full deps reinstall)
#
# Requirements: Python 3.11 x64 in PATH (Windows build), Docker Desktop (Astra
# build), internet (dependencies are pulled from PyPI).
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as the
# system codepage, not UTF-8, so non-ASCII text here breaks the parser.
# =============================================================================
[CmdletBinding()]
param(
    [switch]$WindowsOnly,
    [switch]$AstraOnly,
    [string]$Version = "",
    [switch]$Clean
)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $Root

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = Get-Date -Format "yyyy-MM-dd"
}
$ReleaseDir = Join-Path $Root "dist\release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

Write-Host "=== P&ID client build - version $Version ===" -ForegroundColor Cyan
Write-Host "Project root: $Root"

# ---------------------------------------------------------------------------
# Windows client (native; PyInstaller cannot cross-compile)
# ---------------------------------------------------------------------------
if (-not $AstraOnly) {
    Write-Host ""
    Write-Host "== Windows [1/3] build environment ==" -ForegroundColor Cyan
    $VenvDir = Join-Path $Root ".venv_build"
    if ($Clean -and (Test-Path $VenvDir)) { Remove-Item -Recurse -Force $VenvDir }
    if (-not (Test-Path $VenvDir)) {
        python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "failed to create venv - is Python 3.11 x64 installed and on PATH?" }
    }
    $Py = Join-Path $VenvDir "Scripts\python.exe"

    Write-Host "== Windows [2/3] dependencies from PyPI ==" -ForegroundColor Cyan
    & $Py -m pip install --upgrade pip | Out-Host
    & $Py -m pip install --upgrade -r (Join-Path $Root "requirements\ui.txt") pyinstaller | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "failed to install dependencies (no internet?)" }

    Write-Host "== Windows [3/3] PyInstaller ==" -ForegroundColor Cyan
    $WinDist = Join-Path $Root "dist\win_build"
    $WinWork = Join-Path $Root "build\win_work"
    if (Test-Path $WinDist) { Remove-Item -Recurse -Force $WinDist }
    & $Py -m PyInstaller --noconfirm --clean `
        --distpath $WinDist --workpath $WinWork `
        (Join-Path $PSScriptRoot "pid_client_windows.spec") | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (Windows) failed" }

    $App = Join-Path $WinDist "PID-Client"
    if (-not (Test-Path (Join-Path $App "PID-Client.exe"))) { throw "PID-Client.exe was not produced" }

    # configs + client.cfg + version.txt next to the exe
    Copy-Item -Recurse -Force (Join-Path $Root "configs") (Join-Path $App "configs")
    Copy-Item -Force (Join-Path $PSScriptRoot "client.cfg.template") (Join-Path $App "client.cfg")
    Set-Content -Path (Join-Path $App "version.txt") -Value $Version -NoNewline -Encoding ascii

    $Zip = Join-Path $ReleaseDir "PID-Client_windows_$Version.zip"
    if (Test-Path $Zip) { Remove-Item -Force $Zip }
    Compress-Archive -Path $App -DestinationPath $Zip -Force
    Write-Host "Windows archive ready: $Zip" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Astra client (Docker manylinux_2_28)
# ---------------------------------------------------------------------------
if (-not $WindowsOnly) {
    Write-Host ""
    Write-Host "== Astra: Docker build ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "astra_client\build_astra_client.ps1") -Version $Version
    if ($LASTEXITCODE -ne 0) { throw "Astra build failed" }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== DONE. Files to distribute are in dist\release\ ===" -ForegroundColor Green
Get-ChildItem $ReleaseDir -File | Where-Object { $_.Name -like "*$Version*" } | ForEach-Object {
    Write-Host ("  {0}  ({1} MB)" -f $_.Name, [math]::Round($_.Length / 1MB, 1))
}
Write-Host ""
Write-Host "Upload these files to the cloud and send the link to users."
try { Start-Process explorer.exe $ReleaseDir | Out-Null } catch {}
