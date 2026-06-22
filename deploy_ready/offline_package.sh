#!/usr/bin/env bash
# =============================================================================
# Скелет упаковки ОФЛАЙН-комплекта (Фаза 5) — для закрытого контура без интернета.
# Запускать на ОНЛАЙН-машине, где образы уже собраны/скачаны (Фаза 3/4).
# Кода не меняет — только пакует артефакты.
# =============================================================================
set -euo pipefail
OUT="offline_bundle"
mkdir -p "$OUT"

echo "== 1. Сохранение Docker-образов =="
# Перечень образов проекта + CVAT. Уточни теги под свою сборку (docker images).
IMAGES=$(docker compose config --images 2>/dev/null | sort -u)
echo "Будут сохранены образы:"; echo "$IMAGES"
docker save $IMAGES -o "$OUT/images.tar"
echo "  -> $OUT/images.tar  (на сервере: docker load -i images.tar)"

echo "== 2. Колёса для нативного клиента Astra (Python 3.11 / x86_64) =="
# Запускать ЖЕЛАТЕЛЬНО на самой Astra (или с точными --platform/--python-version).
mkdir -p "$OUT/wheels"
pip download -r requirements/ui.txt -d "$OUT/wheels" || true
pip download "PySide6-WebEngine>=6.6.0" -d "$OUT/wheels" || true
echo "  -> на клиенте: pip install --no-index --find-links wheels -r requirements/ui.txt"

echo "== 3. Модели и кэши =="
# Скопировать веса и прогретые кэши (HF/datalab). Размер ~3 ГБ+.
cp -r models "$OUT/models" 2>/dev/null || echo "  (models/ не найдено — скопируй вручную)"
# Кэши HuggingFace/Surya, если использовались (пути зависят от HF_HOME):
# cp -r ~/.cache/huggingface "$OUT/hf_cache"
# cp -r ~/.cache/datalab     "$OUT/datalab_cache"

echo "== 4. Код и конфиги =="
# Код — без venv/архивов; .env переносится ОТДЕЛЬНО (секреты).
git archive --format=tar HEAD | (mkdir -p "$OUT/code" && tar -x -C "$OUT/code") 2>/dev/null \
  || echo "  (не git-репозиторий — скопируй код вручную без .venv311 и архивов)"

echo
echo "Готово: папка $OUT/. Перенести на закрытый сервер, затем:"
echo "  docker load -i images.tar  &&  docker compose up -d ..."
echo "  не забыть выставить HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 в .env"
echo "ПРОВЕРИТЬ комплект на чистой машине без интернета ДО боевого сервера."
