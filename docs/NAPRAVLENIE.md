# napravlenie — стрелка направления на трубе

**Аудитория:** DEV / ML
**Версия:** 2.0
**Обновлено:** 2026-08-18
**Связанные документы:** [NAPRAVLENIE_E2E_CHECKLIST.md](NAPRAVLENIE_E2E_CHECKLIST.md),
[MODULES.md](MODULES.md), [WORKER_TASKS.md](WORKER_TASKS.md), [DATA_FORMATS.md](DATA_FORMATS.md),
[CONFIG_REFERENCE.md](CONFIG_REFERENCE.md)

> **Происхождение.** Документ переехал из `docs/ADR/napravlenie_integration_plan.md`
> (пункт **ВН1а** дороги рефакторинга, 2026-08-18). Это не ADR, а хэндофф-план фичи,
> и лежал он не в той папке. §2 добавлен при переносе — это сверка каждого утверждения
> плана с кодом; §5 — что от плана отклонилось.

## 1. Задача

Класс `napravlenie` — стрелка направления, стоящая **на** трубе: труба проходит сквозь её
bbox. Для графа прохождение трубы сквозь бокс — правильное поведение, но исходно оно ломало
две подсистемы:

- **Сегментация** — `generate_node_mask` делала узлом всё, кроме `truba`/`annotation`,
  поэтому бокс `napravlenie` вырезался из `pipe_mask` и раздувался постобработкой →
  **труба под боксом пропадала**;
- **Граф** — скелет внутри раздутого bbox узла стирается перед трассировкой → ребро
  обрывалось у границы бокса вместо прохода насквозь.

## 2. Состояние на 2026-08-18 (сверено с кодом)

**Фича внедрена и работает на реальных данных.** Замеры — `MEASUREMENTS.md §28`.

| утверждение хэндофф-плана | что в коде сейчас | вердикт |
|---|---|---|
| `generate_node_mask` вырезает `napravlenie` → труба пропадает | `worker/tasks/segmentation.py:42-47,173-191` — `napravlenie` в списке исключённых | **устарело**: починено (`a1e0e14`, 2026-06-22) |
| правку надо внести и во второй генератор масок | `app/api/validation.py:203-209` — исключён там же | **сделано** |
| инференса классификатора нет | `modules/direction_classifier/classifier.py:74` `DirectionClassifier.predict/predict_batch` | **сделано** |
| направление — отдельный worker-шаг после детекции, пишет в `coco_validated.json` | `worker/tasks/direction.py:52-246`, цепочка в `app/api/segmentation.py:96-100` | **сделано** |
| `napravlenie` (class_id 40) не исключается из узлов графа | `modules/graph/core/nodes.py:71-72` — `EXCLUDED_CLASS_IDS = {34, 36, 38, 39}` | **сделано** |
| тип узла — новый `type: "direction"` | тип остался `equipment`, направление — флаг `direction_node` + `flow_axis`/`flow_direction`/`pass_through` (`modules/graph/core/export_json.py:105-113`) | **отклонение**, см. §5.1 |
| порты только по оси через `contact_map` | ось/перпендикуляр разруливаются на трассировке — `prepare_tracing_data` (`modules/graph/core/nodes.py:640-880`): осевой контакт к боксу, сквозной перпендикуляр — «телепорт» мимо бокса | **отклонение**, см. §5.2 |
| направление штампуется на инцидентные рёбра | код есть (`direction_nodes.py:467`), но его функция в боевой сборке не вызывается | **не работает**, см. §5.3 |
| FXML: тип `direction` → символ стрелки | `modules/graph_to_fxml.py:1173,1257-1262` — треугольник по `direction`; при отсутствии направления оно домысливается по геометрии (`:1296,1857-1858`) | **сделано иначе** (по флагу, не по типу) |
| миграция БД `0006_add_direction_stagetype` | `alembic/versions/0006_add_direction_stagetype.py`, `StageType.DIRECTION_CLASSIFICATION` (`app/models/stage.py:27`) | **сделано** |

## 3. Как это работает сейчас

