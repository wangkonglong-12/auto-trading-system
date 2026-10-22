@echo off
setlocal
cd /d "%~dp0"
set "ATS_ROOT=%~dp0"
set "ATS_PYTHON=%ATS_ROOT%.codex_tmp\r31-venv\Scripts\python.exe"
set "ATS_PYTHONW=%ATS_ROOT%.codex_tmp\r31-venv\Scripts\pythonw.exe"
if not exist "%ATS_PYTHONW%" goto missing_runtime
set "PYTHONPATH=%ATS_ROOT%src"
if /i "%~1"=="--check" (
  "%ATS_PYTHON%" -c "import auto_trading_system; print('WEB_LAUNCHER_OK')"
  exit /b %errorlevel%
)
start "" "%ATS_PYTHONW%" -m auto_trading_system.apps.desktop
exit /b 0
:missing_runtime
echo [ERROR] Local web runtime is missing.
pause
exit /b 1