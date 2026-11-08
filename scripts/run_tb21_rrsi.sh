#!/usr/bin/env bash
set -euo pipefail
TB21_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TB21_PYTHON="${FLASHRSI_PYTHON:-$TB21_ROOT/.venv/bin/python}"
if [[ ! -x "$TB21_PYTHON" ]]; then
  echo "Set FLASHRSI_PYTHON to a Python >=3.12 environment with FlashRSI/Harbor dependencies." >&2
  exit 1
fi
cd "$TB21_ROOT"
exec "$TB21_PYTHON" "$TB21_ROOT/scripts/run_tb21_rrsi.py" "$@"
