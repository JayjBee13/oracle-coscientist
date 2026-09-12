@echo off
setlocal
rem Starts the Oracle backend, which serves the API and the built frontend on one port.
rem Run by the "Oracle Backend" scheduled task at system startup, as the account whose
rem profile holds the claude/codex CLI logins -- the engine spawns those CLIs and they read
rem their OAuth credentials from %APPDATA%, so the account this runs as is load-bearing.
set "ROOT=%~dp0.."
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
echo [%date% %time%] starting as %USERNAME% APPDATA=%APPDATA% >> "%ROOT%\logs\backend.log"
cd /d "%ROOT%\backend"
"%ROOT%\backend\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8787 >> "%ROOT%\logs\backend.log" 2>&1
echo [%date% %time%] exited with %ERRORLEVEL% >> "%ROOT%\logs\backend.log"
