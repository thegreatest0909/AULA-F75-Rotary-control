@echo off
cd /d "%~dp0"
python "menu.py" --stop
timeout /t 1 >nul
pause
