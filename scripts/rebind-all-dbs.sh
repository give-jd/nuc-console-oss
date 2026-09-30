#!/usr/bin/env bash
# Recreates databases started with `docker run` with the port bound only to 127.0.0.1, one at a time, with a volume
# backup and data verification (see scripts/rebind-db-localhost.py: it refuses what it cannot recreate faithfully and, if a
# step fails after the stop, restores the original container).
#   ./scripts/rebind-all-dbs.sh [--dry-run] "NAME HOST_PORT CONTAINER_PORT" ["NAME ..."] ...
# Example: ./scripts/rebind-all-dbs.sh --dry-run "my-redis 6379 6379" "my-pg 5432 5432"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="${BACKUP:-$HOME/backups/db-rebind-$(date +%F)}"
[[ "$BACKUP" = /* ]] || { echo "BACKUP must be an absolute path: $BACKUP" >&2; exit 1; }
DRY=0
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
# each argument: "name  host_port  container_port" (no default: container names are yours)
[ "$#" -gt 0 ] || { echo "usage: $0 [--dry-run] \"NAME HOST_PORT CONTAINER_PORT\" ..." >&2; exit 1; }
DBS=("$@")
for spec in "${DBS[@]}"; do
    # shellcheck disable=SC2086
    set -- $spec
    if [ "$DRY" = 1 ]; then echo "python3 $HERE/rebind-db-localhost.py $1 $2 $3 $BACKUP"; continue; fi
    echo "############ $1"
    rc=0
    python3 "$HERE/rebind-db-localhost.py" "$1" "$2" "$3" "$BACKUP" || rc=$?
    case "$rc" in
        0) ;;
        2) echo "-- $1 refused (untouched): moving on to the next one" >&2 ;;
        *) echo "!!! $1 failed (rc=$rc): stopping. The script tried to restore the original container: check with 'docker ps -a'" >&2; exit 1 ;;
    esac
done
[ "$DRY" = 1 ] || echo "done. Backup in $BACKUP; check the state with 'docker ps'"
