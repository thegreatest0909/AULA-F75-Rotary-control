@echo off
cd /d "%~dp0"
python "app.py" --stop >nul 2>&1
python "aula_knob.py" --stop >nul 2>&1
timeout /t 1 >nul
