#!/usr/bin/env bash
# catalpa-workspace-tooling — npm install + npm run in a directory (nvm when .nvmrc present).
# Source from project scripts: source "$(uv run python -c 'from catalpa_tooling.script_assets import npm_run_helper_path; print(npm_run_helper_path())')"

set -euo pipefail

npm_run_in_dir() {
	local dir="$1"
	local script="$2"
	local install="${3:-1}"

	if [[ ! -d "$dir" ]]; then
		echo "npm_run_in_dir: not a directory: ${dir}" >&2
		return 1
	fi

	cd "$dir"

	if [[ -f .nvmrc && -f "${HOME}/.nvm/nvm.sh" ]]; then
		# shellcheck source=/dev/null
		source "${HOME}/.nvm/nvm.sh"
		nvm use
	fi

	if [[ "$install" == "1" ]]; then
		npm install
	fi

	npm run "$script"
}
