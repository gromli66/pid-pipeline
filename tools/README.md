# tools/ — вспомогательные утилиты разработки

Эти скрипты НЕ участвуют в работе приложения (не импортируются `app/`, `worker/`, `ui/`,
`modules/`). Это отдельные dev/отладочные инструменты, запускаются вручную из корня проекта,
например: `python tools/run_junction_editor.py`.

- run_junction_editor.py, frame_remover_app.py, magic_wand_editor.py, polygon_editor.py,
  pipe_mask_editor_app.py — редакторы/утилиты разметки и обработки
- check_profile_setup.py, compare_profiles.py — проверка/сравнение профилей конфигурации
- enhance_pid.py, fix_stamp.py, visualize_contours.py, reset.py — разовые утилиты
- export_fix.txt, export_method.txt — заметки
- dn1_stage_cost.sql (SQL только на чтение), dn2_edit_diff.py — замеры ДН1/ДН2: цена схемы
  по `processing_stages` и разбор правок оператора на холсте (числа — `MEASUREMENTS.md` §29)
- edit_bench.py (судья «Ручной правки», эталон `bench/edit_baseline.json` в git),
  edit_render_probe.py, drag_stress_probe.py, dn2_edit_diff.py работают на общем корпусе
  локальных сейвов холста — `tools/bench/edit_corpus/graph_edited*.json`. Корпус вне git
  (данные заказчика, ~2.9 МБ); до пункта 0.2 он лежал в корне репозитория
- corpus.py (КД7) — общий вход к корпусу `graph_validated.json` для стендов и тестов:
  сначала три графа из git (`tests/fixtures/graph/`, пункт 0.8), потом локальный
  `storage/diagrams/`. На него переведены layout_bench.py и корпусная цепочка
  `tests/test_canvas_pipeline_golden.py`; cmp_bitexact.py и corner_probe.py пока ищут
  storage сами — их эталоны всё равно лежат в `_scratch/`, которого в git нет
- suite_baseline.py — базовая линия набора тестов (`bench/suite_baseline.json` в git),
  гейт «ни одного нового красного» (docs/TESTING.md §7)
- lint_gate.py (ПР5, пункт 0.10) — храповик линтеров: счётчик широких `except` по файлам
  не имеет права расти (`bench/lint_baseline.json` в git), плюс `mypy` на списке `files`
  из `pyproject.toml`. «Голый» `ruff check` красен по определению — весь долг сразу;
  судит храповик (docs/TESTING.md §8)
- pair_bench.py (ДН4, пункт 0.11) — стенд диффов «до/после оператора»: семь пар артефактов
  (детекция, сегментация, перекрёстки, граф, контуры, OCR, раскладка) и «объём правок» на
  каждую. Эталон `bench/pair_baseline.json` в git, данные — локальный `storage/diagrams/`,
  поэтому в CI стенда нет (там `--check` возвращает 2 «судить нечем»); числа первого замера —
  `MEASUREMENTS.md` §33, разбор — docs/TESTING.md §9
- layout_determinism.py (ПР1, пункт 0.10) — стенд воспроизводимости раскладки: каждый
  прогон отдельным процессом со своим `PYTHONHASHSEED`, сравнение sha256 выхода `layout()`.
  Эталон `bench/determinism_baseline.json` в git; ключ `--no-routing` локализует
  недетерминизм (docs/TESTING.md §8)

Для развёртывания и запуска приложения эти файлы не нужны.
