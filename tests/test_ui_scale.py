"""Whole-sheet spacing/type scale pins.

Cycle 2 pinned every named rule to a scale token; cycle 3 restyles the
dashboard to the strivegymclub.com reference (Fix 1) and restores the
pre-task Persian font-size/line-height literals on Vazirmatn rules (Fix 3).
So scale tokens are pinned where they belong (structure), the Persian
baselines are pinned as literals in test_ui_font.py, and the invariant that
keeps both honest lives here: every `font-size:<digit>` literal must sit on
a rule that names a text family (Vazirmatn or JetBrains Mono). A bare
literal on a structural rule is the cycle-2 regression.
"""
import os
import re
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _rule(css, sel):
    """Body of the one-line rule whose selector is exactly `sel`."""
    pat = re.compile(r"(?m)^\s*" + re.escape(sel) + r"\s*\{([^}]*)\}")
    m = pat.search(css)
    return m.group(1) if m else None


class ScaleTokenTests(unittest.TestCase):
    def test_scale_tokens_defined_in_root(self):
        css = _read("styles.css")
        marker = ':root[data-theme="light"]'
        self.assertIn(marker, css, "light block missing")
        root = css.split(marker)[0]
        # slice out only the light block's own body, not everything after it
        # (uses like var(--sp-1) downstream would false-positive on a substring)
        light_m = re.search(re.escape(marker) + r"\{([^}]*)\}", css)
        self.assertIsNotNone(light_m, "light block body not parseable")
        light_body = light_m.group(1)
        for tok in ("--sp-1", "--sp-6", "--fs-1", "--fs-6", "--r-1", "--r-pill", "--ring-ico"):
            self.assertIn(tok, root, tok)
        # Only length tokens cascade to the light block (CSS custom properties
        # inherit); colours like --ring-ico/--ring-glow must be re-declared so
        # the tint stays legible on a light background.
        for tok in ("--sp-1", "--sp-6", "--fs-1", "--fs-6", "--r-1", "--r-pill"):
            self.assertNotIn(tok + ":", light_body,
                             tok + " must not be re-declared in light block")

    def test_font_size_literals_all_carry_a_text_family(self):
        """Fix 3 restores the pre-task Persian literals; the sheet may carry
        `font-size:<digit>` ONLY on a rule that names Vazirmatn or JetBrains
        Mono. A bare literal on a structural rule is the cycle-2 regression."""
        css = _read("styles.css")
        bad = [ln.strip()[:90] for ln in css.splitlines()
               if re.search(r"font-size:\d", ln)
               and "Vazirmatn" not in ln and "JetBrains" not in ln]
        self.assertEqual(bad, [], "literal font-size off a Persian/mono rule: %s" % bad)

    def test_named_structural_rules_use_scale_tokens(self):
        css = _read("styles.css")
        wants = {
            "header": "--sp-6",
            "h1": "--fs-5",
            ".panel-head": "--sp-5",
            ".usage-table": "--fs-2",
            ".ps-cell .k": "--fs-6",
        }
        for sel, tok in wants.items():
            # anchor at rule start (one rule per line in this sheet) so
            # ".log" matches neither ".logo" nor the reduced-motion media block
            body = _rule(css, sel)
            self.assertIsNotNone(body, sel)
            self.assertIn("var(%s)" % tok, body, sel)

    def test_declutter_marks_applied(self):
        html = _read("index.html")
        for gone in ('class="sub"', 'class="sep"', 'id="routerHint"', '<section class="stats">'):
            self.assertNotIn(gone, html, gone)
        for kept in ('id="upQueueHint"',):
            self.assertIn(kept, html, kept)
        self.assertNotIn('id="stTotal"', html)
        self.assertLess(html.index('id="periodStats"'), html.index('id="psSucceeded"'))
        for line in html.splitlines():
            if 'id="revNow"' in line:
                self.assertNotIn("></div>", line)
                break
        else:
            self.fail("id=revNow not found")

    def test_toolbar_shares_the_pipeline_edge(self):
        """The pipeline toolbar pads to the same 34px edge as .stages /
        .period-stats, and its context gap matches the ps-grid gap."""
        css = _read("styles.css")
        body = _rule(css, "#viewPipeline .toolbar")
        self.assertIsNotNone(body, "#viewPipeline .toolbar")
        self.assertIn("34px", body)
        ctx = _rule(css, "#viewPipeline .tb-context")
        self.assertIsNotNone(ctx, "#viewPipeline .tb-context")
        self.assertIn("gap:var(--sp-3)", ctx)

    def test_upload_form_has_room_and_placeholders(self):
        """The upload form gets its bottom margin; the file picker is a
        centred empty-state block that only shows while the queue is empty;
        the destination dropdowns wear the Gemini/proxy field look (flat
        surface, 1px border, left-aligned caption) instead of a centred pill."""
        css = _read("styles.css")
        form = _rule(css, ".up-form")
        self.assertIsNotNone(form, ".up-form")
        self.assertIn("margin-bottom:var(--sp-5)", form)
        empty = _rule(css, ".up-empty")
        self.assertIsNotNone(empty, ".up-empty")
        for need in ("align-items:center", "justify-content:center",
                     "min-height:300px"):
            self.assertIn(need, empty, need)
        pick = _rule(css, ".up-pick")
        self.assertIsNotNone(pick, ".up-pick")
        self.assertIn("flex-direction:column", pick)
        dest = _rule(css, ".up-dest .dd-btn")
        self.assertIsNotNone(dest, ".up-dest .dd-btn")
        self.assertIn("justify-content:flex-start", dest)
        self.assertIn("border:1px solid var(--border)", dest)

    def test_pipeline_panes_start_with_room(self):
        """The log / files / upload-queue panes start at ~300px instead of
        collapsing to content height."""
        css = _read("styles.css")
        for sel in ("#viewPipeline #log", "#viewPipeline #files",
                    "#viewPipeline #upList"):
            body = _rule(css, sel)
            self.assertIsNotNone(body, sel)
            self.assertIn("min-height:300px", body, sel)


if __name__ == "__main__":
    unittest.main()
