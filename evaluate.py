#!/usr/bin/env python3
"""Score Watcher's crash explanations against this harness's documented ground truth.

Written against Watcher's evaluation contract (``specs/04-evaluation/design.md``):
the identifier modes, ground-truth shapes, similarity metric, and failure
categories mirror that spec, so the harness's numbers are comparable to the
future in-repo ``watcher eval``.

The only handle on Watcher is ``--watcher``: a path or URL to its JSON Lines
incident stream (or a generic webhook payload). Nothing here assumes a sibling
directory or a fixed filesystem path.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCENARIO_DIR = ROOT / "scenarios"
BREAK_SH = ROOT / "break.sh"

LIST_KEYS = ("records", "results", "events", "items", "explanations", "entries", "data")
POLL_INTERVAL_SECONDS = 1.0

# CORE-OUT-3 fields that identify a line as a Watcher result.
RESULT_MARKERS = ("fingerprint", "likely_cause", "explanation_unavailable")

# design.md §4.1 stopword set.
STOPWORDS = {"a", "the", "in", "of", "on", "at", "to", "is", "was"}

# design.md §4.3 failure categories.
PASS = "pass"
CAUSE_MISMATCH = "cause_mismatch"
NO_EXPLANATION = "no_explanation"
NO_RESULT = "no_result"
KIND_MISMATCH = "kind_mismatch"

# restart-loop leaves the stack crash-looping; every other scenario recovers on
# its own, so only this one needs a recreate before and after.
NEEDS_RESET = {"restart-loop"}


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


def is_result(record: dict) -> bool:
    """A line is a Watcher result if it carries any CORE-OUT-3 anchor field.

    This deliberately includes ``explanation_unavailable`` lines, which have no
    ``likely_cause``. Treating those as "no output" would hide the
    ``no_explanation`` failure the spec asks us to count (design.md §4.3).
    """
    return any(find_key(record, key) is not None for key in RESULT_MARKERS)


def normalize(text: str) -> str:
    """design.md §4.1: NFKC, lowercase, strip punctuation, drop stopwords."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(token for token in text.split() if token not in STOPWORDS)


