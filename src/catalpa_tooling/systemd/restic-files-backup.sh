#!/usr/bin/env bash
# Run restic backup of django_media (plus RESTIC_FILES_EXTRA_VOLUMES) via docker (see README_RESTIC.md).
# For deploy hosts / systemd — not intended for local development use.
set -euo pipefail

: "${COMPOSE_PROJECT_NAME:?set COMPOSE_PROJECT_NAME to match compose stack (e.g. in EnvironmentFile)}"
: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY (restic repo URL)}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD}"

DATA_VOLUME="${RESTIC_FILES_DATA_VOLUME:-django_media}"
VOL="${COMPOSE_PROJECT_NAME}_${DATA_VOLUME}"
IMAGE="${RESTIC_IMAGE:-restic/restic:0.17.3}"
MOUNT="${RESTIC_FILES_BACKUP_PATH:-/backup/${DATA_VOLUME}}"

# Other compose volume keys (space-separated), each mounted at /backup/<key> in the same
# snapshot. A missing one is left out (docker run -v would create it empty) and the run
# exits non-zero afterwards so the timer shows the gap.
VOLUME_ARGS=(-v "${VOL}:${MOUNT}:ro")
BACKUP_PATHS=("${MOUNT}")
MISSING=()
# shellcheck disable=SC2086  # word-split the space-separated list
for _key in ${RESTIC_FILES_EXTRA_VOLUMES:-}; do
  _vol="${COMPOSE_PROJECT_NAME}_${_key}"
  if docker volume inspect "${_vol}" >/dev/null 2>&1; then
    VOLUME_ARGS+=(-v "${_vol}:/backup/${_key}:ro")
    BACKUP_PATHS+=("/backup/${_key}")
  else
    echo "restic-files-backup: volume ${_vol} does not exist; backing up the others without it." >&2
    MISSING+=("${_vol}")
  fi
done
DC_BACKUP_CA_CONTAINER_PATH="/etc/ssl/dc-backup-ca/ca.crt"

# restic reads AWS_* inside the container; host can set RESTIC_S3_* (or legacy AWS_*).
EXTRA_ENV=()
AWS_ACCESS_KEY_ID="${RESTIC_S3_ACCESS_KEY_ID:-${AWS_ACCESS_KEY_ID:-}}"
AWS_SECRET_ACCESS_KEY="${RESTIC_S3_SECRET_ACCESS_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
AWS_DEFAULT_REGION="${RESTIC_S3_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-}}"
AWS_SESSION_TOKEN="${RESTIC_S3_SESSION_TOKEN:-${AWS_SESSION_TOKEN:-}}"
if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]; then EXTRA_ENV+=(-e "AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}"); fi
if [[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]]; then EXTRA_ENV+=(-e "AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}"); fi
if [[ -n "${AWS_DEFAULT_REGION:-}" ]]; then EXTRA_ENV+=(-e "AWS_DEFAULT_REGION=${AWS_DEFAULT_REGION}"); fi
if [[ -n "${AWS_SESSION_TOKEN:-}" ]]; then EXTRA_ENV+=(-e "AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN}"); fi

# Optional DOCKER_ADD_HOST=name:ip[,name:ip…] and DC_BACKUP_CA_FILE=/abs/path/on/host
EXTRA_HOSTS=()
if [[ -n "${DOCKER_ADD_HOST:-}" ]]; then
  # shellcheck disable=SC2206
  IFS=', ' read -r -a _add_host_parts <<< "${DOCKER_ADD_HOST}"
  for _ent in "${_add_host_parts[@]}"; do
    [[ -z "${_ent}" ]] && continue
    EXTRA_HOSTS+=(--add-host "${_ent}")
  done
fi
EXTRA_CA=()
if [[ -n "${DC_BACKUP_CA_FILE:-}" ]]; then
  EXTRA_CA+=(-v "${DC_BACKUP_CA_FILE}:${DC_BACKUP_CA_CONTAINER_PATH}:ro")
  EXTRA_ENV+=(-e "AWS_CA_BUNDLE=${DC_BACKUP_CA_CONTAINER_PATH}")
  RESTIC_CACERT=(--cacert "${DC_BACKUP_CA_CONTAINER_PATH}")
else
  RESTIC_CACERT=()
fi

docker run --rm --platform linux/amd64 \
  -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
  "${EXTRA_ENV[@]}" \
  "${EXTRA_HOSTS[@]}" \
  "${EXTRA_CA[@]}" \
  "${VOLUME_ARGS[@]}" \
  "${IMAGE}" \
  "${RESTIC_CACERT[@]}" \
  backup "${BACKUP_PATHS[@]}"

if (( ${#MISSING[@]} > 0 )); then
  echo "restic-files-backup: missing volume(s): ${MISSING[*]}" >&2
  exit 1
fi
