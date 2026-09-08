#!/usr/bin/env bash
# Linux/macOS launcher – same behaviour as run.bat
cd "$(dirname "$0")"
VENV="${FACILITY_VENV:-.venv}"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || { echo "could not create venv"; exit 1; }
fi
if ! "$VENV/bin/python" -c "import PySide6.QtWidgets, pyqtgraph, numpy, yaml" 2>/dev/null; then
  "$VENV/bin/pip" install -r requirements.txt || { echo "dependency installation failed"; exit 1; }
fi
exec "$VENV/bin/python" run_facility.py "$@"