### 3.1 Цепочка

`POST /api/segmentation/{uid}/segment` (`app/api/segmentation.py:39`) при полном запуске и при
retry с шага `segmenting`/`direction_classification` диспетчит **chain**:

```
task_classify_direction → task_segment_pipes → … → task_build_graph
```

`task_classify_direction` (`worker/tasks/direction.py`) не создаёт нового артефакта и **не
меняет `DiagramStatus`** — правит `coco_validated.json` на месте (атомарно, через `.tmp` +
`replace`), поэтому встраивается между валидацией детекции и сегментацией, не ломая флоу.
Стадия при этом заводится (`StageType.DIRECTION_CLASSIFICATION`), лимиты — 600/540 с.

Мягкие выходы (пайплайн не блокируется): `disabled` (флаг в конфиге), `no_classes`,
`weights_missing` (нет файла весов), `empty` (нет целевых боксов на схеме).

### 3.2 Конфиг

`configs/projects/thermohydraulics/thermohydraulics.yaml:253-270`:

```yaml
direction_classification:
  enabled: true
  weights: "/models/direction/best.pt"
  device: "cuda"           # ⚠ поле НЕ читается кодом, см. §5.4
  img_size: 128
  pad_frac: 0.20
  confidence_threshold: 0.5
  classes: [napravlenie, nasos, rashodomernaya_shaiba]
```

Устройство выбирается не из конфига, а `worker/utils/device.py::resolve_device()` по
переменной `PID_DEVICE` (`auto`/`cpu`/`cuda`) — на CPU-only сервере шаг работает.

### 3.3 Что попадает в артефакты

| артефакт | что появляется |
|---|---|
| `detection/coco_validated.json` | у целевых аннотаций `attributes.direction` (`up`/`right`/`down`/`left`), `attributes.direction_confidence`, `attributes.direction_low_confidence` |
| `segmentation/node_mask.png` | боксы `napravlenie` **не** вырезаются (труба под ними живёт в `pipe_mask`) |
| `graph/graph.json` | узел остаётся `type: "equipment"` с `class_name: "napravlenie"`, плюс `direction`, `direction_node: true`, `flow_axis` (`h`/`v`), `flow_direction`, `pass_through`, `ann_id` |
| FXML | треугольник направления по `flow_direction`/`direction` |

Маппинг классов (две системы, согласованы через `+1`): внутренний YOLO `class_id`
`truba=36, annotation=34, strelka=38, background=39, napravlenie=40`; CVAT `category_id`
= `class_id + 1` (`napravlenie = 41`), приведение — `modules/graph/core/nodes.py:156`.

### 3.4 Модель узла (реализованная)

`modules/graph/core/direction_nodes.py` — бокс «владеет» своей внутренностью:

- **осевая** труба (по стрелке: `left`/`right` → грани LEFT/RIGHT, `up`/`down` → TOP/BOTTOM)
  подключается к боксу — это вход/выход;
- **сквозной перпендикуляр** (входит и выходит через противоположные глухие грани) идёт
  одним ребром **в обход** бокса;
- **перпендикуляр-поворот** (одна глухая грань) получает отдельный `connector`, к боксу не
  цепляется;
- короткие огрызки у грани (шум заливки бокса) выбрасываются.

В `builder.build` (этап 5, `modules/graph/core/builder.py:374-407`) вызываются:
`annotate_direction_nodes` → `drop_degenerate_stubs` → `drop_duplicate_contact_stubs` →
`cap_dangling_ends` → `collapse_straight_connectors` → `update_node_degrees` →
`set_direction_pass_through`.

## 4. Проектные решения 2026-06 (сохранены как есть)

1. Классификация направления — отдельный worker-шаг после детекции; `coco_validated.json` —
   единый источник правды для сегментации и графа.
2. Перпендикулярная труба, пересекающая бокс, остаётся своим ребром и не соединяется
   со «стрелочной» трубой через бокс.
3. `strelka` — оставить как есть, правится только `napravlenie`.
4. Если труба под боксом всё же рвётся — дотягивать по точным границам из «Контур»
   (SAM2, `worker/tasks/contours.py`).
