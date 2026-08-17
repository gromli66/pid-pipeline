# Сквозной чек-лист: `napravlenie` (пункт ВН1б)

**Аудитория:** DEV / OPS
**Версия:** 2.0
**Обновлено:** 2026-08-18
**Связанные документы:** [NAPRAVLENIE.md](NAPRAVLENIE.md), [DEPLOYMENT.md](DEPLOYMENT.md),
[AUTO_LAYOUT_E2E_TESTPLAN.md](AUTO_LAYOUT_E2E_TESTPLAN.md)

> **Происхождение.** Переехал из `docs/ADR/napravlenie_e2e_checklist.md` (пункт **ВН1а**
> дороги рефакторинга, 2026-08-18). Тогда же сверен с кодом: проверки §3.3–§3.4 были
> написаны под нереализованную модель `type: "direction"` и давали бы ложный отрицательный
> результат; исправлены по факту кода. Что уже замерено на артефактах и переспрашивать
> не нужно — §0.

Проверка цепочки **детекция → классификация направления → сегментация → граф** на реальной
схеме. Запускать в поднятой среде (БД, celery, worker) — пункт **0.12** дороги.

---

## 0. Что уже известно ДО прогона (замер 2026-08-18, `MEASUREMENTS.md §28`)

| факт | число | вывод |
|---|---|---|
| схем в `storage/` с боксами `napravlenie` | 16 из 17 | материал для прогона есть |
| боксов `napravlenie` в `coco_validated.json` | 722 | — |
| из них с `attributes.direction` | 721 (99.9 %) | **§3.1 уже выполняется** — классификатор отработал на реальных данных |
| direction-узлов в `graph/graph.json` | 721 | боксы доезжают до графа без потерь |
| из них `pass_through: true` (степень ≥ 2) | 63 (8.7 %) | норма |
| из них `degree == 1` | 525 | **норма** — стрелка на терминальном участке (решение Максима 2026-08-18) |
| из них `degree == 0` | 133 | ⛔ **дефект и главная цель прогона**: труба там должна быть |
| рёбер с `direction` | **0** | так и останется: граф ненаправленный, поле нужно только для треугольника у степени 1 (`NAPRAVLENIE.md §5.3`) |

Поэтому цель прогона — **не** «проставилось ли направление» (проставилось) и не разбор
терминальных стрелок (степень 1 законна), а **боксы со степенью 0**: цела ли труба под боксом
и почему она к нему не подключилась (§4.2–§4.3).

⚠ Требование к будущей починке (ВН1в, решение Максима 2026-08-18): трубу восстанавливать
только там, где это делается со **100 % уверенностью**; где уверенности нет — не добавлять
вовсе, оставить оператору. Догаданная труба уезжает в FXML как настоящая топология.

---

## 1. Предпосылки (один раз)

- [ ] **Веса классификатора** на месте: `models/direction/best.pt` (в контейнере — `/models`).
      Источник: `…/napravlenie_razmetka/result_direction/experiments/dir_v1/weights/best.pt`.
- [ ] **Конфиг** `configs/projects/thermohydraulics/thermohydraulics.yaml` → секция
      `direction_classification`: `enabled: true`, `weights: "/models/direction/best.pt"`,
      `classes: [napravlenie, nasos, rashodomernaya_shaiba]`.
      ⚠ Поле `device` в этой секции **не читается**: устройство берётся из `PID_DEVICE`
      (`worker/utils/device.py::resolve_device`). На CPU-стенде выставить `PID_DEVICE=cpu`.
- [ ] **Миграция БД** применена (стадия `direction_classification`):
      ```
      docker compose run --rm api alembic upgrade head   # ревизия 0006_add_direction_stagetype
      ```
- [ ] **Контейнеры пересобраны** под текущий код: `docker compose build worker api` →
      `docker compose up -d worker api`.
