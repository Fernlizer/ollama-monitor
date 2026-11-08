#!/bin/bash
# Ollama Monitor — start script
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo ">> สร้าง virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

PORT="${PORT:-9020}"
echo ">> Ollama Monitor กำลังทำงานที่ http://localhost:${PORT}"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
