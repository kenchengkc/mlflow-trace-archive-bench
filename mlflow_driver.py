"""Run the released MLflow archival finder and inspect the SQL sent to PostgreSQL."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import Session

MLFLOW_VERSION = "3.18.0"
MLFLOW_REVISION = "ba41106d110b67096384da12b6b85473457598db"


@contextmanager
def open_store(db_uri: str) -> Iterator[SqlAlchemyStore]:
    """Initialize and verify the tracking schema using MLflow's normal constructor."""
    from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore

    installed = version("mlflow")
    if installed != MLFLOW_VERSION:
        raise RuntimeError(f"Expected MLflow {MLFLOW_VERSION}, found {installed}")
    with TemporaryDirectory(prefix="trace-artifacts-") as artifacts:
        store = SqlAlchemyStore(db_uri=db_uri, default_artifact_root=Path(artifacts).as_uri())
        try:
            yield store
        finally:
            store.engine.dispose()


def find_candidates(store: SqlAlchemyStore, session: Session, experiment_ids: list[str],
                    cutoff: int, batch_size: int):
    """Return the upstream objects, including its ordered merge and materialization."""
    return store._find_archivable_trace_candidates_for_experiments(
        session=session, experiment_ids=experiment_ids, max_timestamp_millis=cutoff, limit=batch_size,
    )


@contextmanager
def capture_statements(engine: Engine) -> Iterator[list]:
    """Capture the actual DBAPI SQL and parameters during an untimed verification call."""
    from sqlalchemy import event

    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def explain(session: Session, statement: str, parameters) -> dict:
    """Replay a captured SELECT with its original driver parameters and return its plan."""
    result = session.connection().exec_driver_sql(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + statement, parameters,
    )
    return result.scalar_one()[0]
