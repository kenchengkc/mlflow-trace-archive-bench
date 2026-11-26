"""Measure where larger spans add writer time during concurrent archival selection."""

import argparse
from collections import defaultdict
import gzip
from itertools import product
import json
from pathlib import Path

from benchmark import positive_int
from chunk_ingest_study import SIZES, run_trials
from ingest_phases import PHASES
from payload_study import verify_payload_comparison


LARGE_BYTES = 64 * 1024
CONDITIONS = tuple(product(("mixed_1000", "mixed_1001"), (0, LARGE_BYTES), (False, True)))
# One completed, newly inserted root span through the pinned MLflow store.
# Both the auxiliary lookup session and the session doing the writes commit.
EXPECTED_CALLS = {"build_span": 1, "translate_span": 1, "encode_content": 1, "span_write": 1,
                  "other_write": 5, "other_sql": 4, "commit": 2}


def trial_schedule(rounds):
    """Balance trial position and within-round predecessor over eight rounds."""
    # The seven differences between adjacent indices cover 1..7 modulo eight.
    # Shifting the indices once per round visits every ordered pair of distinct conditions.
    positions = (0, 1, 7, 2, 6, 3, 5, 4)
    for r in range(rounds):
        for position in positions:
            yield r + 1, *CONDITIONS[(position + r) % len(CONDITIONS)]


def phase_totals(writer, count):
    """Reconcile each trace to its wall interval before combining any observations."""
    ends = writer["commit_offsets_ns"]
    samples = writer["phase_samples"]
    if len(ends) != count or len(samples) != count or ends[-1] != writer["elapsed_ns"]:
        raise ValueError("Phase samples and acknowledgments must cover the entire writer trial")
    totals = dict.fromkeys((*PHASES, "residual"), 0)
    previous = 0
    for end, sample in zip(ends, samples):
        durations = sample["duration_ns"]
        calls = sample["calls"]
        if set(durations) != set(PHASES):
            raise ValueError("Incomplete phase sample")
        if calls != EXPECTED_CALLS:
            raise ValueError(f"Unexpected writer phase call counts: {calls}")
        if any(ns < 0 for ns in durations.values()):
            raise ValueError("Negative writer phase duration")
        residual = end - previous - sum(durations.values())
        if residual < 0 or end <= previous:
            raise ValueError("Writer phases exceed their trace's elapsed time")
        totals["residual"] += residual
        for key, value in durations.items():
            totals[key] += value
        previous = end
    return {**totals, "total": previous}


def summarize(report):
    """Arithmetic means keep component costs additive at equal write counts."""
    count = report["traces_per_writer_trial"]
    rounds = report["round_count"]
    trials = defaultdict(dict)
    for trial in report["trials"]:
        key = (trial["mode"], trial["padding_bytes"], trial["profiled"])
        if trial["round"] in trials[key] or key not in CONDITIONS:
            raise ValueError("Unexpected or duplicate treatment in a round")
        trials[key][trial["round"]] = trial["writer"]
    if any(set(trials[key]) != set(range(1, rounds + 1)) for key in CONDITIONS):
        raise ValueError("Each round needs all eight treatment combinations")

    result = {}
    divisor = 1e6 * count * rounds
    for size in SIZES:
        scope = {}
        for padding, label in [(0, "small"), (LARGE_BYTES, "64_kib")]:
            mode = f"mixed_{size}"
            controls = trials[mode, padding, False].values()
            measured = [phase_totals(writer, count) for writer in trials[mode, padding, True].values()]
            scope[label] = {
                "profiled_ms_per_trace": {key: sum(row[key] for row in measured) / divisor
                                          for key in measured[0]},
                "control_ms_per_trace": sum(w["elapsed_ns"] for w in controls) / divisor,
            }
        large, small = scope["64_kib"], scope["small"]
        scope["delta"] = {
            "profiled_ms_per_trace": {key: value - small["profiled_ms_per_trace"][key]
                                      for key, value in large["profiled_ms_per_trace"].items()},
            "control_ms_per_trace": large["control_ms_per_trace"] - small["control_ms_per_trace"],
        }
        result[str(size)] = scope
    return result


def run(rounds=8, traces=512):
    if rounds < 1:
        raise ValueError("Use at least one measurement round")
    settings = {
        "round_count": rounds, "padding_sizes_bytes": [0, LARGE_BYTES],
        "attribute_key": "benchmark.payload", "phase_unit": "nanoseconds",
        "aggregation": "Sum elapsed time across all traces and divide by their count. "
                       "Residual is each acknowledgment interval minus its measured phases.",
    }
    report = run_trials(trial_schedule(rounds), traces, idle_calls=0, study_settings=settings,
                        record_durability=True)
    verify_payload_comparison(report)
    report["summary"] = summarize(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=8, help="eight rounds balance trial order")
    parser.add_argument("--traces", type=positive_int, default=512, help="single-span commits per trial")
    parser.add_argument("--output", type=Path, help="save per-trace phases, counts and wall timings")
    args = parser.parse_args()
    report = run(args.rounds, args.traces)
    print("| Selected experiments | Writer phase | Small (ms/trace) | +64 KiB (ms/trace) | Change (ms/trace) |")
    print("| ---: | --- | ---: | ---: | ---: |")
    for size, scope in report["summary"].items():
        arms = [scope[label] for label in ("small", "64_kib", "delta")]
        for phase in (*PHASES, "residual", "total"):
            values = " | ".join(f"{arm['profiled_ms_per_trace'][phase]:.3f}" for arm in arms)
            print(f"| {size} | {phase} | {values} |")
        values = " | ".join(f"{arm['control_ms_per_trace']:.3f}" for arm in arms)
        print(f"| {size} | total without instrumentation | {values} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as target:
            json.dump(report, target)
            target.write("\n")


if __name__ == "__main__":
    main()
