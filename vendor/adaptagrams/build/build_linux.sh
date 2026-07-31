#!/bin/sh
# Сборка _adaptagrams.so (SWIG-биндинг Adaptagrams/libavoid) для Linux.
# Вызов: build_linux.sh <outdir>   — кладёт в <outdir> _adaptagrams.so и adaptagrams.py
#
# Зеркалит проверенную MSVC-сборку (build_*_msvc.bat из разведки Э7-b):
# без autotools, пять библиотек компилируются напрямую, обёртка — swig 3/4.
# Коммит фиксирован: клон master нестабилен, а бинарь обязан быть
# воспроизводимым (см. README.md рядом).
set -eu

OUT="${1:?usage: build_linux.sh <outdir>}"
COMMIT=840ebcff20dbba36ad03a2160edf7cbaf9859984
SRC="$(mktemp -d)/adaptagrams"
HERE="$(cd "$(dirname "$0")" && pwd)"
PYINC="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')"

git clone --no-checkout https://github.com/mjwybrow/adaptagrams.git "$SRC"
git -C "$SRC" checkout "$COMMIT"
git -C "$SRC" apply "$HERE/micropatches.diff"

cd "$SRC/cola"
CXX="${CXX:-g++}"
FLAGS="-O2 -fPIC -std=c++17 -DNDEBUG -DUSE_ASSERT_EXCEPTIONS -DSWIG_PYTHON_SILENT_MEMLEAK -I."

for lib in libvpsc libcola libtopology libdialect libavoid; do
    echo "===== $lib ====="
    for f in "$lib"/*.cpp; do
        $CXX $FLAGS -c "$f" -o "${f%.cpp}.o"
    done
done

echo "===== swig ====="
swig -DNDEBUG -c++ -python adaptagrams.i

echo "===== link ====="
$CXX $FLAGS -I"$PYINC" -c adaptagrams_wrap.cxx -o adaptagrams_wrap.o
$CXX -shared -o _adaptagrams.so adaptagrams_wrap.o \
    libavoid/*.o libvpsc/*.o libcola/*.o libtopology/*.o libdialect/*.o

mkdir -p "$OUT"
cp _adaptagrams.so adaptagrams.py "$OUT/"
python3 -c "import sys; sys.path.insert(0, '$OUT'); import adaptagrams as ag; \
r = ag.Router(ag.OrthogonalRouting); print('adaptagrams OK:', ag.__name__)"
echo "BUILD_OK -> $OUT"
