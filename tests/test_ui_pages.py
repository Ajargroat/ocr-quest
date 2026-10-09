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
        for need in ('id="psTotal"',):
            self.assertIn(need, view, need)

    def test_period_stats_single_set_and_24h_default(self):
        """One stats set only: the five duplicate st* cells are gone and the
        default chip is 24h (markup + JS agree)."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        for kept in ('id="psTotal"',):
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

    def test_connection_pill_is_removed(self):
        """Rework item 10: the connection pill (signal bars + ping readout)
        is gone — no markup and no ping/provider-check wiring in the JS."""
        html = _read("index.html")
        js = _read("app.js")
        for gone in ('id="conn"', 'id="connBars"', 'id="connPing"',
                     'id="connText"'):
            self.assertNotIn(gone, html, gone)
        for gone in ("pingProvider", "connBars", "connPing", "setConn",
                     "paintConn"):
            self.assertNotIn(gone, js, gone)

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
        self.assertEqual(rail.count('class="rail-btn'), 4, "4 services")
        for svc in ("ocr", "revision", "database", "proxy"):
            self.assertIn('data-cred="%s"' % svc, rail, svc)

    def test_one_detail_pane_per_service(self):
        html = _read("index.html")
        for svc in ("ocr", "revision", "database", "proxy"):
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
        self.assertIn('id="usagePeriod"', html)
        self.assertIn('id="usageYMode"', html)
        self.assertIn('<option value="calls">', html)
        self.assertIn('<option value="tokens">', html)
        # the renderer consumes the server-side chart and both new controls
        self.assertIn("function usagePlot(", js)
        self.assertIn("usageGroup", js)
        self.assertIn("d.chart", js)
        self.assertIn(".usage-legend{", css)

    def test_profile_buttons_live_in_credentials(self):
        html = _read("index.html")
        cred = _view(html, "viewCredentials")
        pipe = _view(html, "viewPipeline")
        # the profile ADD button sits in Credentials, not the pipeline
        self.assertIn('id="pipeProxyNew"', cred)
        self.assertNotIn('id="pipeProxyNew"', pipe)

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
        self.assertIn(".cred-head{", css)
        self.assertNotIn(".cred-savebar{", css)
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


class PeriodStatsDbTests(unittest.TestCase):
    def test_all_five_cards_are_db_sourced(self):
        """Item 2.1: five DB-sourced cards (total/question/answer files +
        questions + answers); Succeeded/Errors and the live rail are gone."""
        html = _read("index.html")
        js = _read("app.js")
        view = _view(html, "viewPipeline")
        for cid in ("psTotal", "psQFiles", "psAFiles", "psQuestions", "psAnswers"):
            self.assertIn('id="%s"' % cid, view, cid)
        self.assertNotIn('data-live="rail"', view)
        self.assertNotIn('data-live="db"', view)
        self.assertNotIn('id="psSucceeded"', view)
        self.assertNotIn('id="psErrors"', view)
        # statsLoad is the only writer of the cards; statsPaint is caption-only
        self.assertNotIn("set('psSucceeded'", js)
        self.assertNotIn("set('psErrors'", js)
        load = js.split("async function statsLoad(period){", 1)[1].split("\n}\n", 1)[0]
        for cid in ("psTotal", "psQFiles", "psAFiles", "psQuestions", "psAnswers"):
            self.assertIn("set('%s'" % cid, load, cid)


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
    def test_add_opens_the_picker(self):
        """Item 2.2: #upAdd (now "ADD") opens the file picker; selection still
        enqueues straight away."""
        js = _read("app.js")
        html = _read("index.html")
        self.assertIn("async function upQueue(){", js)
        self.assertIn("$('upAdd').onclick = () => $('upFiles').click();", js)
        self.assertNotIn("$('upAdd').onclick = upQueue;", js)
        onchange = js.split("$('upFiles').onchange", 1)[1].split("\n", 1)[0]
        self.assertIn("upQueue", onchange)
        self.assertNotIn("file(s) selected", js)
        self.assertIn(">ADD<", html)


class UsageTabTests(unittest.TestCase):
    def test_usage_table_shows_delay_in_seconds(self):
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn('<select class="cred-in" id="usageGroup">', html)
        self.assertIn("Delay (seconds)", js)
        self.assertNotIn("<th>ms</th>", js)
        self.assertIn(".usage-table th.num{text-align:right", css)


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
    def test_pipeline_mode_is_a_select(self):
        """Item 1.5: Run mode is a dropdown like its siblings; one-by-one is
        the default (parallel is opt-in)."""
        html = _read("index.html")
        view = _view(html, "viewPipeline")
        self.assertIn('id="pipeMode"', view)
        self.assertIn('<select id="pipeMode" class="cred-in">', view)
        self.assertIn('<option value="one">', view)
        self.assertIn('<option value="parallel">', view)
        self.assertNotIn('data-mode="one"', view)
        js = _read("app.js")
        self.assertIn("PipeRun", js)
        self.assertIn("enabled_keys", js)
        self.assertIn("PipeRun.mode = pipeModeEl.value", js)


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

    def test_toggle_persists_to_localstorage(self):
        """Item 1.2: the per-key choice is remembered per-browser in
        localStorage (non-secret UI state) — never in .env."""
        js = _read("app.js")
        self.assertIn("LANE_OFF_KEY", js)
        self.assertIn("localStorage.getItem(LANE_OFF_KEY", js)
        self.assertIn("localStorage.setItem(LANE_OFF_KEY", js)
        self.assertIn("laneOffSave(set)", js)


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

    def test_lane_hub_icon_follows_the_stage(self):
        """Item 1.3: the lane's .hub icon IS the current stage icon (key when
        idle, then the stage glyph); the old .lane-rail strip is gone."""
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn("fi(laneHubIcon(", js)
        self.assertIn("const LANE_HUB_ICON", js)
        for icon in ("cloud-arrow-up", "eye", "database"):
            self.assertIn(icon, js, icon)
        self.assertNotIn('class="lane-rail"', js)
        self.assertNotIn(".lane-rail{", css)
        self.assertNotIn(".lane-step{", css)

    def test_lane_ring_and_eye_colour(self):
        """Item 1.4: the hub icon's colour carries the state, and a countdown
        ring circles it during a retry — the text chip is gone."""
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn('class="ring"', js)
        self.assertNotIn("laneChipText", js)
        self.assertIn('.lane[data-state="wait"] .hub .ring', css)
        self.assertIn('.lane[data-state="done"] .hub .fa{color:var(--green)}', css)
        self.assertIn('.lane[data-state="wait"] .hub .fa{color:var(--amber)}', css)
        self.assertIn('.lane[data-state="failed"] .hub .fa{color:var(--red)}', css)

    def test_lane_file_sits_beside_the_name(self):
        """Item 1.6: .lane-file renders next to .lane-name, with its own gap."""
        js = _read("app.js")
        css = _read("styles.css")
        seg = js.split("function lanesRender(){", 1)[1].split("box.querySelectorAll", 1)[0]
        self.assertLess(seg.index('class="lane-name"'), seg.index('class="lane-file"'))
        self.assertIn(".lane-file{", css)
        self.assertIn("margin-left:var(--sp-2)", css)


class SidebarLayoutTests(unittest.TestCase):
    def test_sidebar_header_removed_and_footer_added(self):
        """The top <header> is gone; the theme toggle + connection pill live
        in a sidebar footer; the sidebar's top slot is the brand, not a
        collapse control (Q1/Q2 — the sidebar no longer collapses)."""
        html = _read("index.html")
        self.assertNotIn("<header", html)
        side = html.split('id="side"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn('id="sideFooter"', side)
        self.assertNotIn('id="conn"', side)
        self.assertNotIn('id="themeToggle"', side)
        self.assertIn('class="side-brand"', side)
        self.assertNotIn('id="sideToggle"', html)


class UsageDelayOptionsTests(unittest.TestCase):
    def test_usage_y_mode_has_avg_delay_option(self):
        """Item 4.1: the Y control is a dropdown; it offers the average-delay
        metric but not the useless cumulative "Delay (s)"."""
        html = _read("index.html")
        self.assertIn('<option value="avg_sec">', html)
        self.assertNotIn('data-ymode="sec"', html)
        self.assertNotIn('data-ymode="ms"', html)


class SidebarIdentityTests(unittest.TestCase):
    def test_sidebar_brand_replaces_the_collapse_control(self):
        """Q1/Q2: the sidebar no longer collapses — the brand takes the top
        slot; no #sideToggle / .side-collapse / body.side-min survives."""
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        for gone in ("sideToggle", "side-collapse", "side-min"):
            self.assertNotIn(gone, html, gone)
            self.assertNotIn(gone, js, gone)
            self.assertNotIn(gone, css, gone)
        self.assertIn('class="side-brand"', html)
        self.assertIn("fa-qrcode", html)
        self.assertIn("OCR hub", html)


