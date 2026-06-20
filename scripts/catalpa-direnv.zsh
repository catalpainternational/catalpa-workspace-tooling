# Canonical copy: src/catalpa_tooling/shell/catalpa-direnv.zsh (shipped in the wheel).
# This path is kept for direct links from docs and older checkouts.
#
# Prefer: uv run setup-shell
#
# catalpa-workspace-tooling: register tab completion when direnv loads a tooling repo.
#
# One-time setup (~/.zshrc, after eval "$(direnv hook zsh)"):
#   [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh" ]] && \
#     source "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh"
#
# Per-repo .envrc must export CATALPA_REGISTER_PYTHON_ARGCOMPLETE (see scripts/envrc.template).

_catalpa_direnv_completion() {
  emulate -L zsh
  (( $+functions[compdef] )) || return 0
  local reg="${CATALPA_REGISTER_PYTHON_ARGCOMPLETE:-}"
  if [[ -z "$reg" || ! -x "$reg" ]]; then
    _CATALPA_COMP_DONE=
    return 0
  fi
  # Re-register after `source ~/.zshrc` (compinit resets _comps) or setup-shell reinstall.
  if [[ "${_CATALPA_COMP_DONE:-}" == "$reg" && "${_comps[dk]:-}" == "_python_argcomplete" ]]; then
    return 0
  fi
  local cmd out
  for cmd in dk native local dev test scripts setup-shell; do
    out="$("$reg" -s zsh "$cmd" 2>/dev/null)" || continue
    [[ -n "$out" ]] && eval "$out"
  done
  _CATALPA_COMP_DONE="$reg"
}

_catalpa_direnv_install_hook() {
  emulate -L zsh
  (( $+commands[direnv] )) || return 0
  autoload -Uz add-zsh-hook
  add-zsh-hook -d precmd _catalpa_direnv_completion 2>/dev/null || true
  unset _CATALPA_COMP_DONE
  add-zsh-hook precmd _catalpa_direnv_completion
  _catalpa_direnv_completion
}

_catalpa_direnv_install_hook
