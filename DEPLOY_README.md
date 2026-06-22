# Развёртывание P&ID Pipeline — сервер и клиенты (пошагово)

Эта инструкция проверена на боевом прогоне (облачный сервер + клиенты Windows и Astra Linux).

**Архитектура:** один **сервер** (Docker-стек: PostgreSQL, Redis, FastAPI `api`, CVAT, воркеры) + любое число **клиентов** (десктоп-UI на Astra Linux / Windows), которые подключаются к серверу по сети. Все данные (схемы, артефакты, БД) хранятся **на сервере**; клиенты — тонкие, тянут/отдают всё по HTTP.

Ветка `deploy` рассчитана на **CPU-сервер** (GPU в `docker-compose.yml` отключён). Если сервер с NVIDIA GPU — раскомментируй `deploy.resources...devices` в `docker-compose.yml` и поставь `PID_DEVICE=auto`.

---

# ЧАСТЬ A. СЕРВЕР

## A1. Сервер
- OS: **Ubuntu 22.04/24.04** (Docker ставится просто). RAM **16–20 ГБ**, диск **80–120 ГБ**, CPU (GPU не нужен), **публичный IP**.
- В облаке: в группе безопасности/файрволе открыть входящие **TCP 22, 8000, 8080**.

## A2. Установить Docker (на сервере по SSH)
```bash
ssh root@<IP-сервера>
curl -fsSL https://get.docker.com | sudo sh
docker --version && docker compose version
```

## A3. Получить код (ветка deploy) и модели
```bash
git clone -b deploy https://github.com/<user>/pid_pipeline.git ~/pid
cd ~/pid
```
**Модели не в git** (~3 ГБ) — перенести отдельно с машины, где они есть:
```bash
# с локальной машины:
scp -r <путь>/models/* root@<IP-сервера>:~/pid/models/
```
Проверка: `ls ~/pid/models/yolo/ensemble_v1/tile640/best.pt` (должен быть ~50 МБ).

## A4. Настроить .env
```bash
cp deploy_ready/.env.server.template .env
nano .env
```
Заполнить (адрес сервера — в трёх местах одинаковый):
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
`docker-compose.override.yml` уже в ветке (отдаёт `CVAT_BROWSER_URL` сервисам `api` и `worker`).

## A5. Фаервол на сервере (если нет облачного)
```bash
sudo ufw allow 22/tcp && sudo ufw allow 8000/tcp && sudo ufw allow 8080/tcp && sudo ufw enable
```

## A6. Собрать и поднять стек
```bash
docker compose build          # тянет torch/CUDA-слои и зависимости — не быстро, разово
docker compose up -d
docker compose ps             # все контейнеры running/healthy
docker exec -it pid_api alembic upgrade head
```

## A7. Создать суперпользователя и токен CVAT
```bash
docker exec -e DJANGO_SUPERUSER_PASSWORD=<пароль_cvat> cvat_server \
  python3 /home/django/manage.py createsuperuser --username admin --email a@a.com --noinput
docker exec cvat_server python3 /home/django/manage.py drf_create_token admin
```
Вторая команда выведет `Generated token <КЛЮЧ>...`. Вписать ключ в `.env` и перезапустить:
```bash
sed -i 's/^CVAT_TOKEN=.*/CVAT_TOKEN=<КЛЮЧ>/' .env
docker compose up -d api worker
docker exec pid_worker printenv CVAT_TOKEN     # должен показать новый ключ
```
> Токен берётся из `.env` (`${CVAT_TOKEN}`) и `api`, и `worker`. На свежем CVAT он всегда новый.

## A8. Проверка сервера
```bash
curl http://localhost:8000/health
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Token <КЛЮЧ>" -H "Host: <IP-сервера>" http://localhost:8080/api/projects   # ждём 200
```
С другого компьютера: `curl http://<IP-сервера>:8000/health` → должен ответить `healthy`.

---

# ЧАСТЬ B. КЛИЕНТ (Astra Linux / Windows)

На клиент ставится **только UI**. Модели и Docker не нужны.

## B1. Получить код клиента
```bash
git clone -b deploy https://github.com/<user>/pid_pipeline.git ~/pid_client
cd ~/pid_client
```
(нужны папки `ui/ app/ modules/ configs/ requirements/` — они в репозитории).

## B2. Python 3.11 и зависимости

**Windows:**
```bat
py -3.11 -m venv .venv311
.venv311\Scripts\activate
pip install -r requirements\ui.txt
```

**Astra Linux (с интернетом):**
```bash
sudo apt-get install -y python3-venv python3-pip libgl1-mesa-glx libegl1 libxkbcommon0 libdbus-1-3 libnss3
python3 -m venv .venv_ui && source .venv_ui/bin/activate
pip install -r requirements/ui.txt
```

**Astra Linux (закрытый контур, без интернета)** — офлайн-колёса:
```bash
# на машине с интернетом (Python 3.11 / x86_64):
pip download -r requirements/ui.txt --python-version 311 --only-binary=:all: \
  --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 -d wheels_linux
scp -r wheels_linux administrator@<IP-клиента>:~/
# на клиенте Astra:
python3 -m pip install --user --break-system-packages --no-index --find-links ~/wheels_linux -r requirements/ui.txt
```
> На закрытой Astra нет pip/venv из коробки — ставятся с установочного DVD (`sudo apt-cdrom add`) или внутреннего зеркала. Подробности — в `MIGRATION_PLAN.md` (Фаза 2).

## B3. Запуск клиента
**Windows:**
```bat
set PID_API_URL=http://<IP-сервера>:8000
python -m ui.main
```
**Astra (на рабочем столе, не по SSH):**
```bash
export PID_API_URL=http://<IP-сервера>:8000
export PID_GL_BACKEND=software
python3 -m ui.main
```
Удобно завернуть в ярлык/скрипт с этими переменными.

## B4. Работа
- Список диаграмм слева загрузился → клиент подключён к серверу.
- Загрузить схему → детекция → вкладка **CVAT** (логин: `admin` / пароль из A7) → разметка → **Сохранить**.
- Несколько клиентов работают с одним сервером одновременно, видят общие данные.

---

# Примечания и эксплуатация

- **CPU медленный на тяжёлых этапах.** SAM2-контуры и OCR на CPU превышают дефолтные таймауты Celery (`worker/tasks/contours.py`, `worker/tasks/ocr.py` — `time_limit`/`soft_time_limit`). На ветке `deploy` поднимай их (или ставь GPU под эти этапы). Это твоя зона тюнинга.
- **CSRF при сохранении в CVAT.** Если сохранение разметки по сети не проходит — раскомментируй `CSRF_TRUSTED_ORIGINS` в `docker-compose.override.yml` (= `http://<IP-сервера>:8080`) и `docker compose up -d cvat_server`.
- **Секреты.** Реальные пароли/токен — только в `.env` (он в `.gitignore`), в репозиторий не коммитятся. В git — `.env.server.template` с плейсхолдерами.
- **Бэкап (боевой сервер):** дамп БД `docker exec pid_postgres pg_dump -U pid_user pid_pipeline > backup.sql` + `rsync ./storage`. Подробнее — `docs/DEPLOYMENT.md` §11.
- **Закрытый контур (без интернета):** образы переносить `docker save/load`, модели и колёса — файлами. См. `MIGRATION_PLAN.md` Фаза 5.
- **Остановить/удалить тестовый сервер:** `docker compose down` (данные в томах остаются) или удалить ВМ в панели (почасовая оплата).
