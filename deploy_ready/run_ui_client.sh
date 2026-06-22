#!/usr/bin/env bash
# Запуск десктоп-клиента P&ID на Astra Linux.
# Перед использованием: задать IP сервера в SERVER_IP.
# Требует применённой правки Д1 (PID_API_URL) — см. 01_change_ui_api_url.md

set -euo pipefail

SERVER_IP="REPLACE_WITH_SERVER_IP"

export PID_API_URL="http://${SERVER_IP}:8000"
# software-рендер GL — универсально, лечит мигание вкладки CVAT
export PID_GL_BACKEND="software"

# venv должен быть активирован, либо укажи путь к python:
#   source .venv311/bin/activate
python -m ui.main
