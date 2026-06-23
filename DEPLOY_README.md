# Развёртывание P&ID Pipeline (Linux-сервер + Astra-клиент)

Проверено на боевом прогоне: облачный Linux-сервер + клиент на Astra Linux, несколько клиентов одновременно.

**Архитектура:** один **сервер** (Docker-стек: PostgreSQL, Redis, FastAPI `api`, CVAT, воркеры) + **клиенты** (десктоп-UI на Astra Linux), подключаются по сети. Все данные (схемы, артефакты, БД) — **на сервере**; клиент тонкий, тянет/отдаёт всё по HTTP.

Ветка `deploy` — для **CPU-сервера** (GPU в `docker-compose.yml` отключён). Если на сервере есть NVIDIA GPU — раскомментируй блок `deploy.resources...devices` у `worker`/`worker_ocr` в `docker-compose.yml` и поставь в `.env` `PID_DEVICE=auto`.

Репозиторий: `https://github.com/gromli66/pid-pipeline` (ветка `deploy`).

---

# ЧАСТЬ A. СЕРВЕР (Linux)

## A1. Требования
- OS: Ubuntu 22.04/24.04 (или другой Linux с Docker). RAM **16–20 ГБ**, диск **80–120 ГБ**, CPU (GPU не нужен), **публичный/доступный по сети IP**.
- Открыть входящие порты: **22** (SSH), **8000** (API), **8080** (CVAT). БД/Redis наружу НЕ открывать.

## A2. Docker и git (по SSH на сервере)
```bash
ssh root@<IP-сервера>
apt-get update && apt-get install -y git
curl -fsSL https://get.docker.com | sudo sh
docker --version && docker compose version
```

## A3. Код (ветка deploy) и модели
```bash
git clone -b deploy https://github.com/gromli66/pid-pipeline.git ~/pid
cd ~/pid
```
**Модели не в git** (~3 ГБ) — перенести отдельно с машины, где они есть:
```bash
# с машины с моделями (папка models целиком кладётся в ~/pid/):
scp -r <путь>/models root@<IP-сервера>:~/pid/
```
Проверка: `ls ~/pid/models/yolo/ensemble_v1/tile640/best.pt` (~50 МБ).

## A4. Настроить .env
```bash
cp deploy_ready/.env.server.template .env
nano .env
```
Заполнить (адрес сервера — одинаковый в трёх местах):
```
SERVER_HOST=<IP-сервера>
CVAT_HOST=<IP-сервера>
CVAT_BROWSER_URL=http://<IP-сервера>:8080
DB_PASSWORD=<надёжный пароль>
CVAT_SUPERUSER=admin
CVAT_SUPERUSER_PASSWORD=<надёжный пароль>
PID_DEVICE=cpu
YOLO_DEVICE=cpu
CVAT_TOKEN=               # заполним на шаге A7
```
`docker-compose.override.yml` уже в ветке — отдаёт `CVAT_BROWSER_URL` сервисам `api` и `worker`.

## A5. Фаервол (если нет облачного)
```bash
sudo ufw allow 22/tcp && sudo ufw allow 8000/tcp && sudo ufw allow 8080/tcp && sudo ufw enable
```

## A6. Сборка и запуск
```bash
docker compose build      # тянет torch/CUDA-слои и зависимости — разово, не быстро
docker compose up -d
docker compose ps         # все контейнеры running/healthy
docker exec -it pid_api alembic upgrade head
```

## A7. Суперпользователь и токен CVAT
```bash
docker exec -e DJANGO_SUPERUSER_PASSWORD=<пароль_cvat> cvat_server \
  python3 /home/django/manage.py createsuperuser --username admin --email a@a.com --noinput
docker exec cvat_server python3 /home/django/manage.py drf_create_token admin
```
Вторая команда выведет `Generated token <КЛЮЧ>`. Вписать ключ в `.env` и перезапустить:
```bash
sed -i 's/^CVAT_TOKEN=.*/CVAT_TOKEN=<КЛЮЧ>/' .env
docker compose up -d api worker
docker exec pid_worker printenv CVAT_TOKEN     # должен показать новый ключ
```
> Токен читается из `.env` (`${CVAT_TOKEN}`) и `api`, и `worker`. На свежем CVAT он всегда новый.

## A8. Проверка
```bash
curl http://localhost:8000/health
```
С другого компьютера: `curl http://<IP-сервера>:8000/health` → `healthy`.

---

# ЧАСТЬ B. КЛИЕНТ (Astra Linux)

На клиент ставится **только UI**. Модели и Docker не нужны.

