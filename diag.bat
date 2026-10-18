@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Диагностика чтения лога. Запускайте ПРИ ЗАПУЩЕННОЙ игре.
rem Параметр --vss только от администратора (теневая копия, ~5–10 с).
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите install.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m afkj_overlay.diag %*
echo.
pause
