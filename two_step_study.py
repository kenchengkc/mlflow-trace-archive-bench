"""Compare MLflow's finder, a bounded SQL statement and its two-step alternative."""
import argparse
import gzip
import json
from functools import partial
from itertools import permutations
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from benchmark import ALL_EXPERIMENTS, positive_int
from driver_study import CUTOFF, ordered_rows, run_queues
from mlflow_driver import capture_statements, explain, find_candidates
from study import parse_scope
from two_step import bounded_candidates, snapshot_connection, two_step_candidates

METHODS = ("mlflow", "bounded_probe", "two_step")


def select_batch(store, session, method, experiments, batch_size):
    if method == "two_step":
        connection = snapshot_connection(session)
        rows, fallback = two_step_candidates(connection, experiments, CUTOFF, batch_size)
        return rows, fallback, connection
    connection = session.connection()
    if method == "mlflow":
        rows = find_candidates(store, session, experiments, CUTOFF, batch_size)
    else:
        rows = bounded_candidates(connection, experiments, CUTOFF, batch_size)
    return rows, False, connection


def describe_call(store, method, experiments, batch_size, expected):
    # Capture the application SQL first. Collect the transaction settings and
    # EXPLAIN plans separately from the caller-side latency samples.
    with capture_statements(store.engine) as captured, store.ManagedSessionMaker() as session:
        rows, fallback, connection = select_batch(store, session, method, experiments, batch_size)
        statements = list(captured)
        if ordered_rows(rows, method) != expected:
            raise RuntimeError(f"{method} returned different ordered rows than the fixture oracle")
        if len(statements) != 1 + int(fallback):
            raise RuntimeError(f"Unexpected number of SQL statements during {method}: {len(statements)}")
        isolation, read_only = connection.exec_driver_sql(
            "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')"
        ).one()
        if method == "two_step" and (isolation, read_only) != ("repeatable read", "on"):
            raise RuntimeError("The two-step selector needs a read-only repeatable-read transaction")
        plans = [
            {"sql": statement, "bindings": params, "plan": explain(session, statement, params)}
            for statement, params in statements
        ]
    return {"candidate_selects": len(statements), "issued_fallback_select": fallback,
            "isolation": isolation, "read_only": read_only == "on", "plans": plans}


def measure_queue(store, batch_size, samples, expected, *, experiment_ids):
    experiments = [str(i) for i in experiment_ids]
    # A fresh pool prevents rotation among idle connections left by migrations.
    store.engine.dispose()
    diagnostics = {m: describe_call(store, m, experiments, batch_size, expected) for m in METHODS}
    measurements = {m: {"transaction_ms": []} for m in METHODS}
    backend_pid = None
    fallback_calls = 0
    orders = list(permutations(METHODS))
    # Warm each method, then balance its position and neighbors in the measured rounds.
    for round_no in range(-1, samples):
        for method in METHODS if round_no < 0 else orders[round_no % len(orders)]:
            start = perf_counter_ns()
            with store.ManagedSessionMaker() as session:
                rows, fallback, connection = select_batch(store, session, method, experiments, batch_size)
                pid = connection.connection.driver_connection.get_backend_pid()
            elapsed = (perf_counter_ns() - start) / 1e6
            if backend_pid is None:
                backend_pid = pid
            if pid != backend_pid:
                raise RuntimeError("The PostgreSQL backend changed during the comparison")
            if ordered_rows(rows, method) != expected or fallback != diagnostics[method]["issued_fallback_select"]:
                raise RuntimeError(f"{method} changed the ordered result or branch in round {round_no}")
            if round_no >= 0:
                measurements[method]["transaction_ms"].append(elapsed)
                fallback_calls += int(fallback)
    return {"diagnostics": diagnostics, "measurements": measurements, "backend_pid": backend_pid,
            "fallback_calls": fallback_calls}


def run(rows, batch_size, samples, seed, experiment_ids=ALL_EXPERIMENTS, fresh_only=False):
    measure = partial(measure_queue, experiment_ids=experiment_ids)
    report = run_queues(rows, batch_size, samples, seed, methods=METHODS,
                        measure=measure, experiment_ids=experiment_ids, fresh_only=fresh_only)
    for case in report["cases"]:
        prefix_suffices = (case["matching_headers"] < case["probe_limit"]
                           or case["eligible_in_prefix"] >= batch_size)
        if case["fallback_calls"] != (0 if prefix_suffices else samples):
            raise RuntimeError(f"Two-step fallback usage disagrees with the fixture for {case['layout']}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100000)
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--samples", type=positive_int, default=12, help="rounds per method per queue")
    parser.add_argument("--scope", type=parse_scope, default=ALL_EXPERIMENTS,
                        help="comma-separated experiment IDs (default: all eight)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fresh-only", action="store_true",
                        help="repeat the fresh-queue control alone, e.g. at one million traces")
    parser.add_argument("--output", type=Path, help="save raw samples, SQL, plans and fixture checks")
    args = parser.parse_args()
    report = run(args.rows, args.batch_size, args.samples, args.seed, args.scope, args.fresh_only)
    print("| Queue | MLflow (ms) | Bounded probe (ms) | Two-step (ms) | Two-step fallback calls |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for case in report["cases"]:
        cells = [f"{median(case['measurements'][m]['transaction_ms']):.2f}" for m in METHODS]
        print(f"| {case['layout']} | " + " | ".join(cells) + f" | {case['fallback_calls']}/{args.samples} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")


if __name__ == "__main__":
    main()
