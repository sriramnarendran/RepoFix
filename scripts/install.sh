#!/usr/bin/env bash
# Install the RepoFix CLI (`repofix`) from PyPI.
#
#   curl -sSf https://raw.githubusercontent.com/sriramnarendran/RepoFix/main/scripts/install.sh | bash
#
# With options (note the `bash -s --` when piping):
#
#   curl -sSf ... | bash -s -- --pipx
#   curl -sSf ... | bash -s -- --pip
#
# Environment:
#   REPOFIX_VERSION   If set (e.g. 0.1.0), install that exact version; otherwise latest.
#   REPOFIX_USE_PIPX  If set to 1, prefer pipx when available (same as --pipx).

set -euo pipefail

PATH_HELP_PRINTED=0

usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

  --pipx    Install with pipx into an isolated env (recommended if pipx is installed).
  --pip     Install with pip --user (default when pipx is not used).
  -h        Show this help.

Environment:
  REPOFIX_VERSION=0.1.0   Pin the release; omit for latest from PyPI.
  REPOFIX_USE_PIPX=1      Prefer pipx when available.
EOF
}

# Default: pip --user. Use --pipx or REPOFIX_USE_PIPX=1 for an isolated pipx env.
USE_PIPX=0
for arg in "$@"; do
  case "${arg}" in
    --pipx) USE_PIPX=1 ;;
    --pip) USE_PIPX=0 ;;
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

if [[ -n "${REPOFIX_USE_PIPX:-}" ]]; then
  case "${REPOFIX_USE_PIPX}" in
    1|true|yes|on) USE_PIPX=1 ;;
    0|false|no|off) USE_PIPX=0 ;;
  esac
fi

have_cmd() { command -v "$1" >/dev/null 2>&1; }

pick_python() {
  local cmd
  for cmd in python3.12 python3.11 python3.10 python3; do
    if have_cmd "${cmd}"; then
      if "${cmd}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        printf '%s\n' "${cmd}"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python || true)"
if [[ -z "${PY}" ]]; then
  echo "repofix install: need Python 3.10+ on PATH (tried python3.12 … python3)." >&2
  exit 1
fi

if [[ -n "${REPOFIX_VERSION:-}" ]]; then
  SPEC="repofix==${REPOFIX_VERSION}"
else
  SPEC="repofix"
fi

if [[ "${USE_PIPX}" == "1" ]]; then
  if ! have_cmd pipx; then
    echo "repofix install: --pipx / REPOFIX_USE_PIPX=1 but pipx not found. Install pipx: https://pipx.pypa.io/" >&2
    exit 1
  fi
  echo "Installing ${SPEC} with pipx …"
  pipx install --force "${SPEC}"
else
  echo "Installing ${SPEC} with ${PY} -m pip (user site) …"
  "${PY}" -m pip install --user --upgrade "${SPEC}"
  local_bin="$("${PY}" -m site --user-base 2>/dev/null)/bin"
  if [[ -d "${local_bin}" ]] && [[ ":${PATH}:" != *":${local_bin}:"* ]]; then
    echo "" >&2
    echo "Scripts were installed under: ${local_bin}" >&2
    echo "That directory is not on your PATH in this session." >&2
    echo "" >&2
    echo "  Use repofix in this terminal (copy–paste):" >&2
    echo "    export PATH=\"${local_bin}:\${PATH}\"" >&2
    echo "" >&2
    echo "  To make that permanent, add the same export line to ~/.bashrc or ~/.zshrc," >&2
    echo "  or run: pipx ensurepath   (if you use pipx; then open a new terminal)." >&2
    PATH_HELP_PRINTED=1
  fi
fi

if have_cmd repofix; then
  echo "repofix is installed. Try: repofix --help"
else
  user_bin="${HOME}/.local/bin"
  if [[ "${PATH_HELP_PRINTED}" == "0" ]] && [[ -x "${user_bin}/repofix" ]] && [[ ":${PATH}:" != *":${user_bin}:"* ]]; then
    echo "" >&2
    echo "\`repofix\` is installed at ${user_bin}/repofix but that directory is not on PATH." >&2
    echo "  In this terminal:" >&2
    echo "    export PATH=\"${user_bin}:\${PATH}\"" >&2
    echo "  Or: pipx ensurepath   # then open a new terminal" >&2
  fi
  echo "" >&2
  echo "Install finished. Run the export PATH=… line above in this shell (or fix PATH in your rc file), then: repofix --help" >&2
fi
