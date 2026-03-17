#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# ── Check for Python 3 ───────────────────────────────────────────────────
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "Error: Python 3 is not installed."
    echo "Download it from https://www.python.org/downloads/"
    exit 1
fi

# Verify it's actually Python 3
if ! "$PYTHON" -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null; then
    echo "Error: Python 3.10 or newer is required."
    echo "Download it from https://www.python.org/downloads/"
    exit 1
fi

# ── Create virtual environment if needed ──────────────────────────────────
if [ ! -d "venv" ]; then
    echo "Setting up virtual environment..."
    "$PYTHON" -m venv venv
fi

# ── Activate and install dependencies ─────────────────────────────────────
source venv/bin/activate

echo "Checking dependencies..."
pip install -q -r requirements.txt

# ── Check for .env file ──────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo ""
    echo "Error: No .env file found."
    echo ""
    echo "Create a .env file with your API keys:"
    echo "  cp .env.example .env"
    echo "  Then edit .env and paste in your keys."
    echo ""
    exit 1
fi

# ── Launch ────────────────────────────────────────────────────────────────
PORT=8000
echo ""
echo "Starting Personal AI on http://localhost:$PORT"
echo "Press Ctrl+C to stop."
echo ""

# Open browser after a short delay
(sleep 1 && open "http://localhost:$PORT" 2>/dev/null || xdg-open "http://localhost:$PORT" 2>/dev/null) &

exec uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
