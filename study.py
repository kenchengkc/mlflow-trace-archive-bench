"""Measure three ways to select MLflow archival candidates on the same indexed PostgreSQL data."""
import argparse
import gzip
import hashlib
import json
import os
import platform
from itertools import permutations
from pathlib import Path

from benchmark import (
    ALL_EXPERIMENTS, CREATE_INDEX, MLFLOW_REVISION, QUERY, positive_int,
    postgres_settings, prepare, scoped_query, summarize_plans,
)
from local_postgres import Sql, local_postgres


def strategy_queries(experiment_ids: tuple[int, ...]) -> dict[str, str]:
    return {
        "global": scoped_query("candidates.sql", experiment_ids),
        "per_experiment": scoped_query("per_experiment.sql", experiment_ids),
        "bounded_probe": scoped_query("bounded_probe.sql", experiment_ids),
    }


def parse_scope(value: str) -> tuple[int, ...]:
    try:
        ids = sorted({int(item) for item in value.split(",")})
    except ValueError:
        ids = []
    if not ids or not set(ids).issubset(ALL_EXPERIMENTS):
        raise argparse.ArgumentTypeError("use comma-separated fixture experiment IDs from 1 through 8")
    return tuple(ids)


def compare(sql: Sql, queries: dict[str, str], batch_size: int, expected_count: int, samples: int) -> dict:
    values = {"batch_size": batch_size}
    expected = sql(queries["global"], variables=values)
    returned = len(expected.splitlines())
    if returned != expected_count:
        raise RuntimeError(f"Expected {expected_count} candidates, got {returned}")
    plans = {name: [] for name in queries}
    explain = {name: "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + query
               for name, query in queries.items()}
    for name, query in queries.items():
        if name != "global" and sql(query, variables=values) != expected:
            raise RuntimeError(f"{name} returned different ordered rows than the global query")
        sql(explain[name], variables=values)  # Exclude the first instrumented execution.

    # A full cycle places each strategy first, middle and last twice on this fixture.
    orders = list(permutations(queries))
    for sample in range(samples):
        for name in orders[sample % len(orders)]:
            plans[name].append(json.loads(sql(explain[name], variables=values))[0])
    return {
        "batch_size": batch_size, "returned": returned,
        "result_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        "strategies": {name: summarize_plans(group) for name, group in plans.items()},
    }


def run(args: argparse.Namespace) -> dict:
    scopes = {ids: strategy_queries(ids) for ids in args.scope}
    report = {
        "mlflow_revision": MLFLOW_REVISION, "system": platform.system(),
        "architecture": platform.machine(), "cpu_count": os.cpu_count(),
        "python": platform.python_version(), "samples_per_strategy": args.samples,
        "order_cycle": list(permutations(next(iter(scopes.values())))),
        "cases": [],
    }
    print("   rows   arch%   experiments           batch  returned    global ms   per-exp ms   bounded ms")
    for pct in args.archived_pct:
        with local_postgres() as sql:
            counts = prepare(sql, args.rows, pct)
            sql(CREATE_INDEX)
            report["postgres_settings"] = postgres_settings(sql)
            # Count the remaining eligible population once, outside all timed queries.
            groups = sql(f"SELECT experiment_id, count(*) FROM ({QUERY}) AS c GROUP BY experiment_id",
                         variables={"batch_size": args.rows})
            remaining = {}
            for line in groups.splitlines():
                experiment, count = line.split("|")
                remaining[int(experiment)] = int(count)
            if sum(remaining.values()) != counts["eligible_before"] - counts["archived"]:
                raise RuntimeError("Archive fixture has an unexpected number of remaining candidates")
            for ids, queries in scopes.items():
                eligible = sum(remaining.get(i, 0) for i in ids)
                for batch in args.batch_size:
                    case = compare(sql, queries, batch, min(batch, eligible), args.samples)
                    case.update(rows=args.rows, archived_pct=pct, experiment_ids=ids,
                                eligible_remaining_in_scope=eligible, **counts)
                    report["cases"].append(case)
                    times = [case["strategies"][name]["median_ms"] for name in queries]
                    print(f"{args.rows:7} {pct:7}   {','.join(map(str, ids)):18} {batch:5} {case['returned']:9}"
                          + "".join(f"{t:13.3f}" for t in times), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100000)
    parser.add_argument("--archived-pct", type=int, nargs="+", default=[0, 95],
                        help="percent of the initially eligible eight-experiment population already archived")
    parser.add_argument("--scope", type=parse_scope, nargs="+", default=[(8,), ALL_EXPERIMENTS],
                        help="one or more comma-separated lists of experiment IDs, e.g. --scope 8 1,2,3")
    parser.add_argument("--batch-size", type=positive_int, nargs="+", default=[100])
    parser.add_argument("--samples", type=positive_int, default=6,
                        help="measured executions per strategy; multiples of six balance all execution orders")
    parser.add_argument("--output", type=Path, help="save timings and query plans as .json or .json.gz")
    args = parser.parse_args()
    if any(pct < 0 or pct > 100 for pct in args.archived_pct):
        parser.error("--archived-pct must be between 0 and 100 inclusive")
    report = run(args)
    if args.output:
        opener = gzip.open if args.output.suffix == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")


if __name__ == "__main__":
    main()
