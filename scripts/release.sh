#!/usr/bin/env bash
# Tag the current commit and push; CI creates the GitHub release.
#
# Prerequisites:
#   - On main, clean tree, commits pushed to origin/main
#   - CHANGELOG.md has a "## X.Y.Z" section (move items out of Unreleased first)
#   - gh CLI authenticated (optional; only needed for local release notes preview)
#
# Usage:
#   scripts/release.sh 0.9.1
#   scripts/release.sh v0.9.1
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'EOF'
Usage: scripts/release.sh <version>

Examples:
  scripts/release.sh 0.9.1
  scripts/release.sh v0.9.1

Before running:
  1. Move CHANGELOG.md "Unreleased" notes into "## X.Y.Z"
  2. Commit and push to origin/main
  3. Run this script from that commit
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

yes=false
if [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]]; then
  yes=true
  shift
fi

if [[ $# -ne 1 ]]; then
  usage
  exit 1
fi

version="${1#v}"
tag="v${version}"

if [[ ! "$version" =~ ^[0-9]+(\.[0-9]+)*(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "Invalid version: $1" >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Release tags must be created on main (current: $(git branch --show-current))." >&2
  exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree is not clean. Commit or stash changes first." >&2
  exit 1
fi

if ! git rev-parse "@{u}" >/dev/null 2>&1; then
  echo "main has no upstream. Push main to origin first." >&2
  exit 1
fi

upstream="$(git rev-parse @{u})"
head="$(git rev-parse HEAD)"
if [[ "$head" != "$upstream" ]]; then
  echo "HEAD is not pushed to $(git rev-parse --abbrev-ref @{u}). Push main first." >&2
  exit 1
fi

if git rev-parse "$tag" >/dev/null 2>&1; then
  echo "Tag $tag already exists." >&2
  exit 1
fi

notes="$("$root/scripts/extract-changelog.sh" "$version")"
if [[ -z "${notes//[[:space:]]/}" ]]; then
  echo "CHANGELOG.md has no section for version $version." >&2
  echo "Add a '## $version' heading with release notes before tagging." >&2
  exit 1
fi

echo "Release notes for $tag:"
echo "------------------------"
printf '%s\n' "$notes"
echo "------------------------"
if [[ "$yes" != true ]]; then
  read -r -p "Create and push tag $tag? [y/N] " confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
  fi
fi

git tag -a "$tag" -m "Release $tag"
git push origin "$tag"

cat <<EOF

Tagged and pushed $tag.
GitHub Actions will run tests, verify the wheel build, and publish the release.

Consumer bump (per repo):
  uv add --group tooling \\
    "catalpa-workspace-tooling @ git+https://github.com/catalpainternational/catalpa-workspace-tooling@${tag}"
  uv sync
EOF
