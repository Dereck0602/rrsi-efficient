#!/usr/bin/env bash
# Do not expose credentials even when invoked with bash -x.
set +x
set -euo pipefail
TB21_VECTRON_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TB21_VECTRON_ENV="${FLASHRSI_VECTRON_ENV:-$TB21_VECTRON_ROOT/.env.vectron}"

# An explicit environment value takes precedence over the private local file.
if [[ -z "${VECTRON_API_KEY:-}" && -f "$TB21_VECTRON_ENV" ]]; then
  source "$TB21_VECTRON_ENV"
fi
if [[ -z "${VECTRON_API_KEY:-}" ]]; then
  echo "Set VECTRON_API_KEY or put it in .env.vectron (chmod 600)." >&2
  exit 1
fi
export VECTRON_API_KEY
export FLASHRSI_AGENT_API_KEY="$VECTRON_API_KEY"
export FLASHRSI_WORKER_API_KEY="$VECTRON_API_KEY"

# Take model routing from this profile, not ambient overrides for the local run.
unset FLASHRSI_AGENT_MODEL FLASHRSI_AGENT_BASE_URL
unset FLASHRSI_WORKER_MODEL FLASHRSI_WORKER_BASE_URL
exec bash "$TB21_VECTRON_ROOT/scripts/run_tb21_rrsi.sh" \
  --config "$TB21_VECTRON_ROOT/examples/terminal_bench21_rrsi_vectron.yaml" "$@"
