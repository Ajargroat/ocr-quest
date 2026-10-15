"""Studio view + endpoint coherence pins (Phase 1).

Holds the contract the studio UI binds to: the sidebar entry, the seven-stage
rail, the panel switcher and every id studio.js reaches for — so a rename in
the HTML can never silently break the JS. Also pins the studio routes onto the
FastAPI app (offline; no DB, no network).

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

    def test_studio_view_exists_and_is_after_revision(self):
        html = _read("index.html")
        self.assertIn('id="viewStudio"', html)
        self.assertLess(html.index('id="viewRevision"'), html.index('id="viewStudio"'))

    def test_studio_has_the_seven_stage_rail(self):
        html = _read("index.html")
        view = _view(html, "viewStudio")
        for stage in ("intake", "probe", "extract", "enrich", "geometry",
                      "review", "import"):
            self.assertIn('data-stage="%s"' % stage, view, stage)

    def test_studio_panel_switcher_has_every_panel(self):
        html = _read("index.html")
        view = _view(html, "viewStudio")
        for panel in ("jobs", "profile", "bulk", "crops", "tags", "models", "import"):
            self.assertIn('data-spanel="%s"' % panel, view, panel)

    def test_every_id_studio_js_binds_exists_in_the_html(self):
        html = _read("index.html")
        js = _read("studio.js")
        ids = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", js))
        # ids created dynamically by studio.js itself are not in the HTML
        dynamic = {"stKind", "stComp", "stAnswer", "stSaveProfile", "stReProbe"}
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
                  "Per-stage model pickers land", "Bulk import lands")
        for phrase in banned:
            self.assertNotIn(phrase, view, phrase)
            self.assertNotIn(phrase, js, phrase)

class StudioRouteTests(unittest.TestCase):
    def test_studio_routes_are_registered(self):
        import main  # noqa: E402  (offline-safe)
        paths = {r.path for r in main.app.routes if getattr(r, "path", "").startswith("/api/studio")}
        for want in ("/api/studio/jobs", "/api/studio/jobs/{job_id}",
                     "/api/studio/jobs/{job_id}/probe",
                     "/api/studio/jobs/{job_id}/profile",
                     "/api/studio/state", "/api/studio/tags",
                     "/api/studio/models"):
            self.assertIn(want, paths, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
