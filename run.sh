#!/usr/bin/env bash
# WeeFee — local dev launcher
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a; . ./.env; set +a
else
  echo "No .env found — copy .env.example to .env and fill it in."
  echo "Starting anyway with defaults (payments will be disabled)."
  echo
fi

exec .venv/bin/uvicorn app.main:app --host "${WEEFEE_HOST:-0.0.0.0}" --port "${WEEFEE_PORT:-8000}" "$@"
