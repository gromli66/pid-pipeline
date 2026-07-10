#!/usr/bin/env bash
# =============================================================================
# Выполняется ВНУТРИ контейнера manylinux_2_28 (см. Dockerfile.manylinux).
# Собирает бинарник клиента PyInstaller-ом и пакует релизный tar.gz.
# Версия передаётся через переменную окружения PID_CLIENT_VERSION.
# =============================================================================
set -euo pipefail

ROOT=/src
VERSION="${PID_CLIENT_VERSION:-dev}"
OUT_NAME="PID-Client"
PY=/opt/buildvenv/bin
BUILD_DIST="$ROOT/dist/astra_build"
RELEASE_DIR="$BUILD_DIST/$OUT_NAME"

echo "== Версия сборки: $VERSION =="
cd "$ROOT"

# Чистим прошлый вывод именно этой сборки (не трогаем dist/release/).
rm -rf "$BUILD_DIST" "$ROOT/build/astra_work"

echo "== PyInstaller =="
"$PY/pyinstaller" --noconfirm --clean \
    --distpath "$BUILD_DIST" \
    --workpath "$ROOT/build/astra_work" \
    deploy_ready/astra_client/pid_client_linux.spec

BUILT="$BUILD_DIST/PID_Pipeline"
if [ ! -x "$BUILT/PID_Pipeline" ]; then
    echo "ОШИБКА: бинарник не собрался ($BUILT/PID_Pipeline)" >&2
    exit 1
fi

# Готовим релизную папку PID-Client/
mv "$BUILT" "$RELEASE_DIR"

# configs рядом с бинарником (клиент читает configs/projects/... в рантайме).
cp -r "$ROOT/configs" "$RELEASE_DIR/configs"

# client.cfg: адрес сервера + софт-рендер GL (универсально для Astra/VM).
cat > "$RELEASE_DIR/client.cfg" <<'CFG'
# Адрес сервера P&ID — заменить IP/домен:
PID_API_URL=http://REPLACE_WITH_SERVER_IP:8000
# Отрисовка GL: software — универсально для Astra и виртуалок.
# При наличии нормального GPU можно сменить на gles.
PID_GL_BACKEND=software
# Софт-рендер Qt Quick: без него не работает встроенный CVAT (WebEngine).
QT_QUICK_BACKEND=software
CFG

# version.txt — клиент показывает версию в заголовке окна.
printf '%s\n' "$VERSION" > "$RELEASE_DIR/version.txt"

# Скрипт запуска.
cat > "$RELEASE_DIR/run_standalone.sh" <<'RUN'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec ./PID_Pipeline "$@"
RUN

# Короткая памятка пользователю.
cat > "$RELEASE_DIR/README.txt" <<'TXT'
P&ID Pipeline — клиент для Astra Linux (x86_64)

1) Впишите адрес сервера в client.cfg (строка PID_API_URL).
2) Запуск:  ./run_standalone.sh      (или напрямую ./PID_Pipeline)

Python/venv/pip ставить НЕ нужно — всё внутри архива.
Если окно не открывается на «голой» системе без десктопа — доустановите
базовые X/GL библиотеки (см. CLIENT_LINUX_README.md).
TXT

chmod +x "$RELEASE_DIR/PID_Pipeline" "$RELEASE_DIR/run_standalone.sh"

# Пакуем tar.gz в общую папку релизов.
mkdir -p "$ROOT/dist/release"
ARCHIVE="$ROOT/dist/release/${OUT_NAME}_astra_${VERSION}.tar.gz"
rm -f "$ARCHIVE"
( cd "$BUILD_DIST" && tar -czf "$ARCHIVE" "$OUT_NAME" )

echo "== Готово: $ARCHIVE =="
ls -lh "$ARCHIVE"
