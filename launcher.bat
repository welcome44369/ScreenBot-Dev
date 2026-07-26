@echo off
pushd "%~dp0"
if exist .venv\Scripts\pythonw.exe (
    .venv\Scripts\pythonw.exe main.py
) else (
    pythonw main.py
)
popd