- [ ] **Юнит-тесты графа** зелёные (чистый Python, без GPU/БД/torch):
      ```
      python -X utf8 -m pytest tests/test_direction_nodes.py -q     # ожидаем 13 passed
      ```
      Файл запускается и напрямую (`python tests/test_direction_nodes.py`), но 6 из 13 тестов
      бьют в `apply_direction_rules`/`stitch_collinear_stubs` — функции, которых боевая сборка
      не вызывает (см. `NAPRAVLENIE.md §5.3`). Их зелёный цвет прогон не подтверждает.

---

## 2. Выбрать схему

- [ ] Найти диаграмму с боксами `napravlenie` (CVAT `category_id = 41`):
  ```python
  import json, glob
  for f in glob.glob("storage/diagrams/*/detection/coco_validated.json"):
      d = json.load(open(f, encoding="utf-8"))
      cats = {c["id"]: c["name"] for c in d.get("categories", [])}
      n = sum(1 for a in d["annotations"] if cats.get(a["category_id"]) == "napravlenie")
      if n:
          print(n, f)
  ```
  Зафиксировать `DIAGRAM_UID`. Хорошо, если на схеме есть перпендикуляр, пересекающий бокс.

---

## 3. Прогон цепочки

Сценарий — обычный поток после валидации детекции: `coco_validated.json` появляется после
CVAT-валидации, запуск сегментации тянет цепочку `направление → сегментация → … → граф`.

- [ ] Статус `validated_bbox`, есть `storage/diagrams/<UID>/detection/coco_validated.json`.
- [ ] Запуск (эндпоинт — под префиксом `/api/segmentation`, а не `/api/diagrams`):
      ```
      curl -X POST http://localhost:8000/api/segmentation/<UID>/segment
      ```
      В цепочке: `task_classify_direction` → `task_segment_pipes` → … → `task_build_graph`.
- [ ] Логи:
      ```
      docker compose logs -f worker | grep -Ei "direction|segment|graph"
      ```
      Ожидаем строку `[<UID>] Direction classification done: N/M classified, … low-conf, … failed`.

> ⚠ Без весов шаг логирует `weights_missing` и **пропускается** — цепочка идёт дальше без
> направления (специально, чтобы не блокировать пайплайн). Для приёмки веса обязательны.

---

## 4. Проверки результата

### 4.1 Направление записано в COCO
```python
import json
d = json.load(open(f"storage/diagrams/{UID}/detection/coco_validated.json", encoding="utf-8"))
cats = {c["id"]: c["name"] for c in d["categories"]}
napr = [a for a in d["annotations"] if cats.get(a["category_id"]) == "napravlenie"]
print(len(napr), "боксов napravlenie")
for a in napr[:5]:
    at = a.get("attributes", {})
    print(at.get("direction"), at.get("direction_confidence"), at.get("direction_low_confidence"))
```
- [ ] `direction ∈ {up, right, down, left}` и `direction_confidence` у каждого.

### 4.2 Труба под боксом сохранилась
```python
import cv2, json
nm = cv2.imread(f"storage/diagrams/{UID}/segmentation/node_mask.png", 0)
pm = cv2.imread(f"storage/diagrams/{UID}/segmentation/pipe_mask.png", 0)
d = json.load(open(f"storage/diagrams/{UID}/detection/coco_validated.json", encoding="utf-8"))
cats = {c["id"]: c["name"] for c in d["categories"]}
for a in [x for x in d["annotations"] if cats.get(x["category_id"]) == "napravlenie"][:5]:
    x, y, w, h = [int(v) for v in a["bbox"]]
    print(f"bbox=({x},{y},{w},{h})",
          f"node%={(nm[y:y+h, x:x+w] > 0).mean():.3f}",
          f"pipe%={(pm[y:y+h, x:x+w] > 0).mean():.3f}")
```
- [ ] `node%` ≈ 0 (бокс не стал узлом), `pipe%` > 0 (труба под боксом на месте).

