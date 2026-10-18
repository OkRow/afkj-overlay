@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Запуск с окном консоли, чтобы видеть ошибки
.venv\Scripts\python.exe -m afkj_overlay
pause
