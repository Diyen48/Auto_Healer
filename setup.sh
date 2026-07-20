#!/bin/bash
# ── Sentinel Pipeline — Deployment Setup Script ──────────────────────
# Run this script ONCE on a fresh Ubuntu VM to install all dependencies.
# Usage: bash setup.sh

set -e  # Stop on any error

echo "============================================================"
echo "  SENTINEL — Auto-Remediation Pipeline Setup"
echo "============================================================"

# ── 1. System Updates ────────────────────────────────────────────────
echo "[1/6] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# ── 2. Install Docker ───────────────────────────────────────────────
echo "[2/6] Installing Docker..."
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker $USER
echo "[OK] Docker installed."

# ── 3. Install Python 3.13 ──────────────────────────────────────────
echo "[3/6] Installing Python 3.13..."
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.13 python3.13-venv python3.13-dev
echo "[OK] Python 3.13 installed."

# ── 4. Install uv (Fast Python Package Manager) ─────────────────────
echo "[4/6] Installing uv package manager..."
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
echo "[OK] uv installed."

# ── 5. Install Python Dependencies ──────────────────────────────────
echo "[5/6] Installing Python dependencies..."
uv sync
echo "[OK] Dependencies installed."

# ── 6. Start Redis Container ────────────────────────────────────────
echo "[6/6] Starting Redis container..."
sudo docker run -d \
    --name sentinel-redis \
    --restart=always \
    -p 6379:6379 \
    redis:7-alpine \
    redis-server --appendonly yes
echo "[OK] Redis running on port 6379."

# ── 7. Build Sandbox Docker Image ───────────────────────────────────
echo "[BONUS] Building sandbox Docker image..."
sudo docker build -t sentinel-sandbox:latest -f docker/Dockerfile.sandbox docker/
echo "[OK] Sandbox image built."

echo ""
echo "============================================================"
echo "  SETUP COMPLETE!"
echo "============================================================"
echo ""
echo "  Next steps:"
echo "  1. Edit your .env file:  nano .env"
echo "  2. Start the pipeline:  bash start.sh"
echo ""
echo "  NOTE: Log out and log back in for Docker group to take effect."
echo "        OR run: newgrp docker"
echo "============================================================"
