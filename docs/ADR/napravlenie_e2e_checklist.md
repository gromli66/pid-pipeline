# Сквозной чек-лист: интеграция `napravlenie` (план #6)

Проверка всей цепочки на реальной схеме: **детекция → классификация направления →
сегментация → граф**. Цель — убедиться, что:

1. направление проставлено в `coco_validated.json`;
2. труба под боксом `napravlenie` сохранилась (не вырезана из `pipe_mask`/`node_mask`);
3. ребро прошло сквозь бокс (есть прозрачный `direction`-узел);
4. перпендикуляр не слился с осевой трубой.

Запускать в рабочей среде (GPU, БД, celery) — в песочнице это не воспроизводится.

---

## 0. Предпосылки (один раз)

- [ ] **Положить веса классификатора.** Скопировать обученные веса в каталог
  моделей, который монтируется в контейнер как `/models`:
  ```
  models/direction/best.pt
  ```
  (источник: `…/napravlenie_razmetka/result_direction/experiments/dir_v1/weights/best.pt`)

- [ ] **Проверить конфиг** `configs/projects/thermohydraulics/thermohydraulics.yaml`:
  ```yaml
  direction_classification:
    enabled: true
    weights: "/models/direction/best.pt"
    device: "cuda"
    img_size: 128
    pad_frac: 0.20
    confidence_threshold: 0.5
    classes:
      - napravlenie
  ```
  Путь `weights` должен указывать на файл внутри контейнера.

- [ ] **Применить миграцию БД** (новая стадия `direction_classification`):
  ```
  docker compose run --rm api alembic upgrade head
  # ожидаем ревизию 0006_add_direction_stagetype
  ```

- [ ] **Пересобрать/перезапустить** контейнеры, чтобы подхватился новый код
  (`worker`, `api`):
  ```
  docker compose build worker api
  docker compose up -d worker api
  ```

- [ ] **Прогнать юнит-тесты графа** (чистый Python, без GPU/БД/torch).
  Проще всего на хосте в активном venv (`tests/` не копируется в образ `api`):
  ```
  python tests/test_direction_nodes.py
  # ожидаем 5/5 passed
  ```
  Либо в контейнере, примонтировав репозиторий поверх /app:
  ```
  docker compose run --rm -v ${PWD}:/app api python tests/test_direction_nodes.py
  ```

- [ ] **(опц.) Smoke классификатора** на размеченных кропах — проверка точности
  и `model.names`:
  ```
  python smoke_direction.py        # из napravlenie_razmetka, пути ROOT/DATA вверху файла
  # ожидаем accuracy на val заметно > 0.8, model.names = {0:down,1:left,2:right,3:up}
  ```

---

## 1. Выбрать схему с `napravlenie`

- [ ] Найти диаграмму, где есть боксы `napravlenie` (CVAT `category_id = 41`):
  ```python
  import json, glob
  for f in glob.glob("storage/diagrams/*/detection/coco_validated.json"):
      d = json.load(open(f))
      n = sum(1 for a in d["annotations"] if a["category_id"] == 41)
      if n: print(n, f)
  ```
  Зафиксировать `DIAGRAM_UID` (UUID из пути). Хорошо, если на схеме есть и
  перпендикуляр, пересекающий бокс.

---

## 2. Прогон цепочки

Сценарий — как обычный поток после валидации детекции. `coco_validated.json`
появляется после CVAT-валидации; затем запуск сегментации тянет цепочку
`направление → сегментация → … → граф`.

- [ ] **Детекция** (если ещё не сделана) и валидация bbox в CVAT → статус
  `validated_bbox`, есть `storage/diagrams/<UID>/detection/coco_validated.json`.

- [ ] **Запуск сегментации** (он же триггерит классификацию направления первым
  шагом цепочки):
  ```
  curl -X POST http://localhost:8000/api/diagrams/<UID>/segment
  ```
  В цепочке: `task_classify_direction` → `task_segment_pipes` → … → `task_build_graph`.

- [ ] **Следить за логами**:
  ```
  docker compose logs -f worker | grep -Ei "direction|Direction|segment|graph"
  ```
  Ожидаем строку вида:
  `[<UID>] Direction classification done: N/M classified, ... low-conf, ... failed`

> ⚠️ Если веса не подложены — шаг логирует `weights_missing` и **пропускается**,
> цепочка идёт дальше без направления (специально, чтобы не блокировать пайплайн).
> Для теста #6 веса обязательны.

---

## 3. Проверки результата

