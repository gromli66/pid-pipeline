# UI_GUIDE.md — Руководство по UI приложения

**Аудитория:** DEV  
**Версия:** 1.0  
**Обновлено:** 2026-04-09  
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

### 3.5 Rollback

При закрытии вкладки валидации без подтверждения — `_rollback_if_validating()` откатывает статус диаграммы через `api_client.rollback_diagram()`. Rollback доступен и через кнопку в title bar.

### 3.6 Tab lifecycle для конкретных кнопок

Примеры:

- **`pipe`** → `_open_pipe()` → создаёт `PipeTab`, скачивает маски → подключает `pipe_confirmed` signal → `_on_pipe_confirmed()` → `api_client.complete_mask_validation()`.
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

1. **Download:** фоновая загрузка артефактов через `_GraphArtifactDownloader` в `QThread`.
2. **Edit:** оператор работает в editor.
3. **Save:** `_save_graph()` → `api_client.upload_validated_graph()`.
4. **Confirm:** `confirmed` signal → workspace вызывает API complete endpoint.

Абстрактные методы: `_create_editor()`, `_setup_toolbar()`.

Signals: `confirmed()` — подтверждение завершения валидации (без аргументов; workspace получает uid из контекста вкладки).

Скачиваемые артефакты: `original_image` (обязательный), `graph_json` или `graph_validated` (с fallback), `coco_validated` (опциональный).

### 5.2 PipeTab

Файл: `ui/tabs/pipe_tab.py`.

Валидация маски труб. Использует `PolylineMaskEditor` (см. §7.1).

Скачивает: original_image, skeleton, skeleton_mask, pipe_mask (validated или raw), coco_validated (опционально).

Кнопки toolbar: инструмент (Полилиния / Ластик), размер кисти (QSlider), Undo, Сохранить, Подтвердить.

### 5.3 JunctionTab

Файл: `ui/tabs/junction_tab.py`.

Валидация масок junction/bridge. Использует `SquareMaskEditor` (см. §7.2).

Скачивает: original_image, skeleton, skeleton_mask, junction_mask + bridge_mask (validated или raw), coco_validated.

Кнопки toolbar: класс (Junction белая / Bridge красная, клавиши 1/2), размер квадрата (QSlider), Undo, Сохранить, Подтвердить.

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
| `resize_node` | `ResizeNodeHandler` | Ctrl+Click на equipment → `ResizableNodeOverlay` → drag handles → `ResizeNodeCommand` |

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

Инструменты: Ctrl+ЛКМ добавляет квадрат (размер из slider, default 15px). Ctrl+ПКМ — flood-fill удаление (BFS по связным компонентам маски).

Undo: стек масок (два numpy arrays — junction + bridge).

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

### 10.1 GraphValidationWindow

Файл: `ui/windows/graph_validation_window.py`.

Standalone `QMainWindow` для валидации графа. Использует `AdvancedGraphEditor`. Загружает graph_json и original_image из API, сохраняет graph_validated обратно.

Toolbar: все режимы AdvancedGraphEditor + кнопки Save, Confirm.

> **Примечание:** в текущей архитектуре валидация графа происходит через tabs (SimpleGraphTab → AdvancedGraphTab) в workspace. GraphValidationWindow сохранён для обратной совместимости.

### 10.2 MaskValidationWindow

Файл: `ui/windows/mask_validation_window.py`.

Standalone `QMainWindow` с 2 вкладками: Junction/Bridge (SquareMaskEditor) и Pipe (PolylineMaskEditor).

> **Примечание:** аналогично, основной путь — через PipeTab и JunctionTab в workspace.

### 10.3 CVATWindow

Файл: `ui/windows/cvat_window.py`.

Standalone `QMainWindow` с встроенным `QWebEngineView` для CVAT.

JS-инъекция `Ctrl+S` для принудительного сохранения перед экспортом. Signals: `validation_confirmed(uid)`, `window_closed(uid)`.

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

Настройки из `UISettings`: `autosave_enabled` (default true), `autosave_interval_sec` (default 120).

### 11.4 UISettings

Файл: `ui/services/ui_settings.py`.

Singleton обёртка над `QSettings` (реестр Windows / `~/.config` Linux). Organization = «PID», Application = «P&ID Pipeline».

| Настройка | Тип | Default |
|-----------|-----|---------|
| `autosave_enabled` | bool | true |
| `autosave_interval_sec` | int | 120 |

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
