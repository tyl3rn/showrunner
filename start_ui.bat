@echo off
rem Launches the showrunner web console at http://127.0.0.1:8000
rem Double-click this file, keep the window open while you use the UI.
cd /d "%~dp0"
start "" http://127.0.0.1:8000
python -m uvicorn web.server:app --port 8000
pause
