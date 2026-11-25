"""Time MLflow's archival finder and two direct SQL controls on the same migrated database."""
import argparse
import gzip
import hashlib
import json
import platform
from importlib.metadata import version
from itertools import permutations
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from benchmark import ALL_EXPERIMENTS, QUERY, positive_int, postgres_settings
from driver_fixture import prepare_driver_queue
from local_postgres import local_database
from matched_queues import inspect_queue
from mlflow_driver import MLFLOW_REVISION, capture_statements, explain, find_candidates, open_store

CUTOFF = 1700000000000
METHODS = ("mlflow", "raw_global", "replayed_sql")


def ordered_rows(rows, method):
    if method == "mlflow":
        return [f"{c.trace_id}|{c.experiment_id}|{c.timestamp_ms}" for c in rows]
    return [f"{trace}|{experiment}|{timestamp}" for trace, experiment, timestamp in rows]


def measure_queue(store, batch_size, samples, expected):
    experiments = [str(i) for i in ALL_EXPERIMENTS]
    # Migrations can leave several idle connections. Start the sequential study
    # with an empty pool so the untimed capture opens the backend all arms reuse.
    store.engine.dispose()
    with capture_statements(store.engine) as statements, store.ManagedSessionMaker() as session:
        initial = find_candidates(store, session, experiments, CUTOFF, batch_size)
    if ordered_rows(initial, "mlflow") != expected:
        raise RuntimeError("MLflow returned different ordered rows than the fixture oracle")
    if len(statements) != 1 or not statements[0][0].lstrip().upper().startswith("SELECT"):
        raise RuntimeError("Expected one captured candidate SELECT for eight experiments")

    controls = {
        "raw_global": (QUERY.replace(":batch_size", "%(batch_size)s"), {"batch_size": batch_size}),
        "replayed_sql": statements[0],
    }
    measurements = {name: {"transaction_ms": [], "select_ms": []} for name in METHODS}
    backend_pid = None
    orders = list(permutations(METHODS))
    # An untimed warm-up per method precedes complete cycles of the six possible orders.
    for round_no in range(-1, samples):
        order = METHODS if round_no < 0 else orders[round_no % len(orders)]
        for method in order:
            started = perf_counter_ns()
            with store.ManagedSessionMaker() as session:
                connection = session.connection()
                pid = connection.connection.driver_connection.get_backend_pid()
                before_select = perf_counter_ns()
                if method == "mlflow":
                    result = find_candidates(store, session, experiments, CUTOFF, batch_size)
                else:
                    statement, params = controls[method]
                    result = connection.exec_driver_sql(statement, params).all()
                after_select = perf_counter_ns()
            finished = perf_counter_ns()
            if backend_pid is None:
                backend_pid = pid
            if pid != backend_pid:
                raise RuntimeError("The PostgreSQL backend changed during the comparison")
            if ordered_rows(result, method) != expected:
                raise RuntimeError(f"{method} returned different ordered rows in round {round_no}")
            if round_no >= 0:
                measurements[method]["transaction_ms"].append((finished - started) / 1e6)
                measurements[method]["select_ms"].append((after_select - before_select) / 1e6)

    # EXPLAIN instrumentation is deliberately outside all caller-side timings.
    plans = {}
    with store.ManagedSessionMaker() as session:
        for name, (statement, params) in controls.items():
            plans[name] = {"sql": statement, "bindings": params, "plan": explain(session, statement, params)}
    return {"measurements": measurements, "plans": plans, "backend_pid": backend_pid,
            "captured_selects_per_finder_call": len(statements)}


def run(rows, batch_size, samples, seed):
    report = {
        "mlflow_revision": MLFLOW_REVISION, "system": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(), "packages": {p: version(p) for p in
        ("mlflow", "SQLAlchemy", "psycopg2-binary")}, "rows": rows, "batch_size": batch_size,
        "samples_per_method": samples, "order_cycle": list(permutations(METHODS)),
        "experiment_ids": list(ALL_EXPERIMENTS), "cutoff_ms": CUTOFF, "seed": seed, "cases": [],
    }
    for label, pct, layout in (("fresh", 0, "oldest_first"), ("oldest_first", 95, "oldest_first"),
                               ("scattered", 95, "scattered")):
        with local_database() as (sql, uri), open_store(uri) as store:
            counts = prepare_driver_queue(sql, rows, pct, layout=layout, seed=seed)
            queue = inspect_queue(sql, batch_size)
            expected = queue.pop("expected_rows")
            remaining = sum(group["remaining"] for group in queue["strata"])
            archived = sum(group["archived"] for group in queue["strata"])
            if (archived != counts["archived"] or remaining + archived != counts["eligible_before"]
                    or archived != counts["eligible_before"] * pct // 100
                    or len(expected) != min(batch_size, remaining)):
                raise RuntimeError(f"Unexpected archive population for {label}")
            settings = postgres_settings(sql)
            revision = sql("SELECT version_num FROM alembic_version ORDER BY version_num")
            cases = report["cases"]
            if cases:
                if (settings != report["postgres_settings"] or revision != report["alembic_revision"]
                        or queue["header_sha256"] != cases[0]["header_sha256"]):
                    raise RuntimeError("Database settings, migrations or trace headers changed between queues")
                if label == "scattered" and queue["strata"] != cases[-1]["strata"]:
                    raise RuntimeError("Archived counts by experiment and span count differ between layouts")
            report.update(postgres_settings=settings, alembic_revision=revision)
            timings = measure_queue(store, batch_size, samples, expected)
            digest = hashlib.sha256("\n".join(expected).encode()).hexdigest()
            cases.append({"layout": label, "archived_pct": pct, **counts, **queue, "remaining": remaining,
                          "returned": len(expected), "result_sha256": digest, **timings})
    return report


def print_table(report):
    print("| Queue | MLflow finder (ms) | Raw global SQL (ms) | MLflow SQL replay (ms) |")
    print("| --- | ---: | ---: | ---: |")
    for case in report["cases"]:
        cells = [f"{median(case['measurements'][m]['transaction_ms']):.2f}" for m in METHODS]
        print(f"| {case['layout']} | " + " | ".join(cells) + " |")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100000)
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--samples", type=positive_int, default=12, help="rounds per method per queue")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, help="save wall times, bound SQL, plans and fixture checks")
    args = parser.parse_args()
    report = run(args.rows, args.batch_size, args.samples, args.seed)
    print_table(report)
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")


if __name__ == "__main__":
    main()
