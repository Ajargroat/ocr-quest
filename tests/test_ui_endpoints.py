"""Offline tests for the UI-facing endpoints added by the 2026-10-06 task.

Covers the three dashboard surfaces that had no coverage yet:
  GET  /api/stats?period=…   — period stats (Q4/Q11, no schema change)
  GET  /api/usage?period=…   — per-call usage history (Q4)
  POST/GET/POST …/cancel /api/uploads — in-app PDF queue (Q9)

No network and no database: the DB call is stubbed on `main.db`, the
usage report is stubbed on `main.router`, uploads write into a temp dir.

Run them either way:
    python tests/test_ui_endpoints.py
    pytest tests/test_ui_endpoints.py
"""
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (offline-safe import, same as the other endpoint tests)
from pipeline import uploads  # noqa: E402

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class UIEndpointCase(unittest.TestCase):
    """Shared harness: temp .env, temp upload root, restored globals."""

    def setUp(self):
        self._env = dict(os.environ)
        fd, self._env_path = tempfile.mkstemp(suffix=".env", prefix="konkour-ui-")
        os.close(fd)
        with open(self._env_path, "w", encoding="utf-8") as fh:
            fh.write("# temp env for UI endpoint tests\n")
        self._old_env_path = main.ENV_PATH
        self._old_cfg = main.cfg
        main.ENV_PATH = self._env_path
        main.cfg = main.load_config()

        self._tmp = tempfile.TemporaryDirectory(prefix="konkour-up-")
        main.cfg = SimpleNamespace(input_root=self._tmp.name, min_age_seconds=60)

        self._old_fetch = main.db.fetch_period_counts
        self._old_usage = main.router.usage_report
        main._STATS_CACHE.update({"at": 0.0, "period": "", "payload": None})
        uploads.reset_for_tests()

    def tearDown(self):
        main.ENV_PATH = self._old_env_path
        main.cfg = self._old_cfg
        main.db.fetch_period_counts = self._old_fetch
        main.router.usage_report = self._old_usage
        main._STATS_CACHE.update({"at": 0.0, "period": "", "payload": None})
        uploads.reset_for_tests()
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()
        try:
            os.unlink(self._env_path)
        except OSError:
            pass

    @staticmethod
    def client():
        from fastapi.testclient import TestClient
        return TestClient(main.app)   # no `with`: the lifespan is not needed


class PeriodStatsEndpoint(UIEndpointCase):

    def test_unknown_period_is_rejected(self):
        res = self.client().get("/api/stats", params={"period": "week"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertFalse(res.json()["ok"])

    def test_counts_map_onto_the_panel_fields(self):
        def fake(date_from=None, date_to=None):
            return {"questions": 11, "answers": 7, "errors": 2,
                    "files": 30, "total": 30, "succeeded": 18}
        main.db.fetch_period_counts = fake
        res = self.client().get("/api/stats", params={"period": "7d"})
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["period"], "7d")
        self.assertEqual(d["succeeded"], 18)
        self.assertEqual(d["errors"], 2)
        self.assertEqual(d["total"], 30)
        self.assertEqual(d["questions"], 11)
        self.assertEqual(d["answers"], 7)

    def test_date_window_follows_the_period(self):
        seen = []
        def fake(date_from=None, date_to=None):
            seen.append((date_from, date_to))
            return {"questions": 0, "answers": 0, "errors": 0,
                    "files": 0, "total": 0, "succeeded": 0}
        main.db.fetch_period_counts = fake
        self.client().get("/api/stats", params={"period": "24h"})
        main._STATS_CACHE.update({"at": 0.0, "period": "", "payload": None})
        self.client().get("/api/stats", params={"period": "all"})
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0][0])   # 24h has a lower bound
        self.assertIsNone(seen[1][0])      # all-time has none
        self.assertIsNotNone(seen[0][1])   # and an upper bound

    def test_result_is_cached_for_sixty_seconds(self):
        calls = []
        def fake(date_from=None, date_to=None):
            calls.append(1)
            return {"questions": 0, "answers": 0, "errors": 0,
                    "files": 0, "total": 0, "succeeded": 0}
        main.db.fetch_period_counts = fake
        self.client().get("/api/stats", params={"period": "30d"})
        self.client().get("/api/stats", params={"period": "30d"})
        self.assertEqual(len(calls), 1)

    def test_db_failure_is_reported_not_raised(self):
        def boom(date_from=None, date_to=None):
            raise RuntimeError("connection refused")
        main.db.fetch_period_counts = boom
        res = self.client().get("/api/stats", params={"period": "all"})
        self.assertEqual(res.status_code, 502)
        self.assertFalse(res.json()["ok"])
        self.assertIn("connection refused", res.json()["error"])


