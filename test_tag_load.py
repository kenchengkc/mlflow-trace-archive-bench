from contextlib import ExitStack
from importlib.util import find_spec
from time import perf_counter_ns
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from drain_fixture import seed_queue
from local_postgres import local_database
from mlflow_driver import MLFLOW_VERSION, capture_statements, open_store
from result_timing_study import compare
from test_driver_fixture import installed_mlflow


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class TagLoadTest(unittest.TestCase):
    def test_tag_load_timers_match_control_order_and_span_contents(self):
        reports = compare(rounds=1, traces=5, study="tag-load")
        expected = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
        for mode, report in reports.items():
            trial, = report["rounds"]
            self.assertEqual(trial["mode_order"], ["control", "detail"])
            self.assertEqual([r["selected_ids"] for r in trial["passes"]], [expected[:3], expected[3:], []])
            self.assertEqual(trial["readback"], {
                "traces_read_back": 5, "archive_files_verified": 5,
                "spans_read_back": 10, "database_span_payloads_cleared": 10,
            })
            for row, count in zip(trial["passes"], (3, 2, 0), strict=True):
                with self.subTest(mode=mode, count=count):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual(row["finalization_calls"]["read_trace_tags"], count)
                    if mode == "control":
                        self.assertFalse(any(k.startswith("tag_load_") for k in row))
                        continue
                    ns = {k: round(v * 1e6) for k, v in row["tag_load_ms"].items()}
                    self.assertEqual(set(ns), {"preparation", "grouping", "attachment", "other"})
                    self.assertEqual(sum(ns.values()), round(row["consumption_ms"]["eager_tags"] * 1e6))
                    self.assertGreaterEqual(ns["other"], 0)
                    self.assertEqual(row["tag_load_calls"], {
                        "preparation": count, "grouping": count, "attachment": count,
                    })
                    for name in ("preparation", "grouping", "attachment"):
                        self.assertEqual(ns[name] > 0, count > 0)
                    self.assertEqual(row["tag_load_counts"], {
                        "tag_rows_grouped": 2 * count, "parent_key_groups": count,
                        "headers_attached": count, "empty_collections_attached": 0,
                    })

    def test_groups_different_tag_sets_and_attaches_an_empty_collection(self):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo, SqlTraceTag
        from sqlalchemy import delete, inspect, select
        from sqlalchemy.orm import Session, lazyload, selectinload

        from consumption_probe import ConsumptionProbe
        from tag_load_probe import TagLoadProbe

        with local_database() as (_, uri), open_store(uri) as store, Session(store.engine) as session:
            ids = sorted(seed_queue(store, 3))
            expected = [{"source": "a", "owner": "b"}, {"source": "c"}, {}]
            session.execute(delete(SqlTraceTag).where(SqlTraceTag.request_id.in_(ids)))
            for trace_id, tags in zip(ids, expected, strict=True):
                session.add_all(SqlTraceTag(request_id=trace_id, key=k, value=v) for k, v in tags.items())
            session.commit()
            query = select(SqlTraceInfo).where(SqlTraceInfo.request_id.in_(ids)).order_by(SqlTraceInfo.request_id)
            headers = session.scalars(query.options(lazyload("*"))).all()
            self.assertTrue(all("tags" in inspect(h).unloaded for h in headers))

            gate = SimpleNamespace(active=True)
            probe = TagLoadProbe(gate)
            execute = Session.execute
            executions = []

            def record_execution(session, *args, **kwargs):
                start = perf_counter_ns()
                result = execute(session, *args, **kwargs)
                executions.append((start, perf_counter_ns()))
                return result

            with ExitStack() as stack:
                probe.install(stack)
                ConsumptionProbe(gate, tag_load_probe=probe).install(stack)
                stack.enter_context(patch.object(Session, "execute", new=record_execution))
                statements = stack.enter_context(capture_statements(store.engine))
                # Result.first() consumes one row after the eager loader has processed
                # the entire chunk. Strong references let us inspect all three headers.
                first = session.scalars(query.options(lazyload("*"), selectinload(SqlTraceInfo.tags))).first()
            self.assertIs(first, headers[0])
            self.assertTrue(all("tags" not in inspect(h).unloaded for h in headers))
            self.assertEqual([{tag.key: tag.value for tag in h.tags} for h in headers], expected)
            self.assertEqual(probe.counts, {
                "tag_rows_grouped": 3, "parent_key_groups": 2,
                "headers_attached": 3, "empty_collections_attached": 1,
            })
            self.assertEqual(len(statements), 2)
            self.assertIn(" FROM trace_tags ", " ".join(statements[1][0].split()))
            (execution_start, execution_end), = executions
            prep = [(a, b) for a, b, name in probe.events if name == "preparation"]
            grouping = [(a, b) for a, b, name in probe.events if name == "grouping"]
            attachment = [(a, b) for a, b, name in probe.events if name == "attachment"]
            self.assertEqual((len(prep), len(grouping), len(attachment)), (1, 1, 3))
            self.assertLessEqual(max(b for a, b in prep), execution_start)
            self.assertLessEqual(execution_end, min(a for a, b in grouping))
            self.assertLessEqual(max(b for a, b in grouping), min(a for a, b in attachment))
