#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"

echo "==> Heso CEO — Miles"
echo ""

cd "$BACKEND"

if command -v uv &>/dev/null; then
    echo "[setup] Syncing deps..."
    uv sync --quiet
    echo "[setup] Installing Playwright browsers..."
    uv run playwright install chromium --quiet 2>/dev/null || true
    echo "[backend] Starting on :8000..."
    exec uv run uvicorn server:app --host 0.0.0.0 --port 8000 --reload
else
    pip install -r requirements.txt --quiet 2>/dev/null || true
    exec uvicorn server:app --host 0.0.0.0 --port 8000 --reload
fi
