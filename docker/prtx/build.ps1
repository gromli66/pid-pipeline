# Собрать образ prtx-конвертера.
#
# Контекст сборки — коробка конвертера (свой git, ~250 МБ, под git этого
# проекта не лежит). Скрипт готовит временный контекст: берёт из коробки
# engine/py/config, выкидывает то, что в образе не нужно (windows-dll,
# исходники java-CLI, накопленные логи движка), докладывает свои файлы.
#
# usage: powershell -File docker\prtx\build.ps1 [-Box <путь>] [-Tag <имя>]
param(
    [string]$Box = "C:\project\prt_convertor\converter-box",
    [string]$Tag = "pid-prtx:latest"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $Box "json2prtx.cmd"))) {
    throw "Коробка конвертера не найдена: $Box"
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ctx = Join-Path $env:TEMP "prtx_ctx"

if (Test-Path $ctx) { Remove-Item -Recurse -Force $ctx }
New-Item -ItemType Directory -Force $ctx | Out-Null

Write-Host "контекст: $ctx"
# /NFL /NDL /NJH /NJS — robocopy иначе печатает построчный список 250 МБ файлов
$mute = @("/NFL", "/NDL", "/NJH", "/NJS", "/NP")
robocopy (Join-Path $Box "engine") (Join-Path $ctx "engine") /E `
    /XF "*.dll" "EventLog.log" "EventLog.log.prev" ".pos" `
    /XD "cli-src" "s3home" "Logs" "__pycache__" @mute | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy engine: код $LASTEXITCODE" }

robocopy (Join-Path $Box "py") (Join-Path $ctx "py") /E /XD "__pycache__" @mute | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy py: код $LASTEXITCODE" }

robocopy (Join-Path $Box "config") (Join-Path $ctx "config") /E /XD "__pycache__" @mute | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy config: код $LASTEXITCODE" }

foreach ($f in @("Dockerfile", "fix_paths.py", "server.py", "json2prtx.sh")) {
    Copy-Item (Join-Path $here $f) (Join-Path $ctx $f) -Force
}

$size = [math]::Round(((Get-ChildItem -Recurse $ctx | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
Write-Host "размер контекста: $size МБ"

docker build -t $Tag $ctx
if ($LASTEXITCODE -ne 0) { throw "docker build: код $LASTEXITCODE" }

Remove-Item -Recurse -Force $ctx
Write-Host "образ собран: $Tag"
