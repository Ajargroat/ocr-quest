"""Static pins for the session-only light/dark switch (TASK acceptance 1)."""
import os
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


class ThemeSwitchTests(unittest.TestCase):
    def test_theme_toggle_is_removed(self):
        """Rework item 11: the theme toggle (and its whole sidebar footer) is
        gone — no markup, no renderer, no per-browser theme state."""
        for name in ("index.html", "styles.css"):
            src = _read(name)
            self.assertNotIn("localStorage", src, name)
            self.assertNotIn("sessionStorage", src, name)
        self.assertNotIn('id="themeToggle"', _read("index.html"))
        self.assertNotIn("themeToggle", _read("app.js"))

    def test_light_theme_overrides_the_palette(self):
        css = _read("styles.css")
        self.assertIn(':root[data-theme="light"]', css)
        self.assertNotIn("prefers-color-scheme", css)   # dark stays the default


if __name__ == "__main__":
    unittest.main()
