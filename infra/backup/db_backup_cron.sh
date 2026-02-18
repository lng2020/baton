#!/bin/bash
# Automatic hourly database backup

BACKUP_DIR="/workspace/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETAIN_COUNT=${BACKUP_RETAIN_COUNT:-24}

mkdir -p "$BACKUP_DIR"

# PostgreSQL backup
pg_dump -h localhost -U dev app > "$BACKUP_DIR/backup_$TIMESTAMP.sql"

# Clean old backups (keep only the most recent N)
ls -t "$BACKUP_DIR"/backup_*.sql | tail -n +$((RETAIN_COUNT + 1)) | xargs -r rm

echo "Backup complete: backup_$TIMESTAMP.sql"
