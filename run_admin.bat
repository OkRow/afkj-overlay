@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Перезапуск от имени администратора (появится запрос UAC).
rem Работает из путей с пробелами (напр. C:\Users\...\Desktop\AFKJ-Overlay).
rem   run_admin.bat        - оверлей
rem   run_admin.bat demo   - демо
rem   run_admin.bat diag   - диагностика (включая raw_ntfs / backup)
rem   run_admin.bat diag --vss  - + теневая копия
set "TARGET=run.bat"
if /i "%~1"=="demo" set "TARGET=demo.bat"
if /i "%~1"=="diag" set "TARGET=diag.bat"
rem Pass remaining args (e.g. --vss) to the target via a temp helper when needed.
if /i "%~1"=="diag" if /i "%~2"=="--vss" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -WorkingDirectory '%~dp0' -FilePath '%~dp0diag.bat' -ArgumentList '--vss'"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -Verb RunAs -WorkingDirectory '%~dp0' -FilePath '%~dp0%TARGET%'"
