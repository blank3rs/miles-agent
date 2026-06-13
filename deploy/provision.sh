#!/usr/bin/env bash
# Provision the Miles Azure VM — run once from your local machine.
# Requires: az CLI, authenticated with `az login`
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-heso-ceo}"
LOCATION="${LOCATION:-eastus}"
VM_NAME="miles-vm"
VM_SIZE="Standard_D8ds_v7"   # 8 vCPU, 32GB RAM ~$370/mo — comfortable for Playwright + FalkorDB + Graphiti
ADMIN_USER="milesadmin"
DATA_DISK_NAME="miles-data"
DATA_DISK_GB="${DATA_DISK_GB:-512}"
BULK_DISK_NAME="miles-bulk"
BULK_DISK_GB="${BULK_DISK_GB:-2048}"  # 2TB Standard HDD ~$17/mo for large files  # Premium SSD P20 ~$36/mo

echo "==> Provisioning Miles on Azure"
printf "    Resource group: %s\n" "$RESOURCE_GROUP"
printf "    Location:       %s\n" "$LOCATION"
printf "    VM size:        %s (~\$185/mo)\n" "$VM_SIZE"
printf "    Data disk:      %dGB Premium SSD (~\$36/mo)\n" "$DATA_DISK_GB"
printf "    Bulk disk:      %dGB Standard HDD (~\$17/mo)\n" "$BULK_DISK_GB"
echo ""

# ── Resource group ────────────────────────────────────────────────────────────
az group create \
    --name "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --output none
echo "[1/5] Resource group: $RESOURCE_GROUP"

# ── VM ────────────────────────────────────────────────────────────────────────
az vm create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --image Ubuntu2204 \
    --size "$VM_SIZE" \
    --admin-username "$ADMIN_USER" \
    --generate-ssh-keys \
    --public-ip-sku Standard \
    --os-disk-size-gb 64 \
    --output none
echo "[2/5] VM created ($VM_SIZE, Ubuntu 22.04)"

# ── Data disk — 512GB Premium SSD ─────────────────────────────────────────────
az vm disk attach \
    --resource-group "$RESOURCE_GROUP" \
    --vm-name "$VM_NAME" \
    --name "$DATA_DISK_NAME" \
    --size-gb "$DATA_DISK_GB" \
    --sku Premium_LRS \
    --new \
    --output none
echo "[3/5] Data disk attached (${DATA_DISK_GB}GB Premium SSD)"

# ── Bulk disk: 2TB Standard HDD for large files ──────────────────────────────
az vm disk attach \
    --resource-group "$RESOURCE_GROUP" \
    --vm-name "$VM_NAME" \
    --name "$BULK_DISK_NAME" \
    --size-gb "$BULK_DISK_GB" \
    --sku Standard_LRS \
    --new \
    --output none
echo "[4/6] Bulk disk attached (${BULK_DISK_GB}GB Standard HDD)"

# ── Network: no inbound ports needed ─────────────────────────────────────────
# Miles communicates exclusively via email (IMAP poll + SMTP send).
# Both are outbound connections — no ports need to be opened on the VM.
echo "[5/6] No inbound ports needed (email-only)"

# ── Output ────────────────────────────────────────────────────────────────────
VM_IP=$(az vm show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --show-details \
    --query publicIps \
    --output tsv)
echo "[6/6] Done"
echo ""
echo "─────────────────────────────────────────"
echo "VM IP:  $VM_IP"
echo "SSH:    ssh $ADMIN_USER@$VM_IP"
echo ""
echo "Next steps:"
echo "  1. Copy .env to VM:"
echo "       scp backend/.env $ADMIN_USER@$VM_IP:/tmp/.env"
echo "  2. SSH in and run vm-setup.sh:"
echo "       ssh $ADMIN_USER@$VM_IP 'bash -s' < deploy/vm-setup.sh"
echo "  3. Deploy Miles:"
echo "       VM_IP=$VM_IP ./deploy/update.sh $VM_IP"
echo "─────────────────────────────────────────"
