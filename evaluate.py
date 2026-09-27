#!/usr/bin/env python3
"""Score Watcher's crash explanations against this harness's documented ground truth.

The only handle on Watcher is the ``--watcher`` argument: a path or URL to its
structured output (a JSON array, JSONL, or a single object / webhook payload).
Nothing here assumes a sibling directory or a fixed filesystem path.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCENARIO_DIR = ROOT / "scenarios"
BREAK_SH = ROOT / "break.sh"

LIST_KEYS = ("records", "results", "events", "items", "explanations", "entries", "data")
POLL_INTERVAL_SECONDS = 1.0


def read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", "replace")
    return Path(source).expanduser().read_text(encoding="utf-8")


def parse_jsonl(raw: str) -> list[dict]:
    records = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def parse_payload(raw: str) -> tuple[str, list[dict] | dict]:
    raw = raw.strip()
    if not raw:
        return "list", []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "list", parse_jsonl(raw)
    if isinstance(data, list):
        return "list", [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return "list", [item for item in value if isinstance(item, dict)]
        return "object", data
    return "list", []


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key(item, key)
            if found is not None:
                return found
    return None


def fingerprint(record) -> str:
    return json.dumps(record, sort_keys=True, default=str)


def with_cause(records: list[dict]) -> list[dict]:
    return [record for record in records if find_key(record, "likely_cause") is not None]


class WatcherFeed:
    """Polls the Watcher output source for records newer than the last baseline."""

    def __init__(self, source: str):
        self.source = source
        self.mode = "list"
        self.baseline_count = 0
        self.baseline_fp = None

    def baseline(self) -> None:
        try:
            mode, payload = parse_payload(read_source(self.source))
        except Exception:
            return
        self.mode = mode
        if mode == "list":
            self.baseline_count = len(payload)
        else:
            self.baseline_fp = fingerprint(payload)

    def wait_for_new(self, timeout_seconds: float) -> dict | None:
        deadline = time.time() + timeout_seconds
        while True:
            try:
                mode, payload = parse_payload(read_source(self.source))
            except Exception:
                mode, payload = self.mode, ([] if self.mode == "list" else {})

            if mode == "list":
                if len(payload) > self.baseline_count:
                    fresh = with_cause(payload[self.baseline_count:])
                    self.baseline_count = len(payload)
                    if fresh:
                        return fresh[-1]
            else:
                current = fingerprint(payload)
                if current != self.baseline_fp:
                    self.baseline_fp = current
                    if find_key(payload, "likely_cause") is not None:
                        return payload

            if time.time() >= deadline:
                return None
            time.sleep(POLL_INTERVAL_SECONDS)


def score(ground_truth: str, likely_cause: str | None, spec: dict) -> tuple[bool, list[str], float]:
    cause = (likely_cause or "").strip()
    if not cause:
        return False, [], 0.0
    lowered = cause.lower()
    keywords = [str(keyword).lower() for keyword in spec.get("keywords", [])]
    hits = [keyword for keyword in keywords if keyword in lowered]
    ratio = difflib.SequenceMatcher(None, ground_truth.lower(), lowered).ratio()
    min_keywords = int(spec.get("min_keywords", max(1, (len(keywords) + 1) // 2)))
    threshold = float(spec.get("similarity_threshold", 0.35))
    passed = (bool(keywords) and len(hits) >= min_keywords) or ratio >= threshold
    return passed, hits, ratio


def load_scenarios(selected: list[str] | None) -> list[dict]:
    scenarios = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if selected and data.get("name") not in selected:
            continue
        scenarios.append(data)
    return scenarios


def run_break(*args: str) -> int:
    proc = subprocess.run(
        ["bash", str(BREAK_SH), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    return proc.returncode


def run_scenario(scenario: dict, feed: WatcherFeed, runs: int, wait_seconds: float) -> dict:
    name = scenario["name"]
    ground_truth = scenario["ground_truth"]
    spec = scenario.get("match", {})
    outcomes = []

    for run_index in range(1, runs + 1):
        run_break("reset")
        feed.baseline()
        trigger_code = run_break(name)
        record = feed.wait_for_new(wait_seconds)
        likely_cause = find_key(record, "likely_cause") if record else None
        passed, hits, ratio = score(ground_truth, likely_cause, spec)
        outcomes.append(
            {
                "run": run_index,
                "passed": passed,
                "likely_cause": likely_cause,
                "matched_keywords": hits,
                "similarity": round(ratio, 3),
                "watcher_output": record is not None,
                "trigger_exit_code": trigger_code,
            }
        )

    passes = sum(1 for outcome in outcomes if outcome["passed"])
    return {
        "name": name,
        "summary": scenario.get("summary", ""),
        "ground_truth": ground_truth,
        "runs": len(outcomes),
        "passes": passes,
        "failures": len(outcomes) - passes,
        "pass_rate": round(passes / len(outcomes), 3) if outcomes else 0.0,
        "outcomes": outcomes,
    }


def print_summary(results: list[dict]) -> None:
    print()
    header = f"{'scenario':<14}{'runs':>5}{'pass':>6}{'fail':>6}{'pass rate':>11}"
    print(header)
    print("-" * len(header))
    total_runs = total_passes = 0
    for result in results:
        total_runs += result["runs"]
        total_passes += result["passes"]
        print(
            f"{result['name']:<14}{result['runs']:>5}{result['passes']:>6}"
            f"{result['failures']:>6}{result['pass_rate'] * 100:>10.0f}%"
        )
    overall = (total_passes / total_runs * 100) if total_runs else 0.0
    print("-" * len(header))
    print(f"{'TOTAL':<14}{total_runs:>5}{total_passes:>6}{total_runs - total_passes:>6}{overall:>10.0f}%")

    print("\nPer-run detail:")
    for result in results:
        print(f"\n  {result['name']}")
        print(f"    ground truth: {result['ground_truth']}")
        for outcome in result["outcomes"]:
            verdict = "PASS" if outcome["passed"] else "FAIL"
            cause = outcome["likely_cause"] or "<no output>"
            print(f"    [{verdict}] run {outcome['run']}: {cause}")


def write_report(path: Path, results: list[dict], source: str, runs: int) -> None:
    total_runs = sum(result["runs"] for result in results)
    total_passes = sum(result["passes"] for result in results)
    overall = (total_passes / total_runs * 100) if total_runs else 0.0

    lines = [
        "# Watcher evaluation results",
        "",
        f"- Watcher output source: `{source}`",
        f"- Runs per scenario: {runs}",
        f"- Overall pass rate: **{overall:.0f}%** ({total_passes}/{total_runs})",
        "",
        "| Scenario | Passed | Failed | Pass rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for result in results:
        lines.append(
            f"| {result['name']} | {result['passes']} | {result['failures']} "
            f"| {result['pass_rate'] * 100:.0f}% |"
        )

    lines += ["", "## Ground truth and runs", ""]
    for result in results:
        lines.append(f"**{result['name']}**")
        lines.append("")
        lines.append(f"Ground truth: {result['ground_truth']}")
        lines.append("")
        for outcome in result["outcomes"]:
            verdict = "PASS" if outcome["passed"] else "FAIL"
            cause = outcome["likely_cause"] or "(no output)"
            lines.append(f"- `{verdict}` run {outcome['run']}: {cause}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score Watcher's likely_cause against documented ground truth.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--watcher",
        required=True,
        help="Path or URL to Watcher's structured output or webhook payload.",
    )
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario.")
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="Only evaluate this scenario (repeatable).",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=15.0,
        help="Seconds to wait for Watcher to explain each triggered crash.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON results.")
    parser.add_argument("--report", type=Path, default=None, help="Write a Markdown report here.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    scenarios = load_scenarios(args.scenario)
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2

    feed = WatcherFeed(args.watcher)
    results = []
    try:
        for scenario in scenarios:
            print(f"running scenario '{scenario['name']}' x{args.runs} ...", file=sys.stderr)
            results.append(run_scenario(scenario, feed, args.runs, args.wait))
    finally:
        run_break("reset")

    if args.as_json:
        print(json.dumps(results, indent=2))
    else:
        print_summary(results)

    if args.report:
        write_report(args.report, results, args.watcher, args.runs)
        print(f"\nreport written to {args.report}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
