"""The read-only matching report (tools/matching_report.py).

The tool's whole value is that it shows the owner what the matcher would do
before anything is written, so the tests pin the two things that could betray
that: it must never reach for the database on the fixture path, and it must
report the verdicts the matcher actually returned.
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import matching_report  # noqa: E402


ANSWERS = [
    {"id": "s1-a1", "question_number": 1, "subject": "zist", "corp": "qalam",
     "year": "1403-1404"},
    {"id": "s1-a2", "question_number": 2, "subject": "zist", "corp": "qalam",
     "year": "1403-1404"},
    {"id": "s1-a3", "question_number": 9, "subject": "zist", "corp": "qalam",
     "year": "1403-1404"},
]

QUESTIONS = [
    {"id": "x-q1", "question_number": 1, "subject": "zist", "corp": "qalam",
     "year": "1403-1404"},
    {"id": "y-q2", "question_number": 2, "subject": "zist", "corp": "qalam",
     "year": "1403-1404"},
    {"id": "z-q2", "question_number": 2, "subject": "zist", "corp": "gaj",
     "year": "1403-1404"},
]


class ReportTests(unittest.TestCase):
    def test_an_unambiguous_answer_links_and_the_ambiguity_is_escalated(self):
        out = matching_report.report(ANSWERS, QUESTIONS)
        by_id = {r["id"]: r for r in out["rows"]}
        self.assertEqual(by_id["s1-a1"]["state"], "auto_ok")
        self.assertEqual(by_id["s1-a1"]["pick"], "x-q1")
        # two questions carry number 2 with different institutes -> a human
        self.assertEqual(by_id["s1-a2"]["state"], "needs_manual")
        self.assertIsNone(by_id["s1-a2"]["pick"])
        # nothing carries number 9 -> nobody may be invented
        self.assertEqual(by_id["s1-a3"]["state"], "needs_manual")

    def test_the_counts_add_up_to_the_total(self):
        out = matching_report.report(ANSWERS, QUESTIONS)
        self.assertEqual(sum(out["counts"].values()), out["total"])
        self.assertEqual(out["total"], 3)

    def test_the_sample_leads_with_what_needs_a_human(self):
        out = matching_report.report(ANSWERS, QUESTIONS, limit=25)
        self.assertTrue(out["sample"])
        self.assertTrue(all(r["state"] != "auto_ok" for r in out["sample"]))

    def test_the_fixture_path_never_touches_the_database(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        apath = os.path.join(tmp.name, "a.json")
        qpath = os.path.join(tmp.name, "q.json")
        out_path = os.path.join(tmp.name, "out.json")
        io.open(apath, "w", encoding="utf-8").write(json.dumps(ANSWERS, ensure_ascii=False))
        io.open(qpath, "w", encoding="utf-8").write(json.dumps(QUESTIONS, ensure_ascii=False))

        real = matching_report.load_live

        def explode():
            raise AssertionError("the report reached for the live database")

        matching_report.load_live = explode
        self.addCleanup(setattr, matching_report, "load_live", real)
        code = matching_report.main(["--answers", apath, "--questions", qpath,
                                     "--json", out_path])
        self.assertEqual(code, 0)
        saved = json.loads(io.open(out_path, encoding="utf-8").read())
        self.assertEqual(saved["total"], 3)
        self.assertEqual(saved["counts"]["auto_ok"], 1)


if __name__ == "__main__":
    unittest.main()
