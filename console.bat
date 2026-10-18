@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Консольный режим: без окна, всё печатается сюда (удобно для диагностики)
.venv\Scripts\python.exe -m afkj_overlay --console
pause
