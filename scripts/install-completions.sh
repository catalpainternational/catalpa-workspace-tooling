#!/usr/bin/env bash
# Register shell completion for catalpa-workspace-tooling CLIs.
# Requires: pip install 'catalpa-workspace-tooling[completion]' (or uv sync --extra completion)
set -euo pipefail

if ! command -v register-python-argcomplete >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1 && uv run register-python-argcomplete --help >/dev/null 2>&1; then
    REGISTER=(uv run register-python-argcomplete)
  else
    echo "register-python-argcomplete not found. Install argcomplete:" >&2
    echo "  uv add --group tooling 'catalpa-workspace-tooling[completion]'" >&2
    echo "  # then: eval \"\$(uv run register-python-argcomplete dk)\"" >&2
    exit 1
  fi
else
  REGISTER=(register-python-argcomplete)
fi

if [[ -n "${ZSH_VERSION-}" ]]; then
  SHELL_FLAG=(-s zsh)
else
  SHELL_FLAG=(-s bash)
fi

for cmd in native local dev dk tests test scripts; do
  if command -v "$cmd" >/dev/null 2>&1 || { command -v uv >/dev/null 2>&1 && uv run "$cmd" --help >/dev/null 2>&1; }; then
    eval "$("${REGISTER[@]}" "${SHELL_FLAG[@]}" "$cmd")"
    echo "Registered completion for: $cmd"
  else
    echo "Skipping $cmd (not on PATH and 'uv run $cmd' failed)" >&2
  fi
done

cat <<'EOF'
Add the eval lines above to ~/.bashrc or ~/.zshrc (after compinit for zsh).

For zsh with multiple tooling repos, prefer direnv + `uv run setup-shell`
(see README “Shell completion” → Multi-project direnv flow).

zsh users with `alias dk='uv run dk'`: use `setopt complete_aliases` before the alias,
or replace the alias with direnv PATH_add so `dk` comes from .venv/bin.
Use `-s zsh` when registering (this script does that automatically in zsh).
EOF
