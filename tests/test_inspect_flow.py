"""HTTP-level smoke for the Inspect page (no browser needed).

Walks every element `ui/inspect.js` reaches for, then saves a row, re-cuts its
box and reads the crop back — the three things the Inspect page exists to do.

Run: python tests/test_inspect_flow.py
"""
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(name):
    with open(os.path.join(ROOT, "ui", name), encoding="utf-8") as fh:
        return fh.read()


def _view(html, view_id):
    start = html.index('id="%s"' % view_id)
    end = html.find("</section>", start)
    return html[start:end]


class MarkupTests(unittest.TestCase):
    def test_every_id_the_script_uses_exists_once(self):
        html = _read("index.html")
        ids = set()
        for name in ("inspect.js", "inspect_doc.js"):
            found = re.findall(r"(?<![\w$])(?:ins|\\$)\('([A-Za-z0-9_]+)'\)", _read(name))
            ids.update(found)
        self.assertTrue(ids, "no element lookups found")
        # built into a template or on demand by a script, never static markup
        generated = {"insAlias", "insTagInput"}
        for element in sorted(ids - generated):
            self.assertEqual(html.count('id="%s"' % element), 1, element)

    def test_the_record_editor_is_a_modal_side_by_side(self):
        view = _view(_read("index.html"), "viewInspect")
        self.assertIn('id="insEdit"', view)
        self.assertIn('class="ins-edit-box"', view)
        self.assertIn('class="ins-edit-pane"', view)     # the picture
        self.assertIn('class="ins-edit-doc"', view)      # the editor
        # the page image and its crop box live INSIDE the modal
        self.assertIn('id="insPageImg"', view)
        self.assertIn('id="insBbox"', view)

    def test_the_document_editor_is_one_surface(self):
        view = _view(_read("index.html"), "viewInspect")
        for part in ('id="insDoc"', 'id="insDocEditor"', 'id="insDocOpts"',
                     'id="insMeta"', 'id="insTags"'):
            self.assertIn(part, view, part)
        # the old second boxes for the table and the caption are gone: one page
        self.assertNotIn('id="insTable"', view)
        self.assertNotIn('id="insCaption"', view)
        # both meta fields the user named, beside the tag editor
        for part in ('id="insCorp"', 'id="insYear"'):
            self.assertIn(part, view, part)


class RoundTripTests(unittest.TestCase):
    """A staged row opens with its stored box, an edit lands, and a re-cut
    writes the crop under the same key — against a throwaway store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from PIL import Image
        from pipeline import staging, staging_api

        self.store = staging.StagingStore(os.path.join(self.tmp.name, "staging.db"))
        app = FastAPI()
        app.include_router(staging_api.build_router(
            type("Cfg", (), {"staging_path": self.store.path})()))
        self.client = TestClient(app)

        # one page image and one OCR row carrying a box
        image = Image.new("RGB", (400, 600), "white")
        self.store.save_page("src1", 1, _jpeg(image))
        self.store.upsert_question({
            "id": "src1-q1", "source_id": "src1", "question_number": 1,
            "question_text": "متن سؤال", "options": [{"label": "1", "text": "الف"}],
            "bbox": [100, 100, 400, 400], "page": 1,
            "fields": {"corp": "ماز", "year": "۱۴۰۳", "tags": ["شیمی"]},
            "status": "staged"})

    def test_the_stored_box_and_fields_reach_the_reviewer(self):
        body = self.client.get("/api/staging/sources/src1").json()
        row = body["questions"][0]
        self.assertEqual(row["bbox"], [100, 100, 400, 400])
        self.assertEqual(row["fields"]["corp"], "ماز")
        self.assertEqual(row["page"], 1)

    def test_an_edit_and_a_recut_share_one_object_id(self):
        saved = self.client.post("/api/staging/records/question/src1-q1", json={
            "question_text": "متن تازه", "tags": ["شیمی", "فصل ۴"],
            "caption": "شکل ۱"}).json()
        self.assertEqual(saved["record"]["status"], "edited")
        self.assertEqual(saved["record"]["tags"], ["شیمی", "فصل ۴"])

        cut = self.client.post("/api/staging/recrop", json={
            "source_id": "src1", "kind": "question", "id": "src1-q1",
            "bbox": [200, 50, 500, 300]}).json()
        key = cut["crop_key"]
        self.assertEqual(cut["record"]["crop_key"], key)
        self.assertEqual(cut["record"]["bbox"], [200, 50, 500, 300])
        name = os.path.basename(key)
        status = self.client.get("/api/staging/crop/src1/" + name)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.headers["content-type"], "image/jpeg")
        # a second cut replaces the SAME file, never a variant beside it
        self.client.post("/api/staging/recrop", json={
            "source_id": "src1", "kind": "question", "id": "src1-q1",
            "bbox": [300, 60, 600, 320]})
        self.assertEqual(sorted(os.listdir(self.store.asset_dir("src1"))),
                         ["p1.jpg", name])


def _jpeg(image):
    import io
    buf = io.BytesIO()
    image.save(buf, "JPEG")
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
