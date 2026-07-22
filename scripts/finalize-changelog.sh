#!/usr/bin/env bash
# Move CHANGELOG.md "Unreleased" notes into a versioned section.
#
# Usage:
#   scripts/finalize-changelog.sh 0.9.1
#   scripts/finalize-changelog.sh v0.9.1 --write
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
changelog="${CHANGELOG:-$root/CHANGELOG.md}"

usage() {
  cat <<'EOF'
Usage: scripts/finalize-changelog.sh <version> [--write]

Renames the current "## Unreleased" section to "## X.Y.Z" and inserts an empty
"## Unreleased" placeholder above it.

Without --write, prints a preview of that section only (dry run).
With --write, updates CHANGELOG.md in place.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 1
fi

version="${1#v}"
write=false
if [[ "${2:-}" == "--write" ]]; then
  write=true
elif [[ -n "${2:-}" ]]; then
  echo "Unknown option: $2" >&2
  usage
  exit 1
fi

if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "Invalid version: $1" >&2
  exit 1
fi

if [[ ! -f "$changelog" ]]; then
  echo "Missing $changelog" >&2
  exit 1
fi

if grep -q "^## ${version}$" "$changelog"; then
  echo "CHANGELOG.md already has section ## ${version}" >&2
  exit 1
fi

updated="$(awk -v version="$version" '
  BEGIN { in_unreleased = 0; unreleased_lines = 0 }
  /^## Unreleased$/ {
    print
    print ""
    print "## " version
    in_unreleased = 1
    next
  }
  in_unreleased && /^## / {
    in_unreleased = 0
  }
  {
    if (in_unreleased) {
      unreleased_lines++
    }
    print
  }
  END {
    if (unreleased_lines == 0) {
      exit 2
    }
  }
' "$changelog")" || {
  status=$?
  if [[ $status -eq 2 ]]; then
    echo "No content under ## Unreleased to finalize." >&2
  fi
  exit "$status"
}

if [[ "$write" == true ]]; then
  printf '%s\n' "$updated" >"$changelog"
  echo "Updated $changelog: moved Unreleased notes to ## ${version}"
else
  # Preview only Unreleased + the new version section (not the whole file).
  printf '%s\n' "$updated" | awk -v version="$version" '
    /^## Unreleased$/ { capture = 1 }
    capture && /^## / && $0 != "## Unreleased" && seen_version { exit }
    capture {
      if (/^## / && $0 != "## Unreleased") seen_version = 1
      print
    }
  '
  echo >&2
  echo "Dry run only (preview above). Re-run with --write to update $changelog." >&2
fi
