"""Offline tests for P5/P6/P7 behaviour: UTC-midnight usage reset,
quota-exhausted skip-till-reset, per-call JSON history + period filter,
exponential backoff, and DB period counts.

No network, no database (DB paths stub the _execute seam; history and
usage paths are redirected to tmp dirs).

Run them either way:
    python tests/test_percall_history.py
    pytest tests/test_percall_history.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import faults, gemini_router
from pipeline.gemini_router import (
    GeminiRouter,
    _next_utc_midnight_ts,
    PERIOD_HOURS,
)
from pipeline.db import Database


class UtcMidnightTests(unittest.TestCase):
    def test_today_is_utc_date(self):
        r = GeminiRouter()
        self.assertEqual(r._today(),
                         datetime.now(timezone.utc).date().isoformat())

    def test_next_utc_midnight_is_tonight(self):
        now = time.time()
        nxt = _next_utc_midnight_ts(now)
        self.assertGreater(nxt, now)
        self.assertLessEqual(nxt - now, 24 * 3600)
        day = datetime.fromtimestamp(nxt, timezone.utc)
        self.assertEqual((day.hour, day.minute, day.second), (0, 0, 0))


class QuotaSkipTests(unittest.TestCase):
    @staticmethod
    def _noon():
        # Pin the clock to 12:00 UTC: the block-until-midnight assertions
        # need a full day's headroom and fail in the last hour before
        # midnight if they use real wall-clock time.
        return datetime.now(timezone.utc).replace(
            hour=12, minute=0, second=0, microsecond=0).timestamp()

    def test_quota_block_runs_to_next_utc_midnight(self):
        r = GeminiRouter()
        now = self._noon()
        r._mark("k1111111111", "quota", "gemini-3.5-flash", now=now)
        blocked_until = r._entry("k1111111111")["blocked"]["gemini-3.5-flash"]
        # Parked until the refill moment — far beyond the old +3600s retry,
        # so no consecutive error chains the same day.
        self.assertGreater(blocked_until, now + 3600)
        self.assertAlmostEqual(blocked_until,
                               _next_utc_midnight_ts(now), delta=1.0)

    def test_quota_blocked_model_leaves_the_plan_same_day(self):
        r = GeminiRouter()
        from tests.test_gemini_router import make_cfg
        now = self._noon()
        r._mark("k1111111111", "quota", "gemini-3.5-flash", now=now)
        plan = r._plan(make_cfg(keys=("k1111111111",)), now + 7200)
        self.assertTrue(plan)
        models = [m for _i, _k, ms, _l, _p in plan for m in ms]
        self.assertNotIn("gemini-3.5-flash", models)


class PerCallHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._real_usage = gemini_router.USAGE_PATH
        gemini_router.USAGE_PATH = os.path.join(self._tmp.name, "usage.json")

    def tearDown(self):
        gemini_router.USAGE_PATH = self._real_usage
        self._tmp.cleanup()

    def _calls_file(self):
        return os.path.join(self._tmp.name, ".gemini-calls.json")

    def test_completed_send_appends_a_history_row(self):
        r = GeminiRouter()
        r._count_send("supersecretkey", "gemini-3.5-flash", ms=123.4)
        with open(self._calls_file(), encoding="utf-8") as fh:
            rows = json.load(fh)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["model"], "gemini-3.5-flash")
        self.assertEqual(row["ms"], 123.4)
        self.assertTrue(row["ok"])
        # the secret itself is never persisted — hash + mask only
        self.assertNotIn("supersecretkey", json.dumps(rows))
        self.assertIn("key_hash", row)
        self.assertIn("key_masked", row)

    def test_report_filters_by_preset_periods(self):
        r = GeminiRouter()
        now = time.time()
        rows = [
            {"ts": now - 3600, "model": "m1", "tokens_in": 1,
             "tokens_out": 2, "ms": 10.0},
            {"ts": now - 3 * 24 * 3600, "model": "m2", "tokens_in": 4,
             "tokens_out": 8, "ms": 20.0},
            {"ts": now - 60 * 24 * 3600, "model": "m3", "tokens_in": 16,
             "tokens_out": 32, "ms": 30.0},
        ]
        with open(self._calls_file(), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        self.assertEqual(len(r.usage_report("24h")["calls"]), 1)
        self.assertEqual(len(r.usage_report("7d")["calls"]), 2)
        self.assertEqual(len(r.usage_report("30d")["calls"]), 2)
        self.assertEqual(len(r.usage_report("all")["calls"]), 3)
        totals = r.usage_report("24h")["totals"]
        self.assertEqual(totals["calls"], 1)
        self.assertEqual(totals["tokens_in"], 1)
        self.assertEqual(totals["tokens_out"], 2)

    def test_period_hours_match_the_preset_chips(self):
        self.assertEqual(PERIOD_HOURS, {"24h": 24, "7d": 24 * 7,
                                        "30d": 24 * 30})


class BackoffTests(unittest.TestCase):
    def test_sequence_doubles_from_base_to_cap(self):
        seq = [faults.backoff_wait(a, base=5.0, cap=120.0) for a in (1, 2, 3, 4, 5, 6, 10)]
        self.assertEqual(seq, [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0])

    def test_env_defaults_are_short_first_wait_with_cap(self):
        old_base, old_cap = (os.environ.get("NET_RETRY_WAIT"),
                             os.environ.get("NET_RETRY_CAP"))
        os.environ["NET_RETRY_WAIT"] = "5"
        os.environ["NET_RETRY_CAP"] = "120"
        try:
            self.assertEqual(faults.backoff_wait(1), 5.0)
            self.assertEqual(faults.backoff_wait(4), 40.0)
            self.assertEqual(faults.backoff_wait(99), 120.0)
        finally:
            for k, v in (("NET_RETRY_WAIT", old_base),
                         ("NET_RETRY_CAP", old_cap)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class PeriodCountsTests(unittest.TestCase):
    def _db(self, counts):
        db = Database(SimpleNamespace(
            postgres_host="", postgres_port=0, postgres_db="",
            postgres_user="", postgres_password="", postgres_sslmode=""))
        it = iter(counts)

        def fake_execute(sql, params=None, fetch=False):
            return [(next(it),)]

        db._execute = fake_execute
        return db

    def test_counts_map_in_order(self):
        db = self._db([10, 4, 1, 7])
        got = db.fetch_period_counts("2026-09-01", "2026-10-05")
        self.assertEqual(got["questions"], 10)
        self.assertEqual(got["answers"], 4)
        self.assertEqual(got["errors"], 1)
        self.assertEqual(got["files"], 7)
        self.assertEqual(got["total"], 7)
        self.assertEqual(got["succeeded"], 14)

    def test_db_failure_degrades_to_zeros(self):
        db = Database(SimpleNamespace(
            postgres_host="", postgres_port=0, postgres_db="",
            postgres_user="", postgres_password="", postgres_sslmode=""))

        def boom(sql, params=None, fetch=False):
            raise RuntimeError("down")

        db._execute = boom
        got = db.fetch_period_counts()
        self.assertEqual(got, {"questions": 0, "answers": 0, "errors": 0,
                               "files": 0, "total": 0, "succeeded": 0})


if __name__ == "__main__":
    unittest.main()
