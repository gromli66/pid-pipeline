# Интеграция нового junction_segmentation (qj)

## Куда положить
Скопировать всю папку `junction_segmentation/` в репозиторий с заменой:
`C:\project\pid_pipeline\modules\junction_segmentation\`

После замены — удалить байткеши и перезапустить воркер:
```
del /s /q modules\junction_segmentation\__pycache__
docker-compose restart worker
```

## Что изменилось (всё — тренировочная сторона)
- `config.py` — добавлены поля аугментаций `aug_contrast*`, `aug_gamma*`, `aug_jpeg*`; дефолты `aug_brightness_limit` 0.2→0.3, `aug_noise_var` 10→15. Поля **не удалялись** (суперсет) → старые чекпоинты грузятся.
- `dataset.py` — в `augment_color` добавлены contrast / gamma / JPEG. `TiledInferenceDataset` (вход инференса) **не тронут**.
- `model.py`, `batch_inference.py`, `find_thresholds.py`, `train.py` — `weights_only=True→False` (только CLI/обучение, не воркер).
- `prepare_junction_dataset.py` переименован в `prepare_dataset.py` (импортёров в приложении нет).
- новый `run_inference_for_unannotated.py` (CLI, импортёров нет).
- `config.json` — оставлен прежний (рантаймом не читается, только пишется train.py). Содержит устаревшие Windows-пути — поправить перед обучением.

## Почему рантайм не ломается
Единственный потребитель — `worker/tasks/junction.py`. Импортируемые им
`run_inference`, `create_binary_mask`, `create_visualization`, `skeletonize_mask`
и класс `JunctionSegModel` — **байт-в-байт идентичны** текущей версии.
Параметры берутся из `thermohydraulics.yaml` (`junction_seg:`) и из `config` в
чекпоинте, не из дефолтов модуля. Формат чекпоинта не изменился → воркерский
`torch.load(weights_only=True)` грузит как старые, так и новые чекпоинты.

## Важно
Замена сама по себе **не меняет поведение инференса** в приложении — код инференса
тот же. Выигрыш qj (новые аугментации) появляется только после **переобучения**
и подкладки нового чекпоинта в `/models/junction_seg/best.pth`.

## Проверки, которые прошли
- `py_compile` всех файлов, нет нулевых байт.
- AST-сверка: сигнатуры `run_inference`/`create_binary_mask`/`create_visualization`/`skeletonize_mask` совпадают с вызовами воркера; `JunctionSegModel` на месте; ни одно поле `Config` не удалено.
- Импорт-смоук с точным набором импортов воркера: ОК (`Config()`: in_channels=5, classes=2, tile_size=512).
