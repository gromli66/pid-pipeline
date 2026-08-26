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

# Канарейка: фаза диаметров обязана быть в контексте.
#
# Без неё образ соберётся МОЛЧА, а канал без Ду останется с заводскими 0.3 м —
# в САПФИР это честный Ду300. Пропуск здесь даёт не отказ, а неверные
# инженерные данные, поэтому сборка падает, а не предупреждает.
$j2x = Join-Path $ctx "py/json2xml.py"
if (-not (Select-String -Path $j2x -Pattern "diameter_value" -SimpleMatch -Quiet)) {
    throw "В контексте нет фазы Ду: py\json2xml.py без diameter_value. Обновите коробку $Box."
}
$cls = Join-Path $ctx "engine/cli-classes/ru/get/dcad/SPIRIT/ChannelMerger.class"
if (-not (Test-Path $cls)) { throw "Нет $cls — cli-classes в контекст не попали" }
$clsText = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($cls))
if ($clsText -notmatch "channelDiameters") {
    throw ("cli-classes собраны БЕЗ фазы Ду (в ChannelMerger.class нет channelDiameters). " +
           "Пересоберите классы в коробке: javac ... cli-src/ru/get/dcad/SPIRIT/*.java")
}
Write-Host "канарейка Ду: фаза на месте (json2xml + cli-classes)"

# Штамп версии коробки. Без него по работающему образу нельзя установить, какой
# конвертер внутри: сервис приезжает готовым файлом, а не сборкой из репозитория
# (в docker-compose.yml у prtx нет секции build). Сервис отдаёт штамп в /health.
$commit = (& git -C $Box rev-parse --short HEAD 2>$null)
if (-not $commit) { $commit = "unknown" }
# Грязным считается только то, что уезжает в образ: SETTINGS движок пишет сам
# на каждом прогоне, и по нему дерево грязное всегда.
$dirty = (& git -C $Box status --porcelain -- py config engine/cli-classes engine/cli-src 2>$null)
$dirtyFlag = 0
if ($dirty) { $dirtyFlag = 1 }
@(
    "commit=$commit",
    "dirty=$dirtyFlag",
    "diam=1",
    "built=$(Get-Date -Format 'yyyy-MM-dd HH:mm')"
) -join "`n" | Set-Content -Path (Join-Path $ctx "box.commit") -Encoding utf8
if ($dirtyFlag -eq 1) {
    Write-Host "версия коробки: $commit — ВНИМАНИЕ, дерево грязное, образ несёт незакоммиченное"
} else {
    Write-Host "версия коробки: $commit"
}

$size = [math]::Round(((Get-ChildItem -Recurse $ctx | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
Write-Host "размер контекста: $size МБ"

docker build -t $Tag $ctx
if ($LASTEXITCODE -ne 0) { throw "docker build: код $LASTEXITCODE" }

Remove-Item -Recurse -Force $ctx
Write-Host "образ собран: $Tag"
