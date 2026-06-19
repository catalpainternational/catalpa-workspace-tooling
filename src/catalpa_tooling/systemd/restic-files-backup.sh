#!/usr/bin/env bash
# Run restic backup of django_media via docker (see README_RESTIC.md).
# For deploy hosts / systemd — not intended for local development use.
set -euo pipefail

: "${COMPOSE_PROJECT_NAME:?set COMPOSE_PROJECT_NAME to match compose stack (e.g. in EnvironmentFile)}"
: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY (restic repo URL)}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD}"

DATA_VOLUME="${RESTIC_FILES_DATA_VOLUME:-django_media}"
VOL="${COMPOSE_PROJECT_NAME}_${DATA_VOLUME}"
IMAGE="${RESTIC_IMAGE:-restic/restic:0.17.3}"
MOUNT="${RESTIC_FILES_BACKUP_PATH:-/backup/${DATA_VOLUME}}"

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

exec docker run --rm --platform linux/amd64 \
  -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
  "${EXTRA_ENV[@]}" \
  -v "${VOL}:${MOUNT}:ro" \
  "${IMAGE}" \
  backup "${MOUNT}"
