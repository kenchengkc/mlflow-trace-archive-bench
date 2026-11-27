"""Archive three traces per pass through MLflow until an entire queue is empty."""

import argparse
import gzip
import json
import platform
from importlib.metadata import version
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from benchmark import positive_int, postgres_settings
from drain_fixture import seed_queue, verify_readback
from drain_measure import BATCH_SIZE, RETENTION, measure_drain
from local_postgres import local_database
from mlflow_driver import MLFLOW_REVISION, open_store


def summarize(rounds):
    """Group timings by actual batch size so the last passes stay comparable."""
    by_count = {}
    for trial in rounds:
        for row in trial["passes"]:
            by_count.setdefault(row["archived_count"], []).append(row)
    return [
        {
            "traces_per_pass": count, "passes": len(rows),
            "median_selection_ms": median(row["selection_ms"] for row in rows),
            "median_total_ms": median(row["total_ms"] for row in rows),
            "selection_share_pct": 100 * sum(row["selection_ms"] for row in rows)
                                      / sum(row["total_ms"] for row in rows),
        }
        for count, rows in sorted(by_count.items(), reverse=True)
    ]


def run(rounds=7, traces=92):
    if rounds < 1 or traces < 1:
        raise ValueError("rounds and traces must be positive")
    report = {
        "round_count": rounds, "traces_per_round": traces, "max_traces_per_pass": BATCH_SIZE,
        "broader_retention": RETENTION, "artifact_storage": "local filesystem",
        "platform": platform.platform(), "python": platform.python_version(),
        "packages": {name: version(name) for name in ("mlflow", "SQLAlchemy", "psycopg2-binary")},
        "mlflow_revision": MLFLOW_REVISION, "rounds": [],
        "selection_interval": "Elapsed time inside _plan_trace_archival within each archive_traces call.",
        "total_interval": "Elapsed time around the full synchronous archive_traces call, including selection.",
    }
    for number in range(1, rounds + 1):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            if number == 1:
                report["postgresql"] = postgres_settings(sql)
            expected = seed_queue(store, traces)
            archive_root = Path(tmp).resolve()
            passes = measure_drain(store, archive_root.as_uri(), expected)
            verification = verify_readback(store, sql, expected, archive_root)
            report["rounds"].append({"round": number, "passes": passes, "readback": verification})
    report["summary"] = summarize(report["rounds"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=7, help="independent queues to drain")
    parser.add_argument("--traces", type=positive_int, default=92, help="eligible two-span traces per queue")
    parser.add_argument("--output", type=Path, help="save every pass and its verification as JSON or JSON.gz")
    args = parser.parse_args()
    report = run(args.rounds, args.traces)
    print("| Traces per pass | Passes | Median selection (ms) | Median total (ms) | Share of total time |")
    print("| ---: | ---: | ---: | ---: | ---: |")
    for row in report["summary"]:
        print(f"| {row['traces_per_pass']} | {row['passes']} | {row['median_selection_ms']:.2f} "
              f"| {row['median_total_ms']:.2f} | {row['selection_share_pct']:.1f}% |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")


if __name__ == "__main__":
    main()
