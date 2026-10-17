@echo off
cd /d %~dp0
if not exist venv python -m venv venv
call venv\Scripts\activate
python -m pip install -r requirements.txt
set PROFITOS_ENV=development
python app.py
pause
