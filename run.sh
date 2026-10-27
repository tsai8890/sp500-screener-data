#!/usr/bin/env bash
# Start the screener on this computer: http://localhost:8000
#   ./run.sh               (Ctrl+C to stop)
#   PORT=8080 ./run.sh     another port
# The first run creates .venv and installs the Python packages; the first data
# download (about 1,000 stocks) takes a few minutes, the page fills in by itself.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv and installing packages (first run only)..."
  "$PY" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r backend/requirements.txt
  touch .venv/.installed
elif [ backend/requirements.txt -nt .venv/.installed ]; then
  echo "requirements.txt changed: updating packages..."
  .venv/bin/pip install --quiet -r backend/requirements.txt
  touch .venv/.installed
fi

export PYTHONUNBUFFERED=1
exec .venv/bin/python -m backend.server
