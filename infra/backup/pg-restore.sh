#!/bin/bash
# Restore a Retriva PostgreSQL backup produced by pg-backup.sh.
#
# By default restores into a SCRATCH database (retriva_restore_test), never
# directly over the live database - this is deliberate: proving a backup
# is restorable is the whole point of doing this at all (see
# docs/aws-deployment.md "PostgreSQL backups" - "backup + restore proof",
# not merely "the backup command exists"), and that proof should never
# risk the real data it's meant to protect. Pass --target=<db-name> to
# restore somewhere else, or --target=REPLACE-LIVE-DATABASE to
# (deliberately verbosely) opt into overwriting the actual `nexus`
# database - never invoked automatically by anything in this repo.
set -euo pipefail

BACKUP_FILE="${1:?Usage: pg-restore.sh <backup-file> [--target=<db-name>]}"
KEY_FILE="/opt/retriva/backup.key"
CONTAINER="retriva-postgres-1"
DB_USER="${POSTGRES_USER:-nexus}"

TARGET="retriva_restore_test"
for arg in "$@"; do
    case "$arg" in
        --target=*) TARGET="${arg#--target=}" ;;
    esac
done

if [ ! -f "$BACKUP_FILE" ]; then
    echo "FATAL: backup file not found: $BACKUP_FILE" >&2
    exit 1
fi
if [ ! -f "$KEY_FILE" ]; then
    echo "FATAL: backup key file $KEY_FILE does not exist - cannot decrypt." >&2
    exit 1
fi

if [ "$TARGET" = "nexus" ] || [ "$TARGET" = "retriva" ]; then
    echo "FATAL: refusing to restore directly over what looks like the live database name ('$TARGET')." >&2
    echo "Use --target=REPLACE-LIVE-DATABASE to explicitly opt into that (never automated)." >&2
    exit 1
fi
if [ "$TARGET" = "REPLACE-LIVE-DATABASE" ]; then
    TARGET="${POSTGRES_DB:-nexus}"
    echo "WARNING: restoring over the live database '$TARGET' in 5 seconds - Ctrl+C to abort." >&2
    sleep 5
    docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET\";"
fi

docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -tc \
    "SELECT 1 FROM pg_database WHERE datname = '$TARGET'" | grep -q 1 \
    || docker exec "$CONTAINER" psql -U "$DB_USER" -d postgres -c "CREATE DATABASE \"$TARGET\";"

# pgvector's extension must exist in the target database before any table
# using the vector type can be restored into it - the live database has it
# because Alembic's earliest migration creates it; a fresh scratch database
# does not.
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$TARGET" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

openssl enc -d -aes-256-cbc -pbkdf2 -pass file:"$KEY_FILE" -in "$BACKUP_FILE" \
    | gunzip \
    | docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$TARGET" -v ON_ERROR_STOP=1 -q

echo "$(date -u +%FT%TZ) restore complete into database '$TARGET'"
echo "Verify with: docker exec $CONTAINER psql -U $DB_USER -d $TARGET -c '\dt'"
