import hashlib
import unittest
from collections import Counter, defaultdict

from benchmark import QUERY, SQL_DIR, archive_sql
from local_postgres import local_postgres


class ArchiveLayoutTest(unittest.TestCase):
    def test_seeded_assignment_matches_oldest_first_quotas(self):
        rows = 1000
        with local_postgres() as sql:
            sql((SQL_DIR / "schema.sql").read_text())
            seed_fixture = (SQL_DIR / "seed.sql").read_text()
            sql(seed_fixture, variables={"rows": rows})
            records = sql(f"""
                SELECT e.request_id, e.experiment_id, count(s.span_id)
                FROM ({QUERY}) AS e JOIN spans AS s ON s.trace_id = e.request_id
                GROUP BY e.request_id, e.experiment_id, e.timestamp_ms
                ORDER BY e.timestamp_ms, e.request_id;
            """, variables={"batch_size": rows}).splitlines()
            ids_by_group = defaultdict(list)
            population = []
            for row in records:
                request_id, experiment, span_count = row.split("|")
                group = (experiment, span_count)
                ids_by_group[group].append(request_id)
                population.append((request_id, group))
            self.assertGreater(len(ids_by_group), 8)

            for layout, pct, seed in [
                ("oldest_first", 95, 0), ("scattered", 95, 0), ("scattered", 95, 37),
                ("scattered", 0, 0), ("scattered", 100, 0),
            ]:
                with self.subTest(layout=layout, pct=pct, seed=seed):
                    sql("TRUNCATE trace_info, spans, trace_tags;")
                    sql(seed_fixture, variables={"rows": rows})
                    sql(archive_sql(layout), variables={"batch_size": rows, "archived_pct": pct, "seed": seed})
                    actual = set(sql("""
                        SELECT request_id FROM trace_tags
                        WHERE key = 'mlflow.trace.spansLocation' AND value = 'ARCHIVE_REPO';
                    """).splitlines())
                    oldest = population[:len(population) * pct // 100]
                    oldest_ids = {trace for trace, _ in oldest}
                    if layout == "oldest_first":
                        self.assertEqual(actual, oldest_ids)
                        continue
                    quotas = Counter(group for _, group in oldest)
                    expected = set()
                    for group, ids in ids_by_group.items():
                        ranked = sorted(ids, key=lambda trace: (
                            hashlib.md5(f"{seed}:{trace}".encode()).hexdigest(), trace
                        ))
                        expected.update(ranked[:quotas[group]])
                    self.assertEqual(actual, expected)
                    if pct == 95:
                        self.assertNotEqual(actual, oldest_ids)
