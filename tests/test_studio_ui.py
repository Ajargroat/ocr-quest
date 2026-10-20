"""Studio view + endpoint coherence pins (Phase 1).

Holds the contract the studio UI binds to: the sidebar entry, the seven-stage
rail, the one-page section stack and every id studio.js reaches for — so a
rename in the HTML can never silently break the JS. Also pins the studio routes
onto the FastAPI app (offline; no DB, no network).

Run either way:
    python tests/test_studio_ui.py
    pytest tests/test_studio_ui.py
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _view(html, view_id):
    body = html.split('id="%s"' % view_id, 1)[1]
    nxt = body.find('<section id="view')
    return body[:nxt] if nxt != -1 else body


class StudioMarkupTests(unittest.TestCase):
    def test_sidebar_has_a_studio_entry(self):
        html = _read("index.html")
        self.assertIn('data-view="studio"', html)

    def test_studio_view_exists_and_revision_view_is_gone(self):
        html = _read("index.html")
        self.assertIn('id="viewStudio"', html)
        self.assertNotIn('id="viewRevision"', html)
        self.assertNotIn('data-view="revision"', html)

    def test_studio_has_the_seven_stage_rail(self):
        html = _read("index.html")
        view = _view(html, "viewStudio")
        for stage in ("intake", "probe", "extract", "enrich", "geometry",
                      "review", "import"):
            self.assertIn('data-stage="%s"' % stage, view, stage)

    def test_studio_is_a_stage_tab_rail_with_one_pane_each(self):
        """One tab per stage, one pane per tab; the old scroll stack is gone."""
        html = _read("index.html")
        view = _view(html, "viewStudio")
        order = ("jobs", "profile", "bulk", "crops", "tags", "models", "import")
        self.assertIn('id="stTabs"', view)
        rail = view[view.index('id="stTabs"'):view.index("</div>", view.index('id="stTabs"'))]
        pos = [rail.index('data-sttab="%s"' % s) for s in order]
        self.assertEqual(pos, sorted(pos), "tabs out of order: %r" % (pos,))
        for s in order:
            self.assertIn('class="st-pane" data-sttab="%s"' % s, view, s)
        self.assertNotIn('data-stsec="', view)
        self.assertNotIn('class="db-panel"', view)

    def test_every_id_studio_js_binds_exists_in_the_html(self):
        html = _read("index.html")
        js = _read("studio.js")
        ids = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js))
        # ids created dynamically by studio.js itself are not in the HTML
        dynamic = {"stKind", "stComp", "stAnswer", "stSaveProfile", "stReProbe",
                   "stExtract", "stRunEnrich", "stRunGeometry",
                   "stPagesPerCall", "stBandBatch", "stSaveBatching",
                   "stImportTarget", "stImportDry", "stImportErr", "stImportRun",
                   "stCropPageIn", "stTagList"}
        missing = sorted(i for i in ids if i not in dynamic and ('id="%s"' % i) not in html)
        self.assertEqual(missing, [], "studio.js binds ids missing from index.html: %s" % missing)

    def test_studio_js_is_loaded_after_app_js(self):
        html = _read("index.html")
        self.assertIn('static/studio.js', html)
        self.assertLess(html.index('static/app.js'), html.index('static/studio.js'))

    def test_app_js_delegates_studio_events(self):
        js = _read("app.js")
        self.assertIn("studioFeed(ev)", js)
        self.assertIn("viewStudio", js)

    def test_studio_panels_have_real_renderers(self):
        js = _read("studio.js")
        for fn in ("studioRenderBulk", "studioRenderCrops", "studioRenderImport",
                   "studioLoadBulk", "studioRunStage"):
            self.assertIn("function " + fn, js, fn)

    def test_extract_panel_exposes_request_batching(self):
        """The two knobs the model lane actually reads: pages and bands/call."""
        js = _read("studio.js")
        for phrase in ("pages / call", "bands / call", "studioSaveBatching",
                       "/api/studio/settings"):
            self.assertIn(phrase, js, phrase)

    def test_no_bare_font_size_literals_in_studio_css(self):
        css = _read("styles.css")
        studio_block = css.split("8 · STUDIO")[1] if "8 · STUDIO" in css else ""
        bad = [ln.strip()[:80] for ln in studio_block.splitlines()
               if re.search(r"font-size:\d", ln)
               and "Vazirmatn" not in ln and "JetBrains" not in ln]
        self.assertEqual(bad, [], "literal font-size off a text-family rule: %s" % bad)


    def test_studio_has_no_coaching_text(self):
        html = _read("index.html")
        js = _read("studio.js")
        view = _view(html, "viewStudio")
        banned = ("Upload any exam file", "No bulk yet", "No crops yet",
                  "later phase", "No studio jobs yet", "upload a PDF or image above",
                  "Pick a job on the Jobs panel", "Leave a field empty",
                  "Seed vocabulary mirrors", "New tags are proposed",
                  "Per-stage model pickers land", "Bulk import lands",
                  "every field is pinned", "extract per question")
        for phrase in banned:
            self.assertNotIn(phrase, view, phrase)
            self.assertNotIn(phrase, js, phrase)

    def test_studio_selects_are_themed_like_the_pipeline_page(self):
        """Every studio <select> lives in .st-field, whose rule carries the
        pipeline's themed-select treatment: mono font, custom arrow, hover/focus."""
        css = _read("styles.css")
        studio_block = css.split("8 · STUDIO")[1]
        # the standalone rule (the combined input,select rule comes first)
        sel = studio_block.split(".st-field select{appearance", 1)
        self.assertEqual(len(sel), 2, "standalone .st-field select rule missing")
        sel = sel[1].split("}", 1)[0]
        for need in ("none", "JetBrains Mono", "background-image",
                     "cursor:pointer"):
            self.assertIn(need, sel, need)
        self.assertIn(".st-field select:hover{", studio_block)
        self.assertIn(".st-field select:focus{", studio_block)

    def test_studio_stats_render_as_themed_chips_not_plain_text(self):
        """Counts/verdicts go through stStats() chips (.st-stat), never a bare
        <p class="hint"> or <span class="hint"> line."""
        js = _read("studio.js")
        css = _read("styles.css")
        self.assertIn("function stStats(", js)
        self.assertIn("class=\"st-stat", js.replace("'", '"'))
        # the three stat surfaces built chips
        self.assertIn("probeStats", js)
        self.assertIn("['ready', stNum(s.ready || 0), 'ok']", js)
        self.assertIn("['crop', crop.approved", js)
        # no plain-hint stat line survives in the panels
        self.assertNotIn('<p class="hint">\' + stEsc(probeLine', js)
        self.assertNotIn("<span class=\"hint\">ready ", js.replace("'", '"'))
        studio_block = css.split("8 · STUDIO")[1]
        self.assertIn(".st-stats{", studio_block)
        self.assertIn(".st-stat{", studio_block)

