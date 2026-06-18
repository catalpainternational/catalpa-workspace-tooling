#!/usr/bin/env bash
# catalpa-workspace-tooling — install + run a package.json script (npm/yarn/pnpm; nvm when .nvmrc or node_version).
# Source from project scripts: source "$(uv run python -c 'from catalpa_tooling.script_assets import package_run_helper_path; print(package_run_helper_path())')"

set -euo pipefail

_package_manager_install_cmd() {
	local package_manager="$1"
	case "$package_manager" in
	yarn) echo "yarn install" ;;
	pnpm) echo "pnpm install" ;;
	*) echo "npm install" ;;
	esac
}

_package_manager_run_cmd() {
	local package_manager="$1"
	local script="$2"
	case "$package_manager" in
	yarn) echo "yarn run ${script}" ;;
	pnpm) echo "pnpm run ${script}" ;;
	*) echo "npm run ${script}" ;;
	esac
}

_detect_package_manager() {
	local dir="$1"
	if [[ -f "${dir}/package.json" ]]; then
		local pm
		pm="$(node -e '
const fs = require("fs");
const path = process.argv[1];
try {
  const data = JSON.parse(fs.readFileSync(path, "utf8"));
  const field = String(data.packageManager || "").split("@")[0].toLowerCase();
  if (["npm", "yarn", "pnpm"].includes(field)) {
    process.stdout.write(field);
    process.exit(0);
  }
} catch (_) {}
process.exit(1);
' "${dir}/package.json" 2>/dev/null || true)"
		if [[ -n "$pm" ]]; then
			echo "$pm"
			return 0
		fi
	fi
	if [[ -f "${dir}/.yarnrc.yml" || -f "${dir}/yarn.lock" ]]; then
		echo "yarn"
		return 0
	fi
	if [[ -f "${dir}/pnpm-lock.yaml" ]]; then
		echo "pnpm"
		return 0
	fi
	echo "npm"
}

package_run_in_dir() {
	local dir="$1"
	local script="$2"
	local install="${3:-1}"
	local package_manager="${4:-auto}"
	local node_version="${5:-}"

	if [[ ! -d "$dir" ]]; then
		echo "package_run_in_dir: not a directory: ${dir}" >&2
		return 1
	fi

	cd "$dir"

	if [[ "$package_manager" == "auto" ]]; then
		package_manager="$(_detect_package_manager "$dir")"
	fi

	if [[ -f "${HOME}/.nvm/nvm.sh" ]]; then
		# shellcheck source=/dev/null
		source "${HOME}/.nvm/nvm.sh"
		if [[ -f .nvmrc ]]; then
			nvm use
		elif [[ -n "$node_version" ]]; then
			nvm use "$node_version"
		fi
	fi

	if [[ "$install" == "1" ]]; then
		# shellcheck disable=SC2086
		eval "$(_package_manager_install_cmd "$package_manager")"
	fi

	# shellcheck disable=SC2086
	eval "$(_package_manager_run_cmd "$package_manager" "$script")"
}
