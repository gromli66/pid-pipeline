# =============================================================================
# Build the P&ID Astra Linux client from Windows via Docker (manylinux_2_28).
# Usually called by deploy_ready\build_client.ps1, but can be run standalone:
#   powershell -ExecutionPolicy Bypass -File deploy_ready\astra_client\build_astra_client.ps1
# Requires a running Docker Desktop and internet (dependencies come from PyPI).
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as the
# system codepage, not UTF-8, so non-ASCII text here breaks the parser.
# =============================================================================
param(
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"

# Project root = two levels up (deploy_ready\astra_client\ -> repo root).
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
$Image = "pid-astra-build:manylinux228"
$Dockerfile = "$PSScriptRoot\Dockerfile.manylinux"

if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = Get-Date -Format "yyyy-MM-dd"
}

Write-Host "Project root: $Root"
Write-Host "Version: $Version"

Write-Host "== [1/2] Docker image $Image ==" -ForegroundColor Cyan
docker build -t $Image -f $Dockerfile $Root
if ($LASTEXITCODE -ne 0) { throw "docker build failed (is Docker Desktop running?)" }

Write-Host "== [2/2] Building the binary in the container ==" -ForegroundColor Cyan
# Inside the container: strip CRLF from the script (in case it was edited on Windows), then run it.
$inner = 'sed -i "s/\r$//" deploy_ready/astra_client/docker_build_inner_manylinux.sh 2>/dev/null; bash deploy_ready/astra_client/docker_build_inner_manylinux.sh'
docker run --rm -e PID_CLIENT_VERSION="$Version" -v "${Root}:/src" -w /src $Image bash -c $inner
if ($LASTEXITCODE -ne 0) { throw "container build failed" }

$archive = Join-Path $Root "dist\release\PID-Client_astra_$Version.tar.gz"
Write-Host ""
Write-Host "DONE. Astra client:" -ForegroundColor Green
Write-Host "  $archive"