class StudioUiSplitChipTests(unittest.TestCase):
    """The split flag is visible in Bulk and releasable in Crops (A's Q1)."""

    def test_bulk_renders_the_split_chip(self):
        js = _read("studio.js")
        for need in ("stSplitChip", "split_joined", "sc-pending"):
            self.assertIn(need, js, need)

    def test_crop_row_writes_split_ok(self):
        js = _read("studio.js")
        for need in ("data-split", "split_ok"):
            self.assertIn(need, js, need)


class StudioUiCropTests(unittest.TestCase):
    """Crops: page bar at the bottom-left, text rows, picture in a drawer."""

    def test_crop_picture_lives_in_a_drawer(self):
        html, js = _read("index.html"), _read("studio.js")
        view = _view(html, "viewStudio")
        self.assertIn('id="stCropDrawer"', view)
        self.assertIn('class="bucket-drawer"', view)
        self.assertIn("function stCropDrawerPaint(", js)
        self.assertIn("data-crop-open", js)
        # no inline canvas/crop image in the row list any more
        self.assertNotIn('<div class="diag">', js)

    def test_page_number_box_and_arrows(self):
        js = _read("studio.js")
        for need in ("stCropPageIn", "stCropPagebar", 'data-page="-1"',
                     'data-page="1"', "function stCropSetPage("):
            self.assertIn(need, js, need)

    def test_rows_are_one_pdf_page_at_a_time(self):
        js = _read("studio.js")
        for need in ("function stBlockPage(", "function stCropPages(",
                     "cur.blocks.map(stCropRowHtml)", "stWireCropPagebar"):
            self.assertIn(need, js, need)

    def test_re_crop_posts_the_reviewed_box(self):
        js = _read("studio.js")
        self.assertIn("stCropDrawerApply", js)
        self.assertIn("+ '/crop/' + encodeURIComponent(key)", js)
        self.assertIn("method:'POST'", js)          # studioJson is a POST


class StudioTagsTests(unittest.TestCase):
    """Tags: one vocabulary group at a time, Persian fields read RTL."""

    def test_scope_picker_wears_the_studio_select_skin(self):
        js = _read("studio.js")
        self.assertIn("stTagSelectHtml(", js)
        self.assertIn('<select class="st-sel"', js)
        self.assertIn("ST_TAG_SCOPES", js)
        for scope in ("corps", "subjects", "lessons"):
            self.assertIn("['%s'," % scope, js)
        self.assertIn("data-tag-scope", js)
        self.assertIn("data-tag-subject", js)
        self.assertIn("data-tag-grade", js)
        self.assertNotIn("stTagDd(", js)
        self.assertNotIn('<div class="dd"', js)
        css = _read("styles.css")
        sel = css.split(".st-tags-bar .st-sel{")[1].split("}")[0]
        self.assertIn("JetBrains Mono", sel)
        self.assertIn("background-size:5px 5px,5px 5px", sel)
        self.assertIn(".st-tags-bar .st-sel:focus{", css)

    def test_tag_fields_are_rtl_and_numbers_ltr(self):
        js = _read("studio.js")
        self.assertIn('class="st-rtl" type="text" dir="rtl"', js)
        self.assertIn('class="st-num" type="text"', js)
        css = _read("styles.css").split("8 · STUDIO")[1]
        self.assertIn(".st-rtl{direction:rtl;text-align:right", css)
        self.assertIn(".st-num{direction:ltr", css)


