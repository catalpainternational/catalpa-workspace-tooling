# catalpa-workspace-tooling: register tab completion when direnv loads a tooling repo.
#
# One-time setup (~/.zshrc, after eval "$(direnv hook zsh)"):
#   [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh" ]] && \
#     source "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh"
#
# Per-repo .envrc must export CATALPA_REGISTER_PYTHON_ARGCOMPLETE (see scripts/envrc.template).

_catalpa_direnv_completion() {
  emulate -L zsh
  local reg="${CATALPA_REGISTER_PYTHON_ARGCOMPLETE:-}"
  if [[ -z "$reg" || ! -x "$reg" ]]; then
    _CATALPA_COMP_DONE=
    return 0
  fi
  [[ "${_CATALPA_COMP_DONE:-}" == "$reg" ]] && return 0
  local cmd out
  for cmd in dk native local dev test scripts; do
    out="$("$reg" -s zsh "$cmd" 2>/dev/null)" || continue
    [[ -n "$out" ]] && eval "$out"
  done
  _CATALPA_COMP_DONE="$reg"
}

if (( $+commands[direnv] )); then
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _catalpa_direnv_completion
fi
