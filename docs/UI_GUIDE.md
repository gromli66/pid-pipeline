# UI_GUIDE.md — Руководство по UI приложения

**Аудитория:** DEV  
**Версия:** 1.4  
**Обновлено:** 2026-08-18  
**Связанные документы:** ARCHITECTURE.md, STATUS_MACHINE.md, CODING_GUIDE.md, API.md

---

## Содержание

1. [Обзор](#1-обзор)
2. [Main Window](#2-main-window)
3. [Diagram Workspace](#3-diagram-workspace)
4. [Progress Beads](#4-progress-beads)
5. [Вкладки (Tabs)](#5-вкладки-tabs)
6. [Редакторы (Editors)](#6-редакторы-editors)
7. [Mask Editors](#7-mask-editors)
8. [OCR Binding Editor](#8-ocr-binding-editor)
9. [Виджеты и диалоги](#9-виджеты-и-диалоги)
10. [Окна (Windows)](#10-окна-windows)
11. [Сервисы UI](#11-сервисы-ui)
12. [Keyboard Shortcuts](#12-keyboard-shortcuts)

---

## 1. Обзор

P&ID Pipeline UI — десктопное PySide6 приложение для оператора pipeline. Точка входа: `python -m ui.main`.

Приложение работает как клиент к FastAPI backend (`http://localhost:8000`). Все данные (изображения, маски, графы, OCR) скачиваются через API, редактируются локально и сохраняются обратно.

**Архитектура UI:**

```
MainWindow (QMainWindow)
├─ Page 0: DiagramListWidget — список диаграмм
└─ Page 1: DiagramWorkspace — рабочее пространство
              ├─ Header: ProgressBeads + кнопки действий
              └─ Tab area: вкладка валидации/редактирования
                    └─ Editor (QGraphicsView / custom)
```

**Ключевые принципы:**

- **Навигация через QStackedWidget:** Page 0 = список, Page 1 = workspace. Внутри workspace — header (бусины + кнопки) и tab area (вкладка валидации).
- **Вкладка занимает 100% пространства:** при открытии вкладки header прячется. Кнопка «← Назад» закрывает вкладку и возвращает header.
- **Артефакты скачиваются в фоне:** каждая вкладка использует `QThread` + downloader для загрузки данных из API.
- **Autosave по таймеру:** единый `AutoSaveService` вызывает метод сохранения активной вкладки.

Настройки: `QImageReader.setAllocationLimit(1024)` — лимит 1 ГБ для P&ID изображений (до 15000×7000), стиль Fusion, высокое DPI.

---

## 2. Main Window

Файл: `ui/windows/main_window.py`.

`MainWindow` — корневой `QMainWindow`. Содержит:

- **Toolbar:** кнопки «📁 Загрузить» и «🔄 Обновить».
- **Progress bar:** индикатор фоновых операций (скрыт по умолчанию).
- **QStackedWidget:** переключение между списком диаграмм и workspace.
- **StatusBar:** сообщения + индикатор соединения (🟢 API / 🔴 API недоступен).

**Shared services** создаются в MainWindow и передаются в дочерние виджеты:
- `APIClient` — HTTP клиент к backend.
- `StatusProvider` — polling статусов диаграмм.

**Навигация:**
- Двойной клик по диаграмме в списке → `stack.setCurrentIndex(1)`, workspace загружает диаграмму.
- Кнопка «← Назад» в workspace → `stack.setCurrentIndex(0)`, возврат к списку.

**Загрузка диаграммы:** кнопка «📁 Загрузить» → `UploadDialog` (выбор файла + проекта) → `api_client.upload_diagram()`.

---

## 3. Diagram Workspace

Файл: `ui/widgets/diagram_workspace.py`.

`DiagramWorkspace` — центральный виджет для работы с одной диаграммой. Состоит из трёх визуальных строк в header:

1. **Title bar:** название диаграммы + кнопка «← Назад» + статус + кнопка rollback.
2. **Progress beads:** цепочка бусин (см. §4).
3. **Action buttons:** 12 кнопок, по одной на каждую бусину.

### 3.1 Бусины и кнопки

12 бусин (1:1 с кнопками):

| # | Key | Кнопка | Назначение |
|---|-----|--------|------------|
| 0 | `detect` | Детекция | Запуск YOLOv8 detection |
| 1 | `cvat` | CVAT | Валидация bbox в CVAT |
| 2 | `segment` | Сегментация | Запуск UNet++ + skeleton |
| 3 | `pipe` | Вал. pipe | Валидация маски труб |
| 4 | `junction` | Вал. j/b | Валидация junction/bridge |
| 5 | `graph` | Граф | Запуск graph builder |
| 6 | `val_graph` | Вал. графа | Двухфазная валидация графа |
| 7 | `contours` | Контуры | SAM2 contour selection |
| 8 | `ocr` | OCR | Запуск OCR pipeline |
| 9 | `ocr_binding` | Привязка | Привязка OCR к графу |
| 10 | `edit_graph` | Редактор | Финальный редактор графа |
| 11 | `fxml` | FXML | Генерация FXML выхода |

### 3.2 Состояния кнопок

Каждая кнопка может быть в одном из 4 состояний (по цвету):

| Цвет | Состояние | Действие по клику |
|------|-----------|-------------------|
| Серый | Недоступна | Заблокирована |
| Жёлтый | Доступна | Запуск этапа / открытие вкладки |
| Зелёный | Завершена | Повторное открытие вкладки |
| Оранжевый | В процессе | Ожидание worker |

Состояние вычисляется функцией `_buttons_for_status(status)` на основе `_BEAD_DEFS` — маппинга бусин к статусам (completed_when, in_progress_statuses, available_when).

**Бегущая стадия глушит кнопку — кроме ручного этапа.** В «в процессе» кнопку переводит не только статус: цикл в `_update_buttons` глушит кнопку любой стадии, у которой в `/stages` есть строка `running` моложе `WAIT_LIMIT_S` (600 с). Цикл написан ради раскладки — своего статуса у неё нет, и без него её кнопка оставалась доступной во время счёта. **Из цикла исключён ручной этап текущего статуса** (`_MANUAL_INPROGRESS`): строку стадии `frame_removal` открывает сам сервер при входе во вкладку (`app/api/frame.py`, канон §8.5 RUNBOOK), а закрыть её при выходе некому — «← Назад» на сервер не ходит. Без исключения после «открыл „Очистку рамки“ → 💾 → ← Назад» кнопка гасла на 600 с, и оператор не мог вернуться к своей же сохранённой очистке (пункт 1.17 дороги). Ручной этап показывает «в процессе» по статусу — строка стадии ему для этого не нужна.

### 3.3 Status polling

`StatusProvider` опрашивает backend каждые 2 секунды. При изменении статуса → `_apply_status()` → обновление бусин и кнопок. Polling останавливается на «финальных» статусах (требующих действия оператора или завершающих).

**OCR polling** — отдельный таймер `_ocr_poll_timer` (3 сек), проверяет наличие артефакта `OCR_RESULT` через API. Это нужно, потому что OCR работает параллельно с graph/contours и не меняет DiagramStatus.

### 3.4 Открытие/закрытие вкладок

Метод `_open_tab(tab_widget, tab_key)`:
1. Прячет header panel.
2. Добавляет tab_widget в layout.
3. Запускает AutoSave для вкладки.
4. Приостанавливает status polling.

Метод `_close_active_tab()`:
1. Если есть несохранённые изменения → спросить.
2. Вызывает `tab.cleanup()`.
3. Удаляет tab_widget из layout.
4. Показывает header panel.
5. Останавливает AutoSave.
6. Обновляет статус.
7. Только для вкладки труб — двойная страховка `_check_masks_completion()`.

**Команда о завершении валидации масок отправляется один раз на одно подтверждение** (пункт 1.20 дороги). Раньше шаг 7 стоял безусловно, а флаг `_pipe_confirmed` жил до конца сеанса с диаграммой — поэтому закрытие ЛЮБОЙ следующей вкладки слало `complete_mask_validation` заново, в том числе когда оператор на шаге 1 ОТКАЗАЛСЯ сохранять изменения. Пока сервер ещё не ушёл дальше `validated_masks`, такая повторная команда диспатчит скелетизацию второй раз (`app/api/validation.py:481-503`) — это UI-нога гонки двойной скелетизации; остальные две ноги (дыра `already_past` и гейт задачи) закрываются отдельно. Право доложить возвращает только новое подтверждение вкладки труб.

### 3.5 Rollback

Откат — **только по просьбе оператора**: кнопка уже пройденного этапа спрашивает «Откатить до …? Все последующие артефакты будут удалены» (`_on_button_click`) и лишь после «Да» зовёт `api_client.rollback_diagram()`.

Двух молчаливых откатов, которые здесь были, больше нет (пункт 1.3 дороги):

- при **открытии** диаграммы `_self_heal_stuck_stage()` откатывал застрявший `*ING`-статус **без preserve-флагов** — для «Проверки схемы» это сносило `graph_validated`, оба контурных артефакта, все четыре OCR-овых, холст и FXML (9 артефактов из 11);
- при **закрытии** вкладки без подтверждения `_rollback_if_validating()` откатывал статус из обоих путей закрытия; для «Очистки рамки» это уносило сохранённую оператором очистку вместе с `image_raw.png`.

Замер: откат не менял **ни одной** кнопки — набор доступных/пройденных совпадает у `*ING`-статуса и у стабильного (кнопку и так держит кликабельной поправка `_MANUAL_INPROGRESS` в `_update_buttons`). Различался только цвет бусины у `frame` и `junction`; теперь её красит такая же поправка в `_update_beads`, а `_note_unconfirmed_stage()` при открытии пишет в лог и в статусбар, что этап остался незавершённым.

### 3.6 Tab lifecycle для конкретных кнопок

Примеры:

- **`pipe`** → `_open_pipe()` → создаёт `PipeTab`, скачивает маски → подключает `pipe_confirmed` signal → `_on_pipe_confirmed()` → `api_client.complete_mask_validation()` — ровно один раз на подтверждение (см. §3.4).
- **`val_graph`** → двухфазный флоу: `SimpleGraphTab` → confirm → `AdvancedGraphTab` → confirm → `complete_graph_validation()`.
- **`contours`** → `ContourTab` → confirm → `complete_contour_validation()`.
- **`ocr_binding`** → `OcrBindingTab` → confirm → `complete_ocr_binding()`.

---

## 4. Progress Beads

Файл: `ui/widgets/progress_beads.py`.

`ProgressBeads` — кастомный `QWidget`, рисующий горизонтальную цепочку бусин: `●—●—●—●—●—●—●—●—●—●—●—●`.

### 4.1 Состояния бусин

| Состояние | Вид | Цвет |
|-----------|-----|------|
| `UNAVAILABLE` | Серая заполненная | `#646464` |
| `AVAILABLE` | Белая обводка | `#B4B4B4` |
| `IN_PROGRESS` | Оранжевая с анимацией sweep | `#FFA500` |
| `COMPLETED` | Зелёная заполненная | `#4CAF50` |
| `ERROR` | Красная заполненная | `#F44336` |

Анимация IN_PROGRESS: sweep arc 0→360° каждые 40 мс (QTimer).

### 4.2 Anchor alignment

Бусины могут выравниваться по X-позициям кнопок через `set_anchor_widgets()`. Это обеспечивает визуальное соответствие: бусина над кнопкой.

Каждая бусина может показывать подпись (`label`) и дату (`date`) под ней.

---

## 5. Вкладки (Tabs)

### 5.1 BaseGraphTab

Файл: `ui/tabs/base_graph_tab.py`.

Абстрактный базовый класс вкладок редактора графа. Реализует template method:

1. **Download:** фоновая загрузка артефактов через общий `ArtifactDownloader`
   (`ui/services/artifact_downloader.py`) в `QThread`; что грузить — список `Job`
   из `_graph_jobs(want_canvas)`.
2. **Edit:** оператор работает в editor.
3. **Save:** `_save_graph()` → `api_client.upload_validated_graph()`.
4. **Confirm:** `confirmed` signal → workspace вызывает API complete endpoint.

Абстрактные методы: `_create_editor()`, `_setup_toolbar()`.

Signals: `confirmed()` — подтверждение завершения валидации (без аргументов; workspace получает uid из контекста вкладки).

Скачиваемые артефакты: `original_image` (обязательный), `graph_json` или `graph_validated` (с fallback), `coco_validated` (опциональный).

**Отказ загрузки сохранённой работы оператор видит** (пункт 1.23 дороги). Правило одно на все артефакты вкладки: **404 = сохранённого нет по-настоящему** (первый заход на этап) — фолбэк/пересборка законны и молчат; **любой другой отказ** (5xx, сеть, диск) — сохранённое могло лежать на сервере и просто не отдалось, поэтому вкладка предупреждает модалкой и пишет строку в лог клиента. Различение приезжает из загрузчика полем `Job.failure_key` (`ui/services/artifact_downloader.py`), тем же механизмом, что и у `contours_validated`:

| артефакт | флаг | что говорит модалка |
|---|---|---|
| `graph_validated` → фолбэк `graph_json` | `saved_graph_download_failed` | «Сохранённый граф не загружен» — открыт исходный, сохранение затрёт серверный `graph_validated` |
| `graph_canvas` (только WYSIWYG) | `canvas_download_failed` | «Холст не загружен» — холст пересобран заново, сохранение затрёт серверный холст |

Раньше обе ветки молчали: фолбэк графа брался без разбора причины, а предупреждение о пересборке холста стояло внутри `if saved:` и при неудачной загрузке не показывалось вовсе — тихая пересборка плюс первый же Ctrl+S затирали ручную раскладку на сервере (семья 1.5: на сервер уходит то, что считает дёрти-флаг).

⚠ Известный остаток: при отказе `graph_validated` СВЕЖИЙ холст вдобавок объявляется устаревшим — его штамп источника сверяется с фолбэком, а не с сохранённым графом. Оператор получает две модалки, и вторая («Схема изменилась») называет неверную причину; первой при этом идёт та, что говорит правду. Ветка `if saved:` пунктом 1.23 не тронута.

### 5.2 PipeTab

Файл: `ui/tabs/pipe_tab.py`.

Валидация маски труб. Использует `PolylineMaskEditor` (см. §7.1).

Скачивает: original_image, skeleton, skeleton_mask, pipe_mask (validated или raw), coco_validated (опционально).

Кнопки toolbar: инструмент (Полилиния / Ластик), размер кисти (QSlider), Undo, Сохранить, Подтвердить.

### 5.3 JunctionTab

Файл: `ui/tabs/junction_tab.py`.

Валидация масок junction/bridge. Использует `SquareMaskEditor` (см. §7.2).

Скачивает: original_image, skeleton, skeleton_mask, junction_mask + bridge_mask (validated или raw), coco_validated, центры квадратов (`junction_points_validated` → `junction_points`, опционально — старый сервер типов не знает).

Кнопки toolbar: класс (Junction белая / Bridge красная, клавиши 1/2), размер квадрата-**кисти** (QSlider 3..15), **«Размер объектов» (QSpinBox 3..100) + «Применить размер»**, Undo, Сохранить, Подтвердить.

**Размер кисти ≠ размер объектов.** Слайдер задаёт квадрат, который ставится по Ctrl+ЛКМ. Спинбокс с кнопкой меняет **реальный** размер уже существующих перекрёстков/мостов (сжатие/расширение относительно центра) и потолок 15 не наследует: расширение может требовать больше. Правило: есть выделение (Shift+протяжка) — меняются выделенные пятна; выделения нет — все пятна текущего класса, с подтверждением.

Сохранение отправляет на сервер обе маски **и центры квадратов** (`points_validated.json`). Без центров операция изменения размера ломала бы себя при переоткрытии вкладки: экстрактор с дефолтным окном 15 px не находит окна в квадратах, ужатых до меньшего, и слипшаяся пара сворачивается в один центр.

При подтверждении вкладки, если у диаграммы уже есть сохранённый холст «Ручной правки», появляется предупреждение: пересборка графа уничтожит ручную раскладку WYSIWYG (холст пересобирается с нуля по `source_sha`). Поведение конвейера при этом не меняется — оператор только информируется.

### 5.4 SimpleGraphTab

Файл: `ui/tabs/simple_graph_tab.py`.

Первая фаза валидации графа. Использует `SimpleGraphEditor` (см. §6.2).

Режимы: добавить ребро, удалить ребро, добавить коннектор, удалить узел, добавить equipment (из списка классов проекта), resize.

Кнопки toolbar: режимы (QButtonGroup), Undo, Сохранить, Подтвердить.

### 5.5 AdvancedGraphTab

Файл: `ui/tabs/advanced_graph_tab.py`.

Вторая фаза валидации графа. Использует `AdvancedGraphEditor` (см. §6.3).

Добавляет к SimpleGraphTab: routing, optimize, drag, multi-select, waypoints, auto-fix, perp stats.

Кнопки toolbar: все режимы Simple + Optimise, L-Route, Drag, Multi-select, Edit waypoints, Auto-Fix, Perp Stats.

Это вкладка «Ручная правка» — единственная работает в холсте 1920x1080 (WYSIWYG, `USE_CANVAS = True`). Особенности холста:

- **Замок авто-инструментов.** На холсте с `layout_applied: true` (продукт авто-раскладки) кнопки «Оптимизировать», «Оптимизировать все» и «Авто-выравнивание» выключены с поясняющим тултипом (`_apply_layout_lock`); на фолбэк- и legacy-холстах работают как раньше.
- **Очаги остатка раскладки** — вырезано 2026-08-02: кнопка «Очаги (N)», маркеры (`ResidualLayerMixin`) и панель (`ResidualPanel`) удалены из UI; артефакт `residual_defects.json` — легаси (см. DATA_FORMATS.md).
- **Починка посадки концов.** При открытии актуального холста концы рёбер пересаживаются каноном `seating` (`ui/tabs/base_graph_tab.py::_reseat_canvas_endpoints`, канон — `modules/graph/core/pretransform.seat_edge_endpoints`); правится локальная temp-копия, на сервер починка уезжает обычным сохранением оператора. Конец, закреплённый пином (см. §6.3), — вне канона: пин восстанавливается последним словом прогона.
- **Миграция легаси-намерений (до 2026-08-03).** Там же, в `_reseat_canvas_endpoints`, старые хранилища ручных правок один раз переводятся в пины: `node['_ports']` с владельцем-парой → пины живых рёбер пары (записи без владельца, сироты без ребра и коннекторы — дроп с логом), `_manual_route` → пины обоих не-коннекторных концов (флаг снимается), `_auto_route` стирается — серверная подпись `avoid_router` продолжает писаться в свежих холстах, но редактором не читается (инертна).

### 5.6 ContourTab

Файл: `ui/tabs/contour_tab.py`.

Выбор и валидация SAM2 контуров. Использует `ContourEditor` (см. §6.4).

Скачивает дополнительно: `contours_auto.json` (автоматические контуры от SAM2).

Workflow: Ctrl+Click на equipment centroid → toggle SAM2 polygon. «Apply all» применяет все контуры с confidence ≥ threshold. Confirm → `complete_contour_validation`.

Кнопки toolbar: Apply All, Threshold (QSlider), Undo, Сохранить, Подтвердить.

### 5.7 OcrBindingTab

Файл: `ui/tabs/ocr_binding_tab.py`.

Привязка OCR текста к узлам/рёбрам графа. Использует `OcrBindingEditor` (см. §8).

Архитектура v2 — 3 подвкладки:
1. **KKS** — коррекция и привязка KKS-кодов к equipment nodes.
2. **Диаметр** — привязка диаметров к рёбрам.
3. **Другое** — прочие блоки + финальное подтверждение.

Классификация OCR-блоков запускается автоматически при загрузке. Цвета боксов по confidence: зелёный (high), жёлтый (medium), оранжевый (low).

### 5.8 CvatTab

Файл: `ui/tabs/cvat_tab.py`.

Встроенный CVAT через `QWebEngineView`. Загружает URL CVAT task, оператор редактирует bbox аннотации.

Перед подтверждением: JS-инъекция `Ctrl+S` для принудительного сохранения в CVAT.

Signal: `confirmed()` — без аргументов.

---

## 6. Редакторы (Editors)

### 6.1 BaseGraphEditor

Файл: `ui/editors/base_graph_editor.py`.

Базовый класс — `QGraphicsView` с рендерингом графа P&ID поверх изображения.

**Данные:** `GraphDataModel` (nodes, edges, coco_annotations) + `UndoManager` (стек команд).

**Рендеринг:**
- Изображение → `QGraphicsPixmapItem` на сцене.
- Узлы → `QGraphicsEllipseItem` (equipment: синий R=6, connector: зелёный R=8 — connector крупнее для визуальной заметности на ребре, isolated: красный).
- Рёбра → `QGraphicsPathItem` (polyline через waypoints).
- Текст → `QGraphicsSimpleTextItem` (KKS labels, diameter labels).
- Подсветка сторон с подключением → `QGraphicsPathItem` (`side_items[node_id]`, z=2.5 — поверх рамки узла, под маркером-центроидом).

**Два радиуса коннектора разведены намеренно.** `CONNECTOR_MARKER_RADIUS` — геометрический: виртуальный bbox коннектора (`_get_node_bbox`), от него зависят посадка рёбер (`source_point`/`target_point` → FXML), сторона ребра и геометрия ОКР-привязки. `CONNECTOR_DRAW_RADIUS` — нарисованный, его и крутит ползунок «размер коннекторов». Дефолты равны, поэтому вид по умолчанию не меняется. То же зеркально сделано в `OcrBindingEditor` (`NODE_RADIUS` / `NODE_DRAW_RADIUS`).

**Подсветка стороны блока, где есть подключение** (по умолчанию включена, переключатель и цвет — в шестерёнке): участок границы длиной `SIDE_MARK_LEN` вокруг точки входа трубы, единый алгоритм для боксов и полигонов (`graph_geometry.boundary_mark_points`). Бокс обрезается по грани — у мелкого бокса штрих иначе завернул бы за угол; контур проходится через вершины. Точка входа берётся из `_visual_edge_ends`, а форма — из `_node_has_polygon`: важно брать то, что **нарисовано**, а не сырые данные (в холсте у скинового узла контур не рисуется, форма = bbox). Участков столько, сколько разных точек входа; совпавшие схлопываются. Обновление — хуком в `create_edge_item` / `_update_edge_path` / `remove_edge_item`, через которые проходят все точечные пересчёты рёбер.

**Навигация:** Ctrl+Wheel = zoom, ЛКМ drag = pan (ScrollHandDrag).

**Hit testing:** `find_node_at(x, y)` — поиск узла в радиусе `CLICK_THRESHOLD=20`. `find_nearest_edge(x, y, threshold)` — ближайшее ребро.

**Mode system:** паттерн ModeHandler (см. CODING_GUIDE.md §3.2). Каждый режим (`add_edge`, `delete_edge`, `drag_node`, ...) — отдельный handler, зарегистрированный через `register_mode()`. Событие Ctrl+Click делегируется текущему handler.

**Connection points:** `get_connection_point(node_id, target_x, target_y)` — вычисляет точку на контуре узла (bbox или polygon), ближайшую к целевой точке. Поддерживает rect, polygon и circle.

### 6.2 SimpleGraphEditor

Файл: `ui/editors/simple_graph_editor.py`.

Наследует `BaseGraphEditor`. CRUD операции над графом.

**Режимы и handlers:**

| Режим | Handler | Действие |
|-------|---------|----------|
| `idle` | `IdleHandler` | Нет действия |
| `add_edge` | `AddEdgeHandler` | Ctrl+Click на source → move → Ctrl+Click на target → `AddEdgeCommand` |
| `delete_edge` | `DeleteEdgeHandler` | Ctrl+Click на ребро → `RemoveEdgeCommand` |
| `add_connector` | `AddConnectorHandler` | Ctrl+Click на ребро → `AddConnectorOnEdgeCommand` / на пустое → `AddConnectorIsolatedCommand` |
| `delete_node` | `DeleteNodeHandler` | Ctrl+Click на узел → `DeleteNodeCommand` |
| `add_node_from_list` | `AddNodeFromListHandler` | Ctrl+Click → `NodeListDialog` → `AddEquipmentNodeCommand` |
| `resize_node` | `ResizeNodeHandler` | Ctrl+2Click на equipment **с рамкой** → `ResizableNodeOverlay` → drag handles → `ResizeNodeCommand`. Узел, чья форма задана контуром, ручками размера не правится (ручки двигали бы bbox и центроид, оставляя `segmentation` на месте). Выход (Esc / клик мимо) возвращает инструмент, который был до входа. Незакоммиченная тяга при выходе снимается (1.8): `_stop_resize` возвращает узел и инцидентные рёбра к последнему снимку тем же кодом, что Ctrl+Z (`ResizeNodeCommand.undo`), в стек undo ничего не пишет и оставляет след в логе клиента. Тягу открывает только настоящее нажатие: синтетический клик (`event=None` из `_on_ctrl_lmb_click`) `start_drag` не зовёт — иначе у мелкого узла (ручка в радиусе перехвата `find_node_at`) одиночный Ctrl+клик по ручке начинал «липкий ресайз» — тягу при отпущенной кнопке |

Рёбра: point-to-point (без L-route). Connection points вычисляются автоматически на контуре bbox/polygon.

### 6.3 AdvancedGraphEditor

Файл: `ui/editors/advanced_graph_editor.py`.

Наследует `SimpleGraphEditor`. Полный редактор с routing, оптимизацией, auto-fix.

**Дополнительные режимы:**

| Режим | Handler | Действие |
|-------|---------|----------|
| `add_edge_waypoints` | `AddEdgeWithWaypointsHandler` | Ctrl+Click для промежуточных waypoints |
| `optimize_edge` | `OptimizeEdgeHandler` | Ctrl+Click на ребро → L-route → `OptimizeEdgeCommand` |
| `drag_node` | `DragNodeHandler` | Ctrl+drag узла → `DragNodeCommand`, пересчёт рёбер |
| `multi_select` | `MultiSelectHandler` | Ctrl+drag rect → select → drag group → `BatchDragCommand` |
| `edit_waypoint` | `EditWaypointHandler` | Ctrl+Click/drag на waypoint → `MoveWaypointCommand` / `AddWaypointCommand` / `DeleteWaypointCommand` |
| `resize_objects` | `ResizeObjectsHandler` | Панель «Размеры» (`ui/widgets/object_resize_panel.py`): набор экземпляров одного класса (Ctrl+ЛКМ добавить, Ctrl+ПКМ убрать, Shift+рамка), живое превью размеров, «Применить» → `SnapshotCommand` |

**Превью панели «Размеры» (Р9, 2026-08-19).** Бегунок/спинбоксы зовут `preview_resize`, и он меняет **модель**, а не только сцену — поэтому у превью есть базлайн (`_capture_resize_base`: геометрия набора, снимок модели и **пины инцидентных рёбер**) и четыре выхода. «Применить» (`apply_resize`) фиксирует превью `SnapshotCommand` от базлайна — Ctrl+Z возвращает и рамки, и пины. Смена набора и выход из режима (Esc или кнопка режима) считаются брошенным превью: `_revert_resize_preview` возвращает набор к базлайну и пишет строку в лог клиента. Остальные два выхода — запись на сервер и команда-на-месте — ниже отдельными абзацами; «любого другого исхода» тут нет, каждый путь снимает превью сам. Пины входят в базлайн обязательно: `rescale_edge_pins` домножает `dx/dy` на месте, и без возврата к исходным каждый тик бегунка множит уже домноженное (замер `MEASUREMENTS §48`: `dx` 10.0 → 45.0 → 202.5 за два тика одним и тем же 90×90, после «Применить» — 911.25 при полуширине узла 45).

⛔ **Живое превью не уходит на сервер (Р13, 2026-08-19).** Третий выход из превью — сохранение: `BaseGraphTab._save_graph` зовёт `editor.drop_uncommitted_preview()` **до** сериализации, и `AdvancedGraphEditor` снимает превью тем же `_revert_resize_preview` (панель при этом не сбрасывается — иначе набранные оператором ширина/высота затёрлись бы медианами). Так путь записи согласован с дёрти-флагом: на сервер уходит ровно то, что флаг считает (см. §11.3). До этого запись шла от модели КАК ЕСТЬ, и после 1.4 сервер расходился с моделью — замер `MEASUREMENTS §50`: «превью → Сохранить → выход» оставлял на сервере `node_11` = `[-10.0, 188.0, 80.0, 278.0]` при исходных `[25, 215, 45, 251]` в модели.

⛔ **Живое превью не вваривается в команду (доработка 1.4 по ревизии связки, 2026-08-19).** Четвёртый выход: команда-на-месте, нажатая поверх живого превью. `SnapshotCommand._before` снимается с модели КАК ЕСТЬ, то есть вместе с превью, — и оно становится НЕОТКАТЫВАЕМЫМ: `undo_mgr.revision` растёт, базлайн объявляется протухшим, а лечение «протух → бросить без отката» превью уже не трогает. Замер `MEASUREMENTS §52` до правки: «превью 90×90 → «Авто-выравнивание» → Ctrl+Z» возвращал `node_11` = `[-10.0, 188.0, 80.0, 278.0]` вместо исходных `[25, 215, 45, 251]`, и то же уезжало на сервер; то же самое давали «Оптимизировать все» и «Удалить выделенное». Поздним откатом это не лечится — `_before` уже отравлен. Поэтому такие команды зовут `drop_uncommitted_preview()` **до** снятия своей точки возврата: `smooth_canvas`, `auto_fix`, `optimize_all_edges`, `batch_delete` — все пути, достижимые в режиме (жестовые команды в нём заперты, `_node_drag_allowed()` = False). Кнопка, не построившая точки возврата (`batch_delete` без выделения), превью НЕ снимает: вваривать нечего.

⛔ **Базлайн протухает от чужой команды.** Между его снятием и применением может пройти Ctrl+Z / Ctrl+Y — тогда базлайн описывает то, что оператор уже отменил, и откат по нему отменил бы сам откат. Сторож — `_resize_baseline_alive()` по `undo_mgr.revision`; протух — базлайн снимается заново от текущего состояния. Сравнивать `nodes`/`edges_data` по ссылке для этого **нельзя**: команды вроде `DragNodeCommand` правят узлы на месте и модель не пересобирают (замер `MEASUREMENTS §48`, сторож 1). После доработки в эту ветку попадает **только** undo/redo, где модель пересобирается `model.restore` и превью физически исчезает вместе с ней; команды-на-месте до неё не доходят — превью снято абзацем выше.

**Пин входа (модель «пин на ребре», 2026-08-03).** Единственное персистентное намерение оператора — пин входа: `edge['pin_source'|'pin_target']` = `{'dx','dy'}`, локальное смещение **(x, y)** от центроида узла конца (едет с узлом, масштабируется при resize через `rescale_edge_pins`, переживает undo/save). API — `modules/graph/core/ports.py`, секция «пины на ребре» (`pin_role` / `edge_pin` / `pinned_on_node` / `set_edge_pin` / `clear_edge_pin` / `rescale_edge_pins`); `ui/editors/port_model.py` — тонкий реэкспорт. Посадка (`seat_end`, `modules/graph/core/edit_engine.py`) сажает конец в пин первее всего; пин на коннекторе запрещён — конец коннектора всегда центроид. Жесты (режим edit_waypoint): протяжка маркера конца ставит/двигает пин (конец липнет к портам); «отвязать вход» — ПКМ по маркеру конца; маркеры портов на время протяжки: голубые — кандидаты (производные, не хранятся), оранжевые — пины. Waypoints — кэш последнего расчёта маршрута: drag узла ведёт все инцидентные рёбра, любой жест вправе перестроить маршрут. Флаги `_manual_route`/`_auto_route` и якоря `node['_ports']` мертвы (до 2026-08-03, мигрируются при открытии — см. §5.5). Пины не входят в sha-проекцию холста (`graph_projection_sha`) и не текут в FXML.

**Auto-fix** (`autofix_chains.py`): выравнивает узлы по ортогональным цепочкам. Union-Find для H- и V-цепочек, weighted median, clamp для equipment/connector, multi-pass до сходимости.

**Edge routing** (`edge_routing.py`): ортогональный маршрутизатор. Правила: ортогональность, перпендикулярный выход, stub ≥5px, не через bbox, уникальность, минимум поворотов. Pipeline: distribute connection points → generate candidates (L/Z/U) → filter bbox → score → clean collinear.

**Perp stats:** статистика перпендикулярности рёбер (`global_axis_perpendicularity`). Показывает: % perfect (0° deviation), % acceptable (<5°), worst cases.

### 6.4 ContourEditor

Файл: `ui/editors/contour_editor.py`.

Наследует `SimpleGraphEditor`. Применение SAM2 контуров.

Добавляет режим `apply_contour` (`ApplyContourHandler`): Ctrl+Click на equipment centroid → toggle polygon из `contours_auto.json`. При применении: обновляет `node["segmentation"]`, пересчитывает centroid из polygon, пересчитывает connection points рёбер.

Цвета узлов по состоянию контура: зелёный (applied, high confidence), жёлтый (applied, low confidence), голубой (available, не applied), серый (нет контура).

Command: `ToggleContourCommand` — undo/redo для toggle.

---

## 7. Mask Editors

### 7.1 PolylineMaskEditor

Файл: `ui/editors/polyline_mask_editor.py`.

`QGraphicsView` для валидации маски труб. Два инструмента:

**Полилиния (рисование):** Ctrl+ЛКМ добавляет точки, 2×ЛКМ / ПКМ / Enter завершает. Рисует на маске белым (`cv2.polylines` + `cv2.fillPoly`).

**Ластик (стирание):** Ctrl+ЛКМ + drag стирает чёрным кругом (размер из slider).

Undo: стек масок (numpy arrays), до `MAX_UNDO_STEPS=50`.

Оптимизации: numpy vectorized mask↔RGBA конверсии, Grayscale 8-bit подложка (28 MB vs 112 MB на 7000×4000), COCO bbox через QPainter.

### 7.2 SquareMaskEditor

Файл: `ui/editors/square_mask_editor.py`.

`QGraphicsView` для валидации junction/bridge масок. Два класса масок:

- **Junction** (белая маска, клавиша `1`).
- **Bridge** (красная маска, клавиша `2`).

Инструменты: Ctrl+ЛКМ добавляет квадрат (размер из slider, default 15px). Ctrl+ПКМ — flood-fill удаление (BFS по связным компонентам маски). Shift+протяжка — обвести и выделить пятна (золотая рамка), Esc — снять выделение.

**Изменение реального размера** — `apply_square_size(size)`: пятно стирается **по связной компоненте** (не по bbox: у L-образных клякс bbox'ы соседей перекрываются, и стирание по bbox съело бы чужие пиксели), затем квадраты `size×size` рисуются заново **по центрам**. Только это разъединяет слипшиеся при сжатии и сохраняет квадратность; число связных компонент при этом легально меняется. Центры — независимый список (`_points`), он и есть источник истины; правило выбора: точка модели выигрывает только для merged-компонент, для одиночного квадрата надёжнее центроид пятна (оператор мог его передвинуть).

Свежепоставленные квадраты живут отдельными `QGraphicsRectItem` и в Shift-выделение не попадают никогда — ветка «все пятна класса» меняет и их.

Undo: стек снимков. Запись операции размера хранит обе маски, геометрию свежепоставленных `QGraphicsRectItem` и список центров — снимка масок недостаточно.

Ctrl+S в редакторе делегируется вкладке (`save_requested_callback`). Напрямую `save_masks()` без путей звать нельзя — PNG упадут в CWD процесса и на сервер не уйдут.

### 7.3 FrameRemoverView («Очистка рамки»)

Файл: `ui/editors/frame_editor.py`, вкладка — `ui/tabs/frame_tab.py`.

Три инструмента: **Полигон** (всё снаружи → фон), **Бокс** (всё внутри → фон), **Обрезать** (лист уменьшается, DPI сохраняется, без ресэмпла).

**Предпросмотр обрезки.** Протяжка ЛКМ не режет лист сразу, а **паркует** зелёную рамку. До применения её можно поправить:

| Жест | Действие | Курсор |
|------|----------|--------|
| Тяга за любую точку стороны | двигать эту сторону | SizeHor / SizeVer |
| Тяга за угол | двигать обе стороны | SizeFDiag / SizeBDiag |
| Тяга внутри рамки | перенести целиком (кламп по листу) | SizeAll |
| Enter | применить обрезку | — |
| Esc | отмена | — |
| Клик мимо рамки | новая протяжка (старая рамка заменяется) | Cross |

Объектов-ручек нет — hit-test идёт по самому прямоугольнику, зоны ±6 экранных px при любом зуме. Вырожденная рамка (<5 px по стороне) отсекается сразу при завершении протяжки, а не молча по Enter.

**Ctrl+Z при живом превью** гасит превью, а не правку изображения: undo пересобирает сцену и убил бы превью-item посреди жеста. Следующий Ctrl+Z отменяет правку как обычно. Смена инструмента снимает припаркованное превью — это считается отказом от обрезки.

**Открытие вкладки и отказ загрузки** (пункт 1.x6). `FrameTab` строится и показывается сразу, а `original_image` качается ОТЛОЖЕННЫМ вызовом (`QTimer.singleShot(0, self, self._load_original)`) — то есть после того, как воркспейс вставил вкладку в layout и подключил её сигналы (`diagram_workspace.py:1752-1759`). Отказ загрузки (нет артефакта, сеть, битый файл) виден **строкой** под тулбаром и в статусбаре и уходит в файл лога клиента с трассировкой (§11.5). Модалки на этом пути нет намеренно: `QMessageBox` из конструктора вешал процесс под offscreen насмерть, а на бою всплывал над ещё не открытой вкладкой; `status_message`, отправленный из конструктора, не слышал никто.

---

## 8. OCR Binding Editor

Файл: `ui/editors/ocr_binding_editor.py`.

`QGraphicsView` для визуальной привязки OCR-блоков к узлам/рёбрам графа.

### 8.1 Визуальные элементы

- **OCR-боксы:** прямоугольники с текстом. Цвет по confidence: зелёный (≥0.94), жёлтый (0.80–0.94), оранжевый (<0.80). Привязанные боксы получают синюю обводку.
- **Equipment nodes:** синие маркеры. Hover — жёлтый, bound — оранжевый.
- **Рёбра графа:** бирюзовые polylines.
- **Secondary blocks:** кириллические описания (серые боксы).
- **Binding lines:** линии от OCR-бокса к привязанному узлу/ребру.

### 8.2 Взаимодействие

Единый UX через Ctrl + drag:

| Действие | Что происходит |
|----------|----------------|
| Ctrl+drag OCR-бокса → на equipment node | Привязка ККС к узлу |
| Ctrl+drag OCR-бокса → на ребро | Привязка диаметра к ребру |
| Ctrl+drag OCR-бокса → на другой OCR-бокс | Слияние боксов |
| Ctrl+drag OCR-бокса → на пустое место | Отмена (бокс возвращается) |
| Ctrl+click на привязанном боксе | Отвязка |
| Ctrl+double click | Редактирование текста |

### 8.3 Validation mode

В режиме валидации OCR-боксы раскрашиваются по результатам classify из domain profile. Цвета определяются для каждой категории (KKS, diameter, noise, other). Оператор может переклассифицировать блок, подтвердить или отклонить.

### 8.4 Diameter bindings

Привязка диаметров к рёбрам с propagation: если ребро получило диаметр, соседние рёбра без диаметра наследуют его по правилу потока. Labels диаметров показываются на середине ребра.

### 8.5 Тестирование редакторов — test_editors.py

Файл: `ui/editors/test_editors.py` (362 строки). Standalone скрипт для ручного тестирования масочных редакторов на реальных данных.

Запуск: `python test_editors.py <diagram_uid>`. Скачивает артефакты через API и открывает 2 вкладки (pipe + junction) в локальном PySide6-окне. Полезен для визуальной верификации редакторов без запуска полного UI.

---

## 9. Виджеты и диалоги

### 9.1 DiagramListWidget

Файл: `ui/widgets/diagram_list.py`.

`QTableWidget` с 5 колонками: Файл, Проект, Статус, Дата, Удалить.

Фильтры: по проекту (QComboBox), по статусу (QComboBox), поиск по имени (QLineEdit).

Статус — цветная метка: серый (uploaded), синий (processing), оранжевый (waiting for operator), зелёный (validated/completed), красный (error).

Двойной клик → signal `diagram_selected(uid, filename)`.

Контекстное меню (ПКМ): Reset, Delete, Copy UID.

### 9.2 UploadDialog

Файл: `ui/widgets/upload_dialog.py`.

`QDialog` с двумя полями: файл (QLineEdit + кнопка browse) и проект (QComboBox). Возвращает `(file_path, project_code)`.

### 9.3 NodeListDialog

Файл: `ui/editors/node_list_dialog.py`.

`QDialog` для выбора класса equipment при добавлении узла. QLineEdit для поиска, QListWidget с фильтрацией.

Скрытые технические классы: `annotation`, `background`, `truba`, `unknow`, `strelka`.

---

## 10. Окна (Windows)

В пакете `ui/windows/` осталось одно живое окно — `MainWindow` (§2). Больше в
приложении окон нет: всё остальное — вкладки внутри workspace (§5).

Три standalone-`QMainWindow` — `GraphValidationWindow`, `MaskValidationWindow`,
`CVATWindow` (1332 строки) — снесены 2026-08-18 пунктом 10.3 дороги. Это были
двойники живых вкладок: `CVATWindow` ↔ `CvatTab` (§5.8), `MaskValidationWindow`
↔ `PipeTab` + `JunctionTab` (§5.2–5.3), `GraphValidationWindow` ↔
`SimpleGraphTab` + `AdvancedGraphTab` (§5.4–5.5). В рантайме их не открывал
никто: единственными импортёрами были `ui/windows/__init__.py` и один тест-файл.

---

## 11. Сервисы UI

### 11.1 APIClient

Файл: `ui/services/api_client.py`.

HTTP клиент к backend на `httpx`. Persistent connection + retry при потере связи.

Основные методы: `health_check()`, `list_diagrams()`, `get_status()`, `upload_diagram()`, `download_artifact()`, `upload_validated_graph()`, `rollback_diagram()`, `complete_*()` (mask, junction, graph, contour, ocr_binding validation), `start_detection()`, `start_segmentation()`, `build_graph()`, `start_ocr()`, `generate_fxml()`.

`DiagramStatus` enum — зеркало backend (29 статусов, от `UPLOADED` до `COMPLETED` + `ERROR`).

### 11.2 StatusProvider

Файл: `ui/services/status_provider.py`.

Polling статусов через HTTP каждые 2 секунды (QTimer).

API: `watch(uid)` — начать отслеживание, `unwatch(uid)` — остановить, `force_update(uid)` — принудительный запрос.

Signals: `status_updated(uid, status_info)` — при изменении статуса, `error_occurred(uid, message)` — при ошибке.

Polling останавливается на финальных статусах: `DETECTED`, `VALIDATED_BBOX`, `SKELETONIZED`, `DETECTED_JUNCTIONS`, `BUILT`, `VALIDATED_GRAPH`, `CONTOURS_EXTRACTED`, `CONTOURS_VALIDATED`, `OCR_COMPLETED`, `OCR_BOUND`, `COMPLETED`, `ERROR`.

### 11.3 AutoSaveService

Файл: `ui/services/autosave.py`.

Единый сервис автосохранения на QTimer. Запускается при открытии вкладки, останавливается при закрытии.

Маппинг `_SAVE_METHODS` — имя класса вкладки → имя метода сохранения:

| Класс вкладки | Метод |
|---------------|-------|
| `PipeTab` | `_save_mask` |
| `JunctionTab` | `_save_masks` |
| `SimpleGraphTab` | `_save_graph` |
| `AdvancedGraphTab` | `_save_graph` |
| `OcrBindingTab` | `_save_binding` |

Перед вызовом проверяет `tab.has_unsaved_changes()`. При успехе — обновляет `status_label` на вкладке.

**Дёрти-флаг графовых вкладок** — `undo_mgr.revision != _saved_revision` (`base_graph_tab.py:1103`). Считает только мутации, прошедшие через стек команд; живое превью панели «Размеры» идёт мимо стека, и флаг его **не видит намеренно**: превью без «Применить» оператор ещё не подтвердил, а грязный от превью флаг поручил бы автосейву отправлять именно его. Согласованность держится с другой стороны — `_save_graph` снимает незафиксированное превью перед записью (см. §6.3 «Живое превью не уходит на сервер»), поэтому **на сервер уходит ровно то, что считает флаг**. Замер `MEASUREMENTS §50` до правки: тик автосейва после протяжки увозил `node_11` = `[455.0, 355.0, 545.0, 445.0]` (протяжка + превью), тогда как в модели после выхода из режима оставалось `[490.0, 382.0, 510.0, 418.0]`.

Настройки из `UISettings`: `autosave_enabled` (default true), `autosave_interval_sec` (default 120).

### 11.4 UISettings

Файл: `ui/services/ui_settings.py`.

Singleton обёртка над `QSettings` (реестр Windows / `~/.config` Linux). Organization = «PID», Application = «P&ID Pipeline».

| Настройка | Тип | Default |
|-----------|-----|---------|
| `autosave_enabled` | bool | true |
| `autosave_interval_sec` | int | 120 |

**Оформление — по диаграмме.** Ключи вида `appearance/{uid}/{key}` (`get/set/has/clear_appearance`), плоские: один ключ действует на все вкладки диаграммы сразу, а общий сброс (`clear_appearance(uid)`) стирает всё поддерево. Побочный эффект принят как норма: сброс из любой вкладки сбрасывает и остальные, уже открытые вкладки увидят это после переоткрытия.

| Ключ оформления | Тип | Default | Где |
|-----------------|-----|---------|-----|
| `bg_darkness` | float % | 60 | все вкладки с шестерёнкой |
| `edge_color`, `edge_no_diam_color`, `edge_bad_color` | hex | — | графовые вкладки |
| `size_connector` | float % | 100 | Проверка схемы, Контуры, Ручная правка |
| `size_outline` | float % | 100 | то же |
| `size_ocr_border` | float % | 100 | Ручная правка, Привязка подписей |
| `size_node` | float % | 100 | Привязка подписей |
| `side_marks` | bool | true | графовые вкладки (подсветка сторон) |
| `side_mark_color` | hex | цвет узла | то же |
| `bridge_gap_factor` | float | 3.0 | Ручная правка (уходит в FXML-генерацию) |

Размерные регуляторы — **только визуал**: в граф и FXML ничего не уходит. Хранится множитель в процентах от базы, а не абсолютный размер: редактор применяет его от собственной базы внутри `_apply_visuals`, поэтому значение переживает переключение системы координат (растр/холст) и не накапливается. Под множитель попадают только адресные ключи (`SIZE_FACTOR_KEYS`); геометрия (`CONNECTOR_MARKER_RADIUS`), hit-test (`CLICK_THRESHOLD`) и толщина трубы (`EDGE_WIDTH`, обязана совпадать с `graph_to_fxml.LINE_STROKE_WIDTH`) в него не входят.

### 11.5 Логи клиента

Файл: `ui/services/client_logging.py`. Ставится в `main()` (`ui/main.py`) первым делом, до создания `QApplication`.

| Функция | Что делает |
|---------|-----------|
| `setup_client_logging()` | консоль (как раньше) + `RotatingFileHandler`; возвращает путь файла или `None`, если каталог недоступен |
| `install_excepthook()` | необработанное исключение → запись `CRITICAL` с трассировкой, затем прежний `sys.excepthook` |
| `bind_uid(uid)` | uid открытой диаграммы в корреляционный контекст (зовётся из `DiagramWorkspace.load_diagram`) |

**Где лежит лог:** `%LOCALAPPDATA%\PID-Client\logs\client.log` (Windows), `~/.local/state/pid-client/logs/client.log` (Linux). Переопределяется переменной `PID_LOG_DIR`, уровень — `PID_LOG_LEVEL` (default `INFO`). Ротация: 5 МБ × 5 файлов.

Формат и корреляционные поля — общие с сервером (`app/core/logging.py`: `LOG_FORMAT`, `ContextFilter`), поэтому строка клиента читается так же, как серверная, и сшивается с ней по `uid=`. Контекст живёт в `contextvars`: в фоновых потоках вкладок (QThread) он свой, там `uid=-`.

Почему файл, а не stdout: в собранном `.exe` `console=False` (`deploy_ready/pid_client_windows.spec:82`) — у процесса нет ни `stdout`, ни `stderr`, и весь вывод пропадал. Файл открывается с `encoding="utf-8", errors="replace"`: эмодзи в сообщениях (💾 ✅ 🔄) иначе роняют запись на cp1251-машине.

---

## 12. Keyboard Shortcuts

### 12.1 Глобальные (все editors)

| Клавиша | Действие |
|---------|----------|
| Ctrl+Wheel | Zoom in/out |
| ЛКМ drag (без Ctrl) | Pan (ScrollHandDrag) |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z | Redo |
| Escape | Отмена текущего действия / закрытие overlay |

### 12.2 Graph editors (Simple + Advanced)

| Клавиша | Действие |
|---------|----------|
| Ctrl+Click на узел | Действие текущего режима (select, delete, resize, ...) |
| Ctrl+Click на ребро | Действие текущего режима (delete, optimize, add connector, ...) |
| Ctrl+Click на пустое | Добавить коннектор / equipment (в соотв. режиме) |
| Delete | Удалить выделенный узел (в режиме delete_node) |

### 12.3 Advanced graph editor

| Клавиша | Действие |
|---------|----------|
| Ctrl+drag на узле | Drag node (в режиме drag_node) |
| Ctrl+drag rect | Multi-select (в режиме multi_select) |
| Ctrl+Click на waypoint | Edit waypoint (в режиме edit_waypoint) |
| Протяжка маркера конца ребра | Поставить/передвинуть пин входа (в режиме edit_waypoint, см. §6.3) |
| ПКМ по маркеру конца ребра | «Отвязать вход» — снять пин (в режиме edit_waypoint) |

### 12.4 Mask editors

| Клавиша | Действие |
|---------|----------|
| Ctrl+ЛКМ | Добавить точку полилинии / квадрат |
| Ctrl+ПКМ | Flood-fill удаление (SquareMaskEditor) |
| 2×ЛКМ / ПКМ / Enter | Завершить полилинию (PolylineMaskEditor) |
| 1 | Выбрать класс Junction (SquareMaskEditor) |
| 2 | Выбрать класс Bridge (SquareMaskEditor) |

### 12.5 Contour editor

| Клавиша | Действие |
|---------|----------|
| Ctrl+Click на equipment | Toggle SAM2 contour |

### 12.6 OCR Binding editor

| Клавиша | Действие |
|---------|----------|
| Ctrl+drag | Drag OCR-бокс (привязка/слияние) |
| Ctrl+Click на привязанном боксе | Отвязка |
| Ctrl+double click | Редактирование текста |
