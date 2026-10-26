"""The Inspect API surface, exercised through the real app (no network, no DB).

These are route-level tests on purpose: `include_router` in the installed
FastAPI nests an `_IncludedRouter` whose `.path` is None, so scanning
`app.routes` would prove nothing. A TestClient request is the only honest
check that a path is wired.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from pipeline import staging  # noqa: E402


class InspectRoutesTest(unittest.TestCase):
    """Every Inspect endpoint answers on the app's own router."""

    @classmethod
    def setUpClass(cls):
        import main
        cls.app = main.app
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = staging.DEFAULT_PATH
        staging.DEFAULT_PATH = os.path.join(cls.tmp.name, "staging.db")

    @classmethod
    def tearDownClass(cls):
        staging.DEFAULT_PATH = cls.saved
        cls.tmp.cleanup()

    def client(self):
        return TestClient(self.app)

    def test_counts_answers_even_on_an_empty_store(self):
        r = self.client().get("/api/staging/counts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("counts", r.json())

    def test_vocabulary_returns_plain_names(self):
        """The seed mixes strings (subjects) with tagged objects (corps); the
        picker needs one flat list of names or it cannot be built at all."""
        r = self.client().get("/api/staging/vocab")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("corps", "subjects", "topics"):
            self.assertIsInstance(body[key], list, key)
            for name in body[key]:
                self.assertIsInstance(name, str, key)

    def test_the_retired_studio_routes_are_gone(self):
        for path in ("/api/studio/state", "/api/studio/jobs", "/api/studio/models"):
            self.assertEqual(self.client().get(path).status_code, 404, path)

    def test_the_dashboard_still_serves_its_views(self):
        html = self.client().get("/").text
        self.assertIn('id="viewInspect"', html)
        self.assertNotIn("viewStudio", html)
        self.assertIn("static/inspect.js", html)


class StatusEndpointTest(unittest.TestCase):
    """A status write through HTTP is the button a human presses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = staging.DEFAULT_PATH
        staging.DEFAULT_PATH = os.path.join(self.tmp.name, "staging.db")
        self.store = staging.StagingStore(staging.DEFAULT_PATH, root=self.tmp.name)
        self.sid = self.store.upsert_source({"rel_path": "s/qu.pdf", "subject": "zist",
                                             "grade": 12, "topic": "ch1"})
        self.store.upsert_question({"id": "%s-q1" % self.sid, "source_id": self.sid,
                                    "question_number": 1, "question_text": "old",
                                    "page": 1, "bbox": [10, 10, 100, 100],
                                    "fields": {"subject": "zist", "topic": "ch1",
                                               "grade": 12}})

    def tearDown(self):
        self.store.close()
        staging.DEFAULT_PATH = self.saved
        self.tmp.cleanup()

    def test_an_edit_then_ready_persists_across_requests(self):
        import main
        c = TestClient(main.app)
        rid = "%s-q1" % self.sid
        r = c.post("/api/staging/records/question/%s" % rid,
                   json={"question_text": "corrected", "caption": "شکل"})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/staging/records/question/%s/status?value=ready" % rid)
        self.assertEqual(r.status_code, 200, r.text)

        again = staging.StagingStore(staging.DEFAULT_PATH, root=self.tmp.name)
        try:
            row = again.get("question", rid)
        finally:
            again.close()
        self.assertEqual(row["question_text"], "corrected")
        self.assertEqual(row["status"], "ready")

    def test_an_unknown_status_is_refused(self):
        """The status vocabulary is closed; a typo must not create a new one."""
        import main
        rid = "%s-q1" % self.sid
        r = TestClient(main.app).post(
            "/api/staging/records/question/%s/status?value=publish" % rid)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_a_retired_record_cannot_be_reopened(self):
        """`imported` is a one-way door: the live bank now owns the text, so a
        human must not be able to drag the row back to `staged` and re-import."""
        import main
        rid = "%s-q1" % self.sid
        c = TestClient(main.app)
        self.assertEqual(c.post(
            "/api/staging/records/question/%s/status?value=imported" % rid).status_code, 200)
        r = c.post("/api/staging/records/question/%s/status?value=staged" % rid)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.store.get("question", rid)["status"], "imported")

    def test_choices_and_difficulty_survive_a_save(self):
        """The Inspect form's two new surfaces must round-trip: the option rows
        it edits and the difficulty field it now carries."""
        import main
        rid = "%s-q1" % self.sid
        c = TestClient(main.app)
        r = c.post("/api/staging/records/question/%s" % rid, json={
            "options": [{"label": "1", "text": "واکنش گرماده است."},
                        {"label": "2", "text": "فشار کمتر است."},
                        {"label": "3", "text": ""}],          # a blank row is dropped
            "difficulty": "متوسط"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([o["text"] for o in body["record"]["options"]],
                         ["واکنش گرماده است.", "فشار کمتر است."])
        self.assertEqual(body["record"]["fields"]["difficulty"], "متوسط")

    def test_an_option_without_a_label_is_numbered(self):
        import main
        rid = "%s-q1" % self.sid
        c = TestClient(main.app)
        r = c.post("/api/staging/records/question/%s" % rid,
                   json={"options": ["یک", "دو"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["record"]["options"],
                         [{"label": "1", "text": "یک"},
                          {"label": "2", "text": "دو"}])

    def test_a_malformed_option_is_refused(self):
        import main
        rid = "%s-q1" % self.sid
        r = TestClient(main.app).post(
            "/api/staging/records/question/%s" % rid, json={"options": [7]})
        self.assertEqual(r.status_code, 400)


class ReparseStoredChoicesTest(unittest.TestCase):
    """Rows staged before the splitter existed still carry their choices glued
    to the stem with an empty options list. Re-OCR is the only other cure, so
    the store can be re-read once instead."""

    GLUED = ("کدام نادرست است؟\n"
             "۱) گزینه یک.\n۲) گزینه دو.\n۳) گزینه سه.\n۴) گزینه چهار.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = staging.DEFAULT_PATH
        staging.DEFAULT_PATH = os.path.join(self.tmp.name, "staging.db")
        self.store = staging.StagingStore(staging.DEFAULT_PATH, root=self.tmp.name)
        self.sid = self.store.upsert_source({"rel_path": "s/qu.pdf", "subject": "zist",
                                             "grade": 12, "topic": "ch1"})
        self.store.upsert_question({"id": "%s-q1" % self.sid, "source_id": self.sid,
                                    "question_number": 1,
                                    "question_text": self.GLUED,
                                    "options": [], "page": 1,
                                    "fields": {"subject": "zist", "topic": "ch1",
                                               "grade": 12}})

    def tearDown(self):
        self.store.close()
        staging.DEFAULT_PATH = self.saved
        self.tmp.cleanup()

    def _open(self):
        return staging.StagingStore(staging.DEFAULT_PATH, root=self.tmp.name)

    def test_a_dry_run_reports_without_writing(self):
        import main
        r = TestClient(main.app).post("/api/staging/reparse-options?dry_run=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], 1)
        again = self._open()
        try:
            row = again.get("question", "%s-q1" % self.sid)
        finally:
            again.close()
        self.assertEqual(row["question_text"], self.GLUED)
        self.assertEqual(row["options_json"], "[]")

    def test_the_split_is_written_and_the_choices_are_no_longer_glued(self):
        import main
        r = TestClient(main.app).post("/api/staging/reparse-options")
        self.assertEqual(r.status_code, 200, r.text)
        again = self._open()
        try:
            row = again.get("question", "%s-q1" % self.sid)
        finally:
            again.close()
        self.assertEqual(row["question_text"], "کدام نادرست است؟")
        options = __import__("json").loads(row["options_json"])
        self.assertEqual([o["text"] for o in options],
                         ["گزینه یک.", "گزینه دو.", "گزینه سه.", "گزینه چهار."])

    def test_a_machine_re_read_does_not_dress_a_row_as_human_edited(self):
        """`edited` means a person touched it; the status must survive."""
        import main
        c = TestClient(main.app)
        c.post("/api/staging/reparse-options")
        again = self._open()
        try:
            row = again.get("question", "%s-q1" % self.sid)
        finally:
            again.close()
        self.assertEqual(row["status"], "staged")

    def test_running_twice_changes_nothing_the_second_time(self):
        import main
        c = TestClient(main.app)
        c.post("/api/staging/reparse-options")
        r = c.post("/api/staging/reparse-options")
        self.assertEqual(r.json()["count"], 0, r.text)

    def test_a_row_that_already_has_choices_is_left_alone(self):
        import main
        self.store.update_record("question", "%s-q1" % self.sid,
                                 {"question_text": "کدام؟", "status": "staged",
                                  "options": [{"label": "1", "text": "الف"},
                                              {"label": "2", "text": "ب"}]})
        r = TestClient(main.app).post("/api/staging/reparse-options")
        self.assertEqual(r.json()["count"], 0, r.text)


class DecodedRecordTests(unittest.TestCase):
    """The reviewer's rows must arrive decoded.

    The store keeps bbox/options/candidates as JSON text; a source response
    that hands back the raw columns gives the bbox editor nothing to draw, and
    nothing on the page would ever show a box.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = staging.DEFAULT_PATH
        staging.DEFAULT_PATH = os.path.join(self.tmp.name, "staging.db")
        self.store = staging.StagingStore(staging.DEFAULT_PATH, root=self.tmp.name)
        self.sid = self.store.upsert_source({"rel_path": "s/qu.pdf", "subject": "zist",
                                             "grade": 12, "topic": "ch1",
                                             "page_count": 3})
        self.store.upsert_question({"id": "%s-q1" % self.sid, "source_id": self.sid,
                                    "question_number": 1, "question_text": "Q",
                                    "page": 2, "bbox": [80, 60, 320, 940],
                                    "options": [{"label": "1", "text": "A"}],
                                    "fields": {"subject": "zist", "topic": "ch1",
                                               "grade": 12}})

    def tearDown(self):
        self.store.close()
        staging.DEFAULT_PATH = self.saved
        self.tmp.cleanup()

    def source(self):
        import main
        r = TestClient(main.app).get("/api/staging/sources/%s" % self.sid)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_source_response_carries_a_decoded_record(self):
        row = self.source()["questions"][0]
        self.assertEqual(row["bbox"], [80, 60, 320, 940])
        self.assertEqual(row["options"], [{"label": "1", "text": "A"}])
        self.assertIsInstance(row["fields"], dict)
        self.assertEqual(row["kind"], "question")
        for raw in ("bbox_json", "options_json", "fields_json"):
            self.assertNotIn(raw, row)

    def test_each_record_points_at_its_own_page(self):
        """A box drawn on page 1 would be a lie about a row on page 3."""
        self.assertEqual(self.source()["questions"][0]["page"], 2)

if __name__ == "__main__":
    unittest.main()
