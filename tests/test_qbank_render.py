"""QBank renderer pins for workstream C3 (PROPOSE-C.md §C3a/C3b/C3c).

The dashboard is vanilla JS with no test runner (the suite has no node
harness), so behaviour is pinned by reading `ui/app.js` / `ui/styles.css` —
the same idiom as `tests/test_ui_pages.py`. What these pins protect:

  C3a  question / options / explanation go through `mdHtml`, which fences
       each bare LaTeX environment and turns pipe tables into real <table>s;
       MathJax is still typeset after the cards are built.
  C3b  a card's picture comes from the `media_images` array (first entry)
       through `/api/media?object=`, never the single-segment legacy route.
  C3c  duplicate / unreadable / split are read-only chips.

Offline, no network, no database.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _func_body(js, name):
    """The source of `function <name>(…)` up to the next top-level `function`."""
    start = js.index("function %s(" % name)
    nxt = js.find("\nfunction ", start + 1)
    return js[start:nxt if nxt != -1 else len(js)]


def _fence_bare(cases):
    """Run the shipped `fenceBareTex` in node on the given inputs.

    The dashboard carries no JS test runner, so this executes the exact source
    the browser receives. Returns None when node is unavailable (same skip
    policy as the syntax check in test_studio_ui)."""
    node = shutil.which("node")
    if not node:
        return None
    js = _read("app.js")
    start = js.index("function fenceBareTex(")
    end = js.index("/* QBank / DB text", start)
    script = (js[start:end]
              + "\nconst cases = " + json.dumps(cases, ensure_ascii=False) + ";\n"
              + "console.log(JSON.stringify(cases.map(c => fenceBareTex(c))));\n")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(script)
        tmp.close()
        proc = subprocess.run([node, tmp.name], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


class BareTexFencingTests(unittest.TestCase):
    """OCR sometimes drops the $ around an expression; mdHtml must still show
    the rendered formula, without touching prose or already-delimited math."""

    def test_bare_tex_is_fenced_and_prose_is_left_alone(self):
        cases = [
            "فشار (\\rho = 0/8 \\frac{g}{cm^3}) است",
            "مقدار $x^2$ ثابت است",
            "متن ساده 123 است",
            "بار q_3 در مدار است",
            "ظرفیت (c_{\\text{آب}} = 4200 \\frac{J}{kg.K}) است",
        ]
        out = _fence_bare(cases)
        if out is None:
            self.skipTest("node unavailable")
        self.assertIn("$(\\rho", out[0])
        self.assertIn("\\frac{g}{cm^3})$", out[0])
        self.assertEqual(out[1], cases[1])
        self.assertEqual(out[2], cases[2])
        self.assertIn("$q_3$", out[3])
        self.assertIn("$(c_{\\text{آب}} = 4200", out[4])
        self.assertNotIn("{$", out[4])

    def test_mdhtml_runs_the_bare_tex_pass(self):
        js = _read("app.js")
        body = _func_body(js, "mdHtml")
        self.assertIn("s = fenceBareTex(s);", body)
        self.assertIn("function fenceBareTex(", js)


class QbankRenderPins(unittest.TestCase):

    def setUp(self):
        self.js = _read("app.js")
        self.css = _read("styles.css")

    # ---- C3a: Markdown tables + LaTeX fences -------------------------------

    def test_mdhlml_exists_and_is_used_for_the_question_body(self):
        self.assertIn("function mdHtml(", self.js)
        self.assertIn("qt.innerHTML = mdHtml(rawText)", self.js)
        self.assertNotIn("qt.appendChild(document.createTextNode(line))", self.js)

    def test_options_and_explanation_go_through_mdhlml(self):
        self.assertIn("txt.innerHTML = mdHtml(o.text", self.js)
        self.assertIn("expl.innerHTML = mdHtml(r.answer_explanation", self.js)
        # the old text-node loops are gone: no field is appended as plain text
        self.assertNotIn("expl.appendChild(document.createTextNode(line))", self.js)

    def test_mdhlml_fences_each_environment(self):
        body = _func_body(self.js, "mdHtml")
        # a function replacement, not a string one: '$$' is an escape in a
        # string replacement, so '$$' + m + '$$' would silently vanish
        self.assertIn("m => '$$' + m + '$$'", body)
        # the fence wraps one environment, not the whole field
        self.assertIn(r"\\begin\{[a-zA-Z*]+\}", body)
        self.assertIn(r"[\s\S]*?", body)

    def test_mdhlml_builds_pipe_tables_and_keeps_prose(self):
        body = _func_body(self.js, "mdHtml")
        self.assertIn('<table class="md-table">', body)
        self.assertIn("out.join('<br>')", body)
        # mdHtml escapes before it adds tags
        self.assertLess(body.index("dbEsc(String(raw"), body.index("<table"))

    def test_table_styles_exist(self):
        self.assertIn(".qcard .md-table", self.css)
        self.assertIn(".qcard .md-table td", self.css)

    def test_mathjax_still_typesets_after_render(self):
        self.assertIn("typesetMath(wrap);", self.js)
        load = self.js.index("async function loadReviewRows()")
        self.assertIn("typesetMath(wrap);", self.js[load:load + 6000])

    # ---- C3b: pictures from the media_images array -------------------------

    def test_media_entry_and_url_helpers(self):
        self.assertIn("function mediaEntry(", self.js)
        self.assertIn("function mediaUrl(", self.js)
        entry = _func_body(self.js, "mediaEntry")
        # a JSON string (older cached payload) and the array both parse
        self.assertIn("JSON.parse(list)", entry)
        self.assertIn("first.object", entry)
        self.assertIn("'/api/media?object=' + encodeURIComponent", self.js)
        self.assertIn("img.src = mediaUrl(object)", self.js)
        self.assertNotIn("img.src = '/api/proxy/image/' + encodeURIComponent(sourceId)",
                         self.js)

    def test_card_builds_the_box_from_media_images(self):
        card = _func_body(self.js, "renderCard")
        self.assertIn("mediaEntry(r.media_images)", card)
        self.assertIn("box.dataset.object = media.object", card)
        self.assertIn("box.dataset.index = '0'", card)
        self.assertNotIn("r.diagram_url", card)

    def test_bucket_drawer_uses_the_shared_media_url(self):
        self.assertIn("$('bucketDrawerImg').src = mediaUrl(name)", self.js)

    # ---- C3c: read-only provenance chips -----------------------------------

    def test_flag_chips_are_read_only(self):
        self.assertIn("'qflag f-' + field", self.js)
        for field in ("duplicate", "unreadable", "split"):
            self.assertIn("['%s'," % field, self.js, field)
        start = self.js.index("/* read-only provenance chips")
        end = self.js.index("card.appendChild(chips);", start)
        block = self.js[start:end]
        # a span with an icon and a label only - no button, no handler
        self.assertIn("document.createElement('span')", block)
        self.assertNotIn("addEventListener", block)
        self.assertNotIn("onclick", block)
        self.assertNotIn("createElement('button')", block)
        self.assertIn(".qcard .qflag", self.css)

    # ---- LaTeX that the OCR actually emits (2026-10-20) -------------------

    def test_mathjax_renders_through_chtml_not_svg(self):
        """Persian inside \text{} only shapes when the run is real text:
        SVG output draws per-glyph from the maths font, so the words come out
        as isolated, reversed letters."""
        html = _read("index.html")
        self.assertIn("mathjax@3/es5/tex-chtml.js", html)
        self.assertNotIn("tex-svg.js", html)
        self.assertNotIn("svg: { fontCache", html)

    def test_mdhlml_renames_the_environments_mathjax_lacks(self):
        body = _func_body(self.js, "mdHtml")
        # \\begin{table}[] is a LaTeX float wrapper -> drop it when a
        # table is fenced; MathJax has no such environment.
        self.assertIn(r"\\begin\{table\}", body)
        self.assertIn(r"\\end\{table\}", body)
        # \\begin{tabular}{|c|c|} -> array, which takes the same spec
        self.assertIn(r"\\begin\{tabular\}", body)
        self.assertIn(r"'\\begin{array}'", body)
        self.assertIn(r"\\end\{tabular\}", body)
        # \centering is a no-op outside a float and MathJax errors on it
        self.assertIn(r"\\centering", body)

    def test_mdhlml_drops_inline_dollars_inside_display_math(self):
        body = _func_body(self.js, "mdHtml")
        # the OCR nests "$^6\\text{A}$" inside a $$ block -> misplaced $
        self.assertIn(r"inner.replace(/(^|[^\\])\$/g, '$1')", body)

    # ---- a stored crop is the picture, not a page -------------------------

    def test_a_stored_crop_is_never_re_cropped(self):
        body = _func_body(self.js, "mountDiagrams")
        self.assertIn(r"/^crops\//.test(object)", body)
        # the page bbox is only applied to a whole page image
        self.assertIn("if (mode === 'crop' && !preCropped)", body)
        self.assertNotIn("if (mode === 'full' && !preCropped)", body)

    def test_the_crop_editor_backdrop_is_the_source_page(self):
        body = _func_body(self.js, "mountDiagrams")
        # the editor is offered again: its backdrop is the page the crop was
        # cut from, fetched lazily the first time a box is opened
        self.assertIn("'/api/qbank/page?question='", body)
        self.assertIn("function activeImg(){", body)
        self.assertIn("(mode === 'full' && preCropped) ? pageImg : img", body)
        self.assertIn("if (next === 'full') ensurePage();", body)
        self.assertNotIn(
            "viewSw.style.display = 'none';\n      editor.style.display = "
            "'none';", body)

    def test_saving_a_box_re_cuts_and_re_uploads_the_same_object(self):
        body = _func_body(self.js, "mountDiagrams")
        self.assertIn("preCropped ? '/api/qbank/recrop' : '/api/review/bbox'",
                      body)
        self.assertIn("index: Number(box.dataset.index || 0)", body)
        self.assertIn("if (preCropped && j.url) img.src = j.url;", body)


if __name__ == "__main__":
    unittest.main()
