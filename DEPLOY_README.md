# Развёртывание P&ID Pipeline (Linux-сервер + Astra/Windows-клиент)

Проверено на боевом прогоне: облачный Linux-сервер + клиент на Astra Linux, несколько клиентов одновременно.

**Архитектура:** один **сервер** (Docker-стек: PostgreSQL, Redis, FastAPI `api`, CVAT, воркеры) + **клиенты** (десктоп-UI на Astra Linux или Windows), подключаются по сети. Все данные (схемы, артефакты, БД) — **на сервере**; клиент тонкий, тянет/отдаёт всё по HTTP.

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

Клиент раздаётся **готовым бинарником** — Python, Qt и зависимости уже внутри.
Ставить на клиент Python/venv/pip **не нужно**. Как собрать клиент —
см. `deploy_ready/BUILD_CLIENT.md`; собранные архивы кладутся в `dist/release/`.

## B1. Установка и запуск
1. Получить архив (ссылка из облака) и распаковать:
   ```bash
   tar -xzf PID-Client_astra_2026-07-02.tar.gz
   cd PID-Client
   ```
2. Вписать IP сервера в `client.cfg` (строка `PID_API_URL`):
   ```bash
   nano client.cfg      # PID_API_URL=http://<IP-сервера>:8000
   ```
3. Запустить на рабочем столе Astra (не по SSH — нужен экран):
   ```bash
   ./run_standalone.sh
   ```
Бинарник собран под glibc 2.28 → работает на Astra **1.7 и 1.8** (x86_64).
Памятка пользователю и список X/GL библиотек для «голой» системы —
`deploy_ready/CLIENT_LINUX_README.md`.

## B2. Обновление клиента
Прислать новый архив (новая дата в имени) → пользователь распаковывает поверх,
сохранив свой `client.cfg`, и запускает. Версия сборки видна в заголовке окна.

## B3. Работа
- Список диаграмм слева загрузился → клиент подключён к серверу.
- Загрузить схему → детекция → вкладка **CVAT** (логин `admin` / пароль из A7) → разметка → **Сохранить**.
- Несколько клиентов работают с одним сервером одновременно, видят общие данные.

# ЧАСТЬ C. КЛИЕНТ (Windows)

Тоже **готовый бинарник** (`PID-Client.exe`) — ставить Python на машину пользователя не нужно.

## C1. Установка и запуск
1. Получить `PID-Client_windows_2026-07-02.zip` (ссылка из облака) и распаковать.
2. В `PID-Client\client.cfg` вписать IP сервера (`PID_API_URL`).
3. Запустить `PID-Client.exe` (двойной клик).

## C2. Обновление клиента
Прислать новый zip → распаковать поверх, сохранив `client.cfg`. Версия — в заголовке окна.

## C3. Работа
То же, что в B3: список диаграмм слева → клиент подключён к серверу; загрузка схемы
→ этапы → CVAT-разметка → сохранение. Клиенты Astra и Windows работают с одним
сервером одновременно.

---

# Примечания и эксплуатация

- **CPU медленный на тяжёлых этапах.** SAM2-контуры и OCR на CPU превышают дефолтные таймауты Celery (`worker/tasks/contours.py`, `worker/tasks/ocr.py` — `time_limit`/`soft_time_limit`). Поднимай их под CPU или ставь GPU под эти этапы. Это зона тюнинга.
- **CSRF при сохранении в CVAT.** Если сохранение разметки по сети не проходит — раскомментируй `CSRF_TRUSTED_ORIGINS` в `docker-compose.override.yml` (= `http://<IP-сервера>:8080`) и `docker compose up -d cvat_server`.
- **Секреты.** Реальные пароли/токен — только в `.env` (в `.gitignore`), в репозиторий не коммитятся. В git — шаблон `deploy_ready/.env.server.template`.
- **Бэкап:** `docker exec pid_postgres pg_dump -U pid_user pid_pipeline > backup.sql` + `rsync ./storage`. Подробнее — `docs/DEPLOYMENT.md` §11.
- **Закрытый контур (офлайн):** образы переносить `docker save/load`, модели и колёса — файлами. Скелет упаковки — `deploy_ready/offline_package.sh`.
- **Остановить/удалить тестовый сервер:** `docker compose down` (данные в томах остаются) или удалить ВМ в панели провайдера.
- **OCR и контуры (после обновления `deploy`).** OCR по умолчанию **выключен** (`configs/projects/thermohydraulics/thermohydraulics.yaml` → `ocr.enabled: false`): этап пропускается, бусины авто-проматываются. Включить — `true` + `docker compose restart api worker_ocr`. Контуры SAM2 **не считаются автоматически** — распознавание запускается во вкладке контуров кнопками «Распознать выбранные» (Shift+ЛКМ по узлам) / «Распознать все»; на CPU тяжёлый этап, удобнее точечно.
