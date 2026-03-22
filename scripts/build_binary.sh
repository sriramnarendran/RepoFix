#!/usr/bin/env bash
# Build a single-file repofix executable with PyInstaller (current OS/arch only).
# Uses a disposable venv so a global torch/tensorflow install is not bundled.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "build_binary: need python3 on PATH" >&2
  exit 1
fi

VENV="${ROOT}/.venv-binary-build"
python3 -m venv "${VENV}"
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
python -m pip install -q -U pip
python -m pip install -q -e ".[binary]"

OUT="${1:-packaging/dist}"
mkdir -p "${OUT}"
rm -f "${OUT}/repofix" "${OUT}/repofix.exe" 2>/dev/null || true
rm -rf build/pyinstaller

python -m PyInstaller --clean --noconfirm \
  --distpath "${OUT}" \
  --workpath "${ROOT}/build/pyinstaller" \
  "${ROOT}/packaging/repofix.spec"

echo "Built: ${OUT}/repofix (or repofix.exe on Windows)"
echo "Note: targets still need git (and Docker when the stack needs it) installed separately."
