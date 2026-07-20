#!/bin/bash
# ── Sentinel Pipeline — Stop Script ──────────────────────────────────
# Stops the running Sentinel pipeline.
# Usage: bash stop.sh

if [ -f sentinel.pid ]; then
    PID=$(cat sentinel.pid)
    echo "Stopping Sentinel (PID: $PID)..."
    kill $PID 2>/dev/null && echo "[OK] Sentinel stopped." || echo "[WARN] Process not found."
    rm -f sentinel.pid
else
    echo "[WARN] No sentinel.pid file found. Trying to find the process..."
    pkill -f "python main.py" && echo "[OK] Sentinel stopped." || echo "[INFO] Sentinel is not running."
fi
