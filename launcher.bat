@echo off
setlocal

set "SCREENBOT_ROOT=%~dp0"
cd /d "%SCREENBOT_ROOT%"

set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\pythonw.exe"
if not exist "%PYTHON_EXE%" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
)

set "MAIN_PY=%SCREENBOT_ROOT%main.py"

if not exist "%PYTHON_EXE%" (
    echo ScreenBot Dev launch failed.
    echo.
    echo Python:
    echo %PYTHON_EXE%
    echo.
    echo Main:
    echo %MAIN_PY%
    pause
    exit /b 1
)

if not exist "%MAIN_PY%" (
    echo ScreenBot Dev launch failed.
    echo.
    echo Python:
    echo %PYTHON_EXE%
    echo.
    echo Main:
    echo %MAIN_PY%
    pause
    exit /b 1
)

start "" "%PYTHON_EXE%" "%MAIN_PY%"

endlocal
