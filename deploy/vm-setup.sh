#!/usr/bin/env bash
# Run this on the Azure VM after provisioning. Requires root.
# ssh milesadmin@VM_IP 'bash -s' < deploy/vm-setup.sh
set -euo pipefail

echo "==> Miles VM setup"

# ── Format and mount both disks ───────────────────────────────────────────────
echo "[1/5] Mounting data disks..."
# Collect unformatted disks (not sda = OS disk)
UNFORMATTED=$(lsblk -dpno NAME,FSTYPE | awk '$2=="" {print $1}' | grep -v "^/dev/sda")
DISK1=$(echo "$UNFORMATTED" | sed -n '1p')
DISK2=$(echo "$UNFORMATTED" | sed -n '2p')

if [ -z "$DISK1" ]; then
    echo "ERROR: no unformatted disks found. Ensure both data disks are attached."
    exit 1
fi

# Fast SSD — active data (memory, sessions, skills, logs)
mkfs.ext4 -F "$DISK1"
mkdir -p /mnt/miles-data
mount "$DISK1" /mnt/miles-data
echo "$DISK1 /mnt/miles-data ext4 defaults,nofail 0 0" >> /etc/fstab
echo "    $DISK1 → /mnt/miles-data (SSD, active data)"

# Bulk HDD — large files (videos, archives, heavy downloads)
if [ -n "$DISK2" ]; then
    mkfs.ext4 -F "$DISK2"
    mkdir -p /mnt/miles-bulk
    mount "$DISK2" /mnt/miles-bulk
    echo "$DISK2 /mnt/miles-bulk ext4 defaults,nofail 0 0" >> /etc/fstab
    echo "    $DISK2 → /mnt/miles-bulk (HDD, bulk storage)"
else
    echo "    WARNING: second disk not found — bulk storage disabled"
fi

# ── Directory structure ───────────────────────────────────────────────────────
mkdir -p /mnt/miles-data/backend/data
mkdir -p /mnt/miles-bulk
mkdir -p /opt/heso
mkdir -p /opt/miles
echo "[2/5] Directories created"

# ── Docker ───────────────────────────────────────────────────────────────────
echo "[3/5] Installing Docker..."
curl -fsSL https://get.docker.com | sh
CALLING_USER="${SUDO_USER:-milesadmin}"
usermod -aG docker "$CALLING_USER" 2>/dev/null || true
systemctl enable --now docker
echo "    Docker installed"

# ── Move .env to data disk ────────────────────────────────────────────────────
echo "[4/5] Checking for .env..."
if [ -f /tmp/.env ]; then
    cp /tmp/.env /mnt/miles-data/.env
    chmod 600 /mnt/miles-data/.env
    rm /tmp/.env
    echo "    .env installed at /mnt/miles-data/.env"
else
    echo "    WARNING: /tmp/.env not found — copy it before deploying:"
    echo "      scp backend/.env milesadmin@$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):/mnt/miles-data/.env"
fi

# ── Heso codebase ─────────────────────────────────────────────────────────────
echo "[5/5] HESO codebase"
echo "    Sync your heso repo to /opt/heso after deploying:"
echo "      rsync -az /path/to/heso/ milesadmin@VM_IP:/opt/heso/"
echo "    Miles mounts this read-only at /heso inside the container."

echo ""
echo "VM ready. From your local machine, run:"
echo "  ./deploy/update.sh <VM_IP>"
