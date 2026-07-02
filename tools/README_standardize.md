# Стандартизация FXML → 1920×1080 + фикс смещения скинов

Приводит ЛЮБОЙ выходной FXML-лист к экрану **1920×1080** и убирает смещение/разрывы
кастом-контрол скинов в Scene Builder. Работает и как standalone-CLI, и как
библиотека внутри пайплайна (воркер).

## Почему скины «съезжают» — корень проблемы

Каждый скин библиотеки `custom-control-skin-based-*.jar` имеет **собственное
соотношение сторон** `AR = prefH/prefW`, которое `skin.resize()` **сохраняет**
(letterbox внутри бокса контрола). Значения взяты из jar
(`getPreferredWidth/Height/getAspectRatio`, см. `extract_skin_geometry.py`):

| Скин(ы) | канва | AR (H/W) | форма |
|---|---|---|---|
| HANDLE_VLV, CHECK_VLV | 100×50 | **0.5** | широкая «бабочка» |
| ELECTRIC_VLV, ELECTROMAGNETIC_VLV, CTRL_VLV_ELEC, CTRL_VLV_HANDLE, CHECK_HYDRO_VLV | 100×90 | **0.9** | привод сверху |
| RELIEF_VLV | 100×75 | **0.75** | |
| AOC_VLV | 100×220 | ~1.5 | большой боковой привод |
| PUMP, FAN, HEATER, HEATER_VOL_TUBE, FILTER, FLOWMETER, ARROW | квадрат | **1.0** | квадрат |
| TEXT_BUTTON | 120×30 | **0.25** | очень широкая |

«Только квадрат» — миф: квадратные лишь насосы/нагреватели/фильтры. Если положить
скин в бокс с другим соотношением — графика вписывается по AR и смещается. Плюс у
клапанов с приводом ось контакта («талия бабочки») ниже центра бокса.

Отдельно: **потерянные контакты у symbol-полигонов** (труба не доходит до контура) —
это НЕ проблема размера, а генератора: труба приходила к bbox детекции, а полигон
рисуется по контуру SAM2. Починено в `graph_to_fxml.py`
(`project_endpoint_to_contour`) и работает во всех размерах.

## Что делает стандартизатор (пайплайн пассов)

Всё считается уже в целевых координатах 1920×1080:

1. **Letterbox** — общий bbox содержимого равномерно вписывается в 1920×1080 с
   центрированием. Пересчитываются ВСЕ координаты/размеры: layoutX/Y, prefW/H,
   width/height, Line, Polyline, Polygon points, Rectangle, Circle/Arc, strokeWidth,
   шрифты (Font size и `-fx-font-size`), Rotate pivot.
2. **Клапаны** — измеренная «талия» скина (`waist` в `skin_geometry.json`) сажается
   на ось подключённой трубы; затем концы труб **дотягиваются до реальной графики
   скина** через letterbox-паддинг (ортогонально, вдоль сегмента). Сам скин не
   трогаем — продолжаем линию.
3. **Датчики** (`DetectorControl`) — у скина жёсткий минимум (~20px, текст), он не
   сжимается. Держим датчик читаемым через `scaleX/scaleY = s·K` (пивот=центр,
   `K=1.5`) и прижимаем **рендер-ребро** к концу подводящей трубы — тело уходит в
   сторону, не наезжая на ребро.
4. **Разрывы мостов** (id `*_b0/_b1`) — половинки раздвигаются до видимого зазора
   (`max(6px, 2.5·strokeWidth)`), иначе на сжатом листе разрыв не виден.
5. Корню ставится `prefWidth=1920 prefHeight=1080`.

## Файлы

- **`fxml_standardize.py`** — стандартизатор. CLI + библиотека.
  - `standardize(in_path, out_path, geo=None, mode="letterbox", margin=0.0)` — файл→файл.
  - `standardize_xml(xml, geo=None, mode="letterbox", margin=0.0) -> str` — строка→строка
    (in-memory, без временных файлов; используется воркером). `<?import?>`/комментарии
    сохраняются.
- **`skin_geometry.json`** — геометрия по `skinType` (canvas, `aspect_hw`, `contact`,
  `waist`). Читают и стандартизатор, и генератор.
