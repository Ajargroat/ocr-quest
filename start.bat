@echo off
setlocal
rem Always work from the folder this script lives in, wherever it is launched from.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -m venv .venv || goto :fail
)

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt || goto :fail

if not exist .env (
  if not exist .env.example (
    echo ERROR: .env.example is missing, cannot create .env.
    goto :fail
  )
  copy .env.example .env >nul || goto :fail
  echo Created .env from .env.example - fill in your credentials.
  notepad .env
)

echo Starting Konkour OCR dashboard...
.venv\Scripts\python.exe main.py
goto :eof

:fail
echo start.bat failed. Check the messages above.
exit /b 1
