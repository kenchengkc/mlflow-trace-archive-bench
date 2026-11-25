import random
import unittest

from local_postgres import local_postgres
from study import strategy_queries
from test_candidates import FIXTURE, SQL_DIR


def load_skewed_traces(sql):
    """Return independently ordered eligible rows while building the test data."""
    rng = random.Random(24976)
    ids = list(range(240))
    rng.shuffle(ids)
    traces, spans, eligible = [], [], []
    for i, request_number in enumerate(ids):
        trace_id = f"trace-{request_number:03}"
        experiment = rng.choice([1] * 8 + [2, 3])
        timestamp = 1699999999900 + i // 5
        has_payload = i >= 80 and rng.random() < 0.75
        status = "IN_PROGRESS" if i % 13 == 0 else "OK"
        traces.append(f"{trace_id}\t{experiment}\t{timestamp}\t{status}")
        payload = "{}" if has_payload else ""
        spans.append(f"{trace_id}\troot\t{payload}")
        if has_payload and status != "IN_PROGRESS":
            eligible.append((timestamp, trace_id, experiment))

    # COPY preserves the deliberately shuffled insertion order within each tie.
    sql("COPY trace_info (request_id, experiment_id, timestamp_ms, status) FROM STDIN;\n"
        + "\n".join(traces) + "\n\\.\n"
        + "COPY spans (trace_id, span_id, content) FROM STDIN;\n"
        + "\n".join(spans) + "\n\\.\n")
    return sorted(eligible)


class StrategyTest(unittest.TestCase):
    def check_strategies(self, sql, experiments, batch_size, expected):
        queries = strategy_queries(experiments)
        self.assertEqual(set(queries), {"global", "per_experiment", "bounded_probe"})
        for strategy, query in queries.items():
            with self.subTest(strategy=strategy, experiments=experiments, batch_size=batch_size):
                actual = sql(query, variables={"batch_size": batch_size}).splitlines()
                self.assertEqual(actual, expected[:batch_size])

    def test_hand_fixture_and_changes_between_calls(self):
        scope = tuple(range(1, 9))
        first = "first|2|1699999999998"
        tie_a = "tie-a|8|1699999999999"
        tie_b = "tie-b|1|1699999999999"
        cutoff = "on-cutoff|3|1700000000000"
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            sql(FIXTURE)
            for size in (0, 2, 4, 10):
                self.check_strategies(sql, scope, size, [first, tie_a, tie_b, cutoff])
            self.check_strategies(sql, (8,), 2, [tie_a])
            self.check_strategies(sql, (1, 4, 88), 2, [tie_b])
            self.check_strategies(sql, (4,), 2, [])   # Contains only ineligible traces.
            self.check_strategies(sql, (88,), 2, [])  # Missing experiment.

            # An old, previously running trace becomes eligible after the first call.
            sql("UPDATE trace_info SET status = 'OK' WHERE request_id = 'still-running';")
            resumed = "still-running|2|1699999999990"
            self.check_strategies(sql, scope, 2, [resumed, first])

            # A new arrival can have a timestamp earlier than any prior result.
            sql("INSERT INTO trace_info VALUES ('late', 1, 1699999999980, 'OK');\n"
                "INSERT INTO spans VALUES ('late', 'root', '{}');")
            self.check_strategies(sql, scope, 2, ["late|1|1699999999980", resumed])

    def test_partial_prefix_during_a_timestamp_tie(self):
        # For K=2 the probe sees a..d. Only c qualifies there; e fills the batch.
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            sql("""INSERT INTO trace_info SELECT id, 1, 1000, 'OK'
                   FROM unnest(ARRAY['a','b','c','d','e','f']) AS id;
                   INSERT INTO spans VALUES ('c','s','{}'), ('e','s','{}'), ('f','s','{}');""")
            self.check_strategies(sql, (1,), 2, ["c|1|1000", "e|1|1000"])

    def test_batch_larger_than_probe_cap(self):
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            sql("""INSERT INTO trace_info SELECT 't' || i, 1, i, 'OK'
                   FROM generate_series(1, 4102) AS i;
                   INSERT INTO spans SELECT request_id, 'root', '{}' FROM trace_info;""")
            expected = [f"t{i}|1|{i}" for i in range(1, 4101)]
            self.check_strategies(sql, (1,), 4100, expected)

    def test_skewed_workload_with_a_long_empty_prefix(self):
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            eligible = load_skewed_traces(sql)
            for scope in ((1, 2, 3), (3,), (3, 1, 77)):
                expected = [
                    f"{trace}|{experiment}|{timestamp}"
                    for timestamp, trace, experiment in eligible if experiment in scope
                ]
                for size in (1, 7, len(expected), len(expected) + 10):
                    self.check_strategies(sql, scope, size, expected)


if __name__ == "__main__":
    unittest.main()
