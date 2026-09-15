#!/bin/bash
# Retriva PostgreSQL backup - single-instance, encrypted, local (no S3: the
# IAM credentials for this deployment have no s3:* permission, and the
# spec's own fallback for exactly that situation is a local encrypted
# strategy - see docs/aws-deployment.md "PostgreSQL backups").
#
# Runs `pg_dump` INSIDE the postgres container (via docker exec), so the
# database port is never published to the host or network for this -
# nothing changes about docker-compose.prod.yml's "no host port for
# postgres" posture. Output is piped straight through openssl encryption
# before ever touching disk unencrypted.
set -euo pipefail

BACKUP_DIR="/opt/retriva/backups"
KEY_FILE="/opt/retriva/backup.key"
RETENTION_DAYS=7
CONTAINER="retriva-postgres-1"
DB_NAME="${POSTGRES_DB:-nexus}"
DB_USER="${POSTGRES_USER:-nexus}"

if [ ! -f "$KEY_FILE" ]; then
    echo "FATAL: backup key file $KEY_FILE does not exist - refusing to write an unencrypted backup." >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_FILE="$BACKUP_DIR/retriva-${TIMESTAMP}.sql.gz.enc"

# --format=plain piped through gzip then openssl, rather than pg_dump's own
# --format=custom, so `restore` is a plain, auditable "decrypt | gunzip |
# psql" pipeline with no pg_restore-specific format assumptions.
docker exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" \
    | gzip \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:"$KEY_FILE" \
    > "$OUT_FILE"

chmod 600 "$OUT_FILE"
echo "$(date -u +%FT%TZ) backup written: $OUT_FILE ($(du -h "$OUT_FILE" | cut -f1))"

# Retention: delete backups older than RETENTION_DAYS. Never deletes the
# backup this run just created (find's -mtime is relative to file age, not
# a count, so a fresh file is always exempt).
find "$BACKUP_DIR" -name 'retriva-*.sql.gz.enc' -mtime "+${RETENTION_DAYS}" -print -delete
