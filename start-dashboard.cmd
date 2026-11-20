@echo off
rem Starts the GitHub triage dashboard on http://127.0.0.1:8765
cd /d "%~dp0"
python triage.py serve %*
