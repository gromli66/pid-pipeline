#!/usr/bin/env bash
# =============================================================================
# Развёртывание серверной части P&ID — Фаза 1/3 (связь + CVAT, БЕЗ воркеров).
# Запускать на СЕРВЕРЕ (Astra/Ubuntu) из корня проекта.
# Предполагается: код уже здесь (git clone), .env заполнен (из .env.server.template),
# docker-compose.override.yml на месте.
# Связано с MIGRATION_PLAN.md, Фаза 3.
# =============================================================================
set -euo pipefail

echo "== 0. Проверка Docker =="
docker --version || { echo "Docker не установлен. См. https://docs.docker.com/engine/install/"; exit 1; }
docker compose version || { echo "Docker Compose v2 не установлен."; exit 1; }

echo "== 0b. Проверка .env =="
[ -f .env ] || { echo "Нет .env — скопируй deploy_ready/.env.server.template в .env и заполни."; exit 1; }
grep -q "REPLACE_WITH_SERVER_IP" .env && { echo "В .env остались плейсхолдеры REPLACE_WITH_SERVER_IP — заполни IP."; exit 1; }

echo "== 1. Подъём стека БЕЗ воркеров (Фаза 1/3) =="
docker compose up -d \
  postgres redis api \
  cvat_db cvat_redis_inmem cvat_redis_ondisk \
  cvat_server cvat_worker_import cvat_worker_export cvat_worker_chunks \
  cvat_ui cvat_opa traefik

echo "== 2. Миграции БД =="
sleep 5
docker exec -it pid_api alembic upgrade head

echo "== 3. Суперпользователь CVAT (интерактивно) =="
echo "   Сейчас создашь логин/пароль администратора CVAT."
docker exec -it cvat_server bash -ic 'python3 ~/manage.py createsuperuser' || true

echo
echo "== 4. Токен CVAT (Д3) =="
echo "   Получи токен (через CVAT UI: профиль -> или REST /api/auth/login),"
echo "   впиши его в .env как CVAT_TOKEN=..., затем выполни:"
echo "     docker compose up -d api"
echo

echo "== 5. Healthcheck =="
sleep 3
curl -fsS http://localhost:8000/health && echo "  -> API OK" || echo "  -> API не отвечает"
curl -fsS http://localhost:8080/api/server/about >/dev/null && echo "  -> CVAT OK" || echo "  -> CVAT не отвечает"

echo
echo "Готово. С клиента проверь:  curl http://<IP-сервера>:8000/health"
echo "Не забудь фаервол: открыть только 8000 и 8080 (лучше только на свой IP)."
