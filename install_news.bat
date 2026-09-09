@echo off
cd /d "%~dp0dashboard"
python -m pip install -r requirements.txt
python server.py
pause
