# Сборка и раздача клиента P&ID (Windows + Astra Linux)

Одна команда собирает **оба** клиента — под Windows (`.exe`) и под Astra Linux
(автономный бинарник) — с датой-версией в имени. Дальше архивы заливаются в
облако, ссылка рассылается пользователям.

Рабочий цикл: **поменял код → запустил скрипт → раздал ссылку.**

---

## 0. Что нужно один раз

- **Windows 10/11 x64** — машина, с которой собираешь.
- **Python 3.11 x64** в `PATH` — нужен только для Windows-сборки
  (проверка: `python --version`).
- **Docker Desktop** (режим Linux-контейнеров, по умолчанию) — нужен для
  Astra-сборки. Должен быть **запущен** (проверка: `docker version` — видны
  секции `Client` и `Server`).
- **Интернет** — зависимости тянутся с PyPI при каждой сборке (см. §5).

Linux-машина для сборки Astra-клиента **не нужна** — всё в Docker.
Бинарник собирается в образе `manylinux_2_28` (glibc 2.28), поэтому работает
и на **Astra 1.7**, и на **1.8** (x86_64).

---

## 1. Сборка — одна команда

Из корня проекта в **PowerShell**:

```powershell
powershell -ExecutionPolicy Bypass -File deploy_ready\build_client.ps1
```

Что происходит:
1. версия = сегодняшняя дата (`ГГГГ-ММ-ДД`);
2. **Windows**: создаётся/обновляется `.venv_build`, ставятся `requirements/ui.txt`
   + PyInstaller **свежими с PyPI**, собирается `PID-Client.exe`, пакуется в zip;
3. **Astra**: бинарник собирается в Docker, пакуется в tar.gz;
4. оба архива кладутся в `dist\release\`, папка открывается в Проводнике.

Первый раз Astra-образ собирается долго (качает системные либы + PySide6,
~15–30 мин). Дальше — из кэша, быстро.

### Флаги

| Флаг | Зачем |
|------|-------|
| `-WindowsOnly` | собрать только Windows-клиент (Docker не нужен) |
| `-AstraOnly`   | собрать только Astra-клиент |
| `-Version 2026-07-02` | задать версию вручную (по умолчанию — дата) |
| `-Clean` | пересоздать `.venv_build` с нуля (полная переустановка зависимостей) |

Примеры:
```powershell
# только Windows
powershell -ExecutionPolicy Bypass -File deploy_ready\build_client.ps1 -WindowsOnly
# только Astra
powershell -ExecutionPolicy Bypass -File deploy_ready\build_client.ps1 -AstraOnly
```

---

## 2. Что получилось

```
dist\release\PID-Client_windows_2026-07-02.zip     ← пользователям на Windows
dist\release\PID-Client_astra_2026-07-02.tar.gz    ← пользователям на Astra 1.7/1.8
```

Дата в имени = версия сборки. Она же зашита внутрь и показывается в **заголовке
окна** клиента — сразу видно, какая сборка у пользователя.

---

## 3. Раздача пользователям

Залей оба файла (или нужный) в облако и разошли ссылку. Файлы крупные
(Windows ~сотни МБ, Astra ~650 МБ) — почта не потянет, облако/файлообменник в самый раз.

**Windows** — пользователь:
1. распаковывает zip;
2. в `PID-Client\client.cfg` вписывает IP сервера (`PID_API_URL`);
3. запускает `PID-Client.exe`.

**Astra** — пользователь:
```bash
tar -xzf PID-Client_astra_2026-07-02.tar.gz
cd PID-Client
nano client.cfg          # вписать IP сервера в PID_API_URL
./run_standalone.sh      # запуск
```
Python/venv/pip пользователю ставить **не надо** — всё внутри архива.

---

## 4. Версионирование

- Версия = дата сборки, попадает в **имя архива** и в файл `version.txt` рядом
  с бинарником; клиент читает её и показывает в заголовке окна.
- Пересборка в тот же день перезаписывает архив с той же датой. Нужна точнее —
  задай `-Version` вручную (например `2026-07-02-hotfix2`).
- Исходный код от сборки к сборке **не меняется** — версия берётся из `version.txt`,
  а не хардкодится в `.py`.

---

## 5. Пересборка после больших изменений

Скрипты рассчитаны на то, что менялось многое — код, зависимости, что угодно:

- **Код** (`ui/`, `app/`, `modules/`, `configs/`) — подхватывается автоматически,
  ничего дополнительно делать не нужно.
- **Зависимости** (`requirements/ui.txt`) — тянутся **заново из интернета**:
  - Windows: `pip install --upgrade` в `.venv_build` при каждом запуске;
  - Astra: слой Docker с зависимостями пересобирается, когда меняется `ui.txt`.
- Хочешь заведомо чистую сборку Windows (с нуля переустановить зависимости) —
  добавь `-Clean`. Для Astra чистый образ: `docker rmi pid-astra-build:manylinux228`
  и собрать заново.

---

## 6. Если что-то пошло не так

**`docker : command not found` / нет секции `Server`** → Docker Desktop не запущен.
Запусти, дождись «спокойного» кита, повтори.

**«выполнение сценариев отключено» (ExecutionPolicy)** → запускай именно с
`-ExecutionPolicy Bypass`, как в §1.

**`не удалось создать venv`** → нет Python 3.11 x64 в `PATH`. Поставь и открой
PowerShell заново.

**Первая Astra-сборка «висит» на скачивании** → это нормально, качается ~1.5 ГБ
базовых слоёв. Дождись.

**Пользователь на «голой» Astra: окно не открывается** → доустановить X/GL либы:
```bash
sudo apt-get install -y libgl1 libegl1 libxkbcommon0 libfontconfig1 \
    libdbus-1-3 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2
```

---

## 7. Где что лежит

| Файл | Назначение |
|------|-----------|
| `deploy_ready/build_client.ps1` | **мастер-сборка** (Windows + Astra), запускаешь его |
| `deploy_ready/build_client.sh` | сборка Astra-клиента с Linux/macOS-хоста (опционально) |
| `deploy_ready/pid_client_windows.spec` | конфиг PyInstaller (Windows) |
| `deploy_ready/client.cfg.template` | шаблон настроек клиента (кладётся рядом с exe) |
| `deploy_ready/astra_client/build_astra_client.ps1` | Astra-сборка через Docker |
| `deploy_ready/astra_client/Dockerfile.manylinux` | образ сборки (glibc 2.28) |
| `deploy_ready/astra_client/docker_build_inner_manylinux.sh` | что выполняется в контейнере |
| `deploy_ready/astra_client/pid_client_linux.spec` | конфиг PyInstaller (Linux) |
| `dist/release/*` | **итоговые архивы — раздать пользователям** |
