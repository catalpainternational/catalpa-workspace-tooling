#!/usr/bin/env bash
# Print the CHANGELOG.md section for a release version (without the heading).
# Usage: scripts/extract-changelog.sh 0.9.1
set -euo pipefail

version="${1#v}"
changelog="${2:-CHANGELOG.md}"

if [[ ! -f "$changelog" ]]; then
  echo "Missing $changelog" >&2
  exit 1
fi

awk -v version="$version" '
  $0 ~ "^## " version "$" { capture = 1; next }
  capture && /^## / { exit }
  capture { print }
' "$changelog"
