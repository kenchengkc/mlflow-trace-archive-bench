"""Compare MLflow's trace archival candidate query before and after its ordering index."""
import argparse
import hashlib
import json
import os
import platform
import statistics
from pathlib import Path

from local_postgres import Sql, local_postgres

SQL_DIR = Path(__file__).resolve().parent / "sql"
INDEX = "index_trace_info_timestamp_ms_request_id"
CREATE_INDEX = f"CREATE INDEX {INDEX} ON trace_info (timestamp_ms, request_id);"
QUERY = (SQL_DIR / "candidates.sql").read_text().strip().removesuffix(";")
EXPLAIN = "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + QUERY


def prepare(sql: Sql, rows: int, archived_pct: int) -> dict:
    sql((SQL_DIR / "schema.sql").read_text())
    sql((SQL_DIR / "seed.sql").read_text(), variables={"rows": rows})
    # Stage the exact population before simulating an oldest-first archive pass.
    archive = f"""
        CREATE TEMP TABLE eligible AS {QUERY};
        CREATE TEMP TABLE archive_prefix AS
            SELECT * FROM eligible ORDER BY timestamp_ms, request_id
            LIMIT (SELECT count(*) * :archived_pct / 100 FROM eligible);
        {(SQL_DIR / 'set_archived.sql').read_text()}
        SELECT json_build_object('eligible_before', (SELECT count(*) FROM eligible),
                                 'archived', (SELECT count(*) FROM archive_prefix));
    """
    counts = json.loads(sql(archive, variables={"archived_pct": archived_pct, "batch_size": rows}))
    # Start both index comparisons with up-to-date visibility maps and planner statistics.
    sql("VACUUM (ANALYZE) trace_info;\nVACUUM (ANALYZE) trace_tags;\nVACUUM (ANALYZE) spans;")
    return counts


def measure(sql: Sql, batch_size: int, samples: int) -> tuple[dict, str]:
    values = {"batch_size": batch_size}
    result_rows = sql(QUERY, variables=values)
    sql(EXPLAIN, variables=values)  # Warm the chosen plan once before collecting samples.
    plans = [json.loads(sql(EXPLAIN, variables=values))[0] for _ in range(samples)]
    times = [plan["Execution Time"] for plan in plans]
    median = statistics.median(times)
    return {
        "median_ms": median,
        "samples_ms": times,
        "representative_plan": min(plans, key=lambda p: abs(p["Execution Time"] - median)),
    }, result_rows


def run_case(rows: int, archived_pct: int, batch_size: int, samples: int, indexed_first: bool):
    with local_postgres() as sql:
        counts = prepare(sql, rows, archived_pct)
        expected_rows = min(batch_size, counts["eligible_before"] - counts["archived"])
        measurements, results = {}, {}
        order = ["indexed", "baseline"] if indexed_first else ["baseline", "indexed"]
        for variant in order:
            if variant == "indexed":
                sql(CREATE_INDEX)
                index_bytes = int(sql(f"SELECT pg_relation_size('{INDEX}');"))
            elif indexed_first:
                sql(f"DROP INDEX {INDEX};")
            measurements[variant], results[variant] = measure(sql, batch_size, samples)
        if results["baseline"] != results["indexed"]:
            raise RuntimeError("The index variants returned different ordered candidate rows.")
        returned = results["baseline"].splitlines()
        if len(returned) != expected_rows:
            raise RuntimeError(f"Expected {expected_rows} candidates, got {len(returned)}.")
        settings = json.loads(sql("""SELECT json_object_agg(name, current_setting(name))
            FROM pg_settings WHERE name IN ('server_version', 'shared_buffers', 'work_mem',
                'max_parallel_workers_per_gather', 'random_page_cost', 'jit', 'block_size');"""))
    case = {
        "rows": rows, "archived_pct": archived_pct, **counts, "returned": len(returned),
        "result_sha256": hashlib.sha256(results["baseline"].encode()).hexdigest(),
        "measurement_order": order, "index_bytes": index_bytes, **measurements,
    }
    return case, settings


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, nargs="+", default=[10000, 100000])
    parser.add_argument("--archived-pct", type=int, nargs="+", default=[0, 50, 95])
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--samples", type=positive_int, default=7)
    parser.add_argument("--output", type=Path, help="save raw timings and representative PostgreSQL plans as JSON")
    args = parser.parse_args()
    if any(p < 0 or p > 100 for p in args.archived_pct):
        parser.error("--archived-pct must be between 0 and 100 inclusive")
    report = {
        "mlflow_revision": "3128eb46928fab08500124e9e0cde6791cf845aa",
        "system": platform.system(), "architecture": platform.machine(), "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "batch_size": args.batch_size, "samples_per_variant": args.samples, "cases": [],
    }
    print("     rows  archived  returned   baseline ms    indexed ms", flush=True)
    for rows in args.rows:
        for pct in args.archived_pct:
            indexed_first = len(report["cases"]) % 2 == 1
            case, report["postgres_settings"] = run_case(rows, pct, args.batch_size, args.samples, indexed_first)
            report["cases"].append(case)
            print(f"{rows:9,d}  {pct:7}%  {case['returned']:8}  {case['baseline']['median_ms']:12.3f}  "
                  f"{case['indexed']['median_ms']:12.3f}", flush=True)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