def _dice(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _trigrams(text: str) -> set:
    compact = text.replace(" ", "")
    return {compact[i : i + 3] for i in range(len(compact) - 2)}


def spec_similarity(expected: str, actual: str) -> float:
    """design.md §4.2: max(token-set Dice, character-trigram Dice)."""
    norm_expected = normalize(expected)
    norm_actual = normalize(actual)
    if not norm_expected or not norm_actual:
        return 0.0
    if norm_expected == norm_actual:
        return 1.0
    tokens = _dice(set(norm_expected.split()), set(norm_actual.split()))
    grams = _dice(_trigrams(norm_expected), _trigrams(norm_actual))
    return max(tokens, grams)


def payload_fingerprint(record) -> str:
    return json.dumps(record, sort_keys=True, default=str)


class WatcherFeed:
    """Polls the Watcher output source for results newer than the last baseline."""

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
            self.baseline_fp = payload_fingerprint(payload)

    def wait_for_new(self, timeout_seconds: float, run_id: str | None = None) -> dict | None:
        deadline = time.time() + timeout_seconds
        while True:
            try:
                mode, payload = parse_payload(read_source(self.source))
            except Exception:
                mode, payload = self.mode, ([] if self.mode == "list" else {})

            if mode == "list":
                fresh = [record for record in payload[self.baseline_count:] if is_result(record)]
                if fresh:
                    chosen = self._choose(fresh, run_id)
                    if chosen is not None:
                        self.baseline_count = len(payload)
                        return chosen
            else:
                current = payload_fingerprint(payload)
                if current != self.baseline_fp and is_result(payload):
                    self.baseline_fp = current
                    return payload

            if time.time() >= deadline:
                return None
            time.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _choose(fresh: list[dict], run_id: str | None) -> dict | None:
        if run_id is None:
            return fresh[-1]
        matched = [record for record in fresh if str(find_key(record, "run_id") or "") == run_id]
        if matched:
            return matched[-1]
        # Producer does not echo run ids yet (milestone 04); fall back to newest.
        if not any(find_key(record, "run_id") is not None for record in fresh):
            return fresh[-1]
        return None


def keyword_match(spec: dict, actual_cause: str) -> tuple[bool, list[str]]:
    lowered = actual_cause.lower()
    keywords = [str(keyword).lower() for keyword in spec.get("keywords", [])]
    hits = [keyword for keyword in keywords if keyword in lowered]
    min_keywords = int(spec.get("min_keywords", max(1, (len(keywords) + 1) // 2)))
    return (bool(keywords) and len(hits) >= min_keywords), hits


def _outcome(category, passed, score, cause, kind, kind_match, error, fp, hits=None) -> dict:
    return {
        "category": category,
        "passed": passed,
        "score": score,
        "likely_cause": cause,
        "actual_kind": kind,
        "kind_match": kind_match,
        "explanation_error": error,
        "fingerprint": fp,
        "matched_keywords": hits or [],
    }


def classify(scenario: dict, record: dict | None, metric: str, threshold: float, require_kind: bool) -> dict:
    if record is None:
        return _outcome(NO_RESULT, False, 0.0, None, None, None, None, None)

    expected_kind = scenario.get("kind")
    unavailable = find_key(record, "explanation_unavailable")
    actual_cause = find_key(record, "likely_cause") or ""
    actual_kind = find_key(record, "kind")
    error = find_key(record, "error")
    fp = find_key(record, "fingerprint")

    if unavailable or not str(actual_cause).strip():
        return _outcome(NO_EXPLANATION, False, 0.0, None, actual_kind, None, error, fp)

    score = spec_similarity(scenario["ground_truth"], actual_cause)
    kind_match = expected_kind is None or actual_kind == expected_kind

    if metric == "similarity":
        cause_ok, hits = score >= threshold, []
    else:
        cause_ok, hits = keyword_match(scenario.get("match", {}), actual_cause)

    if not cause_ok:
        category, passed = CAUSE_MISMATCH, False
    elif not kind_match:
        category, passed = KIND_MISMATCH, not require_kind
    else:
        category, passed = PASS, True

    return _outcome(category, passed, round(score, 3), str(actual_cause), actual_kind, kind_match, error, fp, hits)


def load_scenarios(selected: list[str] | None) -> list[dict]:
    scenarios = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if selected and data.get("name") not in selected:
            continue
        scenarios.append(data)
    return scenarios


def run_break(*args: str) -> int:
    proc = subprocess.run(["bash", str(BREAK_SH), *args], cwd=str(ROOT), capture_output=True, text=True)
    return proc.returncode


def aggregate(scenario: dict, outcomes: list[dict]) -> dict:
    passes = sum(1 for outcome in outcomes if outcome["passed"])
    return {
        "name": scenario["name"],
        "kind": scenario.get("kind", "unknown"),
        "ground_truth": scenario["ground_truth"],
        "runs": len(outcomes),
        "passes": passes,
        "failures": len(outcomes) - passes,
        "pass_rate": round(passes / len(outcomes), 3) if outcomes else 0.0,
        "outcomes": outcomes,
    }


def run_scenario_online(scenario, feed, runs, wait_seconds, metric, threshold, require_kind, identifier):
    outcomes = []
    name = scenario["name"]
    for run_index in range(1, runs + 1):
        if name in NEEDS_RESET:
            run_break("reset")
        run_id = name if identifier == "run_id" else None
        feed.baseline()
        run_break(name)
        record = feed.wait_for_new(wait_seconds, run_id)
        outcome = classify(scenario, record, metric, threshold, require_kind)
        outcome["run"] = run_index
        outcomes.append(outcome)
        if name in NEEDS_RESET:
            run_break("reset")
    return aggregate(scenario, outcomes)


def run_scenario_offline(scenario, records, start, runs, metric, threshold, require_kind):
    outcomes = []
    for offset in range(runs):
        index = start + offset
        record = records[index] if index < len(records) else None
        outcome = classify(scenario, record, metric, threshold, require_kind)
        outcome["run"] = offset + 1
        outcomes.append(outcome)
    return aggregate(scenario, outcomes)


def write_truth(path: Path, scenarios: list[dict], truth_format: str) -> None:
    if truth_format == "map":
        text = json.dumps({s["name"]: s["ground_truth"] for s in scenarios}, indent=2) + "\n"
    elif truth_format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "expected_cause", "kind"])
        for scenario in scenarios:
            writer.writerow([scenario["name"], scenario["ground_truth"], scenario.get("kind", "")])
        text = buffer.getvalue()
    else:
        cases = [
            {"id": s["name"], "expected_cause": s["ground_truth"], "kind": s.get("kind")}
            for s in scenarios
        ]
        text = json.dumps({"cases": cases}, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_report(results, identifier, metric, threshold, require_kind) -> dict:
    cases = []
    by_kind: dict[str, dict[str, int]] = {}
    total = passed = 0
    for result in results:
        bucket = by_kind.setdefault(result["kind"], {"total": 0, "passed": 0})
        for outcome in result["outcomes"]:
            bucket["total"] += 1
            total += 1
            if outcome["passed"]:
                bucket["passed"] += 1
                passed += 1
            cases.append(
                {
                    "id": f"{result['name']}#{outcome['run']}",
                    "scenario": result["name"],
                    "category": outcome["category"],
                    "score": outcome["score"],
                    "expected_cause": result["ground_truth"],
                    "actual_cause": outcome["likely_cause"],
                    "expected_kind": result["kind"],
                    "actual_kind": outcome["actual_kind"],
                    "kind_match": outcome["kind_match"],
                    "explanation_error": outcome["explanation_error"],
                    "fingerprint": outcome["fingerprint"],
                }
            )
    return {
        "schema_version": 1,
        "identifier": identifier,
        "metric": metric,
        "threshold": threshold,
        "require_kind": require_kind,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "by_kind": by_kind,
        "cases": cases,
    }


def print_summary(results, metric, threshold, identifier) -> None:
    report = build_report(results, identifier, metric, threshold, False)
    total, passed = report["total"], report["passed"]

    print()
    print(
        f"Watcher evaluation — {total} cases   "
        f"(identifier: {identifier}, metric: {metric}, threshold = {threshold:.2f})"
    )
    print()
    print(f"  PASS {passed} / {total}   {report['pass_rate'] * 100:.1f}%")

    print("\n  by detector kind")
    for kind, bucket in sorted(report["by_kind"].items()):
        rate = bucket["passed"] / bucket["total"] * 100 if bucket["total"] else 0.0
        print(f"    {kind:<18}{bucket['passed']}/{bucket['total']:<4}{rate:>7.1f}%")

    failures = [(result, outcome) for result in results for outcome in result["outcomes"] if not outcome["passed"]]
    if failures:
        print(f"\n  Failures ({len(failures)})")
        for result, outcome in failures:
            print(f"    {result['name']}#{outcome['run']:<16}{outcome['category']:<16}sim {outcome['score']:.2f}")
            print(f"      expected: {result['ground_truth']}")
            print(f"      actual:   {outcome['likely_cause'] or '(no explanation)'}")


def write_markdown_report(path: Path, results, source, runs, metric, threshold, identifier) -> None:
    report = build_report(results, identifier, metric, threshold, False)
    lines = [
        "# Watcher evaluation results",
        "",
        f"- Watcher output source: `{source}`",
        f"- Runs per scenario: {runs}",
        f"- Identifier: `{identifier}`, metric: `{metric}`, threshold: {threshold:.2f}",
        f"- Overall pass rate: **{report['pass_rate'] * 100:.0f}%** ({report['passed']}/{report['total']})",
        "",
        "| Kind | Passed | Total | Pass rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for kind, bucket in sorted(report["by_kind"].items()):
        rate = bucket["passed"] / bucket["total"] * 100 if bucket["total"] else 0.0
        lines.append(f"| {kind} | {bucket['passed']} | {bucket['total']} | {rate:.0f}% |")

    lines += ["", "## Cases", "", "| Case | Category | Score |", "| --- | --- | ---: |"]
    for case in report["cases"]:
        lines.append(f"| {case['id']} | {case['category']} | {case['score']:.2f} |")

    lines += ["", "## Detail", ""]
    for result in results:
        lines.append(f"**{result['name']}** ({result['kind']})")
        lines.append("")
        lines.append(f"Ground truth: {result['ground_truth']}")
        lines.append("")
        for outcome in result["outcomes"]:
            verdict = "PASS" if outcome["passed"] else "FAIL"
            cause = outcome["likely_cause"] or "(no explanation)"
            lines.append(
                f"- `{verdict}` run {outcome['run']} [{outcome['category']}] (sim {outcome['score']:.2f}): {cause}"
            )
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score Watcher's likely_cause against documented ground truth.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--watcher", default=None, help="Path or URL to Watcher's JSONL output or generic webhook payload.")
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario.")
    parser.add_argument("--scenario", action="append", default=None, help="Only evaluate this scenario (repeatable).")
    parser.add_argument("--wait", type=float, default=15.0, help="Seconds to wait for Watcher per triggered crash.")
    parser.add_argument(
        "--identifier",
        choices=("newest", "run_id"),
        default="newest",
        help="How a result is correlated with a scenario (design.md §2).",
    )
    parser.add_argument(
        "--metric",
        choices=("keywords", "similarity"),
        default="keywords",
        help="Pass metric: harness keywords, or the spec similarity (design.md §4).",
    )
    parser.add_argument("--threshold", type=float, default=0.60, help="Pass threshold for --metric similarity (tau).")
    parser.add_argument("--require-kind", action="store_true", help="Fail a case when the detector kind differs.")
    parser.add_argument("--offline", action="store_true", help="Score records in --watcher in order; do not touch Docker.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit the JSON report (design.md §5.2 shape).")
    parser.add_argument("--report", type=Path, default=None, help="Write a Markdown report here.")
    parser.add_argument("--min-pass-rate", type=float, default=None, help="CI gate: exit non-zero below this pass rate.")
    parser.add_argument("--emit-truth", type=Path, default=None, help="Write scenarios/*.json as a truth file and exit.")
    parser.add_argument(
        "--truth-format",
        choices=("cases", "map", "csv"),
        default="cases",
        help="Truth file shape for --emit-truth (design.md §3.1).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.emit_truth:
        scenarios = load_scenarios(args.scenario)
        if not scenarios:
            print("no scenarios selected", file=sys.stderr)
            return 2
        write_truth(args.emit_truth, scenarios, args.truth_format)
        print(f"wrote {args.truth_format} truth for {len(scenarios)} scenarios to {args.emit_truth}", file=sys.stderr)
        return 0

    if not args.watcher:
        print("--watcher is required (or use --emit-truth)", file=sys.stderr)
        return 2

    scenarios = load_scenarios(args.scenario)
    if not scenarios:
        print("no scenarios selected", file=sys.stderr)
        return 2

    results = []
    if args.offline:
        _, records = parse_payload(read_source(args.watcher))
        cursor = 0
        for scenario in scenarios:
            print(f"scoring scenario '{scenario['name']}' x{args.runs} (offline) ...", file=sys.stderr)
            results.append(
                run_scenario_offline(scenario, records, cursor, args.runs, args.metric, args.threshold, args.require_kind)
            )
            cursor += args.runs
    else:
        feed = WatcherFeed(args.watcher)
        try:
            for scenario in scenarios:
                print(f"running scenario '{scenario['name']}' x{args.runs} ...", file=sys.stderr)
                results.append(
                    run_scenario_online(
                        scenario, feed, args.runs, args.wait, args.metric, args.threshold, args.require_kind, args.identifier
                    )
                )
        finally:
            run_break("reset")

    report = build_report(results, args.identifier, args.metric, args.threshold, args.require_kind)

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print_summary(results, args.metric, args.threshold, args.identifier)

    if args.report:
        write_markdown_report(args.report, results, args.watcher, args.runs, args.metric, args.threshold, args.identifier)
        print(f"\nreport written to {args.report}", file=sys.stderr)

    if args.min_pass_rate is not None:
        verdict = report["pass_rate"] >= args.min_pass_rate
        print(
            f"\ngate: pass rate {report['pass_rate']:.2f} vs min {args.min_pass_rate:.2f} -> "
            f"{'PASS' if verdict else 'FAIL'}",
            file=sys.stderr,
        )
        return 0 if verdict else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
