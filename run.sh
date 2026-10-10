#!/usr/bin/env bash
# One command to get the demo running. Idempotent - safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d venv ]; then
  echo "==> creating virtualenv"
  "$PY" -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> installing requirements"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f .env ] && [ -f .env.example ]; then
  echo "==> no .env found, creating one from .env.example (app runs without a key)"
  cp .env.example .env
fi

if [ ! -f data/demo.db ]; then
  echo "==> building synthetic database"
  python build_data.py
fi

echo "==> running tool + guardrail tests"
python test_tools.py | tail -n 3

echo "==> starting app on port ${PORT:-7860}"
exec python app.py
