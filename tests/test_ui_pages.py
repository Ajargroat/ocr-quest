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


def _pane(html, cred):
    """The markup of one credentials pane, up to the next pane section."""
    body = html.split('class="panel cred-card" data-cred="%s"' % cred, 1)[1]
    nxt = body.find('<section class="panel cred-card"')
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
        for need in ('id="psSucceeded"', 'id="psCaption"'):
            self.assertIn(need, view, need)

    def test_period_stats_single_set_and_24h_default(self):
        """One stats set only: the five duplicate st* cells are gone and the
        default chip is 24h (markup + JS agree)."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        for kept in ('id="psSucceeded"', 'id="psCaption"'):
            self.assertIn(kept, view, kept)
        for gone in ('id="stTotal"', 'id="stProcessed"', 'id="stQuestions"',
                     'id="stAnswers"', 'id="stErrors"'):
            self.assertNotIn(gone, view, gone)
        self.assertIn('class="ps-chip active" data-period="24h"', view)
        self.assertNotIn('class="ps-chip active" data-period="all"', view)
        js = _read("app.js")
        for gone in ("$('stTotal')", "$('stProcessed')", "$('stQuestions')",
                     "$('stAnswers')", "$('stErrors')"):
            self.assertNotIn(gone, js, gone)
        self.assertIn("period: '24h'", js)
        self.assertIn("statsLoad('24h')", js)

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
        for need in ('id="runBtn"', 'id="pipeProvSel"', 'id="pipeProxySel"'):
            self.assertIn(need, bar, need)
        for gone in ('id="pipeProvHint"', 'id="pipeProxyHint"'):
            self.assertNotIn(gone, bar, gone)
        js = _read("app.js")
        self.assertNotIn('pipeProvHint', js)
        self.assertNotIn('pipeProxyHint', js)
        # the profile buttons belong to Credentials, not the pipeline toolbar
        self.assertNotIn('id="pipeProxyNew"', bar)
        self.assertNotIn('id="pipeProxyDel"', bar)

    def test_upload_panel_is_full_width_with_dropdowns(self):
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        panel = view.split('class="panel upload-panel"', 1)[1].split("</section>", 1)[0]
        for need in ('id="upList"', 'id="upFiles"', 'id="upAdd"',
                     'id="upEmpty"', 'id="upSubject"', 'id="upGrade"',
                     'id="upTopic"', 'id="upType"'):
            self.assertIn(need, panel, need)
        # the destination is picked from themed dropdowns, never typed
        self.assertNotIn('id="upPath"', panel)
        self.assertNotIn("<select", panel)

    def test_upload_picker_is_an_empty_state_that_disappears(self):
        """The file-add control is an empty-state: it takes one or many PDFs
        and is hidden as soon as the queue has rows."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertIn('id="upEmpty"', view)
        self.assertRegex(view, r'id="upFiles"[^>]*multiple')
        js = _read("app.js")
        self.assertIn("$('upEmpty')", js)
        self.assertIn("empty.hidden", js)

    def test_conn_is_a_signal_bars_indicator(self):
        """The connection pill is a signal-bars widget: three bars, a ping
        readout and a provider tooltip, fed by the provider check fetch."""
        html = _read("index.html")
        m = re.search(r'<div class="conn" id="conn"[^>]*>(.*?)</div>',
                      html, re.S)
        self.assertIsNotNone(m, "conn pill not found")
        pill = m.group(1)
        self.assertIn('id="connBars"', pill)
        bars = re.search(r'id="connBars"[^>]*>(.*?)</span>', pill, re.S)
        self.assertIsNotNone(bars, "connBars wrapper not found")
        self.assertEqual(bars.group(1).count("<b>"), 3, "want 3 signal bars")
        self.assertIn('id="connPing"', pill)
        self.assertIn('id="connText"', pill)
        self.assertRegex(m.group(0), r"title=")
        js = _read("app.js")
        for need in ("pingProvider", "connBars", "connPing",
                     "'/api/credentials/check/provider'"):
            self.assertIn(need, js, need)

    def test_dd_caption_swap_is_scoped_to_the_upload_form(self):
        """Only the four up-form dropdowns trade their caption for the chosen
        value; the revision rv* hosts keep their static Persian captions."""
        js = _read("app.js")
        m = re.search(r"UPFORM_DDS\s*=\s*\[([^\]]*)\]", js)
        self.assertIsNotNone(m, "UPFORM_DDS list missing")
        hosts = re.findall(r"'([^']+)'", m.group(1))
        self.assertEqual(sorted(hosts),
                         sorted(["upSubject", "upGrade", "upTopic", "upType"]))
        for h in hosts:
            self.assertNotIn("rv", h)
        self.assertIn("cap.textContent", js)

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
        self.assertEqual(rail.count('class="rail-btn'), 3, "3 services")
        for svc in ("ocr", "revision", "database"):
            self.assertIn('data-cred="%s"' % svc, rail, svc)

    def test_one_detail_pane_per_service(self):
        html = _read("index.html")
        for svc in ("ocr", "revision", "database"):
            self.assertIn('class="panel cred-card" data-cred="%s"' % svc, html, svc)

    def test_no_ring_or_live_routing_surface(self):
        """Q4: the model rings and the Live-routing panel are dropped — no
        host, no renderer, no rail button survives."""
        html = _read("index.html")
        js = _read("app.js")
        for gone in ("ringGemini", "ringRouter", 'id="credRoute"',
                     'id="credEvents"', "Live routing", "#usageGemini",
                     "#usageRouter", "usageGemini", "usageRouter"):
            self.assertNotIn(gone, html, gone)
        for gone in ("credRing(", "credRenderSide", "gemModels(", "gemActiveModel("):
            self.assertNotIn(gone, js, gone)

    def test_ocr_pane_holds_two_credential_sections(self):
        """The OCR pane carries the Gemini key rows AND a 9router row inline,
        each behind its own enable toggle; the pool modal keeps only the ladder."""
        html = _read("index.html")
        pane = _pane(html, "ocr")
        for need in ('id="credKeys"', 'id="credKeys9r"', 'id="swGemini"',
                     'id="sw9router"'):
            self.assertIn(need, pane, need)
        pool = html.split('data-form="pool"', 1)[1].split('data-form="supabase"', 1)[0]
        self.assertIn('id="credLadder"', pool)
        self.assertNotIn('id="credKeys"', pool)

    def test_gemini_key_row_shows_name_date_health_and_bars(self):
        js = _read("app.js")
        self.assertIn('class="cred-date"', js)
        self.assertIn("added:k.added", js)
        self.assertIn("${used}/${lim}", js)
        self.assertIn('cred-btn chk', js)      # the ♥ health-check button

    def test_database_pane_holds_supabase_and_connection_cards(self):
        html = _read("index.html")
        pane = _pane(html, "database")
        for need in ('id="supaSummary"', 'id="dbConns"', 'id="dbConnAdd"',
                     'id="swSupabase"'):
            self.assertIn(need, pane, need)
        self.assertNotIn('data-cred="supabase"', html)
        self.assertNotIn('data-cred="postgres"', html)

    def test_usage_is_a_top_level_tab_with_a_plot(self):
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        side = html.split('id="side"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('data-view="usage"', side)
        self.assertIn('id="viewUsage"', html)
        self.assertIn("function usagePlot(", js)
        self.assertIn(".usage-plot{", css)
        self.assertNotIn("usageGemini", html)
        self.assertNotIn("usageRouter", html)

    def test_usage_tab_has_group_and_y_mode_controls(self):
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn('id="usageGroup"', html)
        self.assertIn('id="usageYMode"', html)
        self.assertIn('data-ymode="calls"', html)
        self.assertIn('data-ymode="tokens"', html)
        # the renderer consumes the server-side chart and both new controls
        self.assertIn("function usagePlot(", js)
        self.assertIn("usageGroup", js)
        self.assertIn("d.chart", js)
        self.assertIn(".usage-legend{", css)

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


class PeriodStatsLiveRailTests(unittest.TestCase):
    def test_period_stats_file_cards_read_the_live_rail(self):
        html = _read("index.html")
        js = _read("app.js")
        view = _view(html, "viewPipeline")
        # three file cells declare the live rail source...
        rail = re.findall(r'data-live="rail"><span[^>]*id="(psSucceeded|psErrors|psTotal)"', view)
        self.assertEqual(sorted(rail), ["psErrors", "psSucceeded", "psTotal"])
        db = re.findall(r'data-live="db"><span[^>]*id="(psQuestions|psAnswers)"', view)
        self.assertEqual(sorted(db), ["psAnswers", "psQuestions"])
        # ...and the DB path no longer paints them (statsPaint does)
        self.assertIn("function statsPaint()", js)
        load = js.split("async function statsLoad(period){", 1)[1].split("\n}\n", 1)[0]
        for gone in ("set('psSucceeded'", "set('psErrors'", "set('psTotal'"):
            self.assertNotIn(gone, load, gone)
        self.assertIn("set('psQuestions'", load)
        self.assertIn("set('psAnswers'", load)


class StageNowDetailTests(unittest.TestCase):
    def test_stage_now_detail_tracks_the_live_file(self):
        js = _read("app.js")
        self.assertIn("by_id: {}", js)
        self.assertIn("RAIL.by_id = {}", js)
        case = js.split("case 'file': {", 1)[1].split("case 'snapshot':", 1)[0]
        self.assertIn("RAIL.by_id[ev.id]", case)
        self.assertIn("const meta = RAIL.by_id[ev.id] || {};", case)
        self.assertIn("name: ev.name || meta.name || ''", case)
        self.assertNotIn("RAIL.cur && RAIL.cur.name", case)


class UploadEnqueueTests(unittest.TestCase):
    def test_upload_selection_enqueues_directly(self):
        js = _read("app.js")
        self.assertIn("async function upQueue(){", js)
        self.assertIn("$('upAdd').onclick = upQueue;", js)
        onchange = js.split("$('upFiles').onchange", 1)[1].split("\n", 1)[0]
        self.assertIn("upQueue", onchange)
        self.assertNotIn("file(s) selected", js)


class UsageTabTests(unittest.TestCase):
    def test_usage_table_shows_delay_in_seconds(self):
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn('<select class="cred-in" id="usageGroup">', html)
        self.assertIn("Delay (seconds)", js)
        self.assertNotIn("<th>ms</th>", js)
        self.assertIn(".usage-table th.num{text-align:right", css)
        self.assertIn("t.errors || 0", js)


class PanelBackgroundTests(unittest.TestCase):
    def test_panels_carry_no_filled_background(self):
        css = _read("styles.css")
        for sel in (".panel{", ".panel,.cred-card{", ".cred-section{"):
            body = css.split(sel, 1)[1].split("}", 1)[0]
            self.assertIn("background:transparent", body, sel)
        # scope guard: the content cards keep their fill
        qcard = css.split(".qcard{", 1)[1].split("}", 1)[0]
        self.assertIn("background:var(--panel)", qcard)


class ParallelModeTests(unittest.TestCase):
    def test_pipeline_mode_toggle_present(self):
        """The pipeline toolbar carries a two-way Run mode control; the
        one-by-one button is active by default (parallel is opt-in)."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertIn('id="pipeMode"', view)
        self.assertIn('data-mode="one"', view)
        self.assertIn('data-mode="parallel"', view)
        seg = view.split('id="pipeMode"', 1)[1].split("</div>", 1)[0]
        self.assertIn('data-mode="one" class="active"', seg)
        js = _read("app.js")
        self.assertIn("PipeRun", js)
        self.assertIn("enabled_keys", js)


class CredKeyToggleTests(unittest.TestCase):
    def test_per_key_toggle_lives_on_the_lane_surface(self):
        """Cycle 3: the session-only on/off toggle wears the house `.sw`+`.tr`
        switch (not the old `.cred-btn keyon` button); flipping it never writes
        .env, and the Run mask contract is unchanged."""
        js = _read("app.js")
        self.assertEqual(js.count('data-keyon="${i}"'), 1)   # one construction site
        self.assertEqual(js.count("querySelector('.keyon')"), 0)  # gone from credRowEl
        self.assertIn("r.on = box.checked", js)
        self.assertIn("function activeKeyMask", js)
        handler = js.split("data-keyon]').forEach(box => {", 1)[1].split("};", 1)[0]
        self.assertNotIn("/api/credentials", handler)
        self.assertIn("loadCredentials()", js.split("function pipeModePaint", 1)[1])
        # The house switch, and no trace of the old button:
        self.assertNotIn("cred-btn keyon", js)
        self.assertIn('class="sw lane-sw"', js)
        self.assertIn('<span class="tr" aria-hidden="true"></span>', js)
        self.assertIn('type="checkbox" data-keyon=', js)
        css = _read("styles.css")
        self.assertNotIn(".cred-btn.keyon", css)
        self.assertIn(".lane .lane-sw", css)


class LaneSurfaceTests(unittest.TestCase):
    def test_pipeline_view_carries_a_lane_surface_swapped_by_mode(self):
        """The pipeline view carries the per-key lane surface; Parallel mode
        swaps it in for the 5-stage rail and seeds it from the hub snapshot."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertIn('id="lanes"', view)
        self.assertIn('id="laneList"', view)
        self.assertIn('id="laneNow"', view)
        js = _read("app.js")
        self.assertIn("case 'lane'", js)
        self.assertIn("case 'round'", js)
        self.assertIn("ev.lanes", js)
        paint = js.split("function pipeModePaint()", 1)[1].split("}", 1)[0]
        self.assertIn("$('stages').hidden", paint)
        self.assertIn("$('lanes').hidden", paint)

    def test_lane_chip_reads_the_live_phase(self):
        """Cycle 3: the lane chip renders the server-computed `phase`
        (uploading / ocr / waiting Ns / importing / imported) and the `wait`
        state has a house-colour accent."""
        js = _read("app.js")
        self.assertIn("s.phase === 'uploading'", js)
        self.assertIn("state === 'wait'", js)
        self.assertIn("s.phase === 'imported'", js)
        self.assertIn('.lane[data-state="wait"]', _read("styles.css"))


class SidebarLayoutTests(unittest.TestCase):
    def test_sidebar_header_removed_and_footer_added(self):
        """The top <header> is gone; the theme toggle + connection pill live
        in a sidebar footer, and the collapse control is the first button."""
        html = _read("index.html")
        self.assertNotIn("<header", html)
        side = html.split('id="side"', 1)[1].split("</nav>", 1)[0]
        self.assertIn('id="sideFooter"', side)
        self.assertIn('id="conn"', side)
        self.assertIn('id="themeToggle"', side)
        first_btn = side.split("<button", 1)[1].split(">", 1)[0]
        self.assertIn('id="sideToggle"', first_btn)


class UsageDelayOptionsTests(unittest.TestCase):
    def test_usage_y_mode_has_delay_options(self):
        """Q5: beside Calls/Tokens the Y toggle offers both a cumulative and
        an average delay metric."""
        html = _read("index.html")
        self.assertIn('data-ymode="ms"', html)
        self.assertIn('data-ymode="avg_ms"', html)
        js = _read("app.js")
        self.assertIn("s/call", js)


if __name__ == "__main__":
    unittest.main()
