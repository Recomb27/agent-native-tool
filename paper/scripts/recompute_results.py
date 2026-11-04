#!/usr/bin/env python3
"""Reconstruct the V13.3 two-arm and original-reference summaries from the bundled frozen CSV tables.

Python 3 standard library only. This script does not run campaigns or fetch raw
logs. It checks the supplied derived records and writes outputs/analysis/recomputed.json.
"""
import argparse
from collections import Counter
from pathlib import Path
import csv
import json
import math
import random
import statistics

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED, DRAWS = 20261104, 20000


def rows(name):
    with (DATA / name).open(newline="") as stream:
        return list(csv.DictReader(stream))


def mean(values):
    return statistics.mean(values)


def close(a, b):
    return math.isclose(float(a), float(b), rel_tol=1e-11, abs_tol=1e-8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT.parent / "outputs" / "analysis")
    args = parser.parse_args()
    pairs = rows("paired_endpoints.csv")
    tools = sorted({r["tool"] for r in pairs})
    repeats = sorted({int(r["repeat"]) for r in pairs})
    assert len(pairs) == 120 and len(tools) == 12 and repeats == list(range(1, 11))
    assert len({(r["tool"], r["repeat"]) for r in pairs}) == 120
    arms = ["original", "adapted", "original_control"]
    metrics = ["cash", "activity", "reward", "cost", "days", "profit"]
    controls = {}
    for arm in arms[2:]:
        controls[arm] = []
        for repeat in repeats:
            block = [r for r in pairs if int(r["repeat"]) == repeat]
            fields = [k for k in block[0] if k.startswith(arm + "__")]
            assert all(all(r[k] == block[0][k] for k in fields) for r in block)
            controls[arm].append(block[0])
        assert len({r[arm + "__run_dir"] for r in controls[arm]}) == 10
    means = {arm: {metric: mean(float(r[arm + "__" + metric])
                                for r in controls.get(arm, pairs))
                   for metric in metrics} for arm in arms}
    for arm in arms:
        for r in controls.get(arm, pairs):
            get = lambda m: float(r[arm + "__" + m])
            assert close(get("cash"), 100000 - get("cost") + get("reward"))
            assert close(get("profit"), get("cash") - 100000)
            assert 0 <= get("days") <= 180 and get("activity") >= 1

    rng = random.Random(SEED)
    draws = [rng.choices(range(10), k=10) for _ in range(DRAWS)]

    def interval(records, difference):
        blocks = [mean(difference(r) for r in records if int(r["repeat"]) == repeat)
                  for repeat in repeats]
        samples = sorted(sum(blocks[i] for i in draw) / 10 for draw in draws)
        return {"mean": mean(blocks), "ci95": [samples[499], samples[19499]]}

    pooled = {m: interval(pairs, lambda r, m=m:
                         float(r["adapted__" + m]) - float(r["original__" + m]))
              for m in metrics}
    reported = json.loads((DATA / "endpoints.json").read_text())
    for arm in arms:
        for metric in metrics:
            assert close(means[arm][metric], reported["means"][arm][metric])
    for metric in metrics:
        assert close(pooled[metric]["mean"], reported["pooled"][metric]["mean"])
        assert all(close(x, y) for x, y in zip(pooled[metric]["ci95"],
                                              reported["pooled"][metric]["ci95"]))
    contrasts = {arm: {metric: interval(pairs, lambda r, arm=arm, metric=metric:
        float(r[arm + "__" + metric]) - float(r["original_control__" + metric]))
        for metric in ("cash", "activity")} for arm in arms[:2]}

    scores = rows("six_dimension_scores.csv")
    lookup = {(r["tool"], r["arm"], r["dimension"]): int(r["score"]) for r in scores}
    assert len(scores) == len(lookup) == 144
    assert all(0 <= value <= 10 for value in lookup.values())
    score_summary = {}
    for dimension in [f"D{i}" for i in range(1, 7)]:
        o = [lookup[t, "original", dimension] for t in tools]
        a = [lookup[t, "adapted", dimension] for t in tools]
        delta = [y - x for x, y in zip(o, a)]
        score_summary[dimension] = {"original": mean(o), "adapted": mean(a),
            "up": sum(d > 0 for d in delta), "same": sum(d == 0 for d in delta),
            "down": sum(d < 0 for d in delta)}
    for r in rows("six_dimension_deltas.csv"):
        o, a = [lookup[r["tool"], arm, r["dimension"]] for arm in arms[:2]]
        assert (int(r["original"]), int(r["adapted"]), int(r["delta"])) == (o, a, a-o)

    usage = rows("campaign_usage_and_strategy.csv")
    assert len(usage) == 240
    def truth(value):
        return str(value).lower() in {"true", "1"}
    coverage = {arm: {field: sum(truth(r[field]) for r in usage if r["arm"] == arm)
                      for field in ("invoked", "trace_reference")} for arm in arms[:2]}
    stops = Counter(r["adapted__stop_code"] for r in pairs)
    assert stops == {"agent_stop": 118, "time_insufficient": 2}
    assert all(r["adapted__budget_exhausted"].lower() == "false" for r in pairs)
    cash_changes = [float(r["adapted__cash"]) - float(r["original__cash"]) for r in pairs]
    result = {"scope": "Bundled derived two-arm records; no raw-log re-adjudication",
        "physical_campaigns": 250, "original_no_tool_reference_runs": 10, "repeat_clusters": 10, "bootstrap_draws": DRAWS, "seed": SEED,
        "means": means, "pooled": pooled, "original_reference_contrasts": contrasts,
        "relative_change_percent": {m: (means["adapted"][m]/means["original"][m]-1)*100
                                    for m in ("cash", "activity")},
        "six_dimensions": score_summary, "usage": coverage,
        "adapted_tool_stops": dict(stops),
        "cash_pairs": {"up": sum(d > 0 for d in cash_changes),
                       "same": sum(d == 0 for d in cash_changes),
                       "down": sum(d < 0 for d in cash_changes)},
        "campaigns_above_initial_activity": {arm: sum(float(r[arm+"__activity"]) > 1
                                                      for r in pairs) for arm in arms[:2]}}
    destination = args.output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "recomputed.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "passed", "pairs": len(pairs), "scores": len(scores),
                      "output": str(destination / "recomputed.json")}))


if __name__ == "__main__":
    main()