class IconPolishTests(unittest.TestCase):
    def test_icon_hover_has_no_3d_and_borders_are_circular(self):
        """Item 8/12: icon hover is a size bump only (no 3-D rotation), every
        bordered icon is round, and the flat-green toggle loses glow/gradient."""
        css = _read("styles.css")
        # hover is a size bump only — the rotate hover rules are gone
        self.assertIn(".icon-btn:hover svg,.icon-btn:hover .fa{transform:scale(1.06)}", css)
        self.assertIn(".ghost-btn:hover svg,.ghost-btn:hover .fa{transform:scale(1.06)}", css)
        self.assertNotIn("transform:rotate(-7deg)", css)
        self.assertNotIn("transform:rotate(-12deg)", css)
        # the spinner keyframes are load-bearing — they must survive
        self.assertIn("@keyframes rot{", css)
        self.assertIn(".icon-btn{position:relative;display:grid;place-items:center;"
                      "width:42px;height:42px;border:1px solid var(--border);"
                      "border-radius:50%;", css)
        checked = css.split(".sw input:checked + .tr{", 1)[1].split("}", 1)[0]
        self.assertIn("background:var(--green)", checked)
        self.assertNotIn("gradient", checked)
        self.assertIn("box-shadow:none", checked)

    def test_cred_row_icons_are_flask_trash_and_updown(self):
        """Item 3: close/delete rows wear a trash can, the health-check is a
        flask, the drag grip is up/down arrows — the QBank reject row keeps
        its x-mark (it is not a close/delete control)."""
        js = _read("app.js")
        self.assertIn("fi('flask')", js)
        self.assertEqual(js.count("fi('trash')"), 3)
        self.assertIn("fi('up-down')", js)
        self.assertIn("fi('xmark')", js)


