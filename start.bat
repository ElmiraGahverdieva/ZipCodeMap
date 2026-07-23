@echo off
rem ZIP Code Map — one-click launcher for Windows
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
) else (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set "PY=py"
  ) else (
    echo Python не найден. Установите с https://www.python.org/downloads/
    echo При установке обязательно отметьте галочку "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

if not exist "zcta.db" (
  echo Первый запуск: скачиваю данные ZIP-зон...
  %PY% setup.py
  if errorlevel 1 (
    echo Ошибка загрузки данных. Проверьте интернет.
    pause
    exit /b 1
  )
)

%PY% server.py
pause
