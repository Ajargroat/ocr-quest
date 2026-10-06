"""Pipeline + Credentials coherence pins (restructure 2026-10-07).

The user re-laid the pipeline view: period stats at the VERY TOP, then the
stage strip, the toolbar (run + provider/proxy context on the right), a
full-width upload panel, and the live log / files side by side in a grid
below it. Section titles (.rv-head) are gone — the sidebar tag is the title.
New-profile buttons live in the Credentials toolbar, not the pipeline.

These pins hold that layout and the JS contract (every id app.js binds,
plus #credRail .rail-btn[data-cred] and .cred-card[data-cred]).
"""
import os
import re
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _view(html, view_id):
    """The markup of one view, up to the next view section."""
    body = html.split('id="%s"' % view_id, 1)[1]
    nxt = body.find('<section id="view')
    return body[:nxt] if nxt != -1 else body


class PipelineCoherenceTests(unittest.TestCase):
    def test_pipeline_uses_the_house_vocabulary(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        # stats first, then strip → toolbar → full-width upload → log/files grid
        order = ['id="periodStats"', 'class="stages"', 'class="stage-now"',
                 'class="toolbar"', 'class="panel upload-panel"',
                 'class="grid2"']
        pos = [view.index(tok) for tok in order]
        self.assertEqual(pos, sorted(pos),
                         "pipeline sections are out of order: %s" % list(zip(order, pos)))

    def test_no_section_titles(self):
        """The sidebar tag is the title — no in-view .rv-head blocks anywhere."""
        html = _read("index.html")
        self.assertNotIn('class="rv-head"', html)

    def test_stats_are_first_in_the_view(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertLess(view.index('id="periodStats"'),
                        view.index('class="stages"'),
                        "period stats must sit at the very top of the pipeline")
        for need in ('id="psSucceeded"', 'id="stTotal"', 'id="psCaption"'):
            self.assertIn(need, view, need)

    def test_log_sits_in_the_bottom_grid(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        # the upload panel is full width; log and files share the grid under it
        self.assertLess(view.index('class="panel upload-panel"'),
                        view.index('class="grid2"'))
        grid = view.split('class="grid2"', 1)[1]
        self.assertIn('id="log"', grid, "live log belongs in the grid")
        self.assertIn('id="files"', grid, "files panel belongs in the grid")
        # ... and the grid is after the stats header
        self.assertLess(view.index('id="periodStats"'), view.index('class="grid2"'))

    def test_stage_strip_is_intact(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        strip = view.split('class="stages"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(strip.count('class="stage"'), 5, "want 5 stages")
        self.assertEqual(strip.count('class="wire"'), 4, "want 4 wires")
        self.assertEqual(strip.count('<em class="bubble">'), 5, "want 5 bubbles")
        for st in ("scan", "upload", "ocr", "save", "archive"):
            self.assertIn('data-stage="%s"' % st, strip, st)
        # live-state readout lives inside the strip (like Revision's .stage-now)
        self.assertIn('id="stageNow"', strip)

    def test_toolbar_holds_action_and_context(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        bar = view.split('class="toolbar"', 1)[1].split("</section>", 1)[0]
        for need in ('id="runBtn"', 'id="pipeProvSel"', 'id="pipeProvHint"',
                     'id="pipeProxySel"', 'id="pipeProxyHint"'):
            self.assertIn(need, bar, need)
        # the profile buttons belong to Credentials, not the pipeline toolbar
        self.assertNotIn('id="pipeProxyNew"', bar)
        self.assertNotIn('id="pipeProxyDel"', bar)

    def test_upload_panel_is_full_width_with_dropdowns(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        panel = view.split('class="panel upload-panel"', 1)[1].split("</section>", 1)[0]
        for need in ('id="upList"', 'id="upFiles"', 'id="upAdd"',
                     'id="upSubject"', 'id="upGrade"', 'id="upTopic"', 'id="upType"'):
            self.assertIn(need, panel, need)
        # the destination is picked from themed dropdowns, never typed
        self.assertNotIn('id="upPath"', panel)
        self.assertNotIn("<select", panel)

    def test_log_is_a_house_panel(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertIn('id="log"', view)
        self.assertIn('id="logCount"', view)
        self.assertIn('id="logClearBtn"', view)
        self.assertIn('id="diagCount"', view)
        self.assertNotIn('class="work-log"', view)
        self.assertNotIn('class="run-console"', view)


class CredentialsCoherenceTests(unittest.TestCase):
    def test_rail_is_a_service_switcher(self):
        html = _read("index.html")
        rail = html.split('id="credRail"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(rail.count('class="rail-btn'), 7, "7 services")
        for svc in ("gemini", "router", "ocr", "revision",
                    "supabase", "postgres", "routing"):
            self.assertIn('data-cred="%s"' % svc, rail, svc)

    def test_one_detail_pane_per_service(self):
        html = _read("index.html")
        for svc in ("gemini", "router", "ocr", "revision",
                    "supabase", "postgres", "routing"):
            self.assertIn('class="panel cred-card" data-cred="%s"' % svc, html, svc)

    def test_profile_buttons_live_in_credentials(self):
        html = _read("index.html")
        cred = _view(html, "viewCredentials")
        pipe = _view(html, "viewPipeline")
        # the profile add/delete buttons sit in Credentials, not the pipeline
        self.assertIn('id="pipeProxyNew"', cred)
        self.assertIn('id="pipeProxyDel"', cred)
        self.assertNotIn('id="pipeProxyNew"', pipe)
        self.assertNotIn('id="pipeProxyDel"', pipe)

    def test_css_ships_the_house_layer(self):
        css = _read("styles.css")
        # the invented vocabulary is gone
        for gone in (".run-console", ".work-grid", ".work-side", ".work-log"):
            self.assertNotIn(gone + "{", css, gone)
        # the retired side-rail shell is gone too
        self.assertNotIn(".cred-shell{", css)
        self.assertNotIn("#credRail::before", css)
        # house rules the pages now reuse: a top switcher, side gutters
        self.assertIn(".cred-toolbar{", css)
        self.assertIn(".cred-panels{min-width:0;margin:var(--sp-4) 34px 0}", css)
        self.assertIn(".cred-savebar{margin:var(--sp-4) 34px", css)
        self.assertIn("#viewPipeline > .panel", css)

    def test_active_sidebar_state_has_no_glow(self):
        css = _read("styles.css")
        active = css.split(".side-btn.active{", 1)[1].split("}", 1)[0]
        self.assertNotIn("box-shadow", active, "the active tab must not glow")
        # dark text on the light gradient (white-on-white is unreadable)
        self.assertIn("color:#14161c", active)

    def test_app_js_contract_ids_survive(self):
        """Every id app.js reaches with $('id') must exist in index.html —
        except ids app.js itself renders into the DOM (it emits id="x" in a
        template literal), which are legitimately absent from the static file."""
        js = _read("app.js")
        html = _read("index.html")
        bound = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", js))
        js_created = set(re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", js))
        missing = sorted(i for i in bound
                         if i not in js_created and 'id="%s"' % i not in html)
        self.assertEqual(missing, [], "ids app.js binds but html lacks: %s" % missing)


if __name__ == "__main__":
    unittest.main()
