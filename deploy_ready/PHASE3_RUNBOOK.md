# Фаза 3 — полноценный прогон на облачном сервере (с моделями, воркеры на CPU)

**Цель:** развернуть систему на реальном сервере, подключиться **с другого компьютера по сети** и прогнать схему через весь рабочий цикл (загрузка → детекция → CVAT-валидация → сегментация → … ) — как в бою, на CPU.

**Что заливаем:** всю папку `C:\project\pid_pipeline_deploy` (~4.5 ГБ: код + `models/` 3.2 ГБ + `docker-compose.override.yml` + GPU уже отключён в `docker-compose.yml`, Д5).

---

## Шаг 1. Арендовать сервер

| Параметр | Значение |
|----------|----------|
| OS | **Ubuntu 22.04** (рекомендую — Docker ставится в 2 команды) |
| RAM | 16–20 ГБ |
| Диск | **80–120 ГБ** (НЕ 40 — образы воркеров CUDA+torch ~10–12 ГБ каждый + CVAT + модели) |
| CPU | любой, GPU не нужен |
| Сеть | публичный IP |
| Оплата | почасовая, после теста удалить |

> Почему Ubuntu, а не Astra, для **сервера**: ОС сервера для миграции не важна (это просто Docker-хост), а на Astra мы видели проблему с репозиториями → Docker поставить сложно. Клиент мы уже проверили на Astra (Фаза 2). Если боевой сервер всё же Astra — установку Docker там решим отдельно (внутреннее зеркало).

Запиши: **IP сервера**, логин (обычно `root` или `ubuntu`), пароль/SSH-ключ.

---

## Шаг 2. Установить Docker (на сервере, по SSH)

```bash
ssh <user>@<IP-сервера>
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER     # чтобы docker без sudo (переподключись после)
docker --version && docker compose version
```

---

## Шаг 3. Залить проект с моделями (с Windows)

```bat
scp -r C:\project\pid_pipeline_deploy <user>@<IP-сервера>:~/pid
```
~4.5 ГБ — зависит от скорости твоего интернета на отдачу. (Альтернатива: код через git, а `models/` отдельно — но scp всей папки проще.)

---

## Шаг 4. Настроить .env (на сервере)

```bash
cd ~/pid
nano .env
```
Поменять три адреса на **IP сервера** и пароли:
```
SERVER_HOST=<IP-сервера>
CVAT_HOST=<IP-сервера>
CVAT_BROWSER_URL=http://<IP-сервера>:8080
DB_PASSWORD=<надёжный>
CVAT_SUPERUSER_PASSWORD=<надёжный>
PID_DEVICE=cpu
YOLO_DEVICE=cpu
CVAT_TOKEN=52db370470e5ed7ab05fecae5cc716859271c3d3
```
(GPU-блоки в `docker-compose.yml` уже закомментированы — Д5.)

---

## Шаг 5. Фаервол

Открыть только нужные порты, лучше только на свой IP:
```bash
sudo ufw allow 8000/tcp
sudo ufw allow 8080/tcp
sudo ufw allow 22/tcp
sudo ufw enable
```
(Если провайдер имеет свой firewall в панели — продублируй там; 5433/6380 НЕ открывать.)

---

## Шаг 6. Собрать и поднять стек (полный, воркеры на CPU)

```bash
cd ~/pid
docker compose build           # соберёт api + worker + worker_ocr (тянет torch/CUDA — несколько ГБ, не быстро)
docker compose up -d           # поднимет всё: БД, redis, api, CVAT, worker, worker_ocr
docker compose ps              # все должны быть running/healthy
```
> Сборка образов воркеров скачивает torch и базовые CUDA-слои с интернета — на сервере это разово, но не мгновенно. Образы CVAT тянутся с Docker Hub.

---

## Шаг 7. БД + CVAT

```bash
docker exec -it pid_api alembic upgrade head
docker exec -it cvat_server bash -ic "python3 ~/manage.py createsuperuser"   # admin / свой пароль
```
Токен CVAT: в `.env` уже стоит дефолтный; если CVAT свежий и он не подойдёт — получи новый (через CVAT UI вход) и впиши `CVAT_TOKEN=...`, затем `docker compose up -d api worker`.

Проверка:
```bash
curl http://localhost:8000/health
curl http://localhost:8080/api/server/about
docker logs pid_worker --tail 20      # воркер подключился к очередям
```

---

## Шаг 8. Подключить клиента и прогнать схему (главное)

С клиента (Astra-VM или твой Windows) — UI на адрес сервера:
- Windows: `set PID_API_URL=http://<IP-сервера>:8000` → `python -m ui.main`
- Astra: `PID_API_URL=http://<IP-сервера>:8000 PID_GL_BACKEND=software python3 -m ui.main`

Прогнать **полный цикл**:
1. Список диаграмм грузится (связь по сети ✅).
2. Загрузить схему → пойдёт **детекция** (на CPU ~1 мин).
3. Открыть вкладку **CVAT** → разметить bbox → **Сохранить** (проверка Д4/CSRF по реальной сети — если не сохраняется, раскомментировать `CSRF_TRUSTED_ORIGINS` в `docker-compose.override.yml` на `http://<IP>:8080` и `docker compose up -d cvat_server`).
4. Вести дальше: сегментация → скелет → перекрёстки → контуры (SAM2) → граф → OCR → FXML. На CPU долго (~20 мин суммарно) — это и есть честная проверка боевого CPU-сервера.

**✅ Чекпоинт 3:** удалённый клиент по сети проходит весь цикл, разметка сохраняется, авто-этапы на CPU отрабатывают.

---

## Шаг 9. Замеры и завершение

```bash
docker exec pid_postgres psql -U pid_user -d pid_pipeline -c "SELECT stage_type,status,ROUND(duration_seconds::numeric,1) sec FROM processing_stages ORDER BY started_at DESC LIMIT 15;"
docker stats --no-stream     # пиковая RAM — сверить с лимитом боевого сервера
```
После теста — **удалить сервер** (почасовая оплата). Данные не нужны: это репетиция.

---

## Чеклист

- [ ] Сервер ≥80 ГБ диск, 16–20 ГБ RAM, публичный IP
- [ ] Docker установлен
- [ ] Проект+модели залиты (scp `~/pid`)
- [ ] `.env`: IP сервера в 3 местах, пароли, `PID_DEVICE=cpu`
- [ ] Фаервол: 8000/8080/22
- [ ] `docker compose build && up`, миграции, суперюзер CVAT
- [ ] Клиент по сети: список + детекция + CVAT-сохранение + авто-этапы
- [ ] Замерить время/RAM → удалить сервер

---

## Что держать в уме (из прошлых фаз)

- Сборка/скачивание образов на сервере займёт время (torch, CUDA-слои, CVAT с Docker Hub; из РФ Docker Hub бывает медленным).
- CSRF на сохранение по сети может потребовать `CSRF_TRUSTED_ORIGINS` (фикс готов в override).
- CPU-обработка медленная (~20 мин/схема) — нормально, это и проверяем.
- Если боевой сервер закрытый (без интернета) — образы и модели переносить офлайн (Фаза 5); метод уже отработан на клиенте.
