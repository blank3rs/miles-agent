#!/usr/bin/env bash
# Daily off-box backup of Miles's learned state -> geo-redundant Azure Blob.
# Runs ON the VM (cron). No secrets in this file: reads BACKUP_BLOB_BASE + BACKUP_BLOB_SAS
# (a write-scoped container SAS) from /mnt/miles-data/.env. Uploads two blobs per run:
#   miles.db.<ts>            the source of truth (small) — restores Miles's whole mind
#   miles-state-<ts>.tar.gz  everything else (graph, memory, dreams, secrets, heso identity)
# The miles.db copy is a consistent online sqlite backup (WAL-safe), not a raw cp.
set -euo pipefail

DATA=/mnt/miles-data
WORK="$DATA/backups"
TS="$(date -u +%Y%m%d-%H%M%S)"
KEEP_DAYS=7
FALKOR_CONTAINER="${FALKOR_CONTAINER:-deploy-falkordb-1}"
mkdir -p "$WORK"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# Credentials (write-scoped container SAS) live only in the private .env on the volume.
BACKUP_BLOB_BASE="$(grep -m1 '^BACKUP_BLOB_BASE=' "$DATA/.env" | cut -d= -f2-)"
BACKUP_BLOB_SAS="$(grep -m1 '^BACKUP_BLOB_SAS=' "$DATA/.env" | cut -d= -f2-)"
if [ -z "${BACKUP_BLOB_BASE:-}" ] || [ -z "${BACKUP_BLOB_SAS:-}" ]; then
  log "ERROR: BACKUP_BLOB_BASE / BACKUP_BLOB_SAS not set in $DATA/.env — aborting"
  exit 1
fi

put() {  # put <localfile> <blobname>
  curl -fsS -X PUT \
    -H "x-ms-blob-type: BlockBlob" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @"$1" \
    "$BACKUP_BLOB_BASE/$2?$BACKUP_BLOB_SAS" >/dev/null
}

# 1. Consistent miles.db (online backup; safe while Miles is writing under WAL).
DB_SNAP="$WORK/miles.db.$TS"
python3 - "$DATA/miles.db" "$DB_SNAP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
dst.close(); src.close()
PY
log "miles.db snapshot ok ($(du -h "$DB_SNAP" | cut -f1))"

# 2. Flush the FalkorDB graph to dump.rdb so the tar captures a current copy.
docker exec "$FALKOR_CONTAINER" redis-cli SAVE >/dev/null 2>&1 || log "warn: falkordb SAVE skipped"

# 3. Tar the rest of the durable state (lean: skip bulky/regenerable trees + the live db).
TAR="$WORK/miles-state-$TS.tar.gz"
tar -czf "$TAR" -C "$DATA" \
  --exclude=backups --exclude=photos --exclude=screenshots --exclude=logs \
  --exclude=browser --exclude=bin --exclude=lost+found \
  --exclude='miles.db' --exclude='miles.db-wal' --exclude='miles.db-shm' \
  . 2>/dev/null || true
log "state tarball ok ($(du -h "$TAR" | cut -f1))"

# 4. Upload both blobs (geo-redundant container).
put "$DB_SNAP" "miles.db.$TS"
put "$TAR" "miles-state-$TS.tar.gz"
log "uploaded miles.db.$TS + miles-state-$TS.tar.gz"

# 5. Prune local copies (cloud retention is handled by the blob lifecycle policy).
find "$WORK" -maxdepth 1 -name 'miles.db.*' -mtime +$KEEP_DAYS -delete 2>/dev/null || true
find "$WORK" -maxdepth 1 -name 'miles-state-*.tar.gz' -mtime +$KEEP_DAYS -delete 2>/dev/null || true
log "backup complete"
