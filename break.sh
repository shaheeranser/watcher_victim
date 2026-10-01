#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

COMPOSE=(docker compose)
API_SERVICE="victim-api"
GO_SERVICE="victim-go"
PY_SERVICE="victim-python"

API_URL="${VICTIM_BASE_URL:-http://localhost:18080}"
GO_URL="${VICTIM_GO_URL:-http://localhost:18082}"
PY_URL="${VICTIM_PY_URL:-http://localhost:18083}"

PAYLOAD_BYTES="${VICTIM_PAYLOAD_BYTES:-200000000}"
BAD_API_KEY="not-a-valid-key"

SCENARIOS=(bad-input restart-loop timeout go-panic python-traceback)

usage() {
  cat <<'EOF'
Usage: break.sh <scenario> [variant]
       break.sh capture [scenario]

Scenarios:
  bad-input [name|quantity|tags|malformed]
                 POST a malformed / null-field payload to /api/items.
  restart-loop   Start the stack with an invalid VICTIM_API_KEY so the service
                 crashes on startup and restarts forever (same stack each time).
  timeout        POST an oversized payload to /api/bulk; the service buffers it
                 without a size limit and exhausts the heap.
  go-panic       Trigger an unrecovered nil-map panic in the Go service.
  python-traceback
                 Trigger an unhandled AttributeError in the Python service.
  reset          Recreate the stack with valid config and wait for all services.
  logs [service] Print recent logs (default victim-api).
  capture [scenario]
                 Trigger and write the container stdout to scenarios/<name>.log
                 (all scenarios when none is given).

Container lifecycle:
  Only `reset` and `restart-loop` recreate containers. Every other scenario
  reuses the running ones, so Watcher can follow the Compose project
  (`--containers project=watcher_victim`) across a whole run instead of losing
  the stream on a recreation.

Environment:
  VICTIM_BASE_URL       Node API base URL (default http://localhost:18080)
  VICTIM_GO_URL         Go service base URL (default http://localhost:18082)
  VICTIM_PY_URL         Python service base URL (default http://localhost:18083)
  VICTIM_PAYLOAD_BYTES  Payload size for the timeout scenario (default 200000000)
EOF
}

# up is idempotent: it starts whatever is missing and leaves running containers
# (and therefore Watcher's Docker log stream) untouched.
up() {
  "${COMPOSE[@]}" up -d --remove-orphans >/dev/null 2>&1
}

# recreate forces fresh containers. Used only where a clean slate is required.
recreate() {
  "${COMPOSE[@]}" up -d --force-recreate --remove-orphans >/dev/null 2>&1
}

wait_ready() {
  local url="$1"
  for _ in $(seq 1 30); do
    if curl -fsS -m 2 "$url/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_all() {
  wait_ready "$API_URL" && wait_ready "$GO_URL" && wait_ready "$PY_URL"
}

service_logs() {
  local service="$1"
  local tail="${2:-40}"
  "${COMPOSE[@]}" logs --no-log-prefix --tail "$tail" "$service" 2>&1 || true
}

recent_logs() {
  service_logs "$API_SERVICE" "${1:-40}"
}

log_service_for() {
  case "$1" in
    go-panic) echo "$GO_SERVICE" ;;
    python-traceback) echo "$PY_SERVICE" ;;
    *) echo "$API_SERVICE" ;;
  esac
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
      return 2
      ;;
  esac

  up
  wait_ready "$API_URL" || { recent_logs; return 1; }

  curl -sS -m 10 -X POST "$API_URL/api/items" \
    -H 'content-type: application/json' \
    --data "$payload" || true
  echo

  sleep 2
  recent_logs
}

scenario_restart_loop() {
  VICTIM_API_KEY="$BAD_API_KEY" recreate

  local count=0
  for _ in $(seq 1 30); do
    count="$(docker inspect -f '{{.RestartCount}}' watcher-victim-api 2>/dev/null || echo 0)"
    if [ "${count:-0}" -ge 2 ] 2>/dev/null; then
      break
    fi
    sleep 1
  done

  echo "victim-api restarts observed: ${count}"
  "${COMPOSE[@]}" ps || true
  recent_logs 60
}

scenario_timeout() {
  up
  wait_ready "$API_URL" || { recent_logs; return 1; }

  {
    printf '['
    yes '0,' | head -c "$PAYLOAD_BYTES" || true
    printf '0]'
  } | curl -sS -m 120 -X POST "$API_URL/api/bulk" \
        -H 'content-type: application/json' \
        --data-binary @- || true
  echo

  sleep 2
  recent_logs
}

scenario_go_panic() {
  up
  wait_ready "$GO_URL" || { service_logs "$GO_SERVICE"; return 1; }

  curl -sS -m 10 -X POST "$GO_URL/panic" || true
  echo

  sleep 2
  service_logs "$GO_SERVICE" 60
}

scenario_python_traceback() {
  up
  wait_ready "$PY_URL" || { service_logs "$PY_SERVICE"; return 1; }

  curl -sS -m 10 "$PY_URL/raise" || true
  echo

  sleep 2
  service_logs "$PY_SERVICE" 60
}

scenario_reset() {
  recreate
  if wait_all; then
    echo "stack healthy (victim-api, victim-go, victim-python)"
  else
    echo "stack did not become healthy" >&2
    "${COMPOSE[@]}" ps || true
    recent_logs 20
    return 1
  fi
}

trigger() {
  local name="$1"
  shift
  case "$name" in
    bad-input) scenario_bad_input "${1:-name}" ;;
    restart-loop) scenario_restart_loop ;;
    timeout) scenario_timeout ;;
    go-panic) scenario_go_panic ;;
    python-traceback) scenario_python_traceback ;;
    *)
      echo "unknown scenario: $name" >&2
      return 2
      ;;
  esac
}

capture() {
  local name="$1"
  local out="scenarios/${name}.log"
  scenario_reset >/dev/null 2>&1
  trigger "$name" >/dev/null 2>&1 || true
  sleep 2
  service_logs "$(log_service_for "$name")" 80 > "$out"
  echo "wrote $out"
}

capture_all() {
  for name in "${SCENARIOS[@]}"; do
    capture "$name"
  done
  scenario_reset >/dev/null 2>&1 || true
}

case "${1:-}" in
  bad-input) shift; scenario_bad_input "${1:-name}" ;;
  restart-loop) scenario_restart_loop ;;
  timeout) scenario_timeout ;;
  go-panic) scenario_go_panic ;;
  python-traceback) scenario_python_traceback ;;
  reset) scenario_reset ;;
  logs) shift; service_logs "${1:-$API_SERVICE}" ;;
  capture)
    shift
    if [ -z "${1:-}" ]; then
      capture_all
    else
      capture "$1"
    fi
    ;;
  ""|-h|--help|help) usage ;;
  *)
    echo "unknown scenario: $1" >&2
    usage >&2
    exit 2
    ;;
esac
