#!/usr/bin/env bash
# =============================================================================
# P&ID — сборка Astra-клиента с Linux/macOS-хоста через Docker.
# (Windows .exe с не-Windows хоста не собрать — ограничение PyInstaller;
#  для Windows-клиента используй deploy_ready\build_client.ps1 на Windows.)
#
#   bash deploy_ready/build_client.sh [ВЕРСИЯ]
#
# ВЕРСИЯ по умолчанию — сегодняшняя дата (YYYY-MM-DD).
# Требует установленного Docker и интернета (зависимости тянутся с PyPI).
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VERSION="${1:-$(date +%Y-%m-%d)}"
IMAGE="pid-astra-build:manylinux228"

echo "=== Astra-сборка клиента P&ID — версия $VERSION ==="
echo "Корень проекта: $ROOT"

echo "== [1/2] Docker-образ $IMAGE =="
docker build -t "$IMAGE" -f "$HERE/astra_client/Dockerfile.manylinux" "$ROOT"

echo "== [2/2] Сборка бинарника в контейнере =="
docker run --rm -e PID_CLIENT_VERSION="$VERSION" -v "$ROOT:/src" -w /src "$IMAGE" \
    bash -c 'sed -i "s/\r$//" deploy_ready/astra_client/docker_build_inner_manylinux.sh 2>/dev/null; bash deploy_ready/astra_client/docker_build_inner_manylinux.sh'

echo ""
echo "=== ГОТОВО ==="
echo "  $ROOT/dist/release/PID-Client_astra_$VERSION.tar.gz"
