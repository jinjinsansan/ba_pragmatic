#!/usr/bin/env bash
set -euo pipefail

# Local dev runner for bacopy (API + watchers).
#
# Prereqs:
#   - BACOPY_API_KEY set (export in shell or put in .env and source it)
#   - camoufox available in the Python environment (for watchers)
#
# Usage:
#   export BACOPY_API_KEY="yourkey"
#   bash scripts/run_bacopy_local.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${BACOPY_API_KEY:-}" ]]; then
  echo "ERROR: BACOPY_API_KEY is not set"
  exit 1
fi

export BACOPY_HOST="${BACOPY_HOST:-127.0.0.1}"
export BACOPY_PORT="${BACOPY_PORT:-8010}"

echo "[run] ROOT_DIR=${ROOT_DIR}"
echo "[run] API=http://${BACOPY_HOST}:${BACOPY_PORT}"

mkdir -p "${ROOT_DIR}/data"

API_LOG="${ROOT_DIR}/data/bacopy_api.log"
EVO_LOG="${ROOT_DIR}/data/bacopy_watch_evolution.log"
PRA_LOG="${ROOT_DIR}/data/bacopy_watch_pragmatic.log"

echo "[run] starting API..."
python3 "${ROOT_DIR}/bacopy_api.py" --host "${BACOPY_HOST}" --port "${BACOPY_PORT}" >"${API_LOG}" 2>&1 &
API_PID=$!
echo "[run] API_PID=${API_PID} log=${API_LOG}"

sleep 1

echo "[run] starting Evolution watcher..."
python3 "${ROOT_DIR}/bacopy_watch_evolution.py" >"${EVO_LOG}" 2>&1 &
EVO_PID=$!
echo "[run] EVO_PID=${EVO_PID} log=${EVO_LOG}"

echo "[run] starting Pragmatic watcher..."
python3 "${ROOT_DIR}/bacopy_watch_pragmatic.py" --headless >"${PRA_LOG}" 2>&1 &
PRA_PID=$!
echo "[run] PRA_PID=${PRA_PID} log=${PRA_LOG}"

echo
echo "[run] health check:"
curl -s "http://${BACOPY_HOST}:${BACOPY_PORT}/api/health" && echo
echo "[run] snapshots check (auth):"
curl -s -H "Authorization: Bearer ${BACOPY_API_KEY}" "http://${BACOPY_HOST}:${BACOPY_PORT}/api/snapshots" | head -c 4000 && echo
echo

echo "[run] Running. Stop with:"
echo "  kill ${API_PID} ${EVO_PID} ${PRA_PID}"
echo
echo "[run] Tailing API log (Ctrl+C to stop tail; processes keep running)..."
tail -f "${API_LOG}"
