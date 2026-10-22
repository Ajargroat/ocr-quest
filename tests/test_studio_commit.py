"""Tests for the Inspect desk's local-first commit (drafts + commit).

Offline and DB/bucket-free: the staged pictures live in a temp workspace, the
upload and the database handle are stubbed, and the assertions pin the promise
the feature makes -- nothing leaves the machine until approval, and approval
uploads the reviewer's picture exactly as cropped.

Every literal here is ASCII on purpose: the console this suite runs under is
cp1256 and must never be asked to print Persian text.

Run either way:
    python tests/test_studio_commit.py
    pytest tests/test_studio_commit.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.studio import commit, drafts, workspace  # noqa: E402


def _png(color=(200, 30, 30)):
    """A tiny real PNG, so drafts.save exercises the JPEG re-encode."""
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (40, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _DB:
    """A database handle that records every write, in order."""

    def __init__(self, cfg=None):
        self.calls = []

    def upsert_source(self, item, storage_url):
        self.calls.append(("upsert_source", item, storage_url))

    def upsert_questions(self, rows):
        self.calls.append(("upsert_questions", rows))

    def upsert_answers(self, rows):
        self.calls.append(("upsert_answers", rows))

    def question_rows(self):
        for call in self.calls:
            if call[0] == "upsert_questions":
                return call[1]
        return []


class CommitCase(unittest.TestCase):
    """Shared harness: temp workspace, one job with a question block."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-commit-")
        self.src = tempfile.mkdtemp(prefix="studio-commit-src-")
        self._old_ws = os.environ.get("STUDIO_WORKSPACE")
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_WORKSPACE"] = self.root
        os.environ["STUDIO_SOURCE_ROOT"] = self.src
        self.cfg = SimpleNamespace(studio_crop_prefix="crops")
        self.uploaded = []

    def tearDown(self):
        if self._old_ws is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old_ws
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def _job(self, crop_bbox=(10, 10, 120, 90)):
        """A job whose single question is ready, with one detected crop."""
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf",
                              "exam.pdf")
        profile = {"static": {"subject": "Chemistry", "topic": "Mixtures",
                              "grade": "10"}}
        workspace.write_artifact(jid, "profile", profile)
        workspace.write_artifact(jid, "layer2", {
            "job_id": jid,
            "source": "exam.pdf",
            "profile": profile,
            "stats": {},
            "blocks": [{
                "key": "p1-b1", "kind": "question", "status": "ready",
                "number": 1, "text": "first", "options": ["A) one", "B) two"],
                "proposed": [], "missing": [],
                "subject": "Chemistry", "topic": "Mixtures",
            }],
        })
        boxes = {}
        if crop_bbox:
            boxes["p1-b1"] = {"crop_bbox": list(crop_bbox), "crop_page": 1,
                              "spec": "spec-1", "source": "vision"}
        workspace.write_artifact(jid, "geometry", {"boxes": boxes})
        return jid

    def _job_with_page(self, size=(400, 600)):
        """A job whose geometry entry points at a real stored page image."""
        from PIL import Image, ImageDraw
        jid = self._job()
        page = workspace.page_image_path(jid, 1)
        image = Image.new("RGB", size, (255, 255, 255))
        ImageDraw.Draw(image).rectangle([40, 60, 199, 299], fill=(220, 20, 20))
        image.save(page, format="JPEG", quality=95)
        rel = os.path.relpath(page, workspace.job_dir(jid))
        workspace.write_artifact(jid, "geometry", {"boxes": {
            "p1-b1": {"crop_bbox": [100, 100, 500, 500], "crop_page": 1,
                      "crop_method": "vision", "crop_image": rel,
                      "source": "vision"}}})
        return jid
    def _patch_bucket(self):
        """Stub the bucket and the database for one commit."""
        from pipeline.studio.importers import supabase
        recorder = _DB()

        def upload(cfg, obj, data, mime):
            self.uploaded.append({"object": obj, "bytes": len(data),
                                  "mime": mime})
            return "https://bucket/" + obj

        patches = [
            mock.patch.object(supabase.db_mod, "Database",
                              lambda cfg: recorder),
            mock.patch.object(supabase.storage, "upload_file", upload),
            mock.patch.object(supabase.storage, "delete_file",
                              lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return recorder

    def _apply(self, jid, edits=None, crops=None, dry_run=False):
        recorder = self._patch_bucket()
        result = commit.apply(self.cfg, jid, edits or {}, crops or {},
                              target="supabase", dry_run=dry_run,
                              emit=lambda *a, **k: None)
        return result, recorder


class DraftsTests(CommitCase):
    def test_a_chosen_picture_is_staged_as_jpeg_and_never_uploaded(self):
        jid = self._job()
        info = drafts.save(jid, "p1-b1", _png())
        self.assertEqual(info["width"], 40)
        self.assertEqual(info["height"], 24)
        self.assertEqual(list(drafts.staged(jid)), ["p1-b1"])
        data = drafts.read(jid, "p1-b1")
        self.assertTrue(data.startswith(b"\xff\xd8"), "staged bytes are JPEG")
        with open(drafts.draft_path(jid, "p1-b1"), "rb") as fh:
            self.assertEqual(data, fh.read())
        # local first: staging alone touches neither bucket nor database
        self.assertEqual(self.uploaded, [])

    def test_a_non_image_is_rejected(self):
        jid = self._job()
        with self.assertRaises(ValueError):
            drafts.save(jid, "p1-b1", b"not an image")

    def test_drop_removes_only_that_picture(self):
        jid = self._job()
        drafts.save(jid, "p1-b1", _png())
        drafts.save(jid, "p1-b2", _png())
        self.assertTrue(drafts.drop(jid, "p1-b1"))
        self.assertEqual(list(drafts.staged(jid)), ["p1-b2"])
        self.assertFalse(drafts.drop(jid, "p1-b1"))


class CommitTests(CommitCase):
    def test_edits_are_written_to_review_json(self):
        # no detected crop: an edit alone is enough to import
        jid = self._job(crop_bbox=None)
        result, recorder = self._apply(jid, edits={"p1-b1": {"text": "fixed"}})
        review = workspace.read_artifact(jid, "review")
        self.assertEqual(review["edits"]["p1-b1"]["text"], "fixed")
        rows = recorder.question_rows()
        self.assertEqual(rows[0]["question_text"], "fixed")
        self.assertEqual(result["uploads"], 0)

    def test_an_approved_picture_uploads_as_cropped_and_sets_diagram_url(self):
        jid = self._job()
        drafts.save(jid, "p1-b1", _png())
        staged_bytes = drafts.read(jid, "p1-b1")
        result, recorder = self._apply(jid)
        # one upload, exactly the staged bytes, under the stable object id
        self.assertEqual(len(self.uploaded), 1)
        object_id = self.uploaded[0]["object"]
        self.assertTrue(object_id.startswith("crops/"))
        self.assertTrue(object_id.endswith("/p1-b1.jpg"))
        self.assertEqual(self.uploaded[0]["mime"], "image/jpeg")
        self.assertEqual(self.uploaded[0]["bytes"], len(staged_bytes))
        # the approval consumed the local drafts
        self.assertEqual(drafts.staged(jid), {})
        review = workspace.read_artifact(jid, "review")
        self.assertTrue(review["crops"]["p1-b1"]["approved"])
        self.assertTrue(review["crops"]["p1-b1"]["external"])
        self.assertEqual(result["uploads"], 1)
        row = recorder.question_rows()[0]
        self.assertEqual(row["diagram_url"], "https://bucket/" + object_id)
        # the approved picture has no page box of its own
        self.assertIsNone(row["diagram_bbox"])
        pictures = json.loads(row["media_images"])
        self.assertEqual(pictures[0]["object"], object_id)
        # the crop cache keeps the reviewer's own bytes
        with open(workspace.crop_path(jid, "p1-b1"), "rb") as fh:
            self.assertEqual(fh.read(), staged_bytes)

    def test_a_picture_survives_when_no_box_was_detected(self):
        jid = self._job(crop_bbox=None)
        drafts.save(jid, "p1-b1", _png())
        result, recorder = self._apply(jid)
        row = recorder.question_rows()[0]
        self.assertEqual(result["uploads"], 1)
        self.assertEqual(row["diagram_url"],
                         "https://bucket/" + self.uploaded[0]["object"])
        self.assertIsNone(row["diagram_bbox"])

    def test_dry_run_previews_without_changing_anything(self):
        jid = self._job()
        drafts.save(jid, "p1-b1", _png())
        result, recorder = self._apply(
            jid, edits={"p1-b1": {"text": "preview"}}, dry_run=True)
        self.assertEqual(self.uploaded, [])
        self.assertEqual(recorder.calls, [])
        self.assertEqual(result["staged"], ["p1-b1"])
        self.assertEqual(len(drafts.staged(jid)), 1)
        # the preview is read-only: no review.json, no crop cache write
        self.assertIsNone(workspace.read_artifact(jid, "review"))
        self.assertFalse(os.path.exists(workspace.crop_path(jid, "p1-b1")))


class DraftCutTests(CommitCase):
    """The Inspect square turns into a local picture - never a bucket write."""

    def test_cut_turns_the_square_into_a_staged_picture(self):
        from PIL import Image
        jid = self._job_with_page()
        info = drafts.cut(jid, "p1-b1", [100, 100, 500, 500])
        self.assertTrue(os.path.exists(drafts.draft_path(jid, "p1-b1")))
        self.assertGreater(info["bytes"], 0)
        with Image.open(drafts.draft_path(jid, "p1-b1")) as image:
            # 10 %..50 % of a 400x600 page, plus the two-pixel cut pad
            self.assertEqual(image.size, (164, 244))
            centre = image.convert("RGB").getpixel((image.width // 2,
                                                    image.height // 2))
        self.assertGreater(centre[0], 150)
        self.assertLess(centre[1], 90)
        # local first: the geometry artifact and the crop cache are untouched
        boxes = workspace.read_artifact(jid, "geometry")["boxes"]
        self.assertEqual(boxes["p1-b1"]["crop_bbox"], [100, 100, 500, 500])
        self.assertFalse(os.path.exists(workspace.crop_path(jid, "p1-b1")))

    def test_cut_refuses_a_degenerate_box(self):
        jid = self._job_with_page()
        with self.assertRaises(ValueError):
            drafts.cut(jid, "p1-b1", [100, 100, 101, 101])

    def test_cut_without_a_page_says_so(self):
        jid = self._job()          # fake PDF source, no stored page image
        with self.assertRaises(ValueError):
            drafts.cut(jid, "p1-b1", [100, 100, 500, 500])

class CommitEndpointTests(CommitCase):
    """The HTTP surface the Inspect tab calls."""

    def setUp(self):
        super().setUp()
        import main
        self.main = main
        self._old_cfg = main.cfg
        main.cfg = self.cfg
        self.addCleanup(self._restore_cfg)
        try:
            from fastapi.testclient import TestClient
            self.client = TestClient(main.app)
        except Exception:      # pragma: no cover - fastapi present in the suite
            self.client = None

    def _restore_cfg(self):
        self.main.cfg = self._old_cfg

    def test_stage_read_and_drop_a_picture_over_http(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        jid = self._job()
        res = self.client.post("/api/studio/jobs/%s/draft/crop/p1-b1" % jid,
                               files={"file": ("crop.png", _png(),
                                               "image/png")})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertIn("/draft/crop/p1-b1", body["url"])
        res = self.client.get("/api/studio/jobs/%s/draft/crop/p1-b1" % jid)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "image/jpeg")
        bulk = self.client.get("/api/studio/jobs/%s/bulk" % jid).json()
        self.assertIn("p1-b1", bulk["drafts"])
        res = self.client.delete("/api/studio/jobs/%s/draft/crop/p1-b1" % jid)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(drafts.staged(jid), {})

    def test_cut_endpoint_stages_a_page_box(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        jid = self._job_with_page()
        res = self.client.post("/api/studio/jobs/%s/draft/cut/p1-b1" % jid,
                               json={"bbox": [100, 100, 500, 500]})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertIn("/draft/crop/p1-b1", body["url"])
        self.assertTrue(os.path.exists(drafts.draft_path(jid, "p1-b1")))
        served = self.client.get("/api/studio/jobs/%s/draft/crop/p1-b1" % jid)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["content-type"], "image/jpeg")
        bulk = self.client.get("/api/studio/jobs/%s/bulk" % jid).json()
        self.assertIn("p1-b1", bulk["drafts"])
        stamped = os.path.getmtime(drafts.draft_path(jid, "p1-b1"))
        bad = self.client.post("/api/studio/jobs/%s/draft/cut/p1-b1" % jid,
                               json={"bbox": [0, 0, 1, 1]})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(stamped, os.path.getmtime(drafts.draft_path(jid, "p1-b1")))
    def test_a_bad_job_id_is_refused(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/studio/jobs/..%2F..%2Fx/draft/crop/k",
                               files={"file": ("crop.png", _png(),
                                               "image/png")})
        self.assertIn(res.status_code, (400, 404))

    def test_commit_endpoint_imports_the_batch(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        jid = self._job()
        drafts.save(jid, "p1-b1", _png())
        self._patch_bucket()
        res = self.client.post("/api/studio/jobs/%s/commit" % jid,
                               json={"edits": {"p1-b1": {"text": "fixed"}},
                                     "crops": {}, "target": "supabase"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["report"]["questions"], 1)
        self.assertEqual(drafts.staged(jid), {})

    def test_an_unknown_job_is_a_404(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/studio/jobs/nope/commit", json={})
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