class UsageReportEndpoint(UIEndpointCase):

    def test_report_carries_period_rows_and_totals(self):
        rows = [{"ts": 1, "model": "gemini-3.5-flash", "key_name": "lane-q",
                 "key_masked": "AIza…xyz", "tokens_in": 10, "tokens_out": 20,
                 "ms": 123.4, "ok": True}]
        main.router.usage_report = lambda period="all": {
            "period": period, "calls": rows,
            "totals": {"calls": 1, "tokens_in": 10, "tokens_out": 20, "ms": 123.4}}
        res = self.client().get("/api/usage", params={"period": "24h"})
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["period"], "24h")
        self.assertEqual(d["totals"]["calls"], 1)
        self.assertEqual(d["calls"][0]["key_masked"], "AIza…xyz")

    def test_unknown_period_falls_back_to_all_time(self):
        seen = []
        main.router.usage_report = lambda period="all": (
            seen.append(period) or {"period": period, "calls": [],
                                     "totals": {"calls": 0, "tokens_in": 0,
                                                "tokens_out": 0, "ms": 0}})
        res = self.client().get("/api/usage", params={"period": "decade"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(seen, ["all"])
        self.assertEqual(res.json()["period"], "all")


class BucketEndpoint(UIEndpointCase):
    """C14 (item 3.4): GET /api/db/bucket proxies the Supabase Storage list
    API read-only, with the configured bucket name echoed back."""

    def setUp(self):
        super().setUp()
        self._old_list = main.storage.list_objects
        main.cfg.supabase_bucket = "test-bucket"

    def tearDown(self):
        main.storage.list_objects = self._old_list
        super().tearDown()

    def test_bucket_list_is_proxied(self):
        seen = {}

        def fake(cfg, prefix="", limit=100, offset=0):
            seen.update({"prefix": prefix, "limit": limit, "offset": offset})
            return [{"name": "sid-01.pdf", "id": "x"}]

        main.storage.list_objects = fake
        res = self.client().get("/api/db/bucket",
                                params={"prefix": "math/", "limit": 5})
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["bucket"], "test-bucket")
        self.assertEqual(d["objects"][0]["name"], "sid-01.pdf")
        self.assertEqual(seen["prefix"], "math/")
        self.assertEqual(seen["limit"], 5)

    def test_bucket_error_is_502(self):
        def boom(cfg, prefix="", limit=100, offset=0):
            raise RuntimeError("no bucket")

        main.storage.list_objects = boom
        res = self.client().get("/api/db/bucket")
        self.assertEqual(res.status_code, 502)
        self.assertFalse(res.json()["ok"])


class UploadEndpoints(UIEndpointCase):

    def _post(self, name="sheet.pdf", dest="math/101/algebra/question",
              content=PDF):
        return self.client().post(
            "/api/uploads",
            files={"file": (name, io.BytesIO(content), "application/pdf")},
            data={"dest": dest})

    def test_valid_upload_is_queued_and_listed(self):
        res = self._post()
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["dest_rel"], "math/101/algebra/question")
        self.assertEqual(d["status"], "queued")
        listed = self.client().get("/api/uploads").json()["items"]
        self.assertEqual([i["id"] for i in listed], [d["id"]])

    def test_bad_destination_is_400(self):
        res = self._post(dest="math/101/question")   # only three segments
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.json()["ok"])

    def test_non_pdf_is_400(self):
        res = self._post(name="sheet.txt", content=b"not a pdf")
        self.assertEqual(res.status_code, 400)
        self.assertIn("pdf", res.json()["error"].lower())

    def test_cancel_drops_a_queued_item(self):
        uid = self._post().json()["id"]
        res = self.client().post(f"/api/uploads/{uid}/cancel")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "cancelled")
        self.assertEqual(self.client().get("/api/uploads").json()["items"], [])

    def test_cancel_unknown_id_is_404(self):
        res = self.client().post("/api/uploads/deadbeef/cancel")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(res.json()["ok"])

    def test_in_flight_item_cannot_be_cancelled(self):
        uid = self._post().json()["id"]
        uploads.mark(uid, "extracting", "reading pages")
        res = self.client().post(f"/api/uploads/{uid}/cancel")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
