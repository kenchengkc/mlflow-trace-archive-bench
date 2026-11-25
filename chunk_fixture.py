"""A shared migrated fixture for MLflow's 1,000-experiment archival query boundary."""
from collections.abc import Iterable, Iterator

from driver_fixture import LOAD, STAGE
from local_postgres import Sql


CUTOFF = 1700000000000
BATCH_SIZE = 100
EXPERIMENT_COUNT = 1001
TRACES_PER_EXPERIMENT = 101


def _fixture_rows(experiments: Iterable[int]) -> Iterator[tuple[str, int, int]]:
    for experiment in experiments:
        if experiment < 1000:
            first_id = 10000 + (experiment - 1) * TRACES_PER_EXPERIMENT
            for offset in range(TRACES_PER_EXPERIMENT):
                yield f"tr-{first_id + offset:032x}", experiment, CUTOFF
        else:
            # Anchors sort first by time but last by ID. In the next timestamp
            # group the two experiments alternate, including across the batch limit.
            anchor = 200000 + (1001 - experiment)
            yield f"tr-{anchor:032x}", experiment, CUTOFF - 2
            for slot in range(1, TRACES_PER_EXPERIMENT):
                trace_id = 2 * slot - (experiment - 1000)
                yield f"tr-{trace_id:032x}", experiment, CUTOFF - 1


def expected_candidates(experiment_ids: Iterable[str | int],
                        batch_size: int = BATCH_SIZE) -> list[tuple[str, int, int]]:
    """Sort all eligible input triples globally, independently of SQL and MLflow's chunk merge."""
    selected = {int(experiment) for experiment in experiment_ids}
    selected.intersection_update(range(1, EXPERIMENT_COUNT + 1))
    candidates = sorted(_fixture_rows(selected), key=lambda row: (row[2], row[0]))
    return candidates[:batch_size]


def prepare_chunk_fixture(sql: Sql) -> dict:
    """Populate MLflow's migrated tables once and verify the committed eligible row counts."""
    traces = _fixture_rows(range(1, EXPERIMENT_COUNT + 1))
    input_rows = "".join(f"{trace}\t{experiment}\t{timestamp}\tOK\n" for trace, experiment, timestamp in traces)
    sql(STAGE + "COPY pg_temp.trace_info (request_id, experiment_id, timestamp_ms, status) FROM STDIN;\n"
        + input_rows + "\\.\n" + """
        INSERT INTO pg_temp.spans (trace_id, span_id, content)
        SELECT request_id, '0000000000000001', '{"name":"root"}' FROM pg_temp.trace_info;
        """ + LOAD)
    sql("VACUUM (ANALYZE) trace_info;\nVACUUM (ANALYZE) spans;\nVACUUM (ANALYZE) trace_tags;")
    counts = sql("""
        SELECT count(*), min(eligible), max(eligible), sum(eligible) FROM (
            SELECT experiment_id, count(*) AS eligible FROM trace_info t
            WHERE t.status = 'OK' AND t.timestamp_ms <= :cutoff
              AND EXISTS (SELECT 1 FROM spans s WHERE s.trace_id = t.request_id AND s.content <> '')
              AND NOT EXISTS (SELECT 1 FROM trace_tags g WHERE g.request_id = t.request_id)
            GROUP BY experiment_id
        ) AS groups;
    """, variables={"cutoff": CUTOFF})
    total = EXPERIMENT_COUNT * TRACES_PER_EXPERIMENT
    expected = (EXPERIMENT_COUNT, TRACES_PER_EXPERIMENT, TRACES_PER_EXPERIMENT, total)
    if tuple(map(int, counts.split("|"))) != expected:
        raise RuntimeError(f"Unexpected eligible population in the chunk-boundary fixture: {counts}")
    return {"rows": total, "experiments": EXPERIMENT_COUNT, "traces_per_experiment": TRACES_PER_EXPERIMENT}
