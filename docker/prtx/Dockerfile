# Сборка расчётной схемы САПФИР (.prtx) из graph_validated.json.
#
# Контекст сборки — НЕ этот репозиторий: образ собирается из коробки конвертера
# (`C:\project\prt_convertor\converter-box`, свой git), которая весит 250 МБ и
# под git этого проекта не лежит. Контекст готовит `build.ps1`.
#
# Ключ лицензии в образ НЕ КЛАДЁТСЯ: он приезжает с клиентом на время одного
# прогона и живёт в tmpfs (см. server.py). Ручной прогон монтирует каталог с
# ключом в /lic.
#
# Состав образа проверен замером 2026-08-21: дамп полученного .prtx совпадает
# с windows-дампом байт-в-байт. Не менять список пакетов и библиотек классов
# без повторной сверки дампа — без JavaFX движок падает на QuantumRenderer,
# без переписанных путей в Default.reg молча отдаёт вырожденные 3 КБ.
FROM eclipse-temurin:17-jre

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb libxext6 libxrender1 libxtst6 libxi6 libxrandr2 \
        fontconfig fonts-dejavu-core \
        openjfx libgtk-3-0 libgl1 libglu1-mesa libgl1-mesa-dri \
        python3 python3-pil \
    && rm -rf /var/lib/apt/lists/*

COPY engine /opt/box/engine
COPY py     /opt/box/py
COPY config /opt/box/config
COPY fix_paths.py /opt/box/fix_paths.py
COPY server.py    /opt/box/server.py
COPY json2prtx.sh /usr/local/bin/json2prtx
# Версия коробки, из которой собран образ (пишет build.ps1). Сервис отдаёт её
# в /health: иначе по работающему контейнеру не установить, какой конвертер
# внутри — образ приезжает готовым файлом, а не сборкой из репозитория.
COPY box.commit   /opt/box/box.commit

RUN python3 /opt/box/fix_paths.py \
 && python3 /opt/box/py/_write_s3ini.py /opt/box/engine \
 && chmod +x /usr/local/bin/json2prtx

EXPOSE 8081

# Сервис. Ручной прогон одной схемы: --entrypoint json2prtx
CMD ["python3", "/opt/box/server.py"]
