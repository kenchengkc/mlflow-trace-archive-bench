"""Measure live TOAST values in the benchmark's insert-only spans table."""

from collections import Counter
import json


def toast_snapshot(sql):
    """Read the spans TOAST relation in the current trial database, outside timing."""
    # Identifiers cannot be bound as parameters. PostgreSQL quotes the catalog
    # names of the relation identified by spans.reltoastrelid before execution.
    result = sql(r"""
        SELECT format($snapshot$
            WITH values_by_id AS (
                SELECT chunk_id, count(*) AS chunks, sum(octet_length(chunk_data)) AS bytes
                FROM %2$I.%3$I GROUP BY chunk_id
            ), sizes AS (
                SELECT chunks, bytes, count(*) AS n
                FROM values_by_id GROUP BY chunks, bytes
            )
            SELECT json_build_object(
                'database_oid', (SELECT oid::bigint FROM pg_database WHERE datname = current_database()),
                'relation_oid', %1$s,
                'heap_bytes', pg_relation_size(%1$s::oid),
                'index_bytes', pg_indexes_size(%1$s::oid),
                'value_sizes', coalesce(json_agg(json_build_array(chunks, bytes, n)
                                                 ORDER BY chunks, bytes), '[]'::json)
            ) FROM sizes
        $snapshot$, toast.oid, ns.nspname, toast.relname)
        FROM pg_class AS spans
        JOIN pg_class AS toast ON toast.oid = spans.reltoastrelid
        JOIN pg_namespace AS ns ON ns.oid = toast.relnamespace
        WHERE spans.oid = 'public.spans'::regclass
        \gexec
    """)
    if not result:
        raise RuntimeError("The spans table has no TOAST relation")
    return json.loads(result)


def toast_delta(before, after, count, padding_bytes):
    """Subtract the seed and warmup, keeping the distribution of external values."""
    if any(before[key] != after[key] for key in ("database_oid", "relation_oid")):
        raise ValueError("TOAST snapshots refer to different relations")
    # Each histogram row is [chunks per value, payload bytes per value, value count].
    sizes = Counter({(c, b): n for c, b, n in after["value_sizes"]})
    sizes.subtract({(c, b): n for c, b, n in before["value_sizes"]})
    if any(n < 0 for n in sizes.values()):
        raise ValueError("Existing TOAST values disappeared during an insert-only trial")
    external = sum(sizes.values())
    if (external > count
            or (padding_bytes == 0 and external != 0)
            or (padding_bytes == 65536 and external != count)):
        raise ValueError("Unexpected number of externally stored values for this span workload")
    return {
        "external_values": external,
        "chunks": sum(c * n for (c, _), n in sizes.items()),
        "chunk_bytes": sum(b * n for (_, b), n in sizes.items()),
        "value_sizes": [[c, b, n] for (c, b), n in sorted(sizes.items()) if n],
        "heap_bytes": after["heap_bytes"] - before["heap_bytes"],
        "index_bytes": after["index_bytes"] - before["index_bytes"],
    }
