#!/usr/bin/env bash
# Приёмка выката P&ID на сервере: код, база, сервисы и сквозная сборка .prtx.
#
#   bash tools/deploy_check.sh [путь-к-ключу-лицензии]
#
# Ключ нужен только для последней проверки (сборка расчётной схемы). Без него
# скрипт отработает остальные пункты и честно скажет, что prtx не проверен.
# Содержимое ключа никуда не печатается.
set -u

API="${API:-http://localhost:8000}"
KEY="${1:-$HOME/lic_key.bin}"
ok=0; fail=0

say()  { printf '\n=== %s ===\n' "$1"; }
good() { printf '  OK   %s\n' "$1"; ok=$((ok+1)); }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

say "1. Код и место"
git -C ~/pid log --oneline -1
df -h / | tail -1
# Сверяемся с origin, а не с зашитым номером: иначе скрипт «краснеет» после
# каждого следующего выката, хотя код как раз свежий.
git -C ~/pid fetch -q origin deploy 2>/dev/null
local_sha=$(git -C ~/pid rev-parse HEAD)
remote_sha=$(git -C ~/pid rev-parse origin/deploy)
if [ "$local_sha" = "$remote_sha" ]; then
    good "код совпадает с origin/deploy"
else
    bad "код разошёлся с origin/deploy (локально ${local_sha:0:7}, в origin ${remote_sha:0:7})"
fi

say "2. Схема базы"
cur=$(docker compose -f ~/pid/docker-compose.yml run --rm api alembic current 2>/dev/null | tail -1)
echo "  $cur"
case "$cur" in *0013_add_prtx*) good "миграции на head" ;; *) bad "база не на 0013" ;; esac

say "3. Сервисы"
for c in pid_api pid_worker pid_worker_ocr pid_prtx pid_postgres pid_redis; do
    st=$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null)
    case "$st" in
        running|running/healthy) good "$c ($st)" ;;
        *)                       bad  "$c ($st)" ;;
    esac
done

say "4. API"
h=$(curl -s -m 10 "$API/health")
echo "  $h"
case "$h" in *'"status":"healthy"'*) good "/health" ;; *) bad "/health" ;; esac

say "5. Конвертер внутри контейнера"
p=$(docker exec pid_prtx python3 -c \
    "import urllib.request;print(urllib.request.urlopen('http://localhost:8081/health').read().decode())" 2>&1)
echo "  $p"
case "$p" in *'"ok": true'*) good "prtx /health" ;; *) bad "prtx /health" ;; esac

say "6. Диаграмма с валидированным графом"
uid=$(ls -1 ~/pid/storage/diagrams/*/graph/graph_validated.json 2>/dev/null \
      | head -1 | awk -F/ '{print $(NF-2)}')
if [ -z "$uid" ]; then
    bad "не нашёл ни одной диаграммы с graph_validated.json — сборку .prtx проверить не на чем"
else
    good "нашлась $uid"
fi

say "7. Сквозная сборка .prtx"
if [ -z "$uid" ]; then
    echo "  ПРОПУЩЕНО: нет подходящей диаграммы"
elif [ ! -f "$KEY" ]; then
    echo "  ПРОПУЩЕНО: нет файла ключа ($KEY)"
    echo "  Скопируйте ключ и повторите: bash tools/deploy_check.sh ~/lic_key.bin"
else
    # Сборку ждём ОПРОСОМ: /prtx/build отвечает 202 сразу и считает в фоне
    # (движок берёт десятки секунд, держать соединение всё это время нельзя —
    # промежуточные узлы рвут его по бездействию).
    echo "  ставлю задание..."
    t0=$(date +%s)
    code=$(curl -s -m 60 -o /tmp/prtx_build.json -w '%{http_code}' \
           -F "license=@$KEY" "$API/api/graph/$uid/prtx/build")
    echo "  HTTP $code: $(head -c 300 /tmp/prtx_build.json)"
    if [ "$code" != "202" ]; then
        bad "задание не поставлено (ждали HTTP 202)"
    else
        state=timeout
        st=""
        for _ in $(seq 1 300); do        # 300 x 3 c = 15 мин, как у клиента
            sleep 3
            st=$(curl -s -m 15 "$API/api/graph/$uid/prtx/status")
            case "$st" in
                *'"state":"done"'*)  state=done;  break ;;
                *'"state":"error"'*) state=error; break ;;
                *'"state":"idle"'*)  state=idle;  break ;;
            esac
        done
        echo "  сборка: $state за $(( $(date +%s) - t0 )) с: $(printf '%s' "$st" | head -c 300)"
        if [ "$state" = "done" ]; then
            good "схема собрана на сервере"
            dl=$(curl -s -m 60 -o /tmp/prtx_dl.prtx -w '%{http_code}' \
                 "$API/api/diagrams/$uid/download/prtx")
            sz=$(stat -c%s /tmp/prtx_dl.prtx 2>/dev/null || echo 0)
            echo "  скачивание артефакта: HTTP $dl, $sz байт"
            { [ "$dl" = "200" ] && [ "$sz" -gt 10000 ]; } \
                && good "артефакт скачивается" || bad "артефакт не скачался"
        else
            bad "сборка не прошла ($state)"
        fi
    fi
    rm -f /tmp/prtx_build.json /tmp/prtx_dl.prtx
fi

say "8. Ключ в логах (его там быть не должно)"
n=$(docker compose -f ~/pid/docker-compose.yml logs --since 30m api prtx 2>/dev/null \
    | grep -ciE 'lk\$\.bin|license=|BEGIN LICENSE')
echo "  подозрительных строк: $n"
[ "$n" = "0" ] && good "ключ в логи не утёк" || bad "в логах есть упоминания ключа — посмотреть глазами"

printf '\n=== ИТОГ: OK %d, FAIL %d ===\n' "$ok" "$fail"
[ "$fail" = "0" ] || exit 1
