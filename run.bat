@echo off
setlocal

cd /d "%~dp0"

:: ── Check for Python 3 ───────────────────────────────────────────────────
where python >nul 2>&1
if errorlevel 1 (
    echo Error: Python 3 is not installed.
    echo Download it from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Verify it's actually Python 3.10+
python -c "import sys; assert sys.version_info >= (3, 10)" 2>nul
if errorlevel 1 (
    echo Error: Python 3.10 or newer is required.
    echo Download it from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: ── Create virtual environment if needed ──────────────────────────────────
if not exist "venv" (
    echo Setting up virtual environment...
    python -m venv venv
)

:: ── Activate and install dependencies ─────────────────────────────────────
call venv\Scripts\activate.bat

echo Checking dependencies...
pip install -q -r requirements.txt

:: ── Check for .env file ──────────────────────────────────────────────────
if not exist ".env" (
    echo.
    echo Error: No .env file found.
    echo.
    echo Create a .env file with your API keys:
    echo   copy .env.example .env
    echo   Then edit .env and paste in your keys.
    echo.
    pause
    exit /b 1
)

:: ── Launch ────────────────────────────────────────────────────────────────
set PORT=8000
echo.
echo Starting Personal AI on http://localhost:%PORT%
echo Press Ctrl+C to stop.
echo.

:: Open browser after a short delay
start "" http://localhost:%PORT%

uvicorn app.main:app --host 127.0.0.1 --port %PORT%
pause
