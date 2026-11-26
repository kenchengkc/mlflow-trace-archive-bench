"""Read PostgreSQL counters for MLflow's live single-row span upsert."""
import json

from local_postgres import Sql


COUNTERS = ("calls", "rows", "total_exec_time_ms", "wal_records", "wal_fpi", "wal_bytes")


def prepare_server_stats(seed_sql: Sql) -> str:
    """Install the view in the seed so each new database clone inherits it."""
    seed_sql("CREATE EXTENSION pg_stat_statements")
    return seed_sql("SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'")


def upsert_snapshot(sql: Sql) -> dict:
    """Read the normalized statement for this database and the benchmark role."""
    result = sql("""
        SELECT row_to_json(entry) FROM (
            SELECT s.dbid::integer, s.userid::integer, s.toplevel, s.queryid::text, s.query,
                   i.stats_reset, i.dealloc, s.calls, s.rows, s.total_exec_time AS total_exec_time_ms,
                   s.wal_records, s.wal_fpi, s.wal_bytes
            FROM pg_stat_statements s CROSS JOIN pg_stat_statements_info i
            WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND s.userid = current_user::regrole AND s.toplevel
              AND starts_with(s.query, 'INSERT INTO spans (')
              AND s.query LIKE '% ON CONFLICT (%) DO UPDATE SET %'
              AND s.query LIKE '%content = excluded.content%'
        ) AS entry;
    """).splitlines()
    if len(result) != 1:
        raise RuntimeError(f"Expected one top-level MLflow span upsert, found {len(result)}")
    return json.loads(result[0])


def upsert_delta(before: dict, after: dict, count: int) -> dict:
    """Subtract counters for one unchanged statement entry after the single warm-up."""
    for key in ("dbid", "userid", "toplevel", "queryid", "query", "stats_reset", "dealloc"):
        if before[key] != after[key]:
            raise RuntimeError(f"PostgreSQL statement identity or statistics changed: {key}")
    if before["calls"] != 1 or before["rows"] != 1:
        raise RuntimeError("Expected exactly one warm-up upsert before timing")
    delta = {key: after[key] - before[key] for key in COUNTERS}
    if delta["calls"] != count or delta["rows"] != count:
        raise RuntimeError(f"Expected {count} single-row upserts, got {delta['calls']} calls and {delta['rows']} rows")
    if any(value < 0 for value in delta.values()) or delta["total_exec_time_ms"] <= 0:
        raise RuntimeError("Unexpected PostgreSQL execution or WAL counter delta")
    return delta
