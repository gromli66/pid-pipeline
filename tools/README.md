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

Для развёртывания и запуска приложения эти файлы не нужны.
