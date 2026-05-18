#!/usr/bin/env bash
# Run pgBackRest online backup inside the Postgres container (see DEPLOY.md).
# Uses `docker exec` — no compose file on the host; set PGBR_DB_CONTAINER to the running db name/ID.
# For deploy hosts / systemd — not intended for local development use.
set -euo pipefail

usage() {
  echo "usage: ${0##*/} full|incr|diff" >&2
  exit 1
}

[[ "${1:-}" ]] || usage
case "$1" in
  full | incr | diff) ;;
  *) usage ;;
esac

TYPE="$1"
CONTAINER="${PGBR_DB_CONTAINER:?set PGBR_DB_CONTAINER to the running Postgres container name or ID (e.g. in EnvironmentFile)}"
STANZA="${PGBR_STANZA:?set PGBR_STANZA to match PGBR_S3_*_STANZA (e.g. in EnvironmentFile)}"

# Optional: tune what reaches journald when run from systemd (stdout/stderr of `docker exec`).
# See pgbackrest --help: e.g. error|warn|info|detail|debug|off
EXTRA=()
if [[ -n "${PGBR_LOG_LEVEL_CONSOLE:-}" ]]; then
  EXTRA+=(--log-level-console="${PGBR_LOG_LEVEL_CONSOLE}")
fi
if [[ -n "${PGBR_LOG_LEVEL_STDERR:-}" ]]; then
  EXTRA+=(--log-level-stderr="${PGBR_LOG_LEVEL_STDERR}")
fi

# Match compose `user: postgres` and pgBackRest paths owned by postgres in the image.
exec docker exec -u postgres "$CONTAINER" \
  pgbackrest "${EXTRA[@]}" --stanza="$STANZA" backup --type="$TYPE"
