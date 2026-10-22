@echo off
setlocal
cd /d "%~dp0"
if not exist "dist\AutoTradingSystem-0.1.0-dev56.exe" goto missing_exe
start "" "dist\AutoTradingSystem-0.1.0-dev56.exe"
exit /b 0
:missing_exe
echo [ERROR] Packaged application is missing.
pause
exit /b 1