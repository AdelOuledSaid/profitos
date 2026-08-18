@echo off
cd /d %~dp0
if not exist venv python -m venv venv
call venv\Scripts\activate
python -m pip install -r requirements.txt
set PROFITOS_ENV=production
waitress-serve --listen=127.0.0.1:5050 wsgi:app
pause