### 4.3 Граф: direction-узлы и проход насквозь
⚠ Узел **остаётся `type: "equipment"`** — искать по флагу `direction_node`, а не по типу.
```python
import json
g = json.load(open(f"storage/diagrams/{UID}/graph/graph.json", encoding="utf-8"))
dirn = [n for n in g["nodes"] if n.get("direction_node")]
print("direction-узлов:", len(dirn))
for n in dirn[:5]:
    print(n["id"], "axis=", n.get("flow_axis"), "dir=", n.get("flow_direction"),
          "pass_through=", n.get("pass_through"), "degree=", n["degree"])
from collections import Counter
print(Counter(n["degree"] for n in dirn))
```
- [ ] Число direction-узлов совпадает с числом боксов с `direction` в COCO.
- [ ] У проходного бокса `degree == 2`, `pass_through == True`; терминальный `degree == 1` —
      законная норма, разбору не подлежит.
- [ ] ⛔ **Ключевая проверка:** взять все боксы с `degree == 0` (в корпусе их 133 на 16 схем)
      и посмотреть на схеме (`graph_overlay.png` или оверлей графа), где именно потерялась
      труба: она не распозналась на `pipe_mask`, распозналась но не дотянулась до грани бокса,
      или контакт не попал в `contact_map`. Ответ на этот вопрос — вход ВН1в.
- [ ] Для каждого разобранного случая записать, можно ли восстановить трубу **однозначно**
      (есть два конца на одной оси в пределах бокса) или нет. Случаи «нельзя однозначно»
      остаются оператору — это решение Максима, а не недоработка прогона.

> `flow_direction` совпадает с `direction` из COCO для того же бокса (`ann_id`).
> Рёбер с полем `direction` **не будет** — граф ненаправленный, поле работает только для
> треугольника у ручных узлов степени 1 (`NAPRAVLENIE.md §5.3`). Это не дефект прогона.

### 4.4 Перпендикуляр не слился
```python
did = dirn[0]["id"]
inc = [e for e in g["links"] if did in (e["source"], e["target"])]
print(f"рёбер у {did}:", len(inc), "(ожидаем 0–2 — только осевые)")
```
- [ ] Перпендикулярная труба — отдельное ребро, к direction-узлу не инцидентна
      (сквозной перпендикуляр идёт мимо бокса одним ребром, перпендикуляр-поворот получает
      собственный `connector` с id `dirconn_*`).

---

## 5. Критерии приёмки ВН1б

- [ ] У всех `napravlenie` проставлен `direction` (+ confidence) в `coco_validated.json`.
- [ ] `node_mask` не содержит боксы `napravlenie`; труба под ними цела в `pipe_mask`.
- [ ] Боксы, через которые труба идёт насквозь, дают `degree == 2` и `pass_through == True`;
      `degree == 1` принимается как норма без разбора.
- [ ] Все боксы с `degree == 0` разобраны поштучно и разделены на две кучи: «трубу можно
      восстановить однозначно» и «нельзя — оставляем оператору».
- [ ] Перпендикулярные трубы не подключены к direction-узлам.
- [ ] `strelka` ведёт себя как раньше (не затронута).

Критерий «два ребра с `direction` у проходного бокса» из версии 1.0 **снят**: в боевой сборке
направление на рёбра не штампуется (`NAPRAVLENIE.md §5.3`). Возвращать его — решение Максима
вместе с ВН1в.

---

## 6. Если что-то не так

- **Нет `direction` в COCO** → логи `worker`: `weights_missing` (нет файла весов), `disabled`
  (выключено в конфиге), `no_classes`, `empty` (нет боксов на схеме).
- **Бокс всё ещё вырезается из трубы** → проверить ОБА генератора масок
  (`worker/tasks/segmentation.py::generate_node_mask` и
  `app/api/validation.py::_generate_node_mask_from_coco`) и что сегментация перегенерилась
  после ручной правки детекции.
- **Нет direction-узлов в графе** → `coco_validated.json` должен содержать
  `attributes.direction` ДО `task_build_graph`; в `builder.build` должен отработать
  `annotate_direction_nodes` (`modules/graph/core/builder.py:374-407`).
- **Труба под боксом рвётся геометрически** → по решению п.4 плана дотянуть по точным
  границам из «Контур» (SAM2, `worker/tasks/contours.py`) — это уже область ВН1в.