## B1. Код клиента
```bash
sudo apt-get install -y git
git clone -b deploy https://github.com/gromli66/pid-pipeline.git ~/pid_client
cd ~/pid_client
```
(используются `ui/ app/ modules/ configs/ requirements/`).

## B2. Python 3.11 и зависимости

**Astra с интернетом:**
```bash
sudo apt-get install -y python3-venv python3-pip libgl1 libegl1 libxkbcommon0 libdbus-1-3 libnss3
python3 -m venv .venv_ui && source .venv_ui/bin/activate
pip install -r requirements/ui.txt
```
> На свежей Astra 1.8 `apt` по умолчанию видит только установочный DVD, где нет
> `python3-venv`/`python3-pip`. Подключи онлайн-репозитории и закомментируй cdrom:
> создай `/etc/apt/sources.list.d/astra-online.list` со строками
> `deb https://download.astralinux.ru/astra/stable/1.8_x86-64/repository-main/ 1.8_x86-64 main contrib non-free`
> и `.../repository-extended/ ...`, закомментируй `deb cdrom:` в `/etc/apt/sources.list`,
> затем `sudo apt-get update`. Пакет называется `libgl1` (не `libgl1-mesa-glx`).
> Отдельный `PySide6-WebEngine` ставить не нужно — WebEngine входит в `PySide6_Addons`.

**Astra в закрытом контуре (без интернета)** — офлайн-колёса:
```bash
# на машине с интернетом (Python 3.11 / x86_64):
pip download -r requirements/ui.txt --python-version 311 --only-binary=:all: \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 -d wheels_linux
scp -r wheels_linux administrator@<IP-клиента>:~/
# на клиенте:
python3 -m pip install --user --break-system-packages --no-index --find-links ~/wheels_linux -r requirements/ui.txt
```
> На закрытой Astra нет pip/venv из коробки — ставятся с установочного DVD (`sudo apt-cdrom add`) или внутреннего зеркала. Установку велась с выключенной ЗПС; на проде с включённым ужесточением проверить отдельно.

Готовый скрипт установки клиента — `deploy_ready/setup_astra_client.sh`.

## B3. Запуск

Запускать на **рабочем столе Astra** (не по SSH — нужен экран).

**Способ 1 — готовый скрипт (рекомендуется).** Один раз вписать IP сервера, дальше просто запускать:
```bash
nano ~/pid_client/deploy_ready/run_ui_client.sh   # заменить REPLACE_WITH_SERVER_IP на IP сервера
chmod +x ~/pid_client/deploy_ready/run_ui_client.sh
~/pid_client/deploy_ready/run_ui_client.sh        # запуск клиента
```
Скрипт сам переходит в корень репо, активирует `.venv_ui`, выставляет `PID_API_URL`/`PID_GL_BACKEND` и стартует UI. Для повторных запусков достаточно последней строки (или двойной клик в файловом менеджере → «Запустить»).

**Способ 2 — вручную:**
```bash
cd ~/pid_client && source .venv_ui/bin/activate
export PID_API_URL=http://<IP-сервера>:8000
export PID_GL_BACKEND=software
python3 -m ui.main
```

## B4. Работа
- Список диаграмм слева загрузился → клиент подключён к серверу.
- Загрузить схему → детекция → вкладка **CVAT** (логин `admin` / пароль из A7) → разметка → **Сохранить**.
- Несколько клиентов работают с одним сервером одновременно, видят общие данные.

---

# Примечания и эксплуатация

- **CPU медленный на тяжёлых этапах.** SAM2-контуры и OCR на CPU превышают дефолтные таймауты Celery (`worker/tasks/contours.py`, `worker/tasks/ocr.py` — `time_limit`/`soft_time_limit`). Поднимай их под CPU или ставь GPU под эти этапы. Это зона тюнинга.
- **CSRF при сохранении в CVAT.** Если сохранение разметки по сети не проходит — раскомментируй `CSRF_TRUSTED_ORIGINS` в `docker-compose.override.yml` (= `http://<IP-сервера>:8080`) и `docker compose up -d cvat_server`.
- **Секреты.** Реальные пароли/токен — только в `.env` (в `.gitignore`), в репозиторий не коммитятся. В git — шаблон `deploy_ready/.env.server.template`.
- **Бэкап:** `docker exec pid_postgres pg_dump -U pid_user pid_pipeline > backup.sql` + `rsync ./storage`. Подробнее — `docs/DEPLOYMENT.md` §11.
- **Закрытый контур (офлайн):** образы переносить `docker save/load`, модели и колёса — файлами. Скелет упаковки — `deploy_ready/offline_package.sh`.
- **Остановить/удалить тестовый сервер:** `docker compose down` (данные в томах остаются) или удалить ВМ в панели провайдера.
