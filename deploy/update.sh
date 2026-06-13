#!/usr/bin/env bash
# Deploy or update Miles on the Azure VM. Run from repo root.
# Usage: ./deploy/update.sh <VM_IP>
set -euo pipefail

VM_IP="${1:?Usage: ./deploy/update.sh <VM_IP>}"
VM_USER="${VM_USER:-milesadmin}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Deploying Miles to $VM_USER@$VM_IP"

# ── Sync code ─────────────────────────────────────────────────────────────────
echo "[1/3] Syncing code..."
rsync -az --delete \
    --exclude='backend/data/' \
    --exclude='backend/.env' \
    --exclude='backend/.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='*.egg-info/' \
    --exclude='frontend/' \
    --exclude='.git/' \
    "$REPO_ROOT/" \
    "$VM_USER@$VM_IP:/opt/miles/"

# ── Build image ───────────────────────────────────────────────────────────────
echo "[2/3] Building Docker image..."
ssh "$VM_USER@$VM_IP" \
    "cd /opt/miles && docker compose -f deploy/docker-compose.yml build --pull"

# ── Restart ───────────────────────────────────────────────────────────────────
echo "[3/3] Restarting Miles..."
ssh "$VM_USER@$VM_IP" \
    "cd /opt/miles && docker compose -f deploy/docker-compose.yml up -d"

echo ""
echo "Miles is live at http://$VM_IP:8000"
echo "Status: http://$VM_IP:8000/status"
echo "Logs:   ssh $VM_USER@$VM_IP 'docker logs -f deploy-miles-1'"
