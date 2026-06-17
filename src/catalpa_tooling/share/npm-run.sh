#!/usr/bin/env bash
# catalpa-workspace-tooling — npm install + npm run in a directory (nvm when .nvmrc present).
# Source from project scripts: source "$(uv run python -c 'from catalpa_tooling.script_assets import npm_run_helper_path; print(npm_run_helper_path())')"

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=package-run.sh
source "${_SCRIPT_DIR}/package-run.sh"

npm_run_in_dir() {
	local dir="$1"
	local script="$2"
	local install="${3:-1}"
	package_run_in_dir "$dir" "$script" "$install" npm
}