### 3.1 Направление записано в COCO
- [ ] В `storage/diagrams/<UID>/detection/coco_validated.json` у аннотаций
  `napravlenie` (cat 41) появились атрибуты:
  ```python
  import json
  d = json.load(open(f"storage/diagrams/{UID}/detection/coco_validated.json"))
  napr = [a for a in d["annotations"] if a["category_id"] == 41]
  print(len(napr), "боксов napravlenie")
  for a in napr[:5]:
      print(a["attributes"].get("direction"), a["attributes"].get("direction_confidence"))
  ```
  Ожидаем `direction ∈ {up,right,down,left}` и `direction_confidence` у каждого.

### 3.2 Труба под боксом сохранилась
- [ ] `node_mask.png` **не содержит** боксы `napravlenie` (они не вырезаются):
  ```python
  import cv2, numpy as np, json
  nm = cv2.imread(f"storage/diagrams/{UID}/segmentation/node_mask.png", 0)
  pm = cv2.imread(f"storage/diagrams/{UID}/segmentation/pipe_mask.png", 0)
  d = json.load(open(f"storage/diagrams/{UID}/detection/coco_validated.json"))
  for a in [x for x in d["annotations"] if x["category_id"] == 41][:5]:
      x,y,w,h = [int(v) for v in a["bbox"]]
      node_in_box = (nm[y:y+h, x:x+w] > 0).mean()
      pipe_in_box = (pm[y:y+h, x:x+w] > 0).mean()
      print(f"bbox=({x},{y},{w},{h}) node%={node_in_box:.3f} pipe%={pipe_in_box:.3f}")
  ```
  Ожидаем: `node%` около 0 (бокс не стал узлом), `pipe%` > 0 (труба под боксом на месте).

### 3.3 Граф: труба прошла насквозь + direction-узел
- [ ] В `storage/diagrams/<UID>/graph/graph.json` есть узлы типа `direction`:
  ```python
  import json
  g = json.load(open(f"storage/diagrams/{UID}/graph/graph.json"))
  dirn = [n for n in g["nodes"] if n.get("type") == "direction"]
  print("direction-узлов:", len(dirn))
  for n in dirn[:5]:
      print(n["id"], "axis=", n.get("flow_axis"), "dir=", n.get("flow_direction"),
            "pass_through=", n.get("pass_through"), "degree=", n["degree"])
  # рёбра с направлением
  ed = [e for e in g["links"] if e.get("direction")]
  print("рёбер с direction:", len(ed))
  ```
  Ожидаем:
  - проходной бокс → `pass_through=True`, `degree=2`, два ребра с `direction`;
  - терминальный бокс (труба кончается на стрелке) → `pass_through=False`, `degree=1`;
  - `flow_direction` совпадает с `direction` из COCO для того же бокса.

### 3.4 Перпендикуляр не слился
- [ ] Для бокса, через который проходит и осевая, и перпендикулярная труба:
  перпендикулярная труба остаётся **отдельным ребром** и **не** инцидентна
  `direction`-узлу (её `source`/`target` — другие узлы, не `dirnode_*`).
  Проверить визуально на оверлее графа или по `graph.json`:
  ```python
  did = dirn[0]["id"]
  inc = [e for e in g["links"] if did in (e["source"], e["target"])]
  print(f"рёбер у {did}:", len(inc), "(ожидаем 1 или 2 — только осевые)")
  ```

---

## 4. Критерии приёмки (#6)

- [ ] У всех `napravlenie` проставлен `direction` (+ confidence) в `coco_validated.json`.
- [ ] `node_mask` не содержит боксы `napravlenie`; труба под ними цела в `pipe_mask`.
- [ ] В графе есть `direction`-узлы; осевая труба проходит насквозь (degree 2 +
  два ребра с `direction`) либо корректно терминируется (degree 1).
- [ ] Перпендикулярные трубы не подключены к `direction`-узлам.
- [ ] `strelka` ведёт себя как раньше (не затронута).

---

## 5. Если что-то не так

- **Нет `direction` в COCO** → проверить логи `worker`: `weights_missing`
  (нет файла весов), `disabled` (выкл. в конфиге) или `empty` (нет боксов
  napravlenie на этой схеме).
- **Бокс всё ещё вырезается из трубы** → проверить, что правка внесена в ОБА
  генератора `node_mask` (`worker/tasks/segmentation.py::generate_node_mask` и
  `app/api/validation.py::_generate_node_mask_from_coco`) и что сегментация
  перегенерилась после ручной правки детекции.
- **Нет `direction`-узлов в графе** → убедиться, что `coco_validated.json`
  содержит `attributes.direction` ДО шага `task_build_graph`, и что граф
  собирался уже на новом коде (`builder.build` вызывает `insert_direction_nodes`).
- **Труба под боксом всё же рвётся геометрически** → согласно плану п.4,
  дотянуть по точным границам из «Контур» (SAM2, `worker/tasks/contours.py`).