5. `napravlenie` — **узел графа, а не атрибут ребра**: иначе терминальное ребро, упирающееся
   в стрелку, теряет конечную точку и выпадает при сборке, а FXML не на чём якорить.

## 5. Отклонения от плана и открытые хвосты

### 5.1 Узел остался `equipment`, а не `type: "direction"`
Направление — метаданные узла (`direction_node`, `flow_*`), UI рисует обычный бокс.
Практический эффект: **любая проверка вида `node["type"] == "direction"` даст ноль** —
искать нужно по `direction_node` / `class_name == "napravlenie"`.

### 5.2 Ось/перпендикуляр решаются на трассировке, а не через `contact_map`
Правило стороны реализовано в `prepare_tracing_data` (синтетические «телепорты» для сквозных
перпендикуляров, перенос routing-точек мостов, съеденных боксом) — `nodes.py:640-880`.

### 5.3 ⛔ Рёбра не получают `direction` (замерено: 0 из 16 схем)
`apply_direction_rules` и `stitch_collinear_stubs` (`direction_nodes.py:295,416`) **не
вызываются из `builder.build`** — их логика перенесена на трассировку, а сами функции остались
живы только в тестах (`tests/test_direction_nodes.py`). Вместе с ними не выполняется
единственное место, где на ребро ставится `e["direction"]` и `flow_role`. Следствия:
- в `graph.json` рёбер с `direction` нет ни на одной из 16 схем (`MEASUREMENTS.md §28.4`);
- шапка `direction_nodes.py:19-22` описывает старый порядок вызовов — устарела;
- критерий приёмки «два ребра с `direction`» из чек-листа сегодня недостижим.
Решение (оживить штамповку направления на рёбра или снять критерий) — за Максимом, пункты
ВН1б/ВН1в дороги.

### 5.4 `direction_classification.device` в конфиге — мёртвое поле
Читается `PID_DEVICE` через `resolve_device()`; `dc_cfg.device` в коде не используется.

### 5.5 `pass_through` на реальных схемах — редкость
Замер по 16 схемам (`MEASUREMENTS.md §28.3`): direction-узлов **721**, из них
`pass_through: true` — **63 (8.7 %)**, `degree == 1` — **525**, `degree == 0` — **133**.
Ожидание плана «степень обычно 2 (вход/выход)» данными не подтверждается. Это может быть
и нормой схем (стрелка в конце ветки), и симптомом того, что труба под боксом всё же рвётся;
различает только проверка на схеме глазами (ВН1б), см. чек-лист §3.2–3.3.

## 6. Ключевые файлы

| файл | роль |
|---|---|
| `worker/tasks/direction.py` | шаг классификации направления |
| `modules/direction_classifier/classifier.py` | инференс YOLOv8-cls (кроп bbox → `{direction, confidence}`) |
| `worker/tasks/segmentation.py` | `generate_node_mask` — исключение `napravlenie` |
| `app/api/validation.py` | `_generate_node_mask_from_coco` — то же при ревалидации детекции |
| `modules/graph/core/nodes.py` | классы, `EXCLUDED_CLASS_IDS`, `prepare_tracing_data` (ось/перпендикуляр) |
| `modules/graph/core/direction_nodes.py` | пометка боксов, правила оси, `pass_through` |
| `modules/graph/core/export_json.py` | поля `direction_node`/`flow_*` в `graph.json` |
| `modules/graph_to_fxml.py` | треугольник направления в FXML |
| `configs/projects/thermohydraulics/thermohydraulics.yaml` | секция `direction_classification` |
| `alembic/versions/0006_add_direction_stagetype.py` | стадия `direction_classification` |

Обучение классификатора живёт вне этого репозитория:
`…/napravlenie_razmetka/pid_direction_classifier` (ultralytics YOLOv8-cls, классы
`up/right/down/left` для `napravlenie, nasos, rashodomernaya_shaiba, strelka`), веса —
`result_direction/experiments/dir_v1/weights/best.pt` → `models/direction/best.pt`.