class CredentialLayoutTests(unittest.TestCase):
    def test_proxy_is_a_rail_tab_and_save_sits_on_its_header(self):
        """Q3: Proxy joins the rail as a 4th service; Save lives on the proxy
        pane's header and the old savebar is gone."""
        html = _read("index.html")
        js = _read("app.js")
        rail = html.split('id="credRail"', 1)[1].split("</div>", 1)[0]
        self.assertIn('data-cred="proxy"', rail)
        pane = _pane(html, "proxy")
        self.assertIn('id="pipeProxyNew"', pane)      # the ADD button (item 6/7)
        self.assertIn('id="credProxyList"', pane)
        self.assertIn('class="cred-head"', pane)
        self.assertIn('id="credSaveBtn"', html)       # Save lives in the toolbar
        self.assertIn("'proxy'", js)
        self.assertNotIn("cred-savebar", html)

    def test_mbar_ramps_green_to_red_over_20(self):
        """Item 5: the model usage meter is bigger, always shows a track, and
        ramps green -> red over the fixed 1–20 range."""
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn("used / 20", js)
        self.assertIn("height:30px", css)
        self.assertIn("min-width:104px", css)
        self.assertNotIn(".mbar.m-untested i{display:none}", css)


class UsagePlotTests(unittest.TestCase):
    def test_usage_controls_dropdown_and_seconds(self):
        """Items 9/10/11: the period is a 24h-default dropdown, the y-modes
        are seconds (not ms), the plot animates and shows hover values, and
        the old `usage-max` tag is gone."""
        html = _read("index.html")
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn('id="usagePeriod"', html)
        self.assertIn('<select class="cred-in" id="usageYMode">', html)
        self.assertIn('<option value="avg_sec">', html)
        self.assertNotIn('data-ymode=', html)
        self.assertIn("period: '24h'", js)
        self.assertIn("usage-tip", js)
        self.assertNotIn(".usage-max{", css)
        self.assertIn(".usage-plot rect{transition:", css)


class LoadingStateTests(unittest.TestCase):
    def test_every_data_view_has_a_loading_overlay(self):
        """Item 13: one loading treatment, wired on the first lazy load of
        every data-fetching view."""
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn("function viewLoading(", js)
        self.assertIn(".load-shell", js)
        self.assertIn("@keyframes loadWave{", css)
        for view in ("viewUsage", "viewReview", "viewRevision",
                     "viewDatabase", "viewCredentials", "viewPipeline"):
            self.assertIn("viewLoading('%s', true)" % view, js, view)


class HintCopyTests(unittest.TestCase):
    def test_descriptive_hints_are_gone_but_live_counters_stay(self):
        """Item 14: the descriptive hint texts (and their writers) are gone;
        the live count labels survive."""
        html = _read("index.html")
        js = _read("app.js")
        for gone in ("dbBkStatus", "credModelsHint", "upQueueHint", "dbModalHint"):
            self.assertNotIn(gone, html, gone)
            self.assertNotIn(gone, js, gone)
        for kept in ('id="logCount"', 'id="fileCount"', 'id="revLogCount"'):
            self.assertIn(kept, html, kept)
        self.assertIn(".hint{", _read("styles.css"))


if __name__ == "__main__":
    unittest.main()
