@echo off
REM Запуск десктоп-клиента P&ID (Windows).
set "SERVER_IP=REPLACE_WITH_SERVER_IP"

cd /d "%~dp0.."
call .venv_ui\Scripts\activate.bat

set "PID_API_URL=http://%SERVER_IP%:8000"
set "PID_GL_BACKEND=software"
python -m ui.main
