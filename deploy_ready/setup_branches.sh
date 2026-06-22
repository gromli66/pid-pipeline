#!/usr/bin/env bash
# =============================================================================
# Создаёт две ветки из текущей deploy-копии и коммитит:
#   main   — код для GPU / Windows-local (GPU включён, без deploy-файлов)
#   deploy — CPU/Astra (GPU off, docker-compose.override.yml, deploy_ready/,
#            DEPLOY_README.md, MIGRATION_PLAN.md)
# Общее для обеих веток: код приложения, tools/, Д1 (PID_API_URL), Д3 (CVAT_TOKEN из env).
#
# Запускать в Git Bash ИЗ КОРНЯ проекта:
#   bash deploy_ready/setup_branches.sh
# Потом задать remote нового репозитория и запушить (см. вывод в конце).
# Модели, .env, архивы, кэши, tools-нет — в .gitignore, в коммиты не попадут.
# =============================================================================
set -e
cd "$(git rev-parse --show-toplevel)"

rm -f .git/index.lock 2>/dev/null || true
git config user.email "${GIT_EMAIL:-deploy@local}"
git config user.name  "${GIT_NAME:-deploy}"

TMP="$(mktemp -d)"
echo "Временная папка: $TMP"

# Сохраняем вне репозитория: обе версии compose + deploy-only файлы
cp docker-compose.yml          "$TMP/cpu.yml"        # текущий = CPU (GPU закомментирован)
cp docker-compose.gpu.yml      "$TMP/gpu.yml"        # GPU-версия для main
cp docker-compose.override.yml "$TMP/override.yml"
cp DEPLOY_README.md            "$TMP/DEPLOY_README.md"
cp MIGRATION_PLAN.md           "$TMP/MIGRATION_PLAN.md"
cp -a deploy_ready             "$TMP/deploy_ready"

echo "=== Ветка main (GPU / Windows) ==="
cp "$TMP/gpu.yml" docker-compose.yml          # main = GPU-версия
rm -f docker-compose.gpu.yml docker-compose.override.yml DEPLOY_README.md MIGRATION_PLAN.md
rm -rf deploy_ready
git checkout -B main
git add -A
git commit -m "main: код (GPU / Windows local) + PID_API_URL + CVAT_TOKEN из env, утилиты в tools/"

echo "=== Ветка deploy (CPU / Astra) ==="
git checkout -b deploy
cp "$TMP/cpu.yml" docker-compose.yml          # deploy = CPU-версия
cp "$TMP/override.yml" docker-compose.override.yml
cp "$TMP/DEPLOY_README.md" DEPLOY_README.md
cp "$TMP/MIGRATION_PLAN.md" MIGRATION_PLAN.md
cp -a "$TMP/deploy_ready" deploy_ready
git add -A
git commit -m "deploy: CPU/Astra (GPU off, override.yml, deploy_ready/, DEPLOY_README, MIGRATION_PLAN)"

rm -rf "$TMP"

echo
echo "================ ГОТОВО ================"
git --no-pager branch
echo
echo "Дальше — привязать НОВЫЙ пустой репозиторий и запушить:"
echo "  git remote set-url origin https://github.com/<ТВОЙ_АККАУНТ>/<НОВЫЙ_РЕПО>.git"
echo "  git push -u origin main"
echo "  git push -u origin deploy"
echo
echo "Проверить адрес: git remote -v"
echo "Сейчас активна ветка deploy."
