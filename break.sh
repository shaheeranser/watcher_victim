#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE=(docker compose)
SERVICE="victim-api"
BASE_URL="${VICTIM_BASE_URL:-http://localhost:18080}"
PAYLOAD_BYTES="${VICTIM_PAYLOAD_BYTES:-200000000}"
BAD_API_KEY="not-a-valid-key"

usage() {
  cat <<'EOF'
Usage: break.sh <scenario> [variant]

Scenarios:
  bad-input [name|quantity|tags|malformed]
                 Send a malformed / null-field payload to POST /api/items.
  restart-loop   Start the stack with an invalid VICTIM_API_KEY so the service
                 crashes on startup and restarts forever (same stack each time).
  timeout        Send an oversized payload to POST /api/bulk; the service
                 buffers it without a size limit and exhausts the heap.
  reset          Recreate the stack with valid config (recover from restart-loop).
  logs           Print recent service logs.

Environment:
  VICTIM_BASE_URL       Base URL of the service (default http://localhost:18080)
  VICTIM_PAYLOAD_BYTES  Payload size for the timeout scenario (default 200000000)
EOF
}

up() {
  "${COMPOSE[@]}" up -d --force-recreate --remove-orphans >/dev/null 2>&1
}

wait_ready() {
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 "$BASE_URL/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

recent_logs() {
  "${COMPOSE[@]}" logs --no-log-prefix --tail 40 "$SERVICE" 2>&1 || true
}

scenario_bad_input() {
  local variant="${1:-name}"
  local payload
  case "$variant" in
    name) payload='{"name":null,"quantity":1,"tags":[]}' ;;
    quantity) payload='{"name":"widget","tags":[]}' ;;
    tags) payload='{"name":"widget","quantity":2,"tags":null}' ;;
    malformed) payload='{"name":' ;;
    *)
      echo "unknown bad-input variant: $variant" >&2
      exit 2
      ;;
  esac

  up
  wait_ready || { recent_logs; return 1; }

  curl -sS -m 10 -X POST "$BASE_URL/api/items" \
    -H 'content-type: application/json' \
    --data "$payload" || true
  echo

  sleep 2
  recent_logs
}

scenario_restart_loop() {
  VICTIM_API_KEY="$BAD_API_KEY" \
    "${COMPOSE[@]}" up -d --force-recreate --remove-orphans >/dev/null 2>&1

  sleep 8
  "${COMPOSE[@]}" ps || true
  recent_logs
}

scenario_timeout() {
  up
  wait_ready || { recent_logs; return 1; }

  {
    printf '['
    yes '0,' | head -c "$PAYLOAD_BYTES" || true
    printf '0]'
  } | curl -sS -m 120 -X POST "$BASE_URL/api/bulk" \
        -H 'content-type: application/json' \
        --data-binary @- || true
  echo

  sleep 2
  recent_logs
}

scenario_reset() {
  up
  if wait_ready; then
    echo "stack healthy at $BASE_URL"
  else
    echo "stack did not become healthy" >&2
    recent_logs
    return 1
  fi
}

case "${1:-}" in
  bad-input) shift; scenario_bad_input "${1:-name}" ;;
  restart-loop) scenario_restart_loop ;;
  timeout) scenario_timeout ;;
  reset) scenario_reset ;;
  logs) recent_logs ;;
  ""|-h|--help|help) usage ;;
  *)
    echo "unknown scenario: $1" >&2
    usage >&2
    exit 2
    ;;
esac
