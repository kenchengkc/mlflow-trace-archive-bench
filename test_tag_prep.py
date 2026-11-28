from collections import Counter
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
class TagPrepTest(unittest.TestCase):
    def test_matched_passes_preserve_tied_order_and_restore_archived_spans(self):
        reports = compare(rounds=1, traces=5, study="tag-prep")
        ids = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
        for mode, report in reports.items():
            trial, = report["rounds"]
            self.assertEqual(trial["mode_order"], ["control", "detail"])
            self.assertEqual([row["selected_ids"] for row in trial["passes"]], [ids[:3], ids[3:], []])
            self.assertEqual(trial["readback"], {
                "traces_read_back": 5, "archive_files_verified": 5,
                "spans_read_back": 10, "database_span_payloads_cleared": 10,
            })
            for row, count in zip(trial["passes"], (3, 2, 0), strict=True):
                with self.subTest(mode=mode, count=count):
                    self.assertEqual(row["archived_count"], count)
                    self.assertEqual(row["finalization_calls"]["read_trace_tags"], count)
                    self.assertEqual(row["tag_load_calls"]["preparation"], count)
                    parent_ns = round(row["tag_load_ms"]["preparation"] * 1e6)
                    self.assertEqual(parent_ns > 0, count > 0)
                    if mode == "control":
                        self.assertFalse(any(k.startswith("tag_prep_") for k in row))
                        continue
                    times = {k: round(v * 1e6) for k, v in row["tag_prep_ms"].items()}
                    calls = {"select_construction": count, "loader_options": count, "parent_keys": 2 * count}
                    self.assertEqual(row["tag_prep_calls"], calls)
                    self.assertEqual(row["tag_prep_counts"], {"keys_batched": count, "key_batches": count})
                    self.assertEqual(set(times), set(calls) | {"other"})
                    self.assertEqual(sum(times.values()), parent_ns)
                    self.assertGreaterEqual(times["other"], 0)
                    for name in calls:
                        self.assertEqual(times[name] > 0, count > 0)

    def test_one_select_with_loader_criteria_serves_three_parent_key_batches(self):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo, SqlTraceTag
        from sqlalchemy import inspect, select
        from sqlalchemy.orm import Session, lazyload, selectinload
        from sqlalchemy.orm.strategies import SelectInLoader

        from consumption_probe import ConsumptionProbe
        from tag_load_probe import TagLoadProbe

        with local_database() as (_, uri), open_store(uri) as store:
            ids = sorted(seed_queue(store, 5))
            expected = [{"keep": str(i)} if i != 2 else {} for i in range(len(ids))]
            with Session(store.engine) as session:
                session.add_all(SqlTraceTag(request_id=trace_id, key="keep", value=tags["keep"])
                                for trace_id, tags in zip(ids, expected, strict=True) if tags)
                session.commit()

            base = select(SqlTraceInfo).where(SqlTraceInfo.request_id.in_(ids)).order_by(SqlTraceInfo.request_id)
            query = base.options(lazyload("*"), selectinload(SqlTraceInfo.tags.and_(SqlTraceTag.key == "keep")))
            query = query.execution_options(populate_existing=True)
            gate = SimpleNamespace(active=True)
            probe = TagLoadProbe(gate, profile_preparation=True)
            execute = Session.execute
            executions = []

            def record_execution(session, *args, **kwargs):
                start = perf_counter_ns()
                result = execute(session, *args, **kwargs)
                executions.append((start, perf_counter_ns()))
                return result

            def load(detailed):
                # Hold the headers from a lazy load. first() consumes one row but
                # SQLAlchemy eagerly populates all five headers in the result.
                with Session(store.engine) as session, ExitStack() as stack:
                    headers = session.scalars(base.options(lazyload("*"))).all()
                    self.assertTrue(all("tags" in inspect(h).unloaded for h in headers))
                    stack.enter_context(patch.object(SelectInLoader, "_chunksize", 2))
                    if detailed:
                        probe.install(stack)
                        ConsumptionProbe(gate, tag_load_probe=probe).install(stack)
                        stack.enter_context(patch.object(Session, "execute", new=record_execution))
                    statements = stack.enter_context(capture_statements(store.engine))
                    self.assertIs(session.scalars(query).first(), headers[0])
                    self.assertTrue(all("tags" not in inspect(h).unloaded for h in headers))
                    self.assertEqual([{t.key: t.value for t in h.tags} for h in headers], expected)
                    return statements

            original = load(detailed=False)
            self.assertEqual(load(detailed=True), original)
            self.assertEqual(len(original), 4)
            chunks = [ids[:2], ids[2:4], ids[4:]]
            for (sql, params), chunk in zip(original[1:], chunks, strict=True):
                self.assertIn(" FROM trace_tags ", " ".join(sql.split()))
                self.assertIn("keep", params.values())
                self.assertEqual(sorted(v for v in params.values() if v in ids), chunk)
            self.assertEqual(probe.prep_counts, {"keys_batched": 5, "key_batches": 3})
            events = sorted(probe.prep_events)
            preparation = [(a, b) for a, b, kind in probe.events if kind == "preparation"]
            self.assertEqual(len(preparation), 3)
            self.assertEqual(len(executions), 3)
            per_chunk = [Counter(kind for start, end, kind in events if a <= start < end <= b)
                         for a, b in preparation]
            self.assertEqual(per_chunk, [
                {"select_construction": 1, "loader_options": 1, "parent_keys": 2},
                {"parent_keys": 1}, {"parent_keys": 1},
            ])
            for start, end, _ in events:
                self.assertLess(start, end)
                self.assertTrue(any(a <= start < end <= b for a, b in preparation))
                self.assertTrue(all(end <= a or start >= b for a, b in executions))
            self.assertTrue(all(left[1] <= right[0] for left, right in zip(events, events[1:])))
