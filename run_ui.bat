@echo off
REM Запуск десктоп-UI P&ID с фиксированным GL-бэкендом (лечит мигание вкладки CVAT).
REM Переменная задаётся только для этого процесса — в Windows навсегда ничего не пишется.
REM Значения PID_GL_BACKEND: software (без мигания) | gles | desktop | auto
set "PID_GL_BACKEND=software"

REM (опционально) против мигания оверлея CVAT:
REM set "QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu-compositing"

python -m ui.main