class StudioTabTests(unittest.TestCase):
    """The stage tab rail is the navigation; panes hide, the deep link follows."""

    def test_one_pane_visible_at_a_time(self):
        js = _read("studio.js")
        self.assertIn("const ST_TABS = ['jobs', 'profile', 'bulk', 'crops',"
                      " 'tags', 'models', 'import']", js)
        self.assertIn("p.hidden = p.dataset.sttab !== name", js)
        self.assertIn("'#studio/' + name", js)
        self.assertIn("history.replaceState", js)

    def test_studio_init_renders_every_section(self):
        js = _read("studio.js")
        start = js.index("async function studioInit")
        body = js[start:js.index("\n}", start)]
        self.assertIn("studioRenderTags()", body)
        self.assertIn("studioRenderModels()", body)

    def test_rail_is_followed_by_the_live_strip_and_the_tabs(self):
        html = _read("index.html")
        view = _view(html, "viewStudio")
        rail_at = view.index('id="stStages"')
        rail = view[rail_at:view.index("</section>", rail_at)]
        self.assertNotIn('id="stNow"', rail)          # moved out of the rail box
        self.assertLess(view.index('data-stage="import"'),
                        view.index('id="stNow"'))
        self.assertLess(view.index('id="stNow"'), view.index('id="stTabs"'))
        self.assertLess(view.index('id="stTabs"'), view.index('id="stPaneJobs"'))
        self.assertIn('id="stNowPage"', view)
        self.assertIn('id="stNowChunk"', view)

    def test_studio_css_has_the_tab_surfaces(self):
        css = _read("styles.css").split("8 · STUDIO")[1]
        for rule in (".st-tabbar{", ".st-pane[hidden]{", ".st-table-wrap{",
                     ".st-pagebar{", ".st-crop-list{", ".st-rtl{"):
            self.assertIn(rule, css, rule)
        self.assertNotIn(".st-sec-stack{", css)


class StudioLiveStripTests(unittest.TestCase):
    """D2/D4 — the strip consumes the page/chunk contract and moves the rail."""

    def test_feed_consumes_the_chunk_contract(self):
        js = _read("studio.js")
        for token in ("ev.page_start", "ev.page_end", "ev.chunk", "ev.chunks",
                      "stNowPage", "stNowChunk"):
            self.assertIn(token, js, token)

    def test_pages_are_latin_digits_and_collapse_one_page(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        js = _read("studio.js")
        src = js[js.index("const ST_RAIL"):js.index("function stChunks")]
        script = (
            "const stNum = v => String(v == null ? '' : v);\n" + src + """
if (stPages({page_start:4, page_end:4}) !== 'page 4') process.exit(1);
if (stPages({page_start:4, page_end:5}) !== 'page 4\u20135') process.exit(2);
if (stPages({}) !== '') process.exit(3);
""")
        proc = subprocess.run([node], input=script.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=30)
        self.assertEqual(proc.returncode, 0,
                         "node rejected the stPages contract (exit %d)"
                         % proc.returncode)

    def test_success_refreshes_profile_and_rail(self):
        js = _read("studio.js")
        body = js[js.index("function studioFeed"):]
        self.assertIn("studioSelectJob(ev.job, { keepPanel:true })", body)
        self.assertIn("stRailActive(", body)


class StudioRouteTests(unittest.TestCase):
    def test_studio_routes_are_registered(self):
        import main  # noqa: E402  (offline-safe)
        paths = {r.path for r in main.app.routes if getattr(r, "path", "").startswith("/api/studio")}
        for want in ("/api/studio/jobs", "/api/studio/jobs/{job_id}",
                     "/api/studio/jobs/{job_id}/probe",
                     "/api/studio/jobs/{job_id}/profile",
                     "/api/studio/jobs/{job_id}/extract",
                     "/api/studio/jobs/{job_id}/enrich",
                     "/api/studio/jobs/{job_id}/geometry",
                     "/api/studio/jobs/{job_id}/import",
                     "/api/studio/jobs/{job_id}/bulk",
                     "/api/studio/jobs/{job_id}/crop/{key}",
                     "/api/studio/jobs/{job_id}/review",
                     "/api/studio/state", "/api/studio/tags",
                     "/api/studio/models", "/api/studio/settings"):
            self.assertIn(want, paths, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
