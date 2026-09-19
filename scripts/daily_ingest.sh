#!/usr/bin/env bash
# Daily Petfinder pull. Called by launchd (see launchd/ and `make schedule`),
# but safe to run by hand. Appends to data/logs/ingest.log.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
mkdir -p data/logs
LOG="data/logs/ingest.log"
PY="$REPO/.venv/bin/python"

{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) daily ingest ==="
  if [[ ! -x "$PY" ]]; then
    echo "No virtualenv at .venv — run 'make install' first."; exit 1
  fi
  if ! grep -qE '^PETFINDER_KEY=.+' .env 2>/dev/null; then
    echo "PETFINDER_KEY not set in .env — skipping."; exit 0
  fi
  "$PY" -m furrster.cli ingest --type dog --type cat
  "$PY" -m furrster.cli orgs --max-pages 2 || echo "orgs refresh failed (non-fatal)"
  echo "ok"
} >> "$LOG" 2>&1
