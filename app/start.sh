#!/usr/bin/env sh
# Démarre la plateforme susi-bdc sur le port 8760.
set -e
RACINE="$(cd "$(dirname "$0")/.." && pwd)"

if [ -x "$RACINE/.venv/Scripts/python.exe" ]; then
  PY="$RACINE/.venv/Scripts/python.exe"          # Windows
elif [ -x "$RACINE/.venv/bin/python" ]; then
  PY="$RACINE/.venv/bin/python"                  # macOS / Linux
else
  python3 -m venv "$RACINE/.venv"
  PY="$(ls "$RACINE/.venv/bin/python" 2>/dev/null || echo "$RACINE/.venv/Scripts/python.exe")"
  "$PY" -m pip install --progress-bar off -r "$RACINE/app/requirements.txt"
fi

cd "$RACINE"
exec "$PY" -m uvicorn app.app:app --host 0.0.0.0 --port 8760
