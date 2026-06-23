#!/usr/bin/env bash
# Запуск десктоп-клиента P&ID (Astra Linux).
# Перед первым запуском впиши IP сервера в SERVER_IP ниже.
# Требует выполненной установки клиента (см. DEPLOY_README.md, часть B).
set -euo pipefail

SERVER_IP="REPLACE_WITH_SERVER_IP"     # <-- вписать IP/домен сервера один раз

# перейти в корень репозитория (скрипт лежит в deploy_ready/)
cd "$(dirname "$0")/.."

# включить установленный venv
source .venv_ui/bin/activate

export PID_API_URL="http://${SERVER_IP}:8000"
export PID_GL_BACKEND="software"   # софт-рендер GL — универсально для VM/Astra
python3 -m ui.main
