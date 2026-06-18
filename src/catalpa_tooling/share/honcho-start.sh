#!/usr/bin/env bash
# catalpa-workspace-tooling — Honcho supervisor with signal cleanup and port freeing.
# Usage: honcho-start.sh <procfile> [port ...]
# Run from the application repo root (``uv run native start``).

set -euo pipefail

PROCFILE="${1:?procfile path required}"
shift
PORTS=("$@")

HONCHO_PID=""

free_ports() {
	local port pids
	for port in "${PORTS[@]}"; do
		[[ -z "${port}" ]] && continue
		pids=$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)
		if [[ -n "${pids}" ]]; then
			# shellcheck disable=SC2086
			kill -TERM ${pids} 2>/dev/null || true
		fi
	done
	if [[ ${#PORTS[@]} -gt 0 ]]; then
		sleep 0.3
		for port in "${PORTS[@]}"; do
			[[ -z "${port}" ]] && continue
			pids=$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)
			if [[ -n "${pids}" ]]; then
				# shellcheck disable=SC2086
				kill -KILL ${pids} 2>/dev/null || true
			fi
		done
	fi
}

cleanup() {
	if [[ -n "${HONCHO_PID}" ]] && kill -0 "${HONCHO_PID}" 2>/dev/null; then
		pkill -P "${HONCHO_PID}" 2>/dev/null || true
		kill -TERM "${HONCHO_PID}" 2>/dev/null || true
		wait "${HONCHO_PID}" 2>/dev/null || true
	fi
	free_ports
}

trap cleanup EXIT INT TERM HUP

uv run honcho start -f "${PROCFILE}" &
HONCHO_PID=$!
wait "${HONCHO_PID}"
