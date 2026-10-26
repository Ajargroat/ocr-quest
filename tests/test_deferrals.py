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
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import deferrals, faults, runner                          # noqa: E402
from pipeline.runner import Hub, _major_db_halt, _process_item, blocked_remaining  # noqa: E402
from pipeline.scanner import SourceItem                                     # noqa: E402
from pipeline.staging import StagingStore                                   # noqa: E402


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

    def __init__(self, parsed=None, fault=None):
        self.parsed = parsed if parsed is not None else {
            "questions": [{"question_number": 1, "question_text": "hi"}]}
        self.fault = fault

    def call(self, cfg, prompt, data_b64, mime, on_problem=None, on_route=None,
             **kwargs):
        if self.fault is not None:
            raise self.fault
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
    """P3: one OCR file stages locally and writes NOTHING live.

    Before P3 these tests pinned the opposite contract (the runner uploaded to
    the bucket, upserted Postgres and parked failed writes). That path is
    retired, so the pins now say: a staged file touches no db.* write and no
    bucket, and an empty extraction is reported instead of hidden.
    """

    def setUp(self):
        super().setUp()
        self.file_path = os.path.join(self.tmp, "q.jpg")
        from PIL import Image
        Image.new("RGB", (60, 80), (255, 255, 255)).save(self.file_path, "JPEG")
        self._router = runner.router
        self.addCleanup(setattr, runner, "router", self._router)
        runner.router = FakeRouter()
        self.store = StagingStore(os.path.join(self.tmp, "staging", "staging.db"),
                                  root=os.path.join(self.tmp, "staging"))
        self.addCleanup(self.store.close)
        self.cfg = SimpleNamespace(ocr_pages_per_call=1, ocr_provider="gemini",
                                   ocr_model="", staging_path=self.store.path)

    def _item(self):
        return source_item(file_path=self.file_path)

    def test_a_clean_file_stages_and_archives_with_no_live_write(self):
        db = FakeDB()
        hubs = Hub()
        outcome = _process_item(hubs, self.cfg, db, self._item(), 1, 1,
                                store=self.store)
        self.assertEqual(outcome, "clean")
        # zero live writes: the fake DB saw no write-shaped call at all
        self.assertEqual([op for op, _ in db.calls if op != "fetch_context_questions"],
                         [])
        self.assertEqual(hubs.stats["processed"], 1)
        self.assertEqual(hubs.stats["deferred"], 0)
        self.assertFalse(os.path.exists(deferrals.CACHE_PATH))
        self.assertEqual(hubs.files["abc123"]["status"], "done")
        staged = self.store.get("question", "abc123-q1")
        self.assertEqual(staged["status"], "staged")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "done", "q.jpg")))
        self.assertFalse(os.path.exists(self.file_path))

    def test_the_source_page_and_its_row_are_both_kept(self):
        _process_item(Hub(), self.cfg, FakeDB(), self._item(), 1, 1, store=self.store)
        self.assertTrue(os.path.exists(self.store.page_path("abc123", 1)))
        self.assertEqual(self.store.get_source("abc123")["page_count"], 1)

    def test_an_empty_extraction_is_reported_and_still_archived(self):
        runner.router = FakeRouter(parsed={"questions": []})
        hubs = Hub()
        outcome = _process_item(hubs, self.cfg, FakeDB(), self._item(), 1, 1,
                                store=self.store)
        self.assertEqual(outcome, "empty")
        self.assertEqual(hubs.files["abc123"]["status"], "empty")
        self.assertEqual(self.store.get_source("abc123")["status"], "empty")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "done", "q.jpg")))

    def test_a_staging_failure_is_a_store_fault_and_keeps_the_file(self):
        self.store.close()          # every staging write now raises
        hubs = Hub()
        outcome = _process_item(hubs, self.cfg, FakeDB(), self._item(), 1, 1,
                                store=self.store)
        self.assertEqual(outcome, "store_failed")
        self.assertEqual(hubs.stats["errors"], 1)
        self.assertTrue(os.path.exists(self.file_path))
        self.assertFalse(os.path.exists(deferrals.CACHE_PATH))

    def test_pool_exhausted_returns_blocked(self):
        """An exhausted flag on the router's fault means the whole run should
        stop (Q4) — the file is reported blocked, not merely failed."""
        flt = faults.fault("quota", "all keys cooling down")
        flt["exhausted"] = True
        runner.router = FakeRouter(fault=faults.FaultError(flt, "no pair left"))
        hubs = Hub()
        outcome = _process_item(hubs, self.cfg, FakeDB(), self._item(), 1, 1,
                                store=self.store)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(os.path.exists(self.file_path))   # the file is not moved
        # the same fault WITHOUT the flag is an ordinary failure, not a stop
        runner.router = FakeRouter(
            fault=faults.FaultError(faults.fault("quota", "spent"), "spent"))
        outcome = _process_item(hubs, self.cfg, FakeDB(), self._item(), 1, 1,
                                store=self.store)
        self.assertEqual(outcome, "failed")


class BlockedRemainingTest(unittest.TestCase):
    def test_blocked_remaining_counts_the_failing_file(self):
        """N = the failing file plus the remaining queue (Q4)."""
        self.assertEqual(blocked_remaining(50, 1), 50)
        self.assertEqual(blocked_remaining(50, 50), 1)
        self.assertEqual(blocked_remaining(50, 51), 0)


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
