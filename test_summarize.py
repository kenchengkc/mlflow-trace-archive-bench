import gzip
import json
import subprocess
import sys
import unittest
from pathlib import Path

from summarize import summary


ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "results" / "strategy-study"


def summarize(report, *options):
    return subprocess.run(
        [sys.executable, str(ROOT / "summarize.py"), str(REPORTS / report), *options],
        capture_output=True,
    )


class ReportFiltersTest(unittest.TestCase):
    def test_unfiltered_report_matches_saved_matrix(self):
        result = summarize("matrix.json.gz", "--with-planning")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, (REPORTS / "matrix.md").read_bytes())

    def test_layouts_distinguish_otherwise_identical_cases(self):
        with gzip.open(REPORTS / "million-jit.json.gz", "rt") as source:
            report = json.load(source)
        original = report["cases"][0]
        report["cases"] = [dict(original, layout="oldest_first"), dict(original, layout="scattered"), original]
        lines = summary(report, buffers=False, archived_pct=original["archived_pct"]).splitlines()
        for label in ("Layout", "Oldest first", "Scattered", "—"):
            # Both the main and JIT tables identify the layout.
            self.assertEqual(sum(line.startswith(f"| {label} |") for line in lines), 2)

    def test_exact_scope_and_combined_filters(self):
        for scope, label in (("8", "8"), ("8, 6,1,2,3,4,5,7,1", "1–8")):
            with self.subTest(scope=scope):
                result = summarize("matrix.json.gz", "--batch-size", "3", "--archived-pct", "0", "--scope", scope)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = [line for line in result.stdout.decode().splitlines() if "100,000" in line]
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].startswith(f"| {label} | 100,000 | 3 | 0% |"))

    def test_jit_control_uses_same_filters(self):
        result = summarize("million-jit.json.gz", "--archived-pct", "95")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"JIT on (ms)", result.stdout)
        self.assertEqual(result.stdout.count(b"| 95% |"), 2)
        self.assertNotIn(b"| 0% |", result.stdout)

    def test_no_match_and_invalid_values(self):
        for options, message in (
            (("--scope", "1,8"), b"no cases match"),
            (("--batch-size", "0"), b"at least 1"),
            (("--archived-pct", "-1"), b"between 0 and 100"),
            (("--archived-pct", "101"), b"between 0 and 100"),
            (("--scope", "1,"), b"experiment IDs"),
            (("--scope", "-2"), b"experiment IDs"),
        ):
            with self.subTest(options=options):
                result = summarize("matrix.json.gz", *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertIn(message, result.stderr)
