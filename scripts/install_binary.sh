#!/usr/bin/env bash
# Install the RepoFix standalone binary from GitHub Releases (no Python required for RepoFix).
#
#   curl -sSf --proto '=https' --tlsv1.2 \
#     https://raw.githubusercontent.com/sriramnarendran/RepoFix/main/scripts/install_binary.sh | bash
#
# Requires a GitHub Release with attached assets (from CI). Pin a version:
#
#   REPOFIX_VERSION=0.1.0 curl -sSf ... | bash
#
# Environment:
#   REPOFIX_VERSION   e.g. 0.1.0 or v0.1.0 — use that release; omit for latest.
#   REPOFIX_GITHUB_REPO  default sriramnarendran/RepoFix
#   INSTALL_PREFIX      default $HOME/.local/bin

set -euo pipefail

REPO="${REPOFIX_GITHUB_REPO:-sriramnarendran/RepoFix}"
BASE="https://github.com/${REPO}"

detect_os() {
  case "$(uname -s)" in
    Linux*) echo linux ;;
    Darwin*) echo macos ;;
    MINGW*|MSYS*|CYGWIN*) echo windows ;;
    *) echo "install_binary: unsupported OS $(uname -s)" >&2; exit 1 ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo x86_64 ;;
    arm64|aarch64) echo arm64 ;;
    *) echo "install_binary: unsupported CPU $(uname -m)" >&2; exit 1 ;;
  esac
}

pick_asset() {
  local os="$1" arch="$2"
  if [[ "$os" == "windows" ]]; then
    echo "repofix-windows-x86_64.exe"
    return
  fi
  if [[ "$os" == "linux" ]]; then
    if [[ "$arch" != "x86_64" ]]; then
      echo "install_binary: prebuilt Linux binary is only available for x86_64" >&2
      exit 1
    fi
    echo "repofix-linux-x86_64"
    return
  fi
  if [[ "$arch" == "arm64" ]]; then
    echo "repofix-macos-arm64"
  else
    echo "repofix-macos-x86_64"
  fi
}

os=$(detect_os)
arch=$(detect_arch)
asset=$(pick_asset "$os" "$arch")

if [[ -n "${REPOFIX_VERSION:-}" ]]; then
  V="${REPOFIX_VERSION#v}"
  url="${BASE}/releases/download/v${V}/${asset}"
else
  url="${BASE}/releases/latest/download/${asset}"
fi

DEST_DIR="${INSTALL_PREFIX:-${HOME}/.local/bin}"
mkdir -p "${DEST_DIR}"

if [[ "$os" == "windows" ]]; then
  DEST="${DEST_DIR}/repofix.exe"
else
  DEST="${DEST_DIR}/repofix"
fi

TMP="${TMPDIR:-/tmp}/repofix-download.$$"
trap 'rm -f "${TMP}"' EXIT

echo "install_binary: ${url}"
curl -sSfL --proto '=https' --tlsv1.2 "${url}" -o "${TMP}"
mv "${TMP}" "${DEST}"
trap - EXIT
if [[ "$os" != "windows" ]]; then
  chmod +x "${DEST}"
fi

echo "install_binary: installed to ${DEST}"
if [[ ":${PATH}:" != *":${DEST_DIR}:"* ]]; then
  echo "install_binary: add ${DEST_DIR} to your PATH (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\") before using repofix."
fi

echo ""
echo "───────────────────────────────────────────────────────────────"
echo "  Next: run RepoFix on a GitHub repo (replace with any URL):"
echo ""
echo "    repofix run https://github.com/user/repo"
echo ""
echo "  Or try:  repofix --help"
echo "───────────────────────────────────────────────────────────────"
