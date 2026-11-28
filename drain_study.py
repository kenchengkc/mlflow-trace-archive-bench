"""Archive three traces per pass through MLflow until an entire queue is empty."""

import argparse
import gzip
import json
import platform
from importlib.metadata import version
from math import fsum
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from benchmark import positive_int, postgres_settings
from drain_fixture import seed_queue, verify_readback
from drain_measure import BATCH_SIZE, PHASES, RETENTION, measure_drain
from local_postgres import local_database
from mlflow_driver import MLFLOW_REVISION, open_store


def summarize(rounds):
    """Group timings by actual batch size so the last passes stay comparable."""
    by_count = {}
    for trial in rounds:
        for row in trial["passes"]:
            by_count.setdefault(row["archived_count"], []).append(row)
    summary = []
    for count, rows in sorted(by_count.items(), reverse=True):
        total = fsum(row["total_ms"] for row in rows)
        selection = fsum(row["selection_ms"] for row in rows)
        group = {
            "traces_per_pass": count, "passes": len(rows),
            "median_selection_ms": median(row["selection_ms"] for row in rows),
            "median_total_ms": median(row["total_ms"] for row in rows),
            "selection_share_pct": 100 * selection / total,
        }
        if "phase_ms" in rows[0]:
            # Means across the same passes add to mean total time, including the residual.
            totals = {"selection": selection}
            for phase in (*PHASES, "other"):
                totals[phase] = fsum(row["phase_ms"][phase] for row in rows)
            group["mean_total_ms"] = total / len(rows)
            group["mean_phase_ms"] = {phase: ms / len(rows) for phase, ms in totals.items()}
            group["phase_share_pct"] = {phase: 100 * ms / total for phase, ms in totals.items()}
        for name, parent, bucket in (("finalization", "phase_ms", "db_finalize"),
                                     ("orm", "finalization_ms", "other"),
                                     ("query", "orm_ms", "other"),
                                     ("result", "query_ms", "other"),
                                     ("result_detail", "result_ms", "results"),
                                     ("overhead", "result_ms", "other")):
            times, calls = name + "_ms", name + "_calls"
            if times not in rows[0]:
                continue
            parent_total = fsum(row[parent][bucket] for row in rows)
            operation_totals = {
                op: fsum(row[times][op] for row in rows) for op in rows[0][times]
            }
            group["mean_" + times] = {op: ms / len(rows) for op, ms in operation_totals.items()}
            group[name + "_share_pct"] = {
                op: 100 * ms / parent_total if parent_total else None
                for op, ms in operation_totals.items()
            }
            group[calls] = {op: sum(row[calls][op] for row in rows) for op in rows[0][calls]}
        if "query_cache_counts" in rows[0]:
            group["query_cache_counts"] = {
                key: sum(row["query_cache_counts"][key] for row in rows)
                for key in rows[0]["query_cache_counts"]
            }
        summary.append(group)
    return summary


