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


PARTS = ("columns_bundle", "select_from", "in_predicate")


@unittest.skipUnless(installed_mlflow == MLFLOW_VERSION and find_spec("psycopg2"),
                     "requires the driver study's MLflow and psycopg2 dependencies")
class TagSelectTest(unittest.TestCase):
    def test_matched_archival_passes_partition_select_construction(self):
        reports = compare(rounds=1, traces=5, study="tag-select")
        ids = [f"tr-d1a1{i:028x}" for i in (2, 3, 4, 5, 1)]
        self.assertEqual(set(reports), {"control", "detail"})
        for mode, report in reports.items():
            trial, = report["rounds"]
            self.assertEqual([row["selected_ids"] for row in trial["passes"]], [ids[:3], ids[3:], []])
            self.assertEqual(trial["readback"], {
                "traces_read_back": 5, "archive_files_verified": 5,
                "spans_read_back": 10, "database_span_payloads_cleared": 10,
            })
            for row in trial["passes"]:
                count = row["archived_count"]
                with self.subTest(mode=mode, archived=count):
                    self.assertEqual(row["tag_prep_calls"]["select_construction"], count)
                    if mode == "control":
                        self.assertFalse(any(k.startswith("tag_select_") for k in row))
                        continue
                    self.assertEqual(row["tag_select_calls"], dict.fromkeys(PARTS, count))
                    ns = {k: round(v * 1e6) for k, v in row["tag_select_ms"].items()}
                    self.assertEqual(set(ns), set(PARTS) | {"other"})
                    self.assertEqual(sum(ns.values()), round(row["tag_prep_ms"]["select_construction"] * 1e6))
                    self.assertGreaterEqual(ns["other"], 0)
                    for part in PARTS:
                        self.assertEqual(ns[part] > 0, count > 0)

    def test_scopes_cover_real_sql_builders_and_preserve_an_aliased_tag_query(self):
        from mlflow.store.tracking.dbmodels.models import SqlTraceInfo, SqlTraceTag
        from sqlalchemy import Select, select, sql
        from sqlalchemy.orm import Bundle, Session, aliased, lazyload, selectinload

        from consumption_probe import ConsumptionProbe
        from tag_load_probe import TagLoadProbe

        with local_database() as (_, uri), open_store(uri) as store:
            trace_id, = seed_queue(store, 1)
            with Session(store.engine) as session:
                session.add(SqlTraceTag(request_id=trace_id, key="fixture", value="verified"))
                session.commit()
            tag_alias = aliased(SqlTraceTag, name="selected_tags")
            query = select(SqlTraceInfo).where(SqlTraceInfo.request_id == trace_id).options(
                lazyload("*"), selectinload(SqlTraceInfo.tags.of_type(tag_alias)))

            def load():
                with Session(store.engine) as session, capture_statements(store.engine) as statements:
                    header = session.scalars(query).one()
                    return {tag.key: tag.value for tag in header.tags}, statements

            original = load()
            gate = SimpleNamespace(active=True)
            probe = TagLoadProbe(gate, profile_select=True)
            markers = {}
            with ExitStack() as stack:
                probe.install(stack)
                ConsumptionProbe(gate, tag_load_probe=probe).install(stack)
                # Sample entry to a real builder in each scope, independently of the loader probe.
                for (owner, method), part in zip(
                    ((Bundle, "__clause_element__"), (Select, "select_from"), (sql, "bindparam")), PARTS, strict=True,
                ):
                    def mark(*args, original=getattr(owner, method), part=part, **kwargs):
                        markers.setdefault(part, []).append(perf_counter_ns())
                        return original(*args, **kwargs)

                    stack.enter_context(patch.object(owner, method, new=mark))
                self.assertEqual(load(), original)
            tags, statements = original
            self.assertEqual(tags["fixture"], "verified")
            self.assertEqual(len(statements), 2)
            tag_sql, parameters = statements[1]
            self.assertIn("trace_tags AS selected_tags", tag_sql)
            self.assertEqual(list(parameters.values()), [trace_id])
            parent, = [(a, b) for a, b, name in probe.prep_events if name == "select_construction"]
            self.assertEqual([part for _, _, part in probe.select_events], list(PARTS))
            previous = parent[0]
            for start, end, part in probe.select_events:
                self.assertLessEqual(previous, start)
                self.assertLess(start, end)
                self.assertTrue(any(start <= t < end for t in markers[part]))
                previous = end
            self.assertLessEqual(previous, parent[1])
