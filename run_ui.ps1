# Запуск десктоп-UI P&ID с фиксированным GL-бэкендом (лечит мигание вкладки CVAT).
# Переменная задаётся ТОЛЬКО для этого процесса — в Windows навсегда ничего не пишется.
# Значения PID_GL_BACKEND: software (без мигания, универсально) | gles | desktop | auto
$env:PID_GL_BACKEND = "software"

# (опционально) дополнительный рычаг против мигания оверлея CVAT:
# $env:QTWEBENGINE_CHROMIUM_FLAGS = "--disable-gpu-compositing"

python -m ui.main
