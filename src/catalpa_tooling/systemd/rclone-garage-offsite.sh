#!/usr/bin/env bash
# rclone copy: Garage (localhost:3900) → external S3 (additive; no remote deletes).
# Installed on dc_backup_docker_host via `dk <env> dc-backup offsite install`.
set -euo pipefail

: "${GARAGE_BUCKET:?set GARAGE_BUCKET}"
: "${GARAGE_ACCESS_KEY_ID:?set GARAGE_ACCESS_KEY_ID}"
: "${GARAGE_SECRET_ACCESS_KEY:?set GARAGE_SECRET_ACCESS_KEY}"
: "${OFFSITE_S3_BUCKET:?set OFFSITE_S3_BUCKET}"
: "${OFFSITE_S3_ACCESS_KEY_ID:?set OFFSITE_S3_ACCESS_KEY_ID}"
: "${OFFSITE_S3_SECRET_ACCESS_KEY:?set OFFSITE_S3_SECRET_ACCESS_KEY}"

GARAGE_ENDPOINT="${GARAGE_ENDPOINT:-http://127.0.0.1:3900}"
GARAGE_REGION="${GARAGE_REGION:-garage}"
OFFSITE_S3_REGION="${OFFSITE_S3_REGION:-}"
OFFSITE_S3_ENDPOINT="${OFFSITE_S3_ENDPOINT:-}"
OFFSITE_S3_PREFIX="${OFFSITE_S3_PREFIX:-}"
OFFSITE_S3_PROVIDER="${OFFSITE_S3_PROVIDER:-Other}"
RCLONE_IMAGE="${RCLONE_IMAGE:-rclone/rclone:1.68}"

# Dest path: bucket or bucket/prefix (no leading slash on prefix).
DEST_PATH="${OFFSITE_S3_BUCKET}"
if [[ -n "${OFFSITE_S3_PREFIX}" ]]; then
  DEST_PATH="${OFFSITE_S3_BUCKET}/${OFFSITE_S3_PREFIX#/}"
  DEST_PATH="${DEST_PATH%/}"
fi

CONF="$(mktemp)"
trap 'rm -f "${CONF}"' EXIT
umask 077

{
  echo "[garage]"
  echo "type = s3"
  echo "provider = Other"
  echo "env_auth = false"
  echo "access_key_id = ${GARAGE_ACCESS_KEY_ID}"
  echo "secret_access_key = ${GARAGE_SECRET_ACCESS_KEY}"
  echo "endpoint = ${GARAGE_ENDPOINT}"
  echo "region = ${GARAGE_REGION}"
  echo "force_path_style = true"
  echo
  echo "[offsite]"
  echo "type = s3"
  echo "provider = ${OFFSITE_S3_PROVIDER}"
  echo "env_auth = false"
  echo "access_key_id = ${OFFSITE_S3_ACCESS_KEY_ID}"
  echo "secret_access_key = ${OFFSITE_S3_SECRET_ACCESS_KEY}"
  if [[ -n "${OFFSITE_S3_REGION}" ]]; then
    echo "region = ${OFFSITE_S3_REGION}"
  fi
  if [[ -n "${OFFSITE_S3_ENDPOINT}" ]]; then
    echo "endpoint = ${OFFSITE_S3_ENDPOINT}"
  fi
  # Path-style helps Spaces / many non-AWS endpoints.
  echo "force_path_style = true"
} >"${CONF}"

# shellcheck disable=SC2206
EXTRA_ARGS=(--fast-list --retries 5 --low-level-retries 10)
if [[ -n "${RCLONE_EXTRA_ARGS:-}" ]]; then
  # Allow operators to append flags via EnvironmentFile (word-split intentionally).
  # shellcheck disable=SC2206
  EXTRA_ARGS+=(${RCLONE_EXTRA_ARGS})
fi

exec docker run --rm --network host \
  --platform linux/amd64 \
  -v "${CONF}:/config/rclone/rclone.conf:ro" \
  -e RCLONE_CONFIG=/config/rclone/rclone.conf \
  "${RCLONE_IMAGE}" \
  copy "garage:${GARAGE_BUCKET}" "offsite:${DEST_PATH}" \
  "${EXTRA_ARGS[@]}"
