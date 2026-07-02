@echo off
REM ============================================================
REM Double-click = build the client (Windows + Astra).
REM Sits in deploy_ready\ next to build_client.ps1.
REM Details and flags: BUILD_CLIENT.md
REM ============================================================
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "%~dp0build_client.ps1" %*
echo.
pause