def run(rounds=7, traces=92, *, profile_phases=False, profile_finalization=False,
        profile_orm=False, profile_queries=False, profile_results=False,
        profile_result_detail=False, profile_timer_overhead=False):
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
    profile_result_detail = profile_result_detail or profile_timer_overhead
    profile_results = profile_results or profile_result_detail
    profile_queries = profile_queries or profile_results
    profile_orm = profile_orm or profile_queries
    profile_finalization = profile_finalization or profile_orm
    if profile_phases or profile_finalization:
        report["phase_intervals"] = {
            "span_load": "Load trace metadata, tags and ordered span rows and check eligibility.",
            "serialization": "Decode stored spans and encode the trace as an OTLP protobuf.",
            "artifact_write": "Stage the protobuf and publish it through the local artifact repository.",
            "db_finalize": "Recheck the trace, clear span contents, update tags and commit.",
            "other": "Complete pass less selection and the four non-overlapping timed phases.",
        }
    if profile_finalization:
        report["finalization_interval"] = (
            "Within each _finalize_archived_trace call, measure driver cursor executions, "
            "connection commit, and rollback on pool reset. Other is the remaining elapsed time."
        )
    if profile_orm:
        report["orm_interval"] = (
            "Partition finalization time outside the measured driver calls. Each nested interval "
            "belongs to its innermost measured operation. Counts are calls to the timed methods."
        )
        report["orm_boundaries"] = {
            "fetch": "Retrieve rows from the psycopg2 cursor through SQLAlchemy, including cursor cleanup.",
            "objects": "Wrap raw rows, load or allocate mapped objects, and construct trace tags.",
            "session": "Create or close a session, begin a transaction, acquire its connection, merge, flush or commit.",
            "other": "Finalization time outside the measured SQLAlchemy and driver calls.",
        }
    if profile_queries:
        report["query_interval"] = (
            "Partition the remaining ORM 'other' time. Driver, fetch, object and session timers "
            "retain their time. Of the remaining intervals, the innermost timed query method wins. "
            "Call counts and cache outcomes include operations inside the earlier timers."
        )
        report["query_boundaries"] = {
            "construction": "Build query objects, predicates, loader options, and SELECT, UPDATE or DELETE statements.",
            "compile_cache": "Prepare the compiled-statement cache key, look up or store a compiled statement.",
            "compilation": "Generate SQL with SQLAlchemy's statement compiler.",
            "dispatch": "Process ORM execution options, resolve the database bind and run Connection.execute "
                        "through cursor-result setup.",
            "other": "Previously unassigned finalization time outside all timed query methods.",
        }
    if profile_results:
        report["result_interval"] = (
            "Partition query_ms.other. Earlier driver, ORM and query timers keep their time. Within "
            "the remainder, each interval belongs to the innermost eligibility or result-processing "
            "scope. Calls count all invocations of the timed methods."
        )
        report["result_boundaries"] = {
            "eligibility": "On successful finalizations, check the returned header, payload generation, "
                           "span location, failure tag and status, then confirm nonempty span content.",
            "results": "Set up ORM SELECT and bulk UPDATE/DELETE results, adapt scalar and unique results, "
                       "and consume single rows, including eager tag-loading work within those calls.",
            "other": "Time in the query remainder outside the eligibility and result-processing timers.",
        }
    if profile_result_detail:
        report["result_detail_interval"] = (
            "Partition result_ms.results using the same hook timestamps. Setup prepares ORM results, "
            "filtering configures scalar/unique adapters, and consumption reads rows and applies those filters."
        )
    if profile_timer_overhead:
        report["overhead_interval"] = (
            "Partition result_ms.other. Bookkeeping is time within additional timers surrounding the existing "
            "probe wrappers after excluding all previously attributed intervals. Includes observer forwarding; "
            "the remaining time also includes instrumentation outside the observed boundaries."
        )
    for number in range(1, rounds + 1):
        with local_database() as (sql, uri), open_store(uri) as store, TemporaryDirectory() as tmp:
            if number == 1:
                report["postgresql"] = postgres_settings(sql)
            expected = seed_queue(store, traces)
            archive_root = Path(tmp).resolve()
            passes = measure_drain(store, archive_root.as_uri(), expected, profile_phases=profile_phases,
                                   profile_finalization=profile_finalization, profile_orm=profile_orm,
                                   profile_queries=profile_queries, profile_results=profile_results,
                                   profile_result_detail=profile_result_detail,
                                   profile_timer_overhead=profile_timer_overhead)
            verification = verify_readback(store, sql, expected, archive_root)
            report["rounds"].append({"round": number, "passes": passes, "readback": verification})
    report["summary"] = summarize(report["rounds"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=7, help="independent queues to drain")
    parser.add_argument("--traces", type=positive_int, default=92, help="eligible two-span traces per queue")
    parser.add_argument("--profile-phases", action="store_true",
                        help="time span loading, serialization, local artifact writes and database finalization")
    parser.add_argument("--profile-finalization", action="store_true",
                        help="include phase timings and break database finalization into SQL calls, commit and other time")
    parser.add_argument("--profile-orm", action="store_true",
                        help="include driver timings and distinguish result fetching, mapped objects and session handling")
    parser.add_argument("--profile-queries", action="store_true",
                        help="break down remaining ORM time into query construction, cache lookup, compilation and dispatch")
    parser.add_argument("--profile-results", action="store_true",
                        help="break down remaining query time into MLflow eligibility checks and SQLAlchemy result processing")
    parser.add_argument("--output", type=Path, help="save every pass and its verification as JSON or JSON.gz")
    args = parser.parse_args()
    report = run(args.rounds, args.traces, profile_phases=args.profile_phases,
                 profile_finalization=args.profile_finalization, profile_orm=args.profile_orm,
                 profile_queries=args.profile_queries, profile_results=args.profile_results)
    print("| Traces per pass | Passes | Median selection (ms) | Median total (ms) | Share of total time |")
    print("| ---: | ---: | ---: | ---: | ---: |")
    for row in report["summary"]:
        print(f"| {row['traces_per_pass']} | {row['passes']} | {row['median_selection_ms']:.2f} "
              f"| {row['median_total_ms']:.2f} | {row['selection_share_pct']:.1f}% |")
    if "phase_intervals" in report:
        print("\n| Traces per pass | Phase | Mean ms per pass | Share of total time |")
        print("| ---: | --- | ---: | ---: |")
        for row in report["summary"]:
            for phase, ms in row["mean_phase_ms"].items():
                print(f"| {row['traces_per_pass']} | {phase} | {ms:.3f} | {row['phase_share_pct'][phase]:.2f}% |")
    for name, label in (("finalization", "Finalization operation"), ("orm", "SQLAlchemy operation"),
                        ("query", "Query operation"), ("result", "Eligibility or result operation")):
        if name + "_interval" not in report:
            continue
        print(f"\n| Traces per pass | {label} | Total calls | Mean ms per pass |")
        print("| ---: | --- | ---: | ---: |")
        for row in report["summary"]:
            for op, ms in row["mean_" + name + "_ms"].items():
                calls = row[name + "_calls"].get(op, "—")
                print(f"| {row['traces_per_pass']} | {op} | {calls} | {ms:.3f} |")
    if "query_interval" in report:
        print("\n| Traces per pass | Compiled-statement cache outcome | Count |")
        print("| ---: | --- | ---: |")
        for row in report["summary"]:
            for outcome, count in row["query_cache_counts"].items():
                print(f"| {row['traces_per_pass']} | {outcome} | {count} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")


if __name__ == "__main__":
    main()
