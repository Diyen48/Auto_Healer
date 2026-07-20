#!/bin/bash
# ── Sentinel Pipeline — Start Script ─────────────────────────────────
# Starts the Sentinel pipeline (API + Worker) in the background.
# Usage: bash start.sh

set -e

# Load environment
export PATH="$HOME/.local/bin:$PATH"

echo "============================================================"
echo "  Starting Sentinel Pipeline..."
echo "============================================================"

# Check if Redis is running
if ! docker ps | grep -q sentinel-redis; then
    echo "[WARN] Redis not running. Starting Redis..."
    docker start sentinel-redis 2>/dev/null || \
    docker run -d \
        --name sentinel-redis \
        --restart=always \
        -p 6379:6379 \
        redis:7-alpine \
        redis-server --appendonly yes
fi

echo "[OK] Redis is running."

# Check if .env exists
if [ ! -f .env ]; then
    echo "[ERROR] .env file not found! Copy .env.example and fill in your values:"
    echo "        cp .env.example .env && nano .env"
    exit 1
fi

# Start the pipeline using nohup (keeps running after you close SSH)
echo "[START] Launching Sentinel API + Worker..."
nohup uv run python main.py > sentinel.log 2>&1 &
SENTINEL_PID=$!
echo $SENTINEL_PID > sentinel.pid

echo ""
echo "============================================================"
echo "  Sentinel is running!"
echo "============================================================"
echo "  PID:      $SENTINEL_PID"
echo "  API:      http://$(curl -s ifconfig.me):8000"
echo "  Logs:     tail -f sentinel.log"
echo "  Stop:     bash stop.sh"
echo "============================================================"
