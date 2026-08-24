#!/bin/bash
# json2prtx <граф.json> <скан.png|-> <выход.prtx> [all|bound|none]
#
# Коды возврата те же, что у коробки: 1 — аргументы, 2 — вход не разобран,
# 3 — сбой движка, 4 — нет ключа лицензии.
#
# Ключ лицензии ищется в $PRTX_LIC_HOME (по умолчанию /lic): сервис кладёт его
# в tmpfs на время прогона, ручной запуск монтирует каталог в /lic.
set -u
if [ $# -lt 3 ]; then
  echo "usage: json2prtx <graph.json> <scan.png|-> <out.prtx> [all|bound|none]" >&2
  exit 1
fi
IN="$1"; IMG="$2"; OUT="$3"; MODE="${4:-all}"
LIC_HOME="${PRTX_LIC_HOME:-/lic}"
if [ ! -f "$LIC_HOME/.S\$lk\$.bin" ]; then
  echo "Лицензия САПФИР не найдена: нет файла $LIC_HOME/.S\$lk\$.bin" >&2
  exit 4
fi
TMP="$(mktemp -d)"
XML="$TMP/schema.xml"
# Этап 1 — чистый Python. Сайдкары (*.unknow.json и др.) кладутся рядом с XML
# и ОБЯЗАТЕЛЬНЫ: без них движок не видит unknow-аппараты и молча даёт другую
# схему (замер 2026-08-21).
[ "$IMG" = "-" ] && IMG="__no_image__"
python3 /opt/box/py/json2xml.py "$IN" --image "$IMG" \
    --class-map /opt/box/config/class_map_sapfir.json \
    --xconv /opt/box/engine/SETTINGS/aspiritDiagramConverter.xconv \
    --text "$MODE" -o "$XML" || exit 2

cd /opt/box/engine/SETTINGS
xvfb-run -a --server-args="-screen 0 1600x1200x24" \
java --add-opens java.desktop/sun.font=ALL-UNNAMED --add-opens java.desktop/sun.awt=ALL-UNNAMED \
     --add-exports java.desktop/sun.awt.image=ALL-UNNAMED --add-opens java.desktop/sun.java2d.opengl=ALL-UNNAMED \
     --add-opens java.base/java.lang=ALL-UNNAMED -Xmx3072M \
     -Duser.country=RU -Duser.language=ru -Duser.home="$LIC_HOME" \
     -Dprism.order=sw -Djava.awt.headless=false \
     -Djava.library.path=/opt/box/engine:/usr/lib/x86_64-linux-gnu/jni \
     -Dxml2prtx.merge.channels=1 -Dunknow.placeholder=/opt/box/config/unknow_placeholder.png \
     -cp "/opt/box/engine/cli-classes:/opt/box/engine/root_patched.jar:/opt/box/engine/thirdparty/*:/usr/share/openjfx/lib/*" \
     ru.get.dcad.SPIRIT.Xml2PrtxCli "$XML" "$OUT" /opt/box/engine/SETTINGS/Default.reg || exit 3
[ -s "$OUT" ] || { echo "движок отчитался успехом, но файла нет" >&2; exit 3; }
rm -rf "$TMP"
