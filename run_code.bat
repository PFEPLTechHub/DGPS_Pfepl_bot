@echo off
cd /d "%~dp0"

REM Start server.py in a new command window
start cmd /k "python server.py"

REM Start bot.py in another new command window
start cmd /k "python bot.py"
