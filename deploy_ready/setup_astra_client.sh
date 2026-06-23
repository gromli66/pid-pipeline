#!/usr/bin/env bash
# =============================================================================
# Установка и запуск десктоп-клиента P&ID на Astra Linux (Фаза 2).
# Запускать ИЗ КОРНЯ проекта (где папки ui/, app/, modules/, configs/, requirements/).
# Сервер сейчас = твой Windows-ПК со стеком, IP 192.168.1.9.
# =============================================================================
set -e

# Перейти в корень репозитория (скрипт лежит в deploy_ready/), чтобы пути
# requirements/ui.txt, ui/, app/ резолвились независимо от места запуска.
cd "$(dirname "$0")/.."

SERVER_IP="192.168.1.9"     # при необходимости поменяй на адрес сервера

echo "== 0. Проверка Python =="
python3 --version || { echo "Нет python3"; exit 1; }
PYV=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python: $PYV  (нужен 3.11; если другой — см. примечание в конце)"

echo "== 1. Системные библиотеки Qt + WebEngine (Chromium) =="
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  libgl1 libegl1 libxkbcommon0 libdbus-1-3 \
  libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2 \
  || echo "ВНИМАНИЕ: часть пакетов не встала — запиши какие, имена в Astra могут отличаться."

echo "== 2. venv + зависимости UI =="
python3 -m venv .venv_ui
source .venv_ui/bin/activate
pip install --upgrade pip
pip install -r requirements/ui.txt
# WebEngine отдельным пакетом ставить НЕ нужно — он входит в PySide6_Addons,
# который тянется как зависимость PySide6 из requirements/ui.txt.

echo "== 3. Запуск клиента =="
export PID_API_URL="http://${SERVER_IP}:8000"
export PID_GL_BACKEND="software"     # софт-рендер: универсально для VM
python -m ui.main

# -----------------------------------------------------------------------------
# Примечание: если python3 НЕ 3.11 — PySide6 может не встать.
# Варианты: поставить python3.11 из репозитория Astra (apt-cache policy python3.11),
# либо собрать из исходников. Это и есть ключевая проверка Фазы 2 —
# зафиксируй, какая версия Python в твоей редакции Astra.
# -----------------------------------------------------------------------------
