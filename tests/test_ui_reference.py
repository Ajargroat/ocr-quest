"""Minimal-theme pins (rework cycle 3, re-scoped after the verdict).

The strivegymclub.com red/gold direction was rejected ("make the theme less
neon and more simple"), and the user pointed at their own Gymapp
(css/style.css "minimal theme" block): neutral grays, off-white accent,
flat surfaces, no blur / glow / orbs. These pins hold that direction so a
later pass cannot drift back to a neon palette.
"""
import os
import re
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _rule(css, sel):
    pat = re.compile(r"(?m)^\s*" + re.escape(sel) + r"\s*\{([^}]*)\}")
    m = pat.search(css)
    return m.group(1) if m else None


class MinimalThemeTests(unittest.TestCase):
    def test_tokens_are_neutral_not_neon(self):
        css = _read("styles.css")
        root = css.split(':root[data-theme="light"]')[0]
        # neutral palette (Gymapp minimal): near-black bg, off-white accent
        for tok, val in (("--bg", "#0c0e12"), ("--panel", "#12151b"),
                         ("--text", "#e9eaee"), ("--muted", "#8a909c"),
                         ("--brand", "#e6e4df")):
            self.assertIn("%s:%s" % (tok, val), root, tok)
        # the rejected neon accents must not survive in the dark palette
        for banned in ("#C1121F", "#F2A900", "#111315"):
            self.assertNotIn(banned, root, banned)

    def test_no_neon_glow_or_blur_left_anywhere(self):
        css = _read("styles.css")
        for neon in ("34,211,238", "139,92,246", "193,18,31", "242,169,0"):
            self.assertNotIn(neon, css, neon)

    def test_hidden_wins_over_flex_panels(self):
        """The credential rail's switching was invisible because
        .panel{display:flex} beat the UA [hidden] rule — all panels showed
        at once. The override must pin [hidden] as display:none."""
        css = _read("styles.css")
        self.assertRegex(css, r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important")

    def test_surfaces_are_flat(self):
        css = _read("styles.css")
        for sel in (".panel", ".qcard", ".cred-card"):
            body = _rule(css, sel)
            self.assertIsNotNone(body, sel)

    def test_light_theme_buttons_stay_legible(self):
        """`--brand` is near-black in the light theme, so every rule that
        paints text on it must flip to the off-white idiom (like
        `.side-btn.active`). The run button also becomes a circle."""
        css = _read("styles.css")
        for sel in (':root[data-theme="light"] .btn.primary',
                    ':root[data-theme="light"] .stage.active .hub',
                    ':root[data-theme="light"] .stage .bubble',
                    ':root[data-theme="light"] .ps-chip.active'):
            body = _rule(css, sel)
            self.assertIsNotNone(body, sel)
            self.assertIn("#f4f5f7", body, sel)
        dd = _rule(css, ':root[data-theme="light"] .dd.has-value .dd-btn')
        self.assertIsNotNone(dd, "light dd selection colour")
        self.assertIn("color:var(--text)", dd)
        run = _rule(css, "#runBtn")
        self.assertIsNotNone(run, "#runBtn")
        self.assertNotIn("border-radius:50%", run)   # item 8/16: a text button, not a circle


if __name__ == "__main__":
    unittest.main()
