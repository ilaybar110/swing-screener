#!/usr/bin/env bash
# Pull the latest state/ and reports/ from GitHub and open the local dashboard.
# Works in Git Bash on Windows and on Linux/macOS.
set -euo pipefail
cd "$(dirname "$0")"

echo "Pulling latest state from GitHub..."
git pull --ff-only || echo "warning: git pull failed - showing what is already on disk" >&2

if [ ! -d .venv ]; then
    echo "Creating .venv and installing requirements (first run only)..."
    python -m venv .venv 2>/dev/null || python3 -m venv .venv
fi
if [ -f .venv/Scripts/activate ]; then
    # shellcheck disable=SC1091
    source .venv/Scripts/activate   # Windows (Git Bash)
else
    # shellcheck disable=SC1091
    source .venv/bin/activate       # Linux / macOS
fi
if ! python -c "import streamlit" 2>/dev/null; then
    pip install -r requirements-local.txt
fi

exec streamlit run dashboard/app.py "$@"
