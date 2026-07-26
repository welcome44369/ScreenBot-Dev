@echo off
pushd "%~dp0"
if not exist scripts mkdir scripts
if not exist config mkdir config
if not exist logs mkdir logs
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe -m PyInstaller --noconsole --onefile --name ScreenBot --add-data "scripts;scripts" --add-data "config;config" --add-data "logs;logs" main.py
) else (
    python -m PyInstaller --noconsole --onefile --name ScreenBot --add-data "scripts;scripts" --add-data "config;config" --add-data "logs;logs" main.py
)
popd
