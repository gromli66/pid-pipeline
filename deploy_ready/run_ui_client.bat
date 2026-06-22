@echo off
REM Запуск десктоп-клиента P&ID на Windows (для теста связи, Фаза 1).
REM Задать IP сервера в SERVER_IP. Требует правки Д1 (PID_API_URL).

set "SERVER_IP=REPLACE_WITH_SERVER_IP"

set "PID_API_URL=http://%SERVER_IP%:8000"
set "PID_GL_BACKEND=software"

REM при необходимости активировать venv:
REM call .venv311\Scripts\activate.bat
python -m ui.main
