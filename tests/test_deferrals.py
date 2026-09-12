"""Write-cache (deferrals) and circuit-breaker tests (no network, no database).

Run them either way:
    python tests/test_deferrals.py
    pytest tests/test_deferrals.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import deferrals, runner                                      # noqa: E402
from pipeline.runner import Hub, _major_db_halt, _process_item              # noqa: E402
from pipeline.scanner import SourceItem                                     # noqa: E402


def source_item(**overrides):
    base = dict(source_id="abc123", rel_path="riazi/amar/question/q.jpg",
                subject="riazi", grade=12, topic="amar", type="سوال",
                file_name="q.jpg", file_path="C:/tmp/q.jpg", file_size=1024,
                mime_type="image/jpeg")
    base.update(overrides)
    return SourceItem(**base)


class FakeDB:
    """Records applied calls; every op named in `dead` keeps failing."""

    def __init__(self, dead=()):
        self.dead = set(dead)
        self.calls = []

    def _do(self, op, *args):
        self.calls.append((op, args))
        if op in self.dead:
            raise RuntimeError("could not connect to server: Connection refused")

    def upsert_source(self, item, storage_url):
        self._do("upsert_source", item, storage_url)

    def upsert_questions(self, rows):
        self._do("upsert_questions", rows)

    def upsert_answers(self, rows):
        self._do("upsert_answers", rows)

    def ops(self):
        return [op for op, _ in self.calls]


class FakeRouter:
    """Stands in for gemini_router.router inside pipeline.runner."""

    def __init__(self, parsed=None):
        self.parsed = parsed if parsed is not None else {
            "questions": [{"question_number": 1, "question_text": "hi"}]}

    def call(self, cfg, prompt, data_b64, mime, on_problem=None, on_route=None):
        return self.parsed, {}, {}


class CacheCase(unittest.TestCase):
    """Isolates the cache file + retry env for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="defer-test-")
        self._cache_path = deferrals.CACHE_PATH
        deferrals.CACHE_PATH = os.path.join(self.tmp, ".pending-deferrals.json")
        self._env = {k: os.getenv(k) for k in
                     ("DB_SAVE_RETRIES", "DB_SAVE_RETRY_WAIT",
                      "MAX_CONSECUTIVE_DB_FAILURES")}
        os.environ["DB_SAVE_RETRIES"] = "2"
        os.environ["DB_SAVE_RETRY_WAIT"] = "0"

    def tearDown(self):
        deferrals.CACHE_PATH = self._cache_path
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_cache(self):
        with open(deferrals.CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)


