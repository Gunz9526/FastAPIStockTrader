#!/bin/sh
# ADR-0069: local, atomic daily PostgreSQL backups. Never logs credentials.
set -eu
umask 077
backup_tmp=
backup_child=
cleanup() {
    if [ -n "$backup_tmp" ]; then rm -f -- "$backup_tmp"; fi
}
stop() {
    if [ -n "$backup_child" ]; then kill "$backup_child" 2>/dev/null || :; fi
    cleanup
    exit 0
}
trap cleanup EXIT
trap stop TERM INT
mkdir -p /backups
chmod 700 /backups
while :; do
    backup_tmp=$(mktemp /backups/.pending.XXXXXXXX)
    backup_suffix=${backup_tmp##*.}
    backup_stamp=$(date -u +%Y%m%dT%H%M%SZ)
    backup_name="paper-db-${backup_stamp}-${backup_suffix}.dump"
    if PGPASSWORD="$(cat /run/secrets/db_password)" pg_dump --host=db --username=trader --dbname=trader --format=custom --no-owner --no-acl --file="$backup_tmp"; then
        test -s "$backup_tmp"
        sync -f "$backup_tmp"
        mv -- "$backup_tmp" "/backups/$backup_name"
        backup_tmp=
        backup_sha=$(sha256sum "/backups/$backup_name")
        backup_sha=${backup_sha%% *}
        backup_size=$(stat -c %s "/backups/$backup_name")
        backup_tmp=$(mktemp /backups/.metadata.XXXXXXXX)
        printf '{"state":"complete","file":"%s","sha256":"%s","bytes":%s,"created_at":"%s"}\n' "$backup_name" "$backup_sha" "$backup_size" "$backup_stamp" > "$backup_tmp"
        sync -f "$backup_tmp"
        mv -- "$backup_tmp" "/backups/$backup_name.json"
        backup_tmp=
        sync -f /backups
        printf '{"event":"database_backup_complete","file":"%s","bytes":%s}\n' "$backup_name" "$backup_size"
        if [ "${1:-}" = "--once" ]; then exit 0; fi
        sleep 86400 & backup_child=$!
        wait "$backup_child" || :
        backup_child=
    else
        cleanup
        backup_tmp=
        printf '{"event":"database_backup_failed"}\n' >&2
        if [ "${1:-}" = "--once" ]; then exit 1; fi
        sleep 300 & backup_child=$!
        wait "$backup_child" || :
        backup_child=
    fi
done
