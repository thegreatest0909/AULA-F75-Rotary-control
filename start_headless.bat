@echo off
cd /d "%~dp0"
python "aula_knob.py" %*
if errorlevel 1 pause