class CacheRoundTripTest(CacheCase):
    def test_park_then_flush_applies_everything(self):
        item = source_item()
        steps = [{"op": "upsert_source", "item": asdict(item),
                  "storage_url": "http://storage/abc123"},
                 {"op": "upsert_questions",
                  "rows": [{"id": "abc123-q1", "source_id": "abc123"}]}]
        deferrals.park(steps, item=item)

        entries = deferrals.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_id"], "abc123")
        self.assertEqual(entries[0]["rel_path"], item.rel_path)

        db = FakeDB()
        applied, still = deferrals.flush(db)
        self.assertEqual((applied, still), (1, 0))
        # steps replay in order so the sources row exists before its questions
        self.assertEqual(db.ops(), ["upsert_source", "upsert_questions"])
        self.assertIsInstance(db.calls[0][1][0], SourceItem)
        self.assertEqual(db.calls[1][1][0][0]["id"], "abc123-q1")
        # a fully imported cache leaves no file behind
        self.assertFalse(os.path.exists(deferrals.CACHE_PATH))

    def test_flush_keeps_what_still_cannot_be_written(self):
        deferrals.park([{"op": "upsert_questions", "rows": [{"id": "q1"}]}],
                       item=source_item())
        deferrals.park([{"op": "upsert_questions", "rows": [{"id": "q2"}]}],
                       item=source_item(source_id="def456"))

        db = FakeDB(dead={"upsert_questions"})
        applied, still = deferrals.flush(db)
        self.assertEqual((applied, still), (0, 2))
        # DB_SAVE_RETRIES=2 → two attempts per entry
        self.assertEqual(db.ops().count("upsert_questions"), 4)
        self.assertEqual([e["source_id"] for e in deferrals.load()],
                         ["abc123", "def456"])

    def test_corrupt_cache_is_quarantined_not_destroyed(self):
        with open(deferrals.CACHE_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        warns = []
        self.assertEqual(deferrals.load(on_warn=warns.append), [])
        self.assertEqual(len(warns), 1)
        self.assertTrue(os.path.exists(deferrals.CACHE_PATH + ".corrupt"))


class ProcessItemTest(CacheCase):
    def setUp(self):
        super().setUp()
        self.file_path = os.path.join(self.tmp, "q.jpg")
        with open(self.file_path, "wb") as fh:
            fh.write(b"fake image bytes")
        self._upload = runner.upload_file
        self._router = runner.router
        self.addCleanup(setattr, runner, "upload_file", self._upload)
        self.addCleanup(setattr, runner, "router", self._router)
        runner.upload_file = lambda *a, **k: "http://storage/abc123"
        runner.router = FakeRouter()

    def test_working_database_writes_everything_now(self):
        db = FakeDB()
        hub = Hub()
        outcome = _process_item(hub, None, db, source_item(file_path=self.file_path), 1, 1)
        self.assertEqual(outcome, "clean")
        self.assertEqual(db.ops(), ["upsert_source", "upsert_questions"])
        self.assertEqual(hub.stats["processed"], 1)
        self.assertEqual(hub.stats["deferred"], 0)
        self.assertFalse(os.path.exists(deferrals.CACHE_PATH))
        self.assertEqual(hub.files["abc123"]["status"], "done")

    def test_dead_question_save_parks_rows_and_archives_the_file(self):
        db = FakeDB(dead={"upsert_questions"})
        hub = Hub()
        outcome = _process_item(hub, None, db, source_item(file_path=self.file_path), 1, 1)
        self.assertEqual(outcome, "deferred")
        # the Gemini work survives on disk...
        entries = self.read_cache()
        self.assertEqual([s["op"] for s in entries[0]["steps"]], ["upsert_questions"])
        self.assertEqual(entries[0]["steps"][0]["rows"][0]["id"], "abc123-q1")
        # ...the file is archived (re-OCRing would burn quota)...
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "done", "q.jpg")))
        self.assertFalse(os.path.exists(self.file_path))
        # ...and the dashboard shows the parked state
        self.assertEqual(hub.files["abc123"]["status"], "cached")
        self.assertEqual(hub.stats["deferred"], 1)
        self.assertEqual(hub.stats["processed"], 1)
        self.assertEqual(hub.stats["errors"], 0)

    def test_parked_file_is_imported_by_the_end_of_run_flush(self):
        db = FakeDB(dead={"upsert_questions"})
        hub = Hub()
        _process_item(hub, None, db, source_item(file_path=self.file_path), 1, 1)
        # DB comes back for the end-of-run pass
        good = FakeDB()
        _flush_parked_logs = []
        applied, still = deferrals.flush(good, log=lambda *a: _flush_parked_logs.append(a))
        self.assertEqual((applied, still), (1, 0))
        self.assertEqual(good.ops(), ["upsert_questions"])

    def test_down_database_counts_as_store_failure(self):
        runner.upload_file = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("Supabase upload failed: HTTP 503 busy"))
        hub = Hub()
        outcome = _process_item(hub, None, FakeDB(), source_item(file_path=self.file_path), 1, 1)
        self.assertEqual(outcome, "store_failed")
        self.assertEqual(hub.stats["errors"], 1)
        # failed files stay in place for the next run and nothing is parked
        self.assertTrue(os.path.exists(self.file_path))
        self.assertFalse(os.path.exists(deferrals.CACHE_PATH))

    def test_network_failure_is_not_a_database_signal(self):
        runner.upload_file = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("cannot connect to proxy"))
        hub = Hub()
        outcome = _process_item(hub, None, FakeDB(), source_item(file_path=self.file_path), 1, 1)
        self.assertEqual(outcome, "failed")


class MajorHaltTest(CacheCase):
    def test_halt_explains_possible_causes_and_cache_location(self):
        deferrals.park([{"op": "upsert_answers", "rows": []}], item=source_item())
        hub = Hub()
        _major_db_halt(hub, lambda *a, **k: None, 3)
        errors = [e for e in hub.log if e["type"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("MAJOR", errors[0]["label"])
        text = errors[0]["hint"]
        for fragment in ("3 consecutive files", "docker compose",
                         "disk space", ".pending-deferrals.json"):
            self.assertIn(fragment, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
