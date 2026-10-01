# watcher_victim

A small, throwaway **failure-injection harness** used to evaluate
[Watcher](https://github.com/shaheeranser/watcher), a log-watching daemon that
explains crashes with a local LLM.

This repo exists for one reason: to produce known, reproducible failures with
documented ground truth, so Watcher's generated crash explanations can be scored
for accuracy. It is a personal test fixture — built once, used repeatedly, and
not maintained collaboratively.

## What's here

```
Dockerfile              Node 20 service image (digest-pinned, heap cap tunable)
go-service/             Go service that panics (`go-panic`)
python-service/         Python service that raises (`python-traceback`)
docker-compose.yml      Standalone stack (Compose project: watcher_victim)
break.sh                break.sh <scenario> — trigger a failure; also `capture`
evaluate.py             Score Watcher's output against ground truth
examples/               Sample Watcher results for offline scoring
service/                Deliberately fragile Node service
scenarios/              Per scenario: JSON definition + ground truth, plus a
                        captured <name>.log fixture of the real container stdout
```

## Services

| Service | Container port | Host port | Failures it produces |
| ------- | -------------- | --------- | -------------------- |
| `victim-api` (Node) | 8080 | 18080 | `bad-input`, `restart-loop`, `timeout` |
| `victim-go` (Go) | 8081 | 18082 | `go-panic` |
| `victim-python` (Python) | 8082 | 18083 | `python-traceback` |

The Node service is dependency-free and deliberately fragile:

| Method | Path              | Role |
| ------ | ----------------- | ---- |
| GET    | `/health`         | Liveness. |
| POST   | `/api/items`      | Fragile create handler (`bad-input`). |
| POST   | `/api/bulk`       | Unbounded body buffering (`timeout`). |
| GET    | `/api/slow?ms=N`  | Blocking busy-wait; alternate timeout surface. |

Startup config is validated before the server binds: `VICTIM_API_KEY` must be a
32-char lowercase hex string, `VICTIM_PORT` a valid port.

Each service logs in the shape its runtime naturally produces, and the Go and
Node log lines carry an RFC3339 timestamp (`2026-10-01T12:00:00Z INFO ...`) so
Watcher's record-boundary detector closes a crash block cleanly instead of
absorbing the next line.

## Scenarios

`kind` is the Watcher detector each scenario exercises (`internal/detect`).

| Scenario | Kind | Failure | Ground-truth root cause |
| -------- | ---- | ------- | ----------------------- |
| `bad-input` | `generic-fatal` | Uncaught `TypeError` with a real stack trace | `POST /api/items` calls `payload.name.trim()` without validating the body; a `null` field dereferences null and crashes the process. |
| `restart-loop` | `generic-fatal` | Crash loop, byte-identical stack every restart | Startup validation rejects `VICTIM_API_KEY`; the process exits before `listen()` and Docker restarts it forever. |
| `timeout` | `generic-fatal` | `FATAL ERROR: ... JavaScript heap out of memory` | `POST /api/bulk` concatenates the whole body with no size limit; an oversized payload exhausts the heap. |
| `go-panic` | `go-panic` | Real Go panic block (`panic:` + `goroutine N [running]:`) | `/panic` assigns into a nil map inside a goroutine; the unrecovered panic terminates the process. |
| `python-traceback` | `python-traceback` | Real Python `Traceback (most recent call last):` | `/raise` calls `.strip()` on a `None` field; the unhandled `AttributeError` prints a traceback. |

`restart-loop` intentionally exercises Watcher's **deduplication** — every cycle
emits the same startup crash, so it should surface as one incident with a rising
`count`, not one alert per occurrence. Recover with `./break.sh reset`.

Each scenario also has a committed `scenarios/<name>.log` — the exact container
stdout from a real run. Those fixtures let Watcher's detector tests consume real
crash text, and let `evaluate.py` score offline (see below).

## How this wires to Watcher

Watcher only ever consumes the stack's logs, and it picks its output by the
consumer: a terminal gets a human rendering, a pipe or redirect gets one JSON
object per line. `evaluate.py` reads those JSON lines.

The harness recreates `victim-api` only for `reset` and `restart-loop`; every
other trigger reuses the running containers. Even so, a piped
`docker compose logs -f | watcher` stream dies whenever a container is recreated,
so the supported way to run the two together is to let Watcher follow the
Compose **project** over the Docker socket — it reattaches as containers start
and stop:

```bash
# 1. Bring the harness up (Compose project name: watcher_victim).
cd watcher_victim && ./break.sh reset

# 2. In one terminal, run Watcher against the harness's Compose project and
#    capture its JSON Lines. No --json flag: JSONL is automatic when piped.
/path/to/bin/watcher run --containers project=watcher_victim \
  --model <model> --ollama-url http://localhost:11434 > /tmp/watcher-victim.jsonl

# 3. In another terminal, trigger each scenario and score what Watcher emitted.
./evaluate.py --watcher /tmp/watcher-victim.jsonl --runs 3 --report reports/latest.md
```

`--wait` defaults to 15 s — raise it for a small model on a VPS. If Watcher runs
as a container inside that same Compose project, plain `watcher run` attaches to
its project's siblings by default, so no `--containers` selector is needed.

### Result fields and ground truth

Watcher emits one JSON object per incident with a fixed field set
(`fingerprint`, `kind`, `source`, `count`, `first_seen`, `last_seen`, `summary`,
`likely_cause`, `evidence`, `suggested_fix`, `confidence`, `severity`, `model`).
When the model call fails it still emits a line, with `explanation_unavailable:
true` and an `error` — `evaluate.py` counts that as the `no_explanation`
failure, not as "no output".

`--emit-truth` writes this repo's scenarios as a ground-truth file in the shape
`specs/04-evaluation/design.md` §3.1 defines, so the (milestone 04) in-repo
scorer can read the same corpus:

```bash
./evaluate.py --emit-truth truth.json                 # {"cases":[{id, expected_cause, kind}]}
./evaluate.py --emit-truth truth.json --truth-format map
./evaluate.py --emit-truth truth.csv  --truth-format csv

# Once `watcher eval` ships:
watcher eval --truth truth.json --results /tmp/watcher-victim.jsonl --identifier run_id
```

The spec recommends **run-id mode**: launch Watcher once per scenario with
`WATCHER_RUN_ID=<scenario id>` so every incident echoes that id. Milestone 04
adds the field; until then `evaluate.py` defaults to `--identifier newest`
(arrival order) and `--identifier run_id` is ready for when it lands.

## Usage

```bash
# Bring the stack up and confirm all three services are healthy.
./break.sh reset

# Trigger failures one at a time (prints the relevant logs).
./break.sh bad-input            # or: bad-input quantity | tags | malformed
./break.sh restart-loop
./break.sh timeout
./break.sh go-panic
./break.sh python-traceback
./break.sh reset                # recover a healthy stack

# Refresh the committed log fixtures (resets, triggers, captures each).
./break.sh capture              # or: ./break.sh capture go-panic

# Score Watcher against ground truth. --watcher is a path OR url — never a
# hardcoded sibling directory.
./evaluate.py --watcher /tmp/watcher-victim.jsonl --runs 3 --report reports/latest.md
./evaluate.py --watcher http://localhost:9000/explanations --runs 5

# Re-score a stored results file with no Docker, using the sample corpus:
./evaluate.py --watcher examples/watcher-results.sample.jsonl --offline --runs 1

# CI gate: exit non-zero below a pass rate.
./evaluate.py --watcher /tmp/watcher-victim.jsonl --runs 3 --min-pass-rate 0.60
```

### Scoring

`evaluate.py` accepts Watcher output as a JSON Lines stream, a JSON array, or a
single JSON object (a generic webhook payload works too — `likely_cause` is
found recursively). It classifies each run using the spec's categories —
`pass`, `cause_mismatch`, `no_explanation`, `no_result`, `kind_mismatch` — and
prints a pass tally, a **per-detector-kind** rollup, and the failures with their
scores. `--json` emits the same in the `§5.2` report shape.

Two pass metrics:

- `--metric keywords` (default) — passes when enough of the scenario's
  evidence-anchored keywords appear in `likely_cause`.
- `--metric similarity` — the spec metric (design.md §4): NFKC + lowercase +
  punctuation strip + stopwords, then `max(token-set Dice, character-trigram
  Dice)`, gated by `--threshold` (τ, default 0.60). The score is printed for
  every run regardless of metric, so a stored run can be re-thresholded without
  re-running anything.

Note the τ default is the spec's *example* value and is explicitly uncalibrated
(`OD-04-1`): against this harness's deliberately verbose ground truths, τ = 0.60
is strict and τ = 0.30 is closer to the keyword verdict.

## Reproducibility and tuning

- The Node base image is **digest-pinned** so the `timeout` OOM threshold does
  not drift between Node patch releases.
- `VICTIM_HEAP_MB` (default 96) sets `--max-old-space-size`, so the `timeout`
  scenario can be re-tuned without editing the Dockerfile.
- `VICTIM_PAYLOAD_BYTES` (default 200000000) sets the `timeout` payload size; the
  payload is streamed straight from `/dev/zero`, so nothing large is written to
  disk.

## Ports

Host ports **18080**, **18082**, and **18083** are used so this stack can run
alongside Watcher's without collisions.

## Deliberately absent

No `CONTRIBUTING.md`, `AGENTS.md`, issue templates, or CI — none of it applies
to a personal fixture with no expected outside contributors.
