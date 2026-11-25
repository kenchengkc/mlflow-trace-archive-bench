"""Measure MLflow's ordered archival batch on either side of its experiment chunk boundary."""

import argparse
import gzip
import hashlib
import json
import platform
import re
from importlib.metadata import version
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from benchmark import positive_int, postgres_settings
from chunk_fixture import BATCH_SIZE, CUTOFF, expected_candidates, prepare_chunk_fixture
from local_postgres import local_database
from mlflow_driver import MLFLOW_REVISION, capture_statements, explain, find_candidates, open_store

CHUNK_SIZE = 1000


def candidate_rows(candidates):
    return [(c.trace_id, int(c.experiment_id), c.timestamp_ms) for c in candidates]


def verify_scope(store, experiment_ids):
    """Verify the finder and its individual SQL chunks outside the timed transactions."""
    reference = expected_candidates(experiment_ids, BATCH_SIZE + 1)
    expected, first_excluded = reference[:BATCH_SIZE], reference[BATCH_SIZE]
    with capture_statements(store.engine) as statements, store.ManagedSessionMaker() as session:
        actual = candidate_rows(find_candidates(store, session, experiment_ids, CUTOFF, BATCH_SIZE))
    if actual != expected:
        raise RuntimeError("MLflow's merged result differs from the independent global ordering")
    if actual[-1][2] != first_excluded[2]:
        raise RuntimeError("The batch limit must split a timestamp tie")

    chunks = [experiment_ids[i:i + CHUNK_SIZE] for i in range(0, len(experiment_ids), CHUNK_SIZE)]
    if len(statements) != len(chunks):
        raise RuntimeError(f"Expected {len(chunks)} candidate SQL statements, got {len(statements)}")
    queries = []
    selected = set(actual)
    with store.ManagedSessionMaker() as session:
        for ids, (statement, bindings) in zip(chunks, statements):
            # Read bind names in SQL order. The captured parameter dictionary alone
            # cannot establish which IDs were used, or the order they were sent in.
            id_binds = re.findall(r"%\((experiment_id_\d+_\d+)\)s", statement)
            limit = re.search(r"\bLIMIT\s+%\((\w+)\)s", statement)
            if (not statement.lstrip().upper().startswith("SELECT")
                    or [bindings[k] for k in id_binds] != [int(i) for i in ids]
                    or not limit or bindings[limit[1]] != BATCH_SIZE):
                raise RuntimeError("Unexpected SQL, experiment bindings or per-chunk limit")
            replay = [tuple(row) for row in session.connection().exec_driver_sql(statement, bindings)]
            if replay != expected_candidates(ids, BATCH_SIZE):
                raise RuntimeError("A captured query returned a different per-chunk top batch")
            plan = explain(session, statement, bindings)
            if plan["Plan"]["Actual Rows"] != len(replay):
                raise RuntimeError("Plan row count differs from the verified query replay")
            queries.append({
                "experiment_count": len(ids), "first_experiment_id": int(ids[0]),
                "last_experiment_id": int(ids[-1]), "candidate_limit": bindings[limit[1]],
                "rows_fetched": len(replay), "rows_in_final_batch": sum(row in selected for row in replay),
                "sql": statement, "bindings": bindings, "plan": plan,
            })
    if sum(q["rows_in_final_batch"] for q in queries) != len(actual):
        raise RuntimeError("The merged batch is inconsistent with the verified per-chunk results")
    digest = hashlib.sha256("\n".join(f"{trace}|{exp}|{ts}" for trace, exp, ts in actual).encode())
    return {"query_count": len(statements), "queries": queries, "returned": len(actual),
            "ordered_rows": actual, "first_excluded": first_excluded, "result_sha256": digest.hexdigest()}


def run(samples):
    sizes = (CHUNK_SIZE, CHUNK_SIZE + 1)
    scopes = {size: [str(i) for i in range(1, size + 1)] for size in sizes}
    with local_database() as (sql, uri), open_store(uri) as store:
        fixture = prepare_chunk_fixture(sql)
        settings = postgres_settings(sql)
        migration = sql("SELECT version_num FROM alembic_version ORDER BY version_num")
        store.engine.dispose()  # Start both scopes on the same pooled backend after migration.
        cases = {}
        for size, ids in scopes.items():
            diagnostics = verify_scope(store, ids)
            reverse = verify_scope(store, ids[::-1])
            if diagnostics["ordered_rows"] != reverse["ordered_rows"]:
                raise RuntimeError("Reversing the experiment list changed the ordered batch")
            cases[size] = {"experiment_count": size, **diagnostics,
                           "reversed_input": reverse, "transaction_ms": []}

        # Warm both paths once. Alternate AB and BA to balance which call goes first.
        backend_pid = None
        for round_no in range(-1, samples):
            order = sizes if round_no < 0 or round_no % 2 == 0 else sizes[::-1]
            for size in order:
                started = perf_counter_ns()
                with store.ManagedSessionMaker() as session:
                    connection = session.connection()
                    pid = connection.connection.driver_connection.get_backend_pid()
                    candidates = find_candidates(store, session, scopes[size], CUTOFF, BATCH_SIZE)
                elapsed = (perf_counter_ns() - started) / 1e6
                if backend_pid is None:
                    backend_pid = pid
                if pid != backend_pid:
                    raise RuntimeError("The PostgreSQL backend changed during timing")
                if candidate_rows(candidates) != cases[size]["ordered_rows"]:
                    raise RuntimeError(f"Wrong ordered batch for {size} experiments in round {round_no}")
                if round_no >= 0:
                    cases[size]["transaction_ms"].append(elapsed)
    return {
        "mlflow_revision": MLFLOW_REVISION, "packages": {p: version(p) for p in
        ("mlflow", "SQLAlchemy", "psycopg2-binary")}, "python": platform.python_version(),
        "system": platform.system(), "architecture": platform.machine(), "postgres_settings": settings,
        "alembic_revision": migration, "backend_pid": backend_pid, "fixture": fixture,
        "batch_size": BATCH_SIZE, "cutoff_ms": CUTOFF, "samples_per_scope": samples,
        "order_cycle": [list(sizes), list(sizes[::-1])], "cases": list(cases.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=positive_int, default=12, help="timed transactions per scope")
    parser.add_argument("--output", type=Path, help="save samples, ordered batches, SQL and plans")
    args = parser.parse_args()
    report = run(args.samples)
    print("| Selected experiments | Candidate SELECTs | Rows per SELECT | Rows kept per chunk | Median (ms) |")
    print("| ---: | ---: | --- | --- | ---: |")
    for case in report["cases"]:
        fetched = " + ".join(str(q["rows_fetched"]) for q in case["queries"])
        kept = " + ".join(str(q["rows_in_final_batch"]) for q in case["queries"])
        print(f"| {case['experiment_count']:,} | {case['query_count']} | {fetched} | {kept} | "
              f"{median(case['transaction_ms']):.2f} |")
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")


if __name__ == "__main__":
    main()
