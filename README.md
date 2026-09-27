# watcher_victim

A small, throwaway **failure-injection harness** used to evaluate
[Watcher](../cli), a log-watching daemon that explains crashes with a local LLM.

This repo exists for one reason: to produce known, reproducible failures with
documented ground truth, so Watcher's generated crash explanations can be scored
for accuracy. It is a personal test fixture — built once, used repeatedly, and
not maintained collaboratively.

## What's here

```
Dockerfile              Node 20 service image (heap capped at 96 MB)
docker-compose.yml      Standalone stack, host port 18080 -> 8080
break.sh                break.sh <scenario> — trigger a failure on demand
evaluate.py             Score Watcher's likely_cause against ground truth
service/                The deliberately fragile service
scenarios/              One JSON file per scenario: trigger + ground truth
```

## The service

A dependency-free Node `http` server, deliberately fragile:

| Method | Path              | Role |
| ------ | ----------------- | ---- |
| GET    | `/health`         | Liveness. |
| POST   | `/api/items`      | Fragile create handler (`bad-input`). |
| POST   | `/api/bulk`       | Unbounded body buffering (`timeout`). |
| GET    | `/api/slow?ms=N`  | Blocking busy-wait; alternate timeout surface. |

Startup config is validated before the server binds: `VICTIM_API_KEY` must be a
32-char lowercase hex string, `VICTIM_PORT` a valid port.

## Scenarios

| Scenario | Trigger | Failure | Ground-truth root cause |
| -------- | ------- | ------- | ----------------------- |
| `bad-input` | `./break.sh bad-input` | Uncaught `TypeError` with a real stack trace | `POST /api/items` calls `payload.name.trim()` without validating the body; a `null` field dereferences null and crashes the process. |
| `restart-loop` | `./break.sh restart-loop` | Crash loop, byte-identical stack every restart | Startup validation rejects `VICTIM_API_KEY`; the process exits before `listen()` and Docker restarts it forever. |
| `timeout` | `./break.sh timeout` | `FATAL ERROR: ... JavaScript heap out of memory` | `POST /api/bulk` concatenates the whole body with no size limit; an oversized payload exhausts the heap. |

`restart-loop` intentionally exercises Watcher's **deduplication** — every cycle
emits the same startup crash. Recover with `./break.sh reset`.

## Usage

```bash
# Bring the stack up and confirm it's healthy.
./break.sh reset

# Trigger failures one at a time (prints the relevant logs).
./break.sh bad-input            # or: bad-input quantity | tags | malformed
./break.sh restart-loop
./break.sh timeout
./break.sh reset                # recover a healthy stack

# Score Watcher against ground truth. --watcher is a path OR url — never a
# hardcoded sibling directory.
./evaluate.py --watcher http://localhost:9000/explanations --runs 5
./evaluate.py --watcher /tmp/watcher-output.json --runs 3 --report reports/latest.md
```

`evaluate.py` accepts Watcher output as a JSON array, JSONL, or a single
JSON object (including a webhook payload — `likely_cause` is found recursively).
It runs each scenario N times, compares `likely_cause` to the documented ground
truth, and prints a per-scenario pass/fail tally plus a results-slide summary.

Tunable for `timeout`: `VICTIM_PAYLOAD_BYTES` (default 200000000); the payload is
streamed straight from `/dev/zero`, so nothing large is written to disk.

## Ports

Host port **18080** is used so this stack can run alongside Watcher's without
collisions.

## Deliberately absent

No `CONTRIBUTING.md`, `AGENTS.md`, issue templates, or CI — none of it applies
to a personal fixture with no expected outside contributors.
