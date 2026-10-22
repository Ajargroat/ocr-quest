"""The per-file lane and the per-file pop-up its row opens.

Covers the studio-local side of "every file can run its own model": the
workspace/<job>/studio.json overrides, the config copy one stage receives, the
job's own prompt reaching the four stage call sites, and the lane / import-rows
endpoints (offline: a temp workspace, no network, no database).

Every literal here is ASCII on purpose: the console is cp1256.

Run either way:
    python tests/test_studio_lane.py
    pytest tests/test_studio_lane.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.studio import lane, llm, workspace  # noqa: E402

JOB = "20260101-000000-abcd"


def _cfg(**over):
    base = dict(studio_extract_active="", studio_enrich_active="",
                studio_vision_active="", studio_gemini_pin="",
                studio_gemini_model_prompt=None,
                studio_providers=({"label": "9router",
                                   "base_url": "http://127.0.0.1:20128/v1",
                                   "api_key": "sk-x", "model": "m1",
                                   "models": ["m1", "m2"],
                                   "models_enabled": None},))
    base.update(over)
    return SimpleNamespace(**base)


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="studio-lane-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.dir
        workspace.job_dir(JOB, create=True)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


class DeleteJobTests(LaneCase):
    """Deleting a job takes the stored source with it: the workspace and the
    source tree are separate directories, and clearing one used to orphan the
    uploaded file for good."""

    def test_delete_removes_workspace_and_source(self):
        from pipeline.studio import jobs
        source = workspace.source_dir(JOB)
        self.assertTrue(source)
        os.makedirs(source, exist_ok=True)
        with io.open(os.path.join(source, "scan.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4 test")

        self.assertTrue(jobs.delete_job(JOB))
        self.assertFalse(os.path.isdir(workspace.job_dir(JOB)))
        self.assertFalse(os.path.isdir(source))

    def test_delete_of_a_missing_job_is_false(self):
        from pipeline.studio import jobs
        self.assertFalse(jobs.delete_job("20990101-000000-nope"))


class LaneFileTests(LaneCase):
    def test_an_unsaved_job_follows_the_stage_defaults(self):
        self.assertEqual(lane.read(JOB), {"provider": "", "prompt": ""})
        cfg = _cfg()
        self.assertIs(lane.lane_cfg(cfg, JOB, "extract"), cfg)
        self.assertEqual(lane.used(cfg, JOB, "extract"), "")

    def test_an_override_is_saved_and_read_back(self):
        lane.save(JOB, {"provider": "9router", "prompt": "BE SHORT"})
        self.assertEqual(lane.read(JOB),
                         {"provider": "9router", "prompt": "BE SHORT"})
        with open(os.path.join(self.dir, JOB, "studio.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["provider"], "9router")

    def test_the_config_copy_carries_the_lane_to_one_stage_only(self):
        lane.save(JOB, {"provider": "9router"})
        cfg = _cfg()
        copy = lane.lane_cfg(cfg, JOB, "extract")
        self.assertEqual(copy.studio_extract_active, "9router")
        self.assertEqual(copy.studio_enrich_active, "")
        self.assertEqual(cfg.studio_extract_active, "", "shared cfg untouched")
        self.assertEqual(lane.used(cfg, JOB, "extract"), "9router")

    def test_a_job_prompt_reaches_the_stage_call(self):
        # the override lands on the copy; the call appends it to the prompt
        copy = SimpleNamespace(studio_extract_active="",
                               studio_gemini_model_prompt={"extract": "SPARE"})
        self.assertEqual(llm.job_prompt(copy, "extract"), "SPARE")
        self.assertTrue(llm.stage_prompt(copy, "extract", "BASE").endswith("SPARE"))
        self.assertEqual(llm.stage_prompt(copy, "enrich", "BASE"), "BASE")

    def test_a_model_pick_is_kept_when_enabled_list_lacks_it(self):
        provider = _cfg().studio_providers[0]
        picked = llm.choose_model(provider, "m2")
        self.assertEqual(picked["model"], "m2")
        # models_enabled None = every rung on, so the pick is honoured as-is
        self.assertIsNone(picked["models_enabled"])
        off = dict(provider, models_enabled=["m1"])
        self.assertIn("m2", llm.choose_model(off, "m2")["models_enabled"])
        self.assertEqual(provider["model"], "m1", "the shared entry is untouched")

    def test_an_old_label_still_finds_the_loopback_provider(self):
        cfg = _cfg(studio_providers=({"label": "local",
                                      "base_url": "http://127.0.0.1:20128/v1",
                                      "api_key": "k", "model": "m"},))
        self.assertTrue(llm.provider_for(cfg, "9router"))
        self.assertTrue(llm.provider_for(cfg, "local"))
        self.assertEqual(llm.provider_for(cfg, "other"), {})

    def test_a_save_drops_the_rendered_preview(self):
        os.makedirs(os.path.join(self.dir, JOB, lane.PREVIEW_DIR), exist_ok=True)
        path = lane.preview_path(JOB, 1)
        with open(path, "wb") as fh:
            fh.write(b"jpeg")
        lane.save(JOB, {"provider": "9router"})
        self.assertFalse(os.path.exists(path))


class LaneEndpointTests(LaneCase):
    def setUp(self):
        super().setUp()
        try:
            from fastapi.testclient import TestClient
            import main
            self.client = TestClient(main.app)
        except Exception:  # pragma: no cover - fastapi is in the suite
            self.client = None

    def test_the_lane_endpoint_reads_saves_and_rejects(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        url = "/api/studio/jobs/%s/lane" % JOB
        view = self.client.get(url).json()
        self.assertTrue(view["ok"])
        for key in ("job", "lane", "stages", "blocks"):
            self.assertIn(key, view)
        saved = self.client.post(url, json={"provider": "",
                                            "prompt": "SHORT"})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["lane"]["prompt"], "SHORT")
        bad = self.client.post(url, json={"provider": "no-such-provider"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.client.get(
            "/api/studio/jobs/nope/lane").status_code, 404)

    def test_the_import_table_lists_rows_not_a_count(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.get("/api/studio/import/rows",
                              params={"job": JOB, "others": 1})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["totals"]["rows"], 0)
        self.assertIn("targets", body)

    def test_the_preview_endpoint_never_500s_into_a_traceback(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        # no source and no layer2: the renderer cannot build a page, and the
        # endpoint answers a clean error rather than raising
        res = self.client.get("/api/studio/jobs/%s/preview/1" % JOB)
        self.assertIn(res.status_code, (404, 500))
        self.assertEqual(self.client.get(
            "/api/studio/jobs/nope/preview/1").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
