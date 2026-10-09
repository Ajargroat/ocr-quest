"""Persian font baseline pins (rework cycle 3, Fix 3).

The cycle-2 pass never removed the Vazirmatn family or its CDN link; the
regression was tokenised `font-size`/`line-height` on Vazirmatn selectors
(e.g. .qtext 14.5px/2.2 -> 20px/1.75), which made Persian text cramped and
broken. These pins restore the pre-task baseline and hold it.
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


class FontBaselineTests(unittest.TestCase):
    def test_vazirmatn_cdn_link_present(self):
        html = _read("index.html")
        self.assertIn('rel="preconnect"', html)
        self.assertIn("family=Vazirmatn", html)
        self.assertIn("fontawesome-free@6.5.2", html)

    def test_persian_type_baseline(self):
        """The exact pre-task sizes/leadings Fix 3 restored (PROPOSE §3 table)."""
        css = _read("styles.css")
        for sel, frag in (
            (".qtext", "font-size:14.5px;line-height:2.2"),
            (".opts li", "font-size:13.5px;line-height:2"),
            (".answer-box", "font-size:13px;line-height:2.1"),
            (".raw-box", "font-size:12.5px;line-height:2"),
            (".log", "font-size:12.5px;line-height:1.9"),
        ):
            body = _rule(css, sel)
            self.assertIsNotNone(body, sel)
            self.assertIn(frag, body, sel)

    def test_persian_rules_still_declare_the_family(self):
        css = _read("styles.css")
        for sel in (".qtext", ".opts li", ".answer-box", ".raw-box"):
            body = _rule(css, sel)
            self.assertIn("'Vazirmatn'", body, sel)


if __name__ == "__main__":
    unittest.main()
