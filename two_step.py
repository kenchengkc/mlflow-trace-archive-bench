"""Select a bounded batch of archive candidates on one PostgreSQL snapshot."""

from benchmark import SQL_DIR

PREFIX_SQL = (SQL_DIR / "two_step_prefix.sql").read_text()
FALLBACK_SQL = (SQL_DIR / "two_step_fallback.sql").read_text()
# Run the existing psql control with driver parameters, preserving its query shape.
BOUNDED_SQL = ((SQL_DIR / "bounded_probe.sql").read_text()
               .replace("ARRAY[:experiment_ids]", "%(experiment_ids)s")
               .replace("1700000000000", "%(cutoff)s")
               .replace(":batch_size", "%(batch_size)s"))


def snapshot_connection(session):
    """Acquire a read-only repeatable-read connection before the session's first query."""
    if session.in_transaction():
        raise ValueError("Open a fresh session before acquiring the archive selection snapshot")
    return session.connection(execution_options={
        "isolation_level": "REPEATABLE READ", "postgresql_readonly": True,
    })


def _parameters(experiment_ids, cutoff, batch_size):
    if type(cutoff) is not int:
        raise ValueError("A finite integer timestamp cutoff in milliseconds is required")
    if type(batch_size) is not int or batch_size < 0:
        raise ValueError("batch_size must be a nonnegative integer")
    return {"experiment_ids": list(experiment_ids), "cutoff": cutoff, "batch_size": batch_size}


def bounded_candidates(connection, experiment_ids, cutoff, batch_size):
    """Run the existing single-statement bounded probe with bound parameters."""
    params = _parameters(experiment_ids, cutoff, batch_size)
    return connection.exec_driver_sql(BOUNDED_SQL, params).all()


def two_step_candidates(connection, experiment_ids, cutoff, batch_size):
    """Return (ordered tuples, used_fallback) for a finite cutoff and batch size."""
    options = connection.get_execution_options()
    if (not connection.in_transaction() or options.get("isolation_level") != "REPEATABLE READ"
            or options.get("postgresql_readonly") is not True):
        raise ValueError("Use snapshot_connection on a fresh managed session")
    params = _parameters(experiment_ids, cutoff, batch_size)
    if not params["experiment_ids"] or batch_size == 0:
        return [], False
    probe_limit = min(2 * batch_size, 4096)
    params["probe_limit"] = probe_limit
    prefix = connection.exec_driver_sql(PREFIX_SQL, params).all()
    # The left join supplies a single NULL candidate when the prefix has no hits.
    header_count = prefix[0][3]
    hits = [row[:3] for row in prefix if row[0] is not None]
    if len(hits) == batch_size or header_count < probe_limit:
        return hits, False
    return [tuple(row) for row in connection.exec_driver_sql(FALLBACK_SQL, params)], True
