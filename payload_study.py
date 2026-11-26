"""Compare small and larger incoming span payloads during MLflow archival selection."""
import argparse
import gzip
from itertools import permutations
import json
from pathlib import Path
from statistics import median

from benchmark import positive_int
from chunk_ingest_study import run_trials


MODES = ("writer_only", "mixed_1000", "mixed_1001")
PADDING_BYTES = (0, 16 * 1024, 64 * 1024)


def trial_schedule(rounds):
    """Pair sizes within mode; balance all orders of modes and sizes over six rounds."""
    mode_orders = list(permutations(MODES))
    size_orders = list(permutations(PADDING_BYTES))
    for r in range(rounds):
        for mode in mode_orders[r % len(mode_orders)]:
            # Stagger the size order across workloads within a round.
            size_order = size_orders[(r + MODES.index(mode)) % len(size_orders)]
            for padding in size_order:
                yield r + 1, mode, padding


def verify_payload_comparison(report):
    """Check that every trial persisted the same traces apart from the payload attribute."""
    count = report["traces_per_writer_trial"]
    base_digests = set()
    for trial in report["trials"]:
        writer = trial["writer"]
        payload = writer["payload"]
        if writer["trace_headers"] != count or writer["root_spans"] != count:
            raise RuntimeError("Trace or span count changed between payload trials")
        if payload["attribute_bytes_per_span"] != trial["padding_bytes"]:
            raise RuntimeError("Stored payload size differs from the requested treatment")
        base_digests.add(payload["base_content_sha256"])
    if len(base_digests) != 1:
        raise RuntimeError("Base span contents differ between payload trials")


def summarize(report):
    """Compute medians across trials and percent changes from the matched small-span control."""
    rows = []
    for mode in MODES:
        for padding in PADDING_BYTES:
            trials = [t for t in report["trials"] if t["mode"] == mode and t["padding_bytes"] == padding]
            throughput = median(t["writer"]["traces_per_second"] for t in trials)
            finder = None if mode == "writer_only" else median(t["finder"]["median_ms"] for t in trials)
            if padding == 0:
                small_rate, small_finder = throughput, finder
            rows.append({
                "mode": mode, "padding_bytes": padding,
                "median_traces_per_second": throughput, "median_finder_ms": finder,
                "throughput_change_pct": 100 * (throughput / small_rate - 1),
                "finder_change_pct": None if finder is None else 100 * (finder / small_finder - 1),
            })
    return rows


def run(rounds=6, traces=512):
    if rounds < 1:
        raise ValueError("Use at least one measurement round")
    settings = {
        "round_count": rounds, "padding_sizes_bytes": list(PADDING_BYTES),
        "attribute_key": "benchmark.payload",
        "payload_generation": "Prefix of base64-encoded SHAKE-256 bytes seeded with "
                              "mlflow-trace-archive-bench/{series}/{index}, prepared before timing.",
    }
    report = run_trials(trial_schedule(rounds), traces, idle_calls=0, study_settings=settings)
    verify_payload_comparison(report)
    report["summary"] = summarize(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=6, help="six rounds balance trial positions")
    parser.add_argument("--traces", type=positive_int, default=512, help="single-trace commits per trial")
    parser.add_argument("--output", type=Path, help="save raw transactions, commits and stored byte counts")
    args = parser.parse_args()
    report = run(args.rounds, args.traces)
    print("| Workload | Added bytes per span | Finder median (ms) | Change vs small "
          "| Committed traces/s | Change vs small |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in report["summary"]:
        latency = "—" if row["median_finder_ms"] is None else f"{row['median_finder_ms']:.2f}"
        change = "—" if row["finder_change_pct"] is None else f"{row['finder_change_pct']:+.1f}%"
        print(f"| {row['mode']} | {row['padding_bytes']:,} | {latency} | {change} | "
              f"{row['median_traces_per_second']:.2f} | {row['throughput_change_pct']:+.1f}% |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as target:
            json.dump(report, target)
            target.write("\n")


if __name__ == "__main__":
    main()
