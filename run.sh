#!/usr/bin/env bash
# Start the screener on this computer: http://localhost:8000
#   ./run.sh               (Ctrl+C to stop)
#   PORT=8080 ./run.sh     another port
#   PYTHON=python3.12 ./run.sh   use a specific Python
#   ./run.sh --setup       only create .venv and install packages
# The first run creates .venv and installs the Python packages; the first data
# download (about 1,000 stocks) takes a few minutes, the page fills in by itself.
set -euo pipefail
cd "$(dirname "$0")"

# pandas / numpy ship ready-made packages only for these Python versions; on any
# other version pip tries to compile them from source, which usually fails.
SUPPORTED="3.13 3.12 3.11 3.10 3.14"

FIX="Install Python 3.13 from https://www.python.org/downloads/macos/ (the macOS installer), then run ./run.sh again."

py_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }
supported() { [[ " $SUPPORTED " == *" $1 "* ]]; }

pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  for v in $SUPPORTED; do
    for p in "python$v" "/usr/local/bin/python$v" "/Library/Frameworks/Python.framework/Versions/$v/bin/python$v" \
             "/opt/homebrew/bin/python$v" "$HOME/.local/bin/python$v"; do
      if command -v "$p" >/dev/null 2>&1; then echo "$p"; return; fi
    done
  done
  echo python3
}

# an existing .venv on an unsupported Python (e.g. from a failed first run) is rebuilt
if [ -x .venv/bin/python ] && ! supported "$(py_version .venv/bin/python)"; then
  echo "Existing .venv uses Python $(py_version .venv/bin/python), which pandas doesn't support: rebuilding it."
  rm -rf .venv
fi

if [ ! -f .venv/.installed ] || [ backend/requirements.txt -nt .venv/.installed ]; then
  if [ ! -x .venv/bin/python ]; then
    PY="$(pick_python)"
    VER="$(py_version "$PY")"
    if [ -z "$VER" ]; then
      echo "Python not found ($PY). $FIX" >&2; exit 1
    fi
    if ! supported "$VER"; then
      echo "Python $VER ($PY) isn't supported by pandas yet; it needs one of: $SUPPORTED." >&2
      echo "$FIX" >&2
      exit 1
    fi
    echo "Creating .venv with Python $VER ($PY)..."
    "$PY" -m venv .venv
  fi
  echo "Installing packages..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  # binary packages only: a clear error instead of a long failed compile
  .venv/bin/python -m pip install --quiet --upgrade --only-binary=pandas,numpy,lxml -r backend/requirements.txt
  touch .venv/.installed
fi

[ "${1:-}" = "--setup" ] && { echo "Setup done."; exit 0; }

export PYTHONUNBUFFERED=1
exec .venv/bin/python -m backend.server
