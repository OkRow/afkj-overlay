@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === AFKJ Overlay: установка ===
set PY=
py -3.12 -c "import sys" >nul 2>&1 && set PY=py -3.12
if not defined PY ( python -c "import sys; assert sys.version_info>=(3,10)" >nul 2>&1 && set PY=python )
if not defined PY (
  echo Python 3.12 не найден. Установите с https://www.python.org/downloads/ и отметьте "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Использую: %PY%
if not exist .venv ( %PY% -m venv .venv || ( echo Не удалось создать .venv & pause & exit /b 1 ) )
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || ( echo Ошибка установки пакетов & pause & exit /b 1 )
echo.
echo Готово. Запустите demo.bat для проверки, затем run.bat.
pause
