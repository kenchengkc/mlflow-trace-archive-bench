"""Compare oldest-first and scattered archival at matched per-experiment and span counts."""
import argparse
import gzip
import hashlib
import json
import platform
from itertools import permutations
from pathlib import Path

from benchmark import ALL_EXPERIMENTS, CREATE_INDEX, MLFLOW_REVISION, positive_int, postgres_settings, prepare
from local_postgres import Sql, local_postgres
from study import compare, strategy_queries
from summarize import include_planning


# Inspect the committed fixture independently of all three timed queries. Aggregate
# the two one-to-many tables separately so extra tags never multiply the span count.
TRACE_STATE = """
    WITH span_state AS (
        SELECT trace_id, count(*) AS span_count, bool_or(content <> '') AS has_content
        FROM spans GROUP BY trace_id
    ), tag_state AS (
        SELECT request_id,
               bool_or(key = 'mlflow.trace.spansLocation' AND value = 'ARCHIVE_REPO') AS archived,
               bool_or(key = 'mlflow.trace.archivalFailure' OR
                       (key = 'mlflow.trace.spansLocation' AND value <> 'TRACKING_STORE')) AS blocked
        FROM trace_tags GROUP BY request_id
    )
    SELECT t.request_id, t.experiment_id, t.timestamp_ms, t.status,
           coalesce(s.span_count, 0), coalesce(s.has_content, false),
           coalesce(g.archived, false), coalesce(g.blocked, false)
    FROM trace_info AS t
    LEFT JOIN span_state AS s ON s.trace_id = t.request_id
    LEFT JOIN tag_state AS g ON g.request_id = t.request_id
    ORDER BY t.timestamp_ms, t.request_id
"""


def inspect_queue(sql: Sql, batch_size: int) -> dict:
    header_hash = hashlib.sha256()
    strata = {}
    expected_rows = []
    headers = prefix_hits = 0
    kth_position = None
    prefix_limit = min(2 * batch_size, 4096)
    for line in sql(TRACE_STATE).splitlines():
        trace, experiment, millis, status, spans, live, archived, blocked = line.split("|")
        header_hash.update(f"{trace}|{experiment}|{millis}|{status}\n".encode())
        if int(experiment) not in ALL_EXPERIMENTS or int(millis) > 1700000000000 or status == "IN_PROGRESS":
            continue
        headers += 1
        eligible = live == "t" and blocked == "f"
        if archived == "f" and not eligible:
            continue

        key = int(experiment), int(spans)
        if key not in strata:
            strata[key] = {"experiment_id": key[0], "span_count": key[1], "archived": 0, "remaining": 0}
        if archived == "t":
            strata[key]["archived"] += 1
            continue
        strata[key]["remaining"] += 1
        if headers <= prefix_limit:
            prefix_hits += 1
        if len(expected_rows) < batch_size:
            expected_rows.append(f"{trace}|{experiment}|{millis}")
            if len(expected_rows) == batch_size:
                kth_position = headers

    return {
        "header_sha256": header_hash.hexdigest(),
        "strata": [strata[key] for key in sorted(strata)],
        "matching_headers": headers, "kth_eligible_header_position": kth_position,
        "probe_limit": prefix_limit, "eligible_in_prefix": prefix_hits, "expected_rows": expected_rows,
    }


def run(rows: int, archived_pct: int, batch_size: int, samples: int, seed: int) -> dict:
    queries = strategy_queries(ALL_EXPERIMENTS)
    report = {
        "mlflow_revision": MLFLOW_REVISION, "system": platform.system(), "architecture": platform.machine(),
        "python": platform.python_version(), "seed": seed, "samples_per_strategy": samples,
        "order_cycle": list(permutations(queries)), "cases": [],
    }
    for layout in ("oldest_first", "scattered"):
        with local_postgres() as sql:
            counts = prepare(sql, rows, archived_pct, layout=layout, seed=seed)
            sql(CREATE_INDEX)
            settings = postgres_settings(sql)
            queue = inspect_queue(sql, batch_size)
            archived = sum(group["archived"] for group in queue["strata"])
            remaining = sum(group["remaining"] for group in queue["strata"])
            if archived != counts["archived"] or archived + remaining != counts["eligible_before"]:
                raise RuntimeError(f"{layout} committed state differs from the initial archive counts")
            if report["cases"]:
                first = report["cases"][0]
                if settings != report["postgres_settings"]:
                    raise RuntimeError("PostgreSQL settings differ between layouts")
                if any(queue[key] != first[key] for key in ("header_sha256", "strata")):
                    raise RuntimeError("Layouts differ in trace headers or archived and remaining stratum counts")
            report["postgres_settings"] = settings
            case = compare(sql, queries, batch_size, min(batch_size, remaining), samples,
                           expected_rows="\n".join(queue["expected_rows"]))
            case.update(layout=layout, rows=rows, archived_pct=archived_pct, experiment_ids=ALL_EXPERIMENTS,
                        eligible_remaining_in_scope=remaining, **counts, **queue)
            report["cases"].append(case)
    return report


def trace_scans(plan: dict) -> list[str]:
    scans = []
    if plan.get("Relation Name") == "trace_info":
        name = plan["Node Type"]
        if "Index Name" in plan:
            name += f" ({plan['Index Name']})"
        scans.append(name)
    for child in plan.get("Plans", []):
        scans.extend(trace_scans(child))
    return scans


def print_table(report: dict) -> None:
    print("| Layout | Kth eligible header | Eligible in probe | Global trace scan | "
          "Global buffer accesses | Global ms | Per-experiment ms | Bounded ms |")
    print("| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: |")
    for case in report["cases"]:
        strategies = case["strategies"]
        plan = strategies["global"]["representative_plan"]["Plan"]
        buffers = plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
        scans = ", ".join(dict.fromkeys(trace_scans(plan))) or "—"
        rank = case["kth_eligible_header_position"]
        timings = " | ".join(f"{include_planning(strategies[key])['median_ms']:.2f}"
                             for key in ("global", "per_experiment", "bounded_probe"))
        print(f"| {case['layout']} | {rank if rank is not None else '—'} | {case['eligible_in_prefix']} "
              f"| {scans} | {buffers:,} | {timings} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=positive_int, default=100000)
    parser.add_argument("--archived-pct", type=int, default=95)
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--samples", type=positive_int, default=6,
                        help="measured executions per strategy per layout (six balances all orders)")
    parser.add_argument("--seed", type=int, default=0, help="integer seed for the scattered layout")
    parser.add_argument("--output", type=Path, help="save raw timings, plans and fixture checks as JSON or JSON.gz")
    args = parser.parse_args()
    if not 0 <= args.archived_pct <= 100:
        parser.error("--archived-pct must be between 0 and 100 inclusive")
    report = run(args.rows, args.archived_pct, args.batch_size, args.samples, args.seed)
    print_table(report)
    if args.output:
        opener = gzip.open if args.output.suffix.lower() == ".gz" else open
        with opener(args.output, "wt", encoding="utf-8") as out:
            json.dump(report, out, indent=2)
            out.write("\n")


if __name__ == "__main__":
    main()
