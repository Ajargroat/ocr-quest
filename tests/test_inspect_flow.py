"""HTTP-level smoke for the Inspect page (no browser needed).

Walks every element `ui/inspect.js` reaches for, then saves a row, re-cuts its
box and reads the crop back — the three things the Inspect page exists to do.

Run: python tests/test_inspect_flow.py
"""
import json
import os
import re
import shutil
import subprocess
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

    def test_a_choice_is_typeset_like_the_stem(self):
        """An option's text is prose plus formulas, so it is rendered through
        the document's own Markdown -> chip pass, never a raw LaTeX <input>."""
        js = _read("inspect.js")
        doc = _read("inspect_doc.js")
        self.assertIn('class="ins-opt-text" contenteditable="true"', js)
        self.assertNotIn('class="ins-opt-text" value=', js)
        self.assertIn("InspDoc.choiceHtml(", js)
        self.assertIn("InspDoc.choiceInit(", js)
        for name in ("choiceHtml", "choiceText", "choiceInit"):
            self.assertIn(name, doc)


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not on PATH")
class ChipTypesetTests(unittest.TestCase):
    """A stored formula must reach MathJax, not stay visible as LaTeX.

    q72 stores its choices as bare `\\frac{1}{۳}` (no `$`), and MathJax skips
    nodes inside a `contenteditable` region — the two reasons a chip used to
    read as raw LaTeX. This runs `ui/inspect_doc.js` in Node with a stub DOM
    and a stub MathJax that records what it was asked to typeset."""

    def _run(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.js")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write(script)
            out = subprocess.run([NODE, probe, os.path.join(ROOT, "ui", "inspect_doc.js")],
                                 capture_output=True, text=True,
                                 encoding="utf-8", timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout

    def test_bare_tex_choice_becomes_a_typeset_chip(self):
        # q72's own stored choice: bare LaTeX, no `$` delimiters
        out = self._run(r"""
const fs = require('fs'), vm = require('vm');
// argv[0] is node, argv[1] this probe, argv[2] the script under test
const SRC = process.argv[2];
const asked = [];
function el(tag){
  const n = { nodeType: 1, tagName: tag.toUpperCase(), childNodes: [], _a: {},
              className: '', drawn: false,
              classList: { contains: c => false, add(){}, toggle(){} },
              setAttribute(k, v){ n._a[k] = String(v); },
              getAttribute(k){ return k in n._a ? n._a[k] : null; },
              appendChild(c){ n.childNodes.push(c); return c; },
              querySelector(sel){ return n.querySelectorAll(sel)[0] || null; },
              querySelectorAll(sel){
                return sel.indexOf('mjx-container') !== -1 && n.drawn
                  ? [el('mjx-container')] : [];
              },
              addEventListener(){},
              get textContent(){ return n.childNodes.length
                ? n.childNodes.map(c => c.textContent).join('') : n._t; },
              set textContent(v){ n.childNodes = []; n._t = v; } };
  return n;
}
const SOURCE = fs.readFileSync(SRC, 'utf8');
const ctx = {
  window: { MathJax: { typesetPromise(els){
    els.forEach(e => { asked.push(e.getAttribute('data-tex')); e.drawn = true; });
    return Promise.resolve(); } } },
  document: { createElement: el, createTextNode: t => ({ nodeType: 3, nodeValue: t }) },
  Node: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
  NodeFilter: { SHOW_TEXT: 4 }, setTimeout, clearTimeout, console,
};
ctx.window.window = ctx.window;
vm.createContext(ctx);
vm.runInContext(SOURCE, ctx);
const D = ctx.window.InspDoc;
const root = el('div');
const chip = el('span');
chip.className = 'ins-math';
chip.setAttribute('data-tex', '\\frac{1}{۳}');
chip.textContent = '\\(\\frac{1}{۳}\\)';
root.querySelectorAll = sel => sel.indexOf('.ins-math') !== -1 ? [chip] : [];
D.scheduleTypeset(root);
setTimeout(() => console.log(JSON.stringify(asked)), 400);
""")
        self.assertIn(r"\frac{1}{۳}", out,
                      "the choice's formula was never handed to MathJax")


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