- **`fxml_preview.py`** — рендер FXML в PNG без JavaFX/SceneBuilder (виден след скина).
- **`extract_skin_geometry.py`** — регенерация геометрии из jar (`pip install jawa`);
  полный дамп — `skin_geometry_full.json`.

## CLI

```bash
# стандартизировать лист (координаты уже верные — напр. из graph_to_fxml)
python3 tools/fxml_standardize.py my_sheet.fxml -o my_sheet_1920x1080.fxml

# посмотреть картинкой
python3 tools/fxml_preview.py my_sheet_1920x1080.fxml -o preview.png
```

Флаги: `--mode letterbox|aspect|full` (по умолчанию `letterbox` — рекомендуется),
`--margin N` (поля, px), `--geo path` (своя таблица).

- `letterbox` — размер + пассы 2–4 (талия/линии/датчики/мосты). Раскладка и привязки
  линий сохраняются; форму боксов клапанов не меняем. **Рекомендуется.**
- `aspect` — дополнительно подгоняет форму боксов под аспект скина по оси реально
  подходящих линий. На тройниках/крестах часть привязок может отойти — такие случаи
  корректнее решать в генераторе.
- `full` — `aspect` + поднять талию на трубу (contact-offset) для сырых боксов.

## Интеграция в пайплайн: выбор размера

В генерации FXML добавлен размер **«1920×1080 (экран)»** рядом с «Оригинал» и A4–A0.
Выбор проходит по существующему сквозному параметру `page_size`:

```
UI: _ask_page_size() → "1920x1080"
  → api_client.generate_fxml(page_size)
  → POST /api/graph/{uid}/generate-fxml?page_size=1920x1080
  → celery task_generate_fxml(page_size="1920x1080")
  → generate_fxml(page_size=None)         # пиксельные координаты
  → standardize_xml(...)                  # letterbox + фикс скинов/датчиков/мостов
  → fxml/diagram.fxml
```

«Оригинал» (`None`) → сырой вывод генератора (как раньше). A4–A0 — без изменений.
Выход всегда один файл `diagram.fxml` (как для A3/A4). Ошибка стандартизации не
фатальна — пишется сырой FXML (см. лог воркера).

Затронутые файлы:

- `ui/widgets/diagram_workspace.py` — `_start_fxml` снова вызывает диалог;
  `_ask_page_size` содержит пункт «1920×1080 (экран)» (по умолчанию).
- `worker/tasks/graph.py` — `task_generate_fxml`: при `page_size=="1920x1080"`
  прогоняет FXML через `standardize_xml`.
- `app/api/graph.py`, `ui/services/api_client.py` — проброс значения (валидации не
  требуется, `page_size` — свободная строка).
- `Dockerfile.worker` (`COPY tools/`), `docker-compose.yml` (`- ./tools:/app/tools`),
  `tools/__init__.py` — чтобы воркер видел `tools.fxml_standardize`.

**После правок нужно пересоздать воркер** (добавлен новый bind-mount `./tools`):

```bash
docker compose up -d worker     # подхватит новый mount (restart недостаточно)
# для production-образа (без mount):
docker compose build worker && docker compose up -d worker
```

## Проверка в Scene Builder

1. SceneBuilder → шестерёнка у панели **Library** → **JAR/FXML Manager** →
   **Add Library/FXML** → `custom-control-skin-based-1.6.0.jar`.
2. Открыть сгенерированный `diagram.fxml` (в режиме 1920×1080). Контролы
   `ValveControl/PumpControl/...` отрисуются скинами на своих местах.

## Что проверено

- Аффинное преобразование точное: труба масштабируется ровно в 1920, клапан на трубе
  остаётся на трубе, полигон масштабируется точно, ничего не выходит за холст.
- `standardize_xml` (in-memory) даёт **побайтово тот же** результат, что файловый
  прогон (эталон — согласованный лист a6d28736 v6).
- Датчик прижат ребром к трубе (не наезжает), разрывы мостов видимы, талии клапанов
  на трубе.

## Оговорки

- `AOC_VLV`: у скина `getAspectRatio≈1.5` при канве 100×220 — значение приблизительное,
  при необходимости откалибруйте по SceneBuilder.
- `waist`-значения в `skin_geometry.json` откалиброваны по линейке-скринам; при смене
  версии jar перепроверьте.
