# Замена одиночной YOLO-детекции на ансамбль — изменённые файлы

Все файлы лежат в `app/` рядом с этим документом — скопируйте их в проект
(`C:\project\pid\pid\app`) по тем же относительным путям.

## Новые файлы

| Файл | Что это |
|---|---|
| `modules/yolo_detector/ensemble.py` | `EnsembleDetector`: N моделей × SAHI (каждая со своим tile_size) + слияние WBF/NMS/Soft-NMS. Код из 1006, адаптирован под детектор приложения — без импорта пакета обучения |
| `modules/yolo_detector/preprocessing.py` | `binarize_image`/`binarize_for_yolo` — единая бинаризация как при обучении (перенос из 1006/data/preprocessing.py) |

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `modules/yolo_detector/__init__.py` | Экспорт `EnsembleDetector`, версия 2.0.0 |
| `app/services/project_loader.py` | `EnsembleMemberConfig`; в `DetectionModelConfig` поля `ensemble_models`, `merge_strategy`, `iou_threshold`, `skip_box_thr`; их парсинг из YAML |
| `configs/projects/thermohydraulics/thermohydraulics.yaml` | `default_model: ensemble_v1`; вместо thermo/paksh — одна модель `type: ensemble` (3 подмодели 640/1280/2048, WBF, **confidence 0.5**) |
| `worker/tasks/detection.py` | `EnsembleDetector` вместо `NodeDetector`; бинаризация при инференсе (`apply_grayscale=True`); time_limit 1800→5400 (3 SAHI-прохода) |
| `requirements/worker.txt` | + `ensemble-boxes>=1.0.9` |

Не тронуто (работает как раньше): `app/api/detection.py`, per-class confidence
фильтрация, `resolve_overlaps`, CVAT-экспорт, статусы БД — формат детекций
у ансамбля идентичен старому.

## Что сделать руками

1. **Веса**: положить три `best.pt` в volume по путям из YAML:
   `/models/yolo/ensemble_v1/tile640/best.pt`, `.../tile1280/best.pt`,
   `.../tile2048/best.pt`. После обучения перенести `weight`/`sahi_overlap`
   из сгенерированного `<имя>_ensemble.yaml` в конфиг проекта (сейчас все 1.0/0.25).
2. **Обучающий пакет**: в корне проекта создать папку `pid_node_detection/`
   с содержимым 1006.rar и удалить старую плоскую копию: `cli.py`,
   `__main__.py`, `_pid_node_detection_init.py`, `augmentation/`, `config/`,
   `data/`, `evaluation/`, `inference/`, `pipelines/`, `training/`,
   корневые `debug_detection.py`, `return_learning.py`, `stage2_finetune.py`.
   Воркер эти каталоги не использует (проверено grep'ом).
3. **Пересобрать** образ воркера (`Dockerfile.worker` менять не нужно).
4. `configs/projects/tec_boiler/` — перевести на ансамбль аналогично, когда
   будут веса (сейчас он сломается при детекции: type != ensemble даст
   понятную ошибку).

## Изменения поведения (важно)

- **Бинаризация при инференсе**: раньше воркер подавал изображение как есть,
  теперь — `binarize_for_yolo(method="full")` (NLM→CLAHE→Otsu→Erode), как при
  обучении ансамбля. Делается один раз на схему, не 3 раза.
- **Порог confidence 0.5** вместо 0.8: WBF с `conf_type="avg"` усредняет
  уверенности по моделям — объект, найденный одной моделью из трёх, получает
  ~⅓ исходного score. `per_class_confidence` при заполнении калибровать
  заново (по `test_ensemble.py`).
- **GPU/время**: 3 модели в VRAM одновременно (~3× памяти), инференс ~3–4×
  дольше. При нехватке VRAM с `--concurrency=2` снизить до 1.

## Проверено

- Синтаксис всех изменённых Python-файлов.
- Парсинг новой секции `detection` из YAML в датаклассы (юнит-прогон).
- Логика слияния на заглушках: 3 пересекающихся бокса → 1 (conf = среднее),
  reverse_reindex 34→35/35→38 применяется после слияния, FP одной модели
  отсеивается порогом 0.5.
- Не проверялось (нет весов/GPU): реальный инференс ансамбля — прогнать
  тестовую диаграмму через `/detect` перед выкаткой.
