"""9router radial ring pins (rework cycle 2, Fix 2)."""
import os
import re
import unittest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


class RingRebuildTests(unittest.TestCase):
    def test_credring_is_radial_with_node_chrome(self):
        js = _read("app.js")
        css = _read("styles.css")
        self.assertIn("function credRing(", js)
        self.assertIn("pane-core", js)
        self.assertIn("pane-card", js)
        self.assertIn("Math.cos(", js)
        self.assertNotIn("pane-spine", js)
        self.assertNotIn("pane-port", js)
        self.assertIn(".pane-edge.on", css)
        self.assertIn(".pane-card.on .pane-node", css)

    def test_pane_rules_use_tokens_only(self):
        css = _read("styles.css")
        bodies = re.findall(r"\.pane-[\w-]+\s*\{([^}]*)\}", css)
        self.assertTrue(bodies)
        for body in bodies:
            self.assertNotIn("#", body, body)
        self.assertIn(".pane-ping", css)
        m = re.search(r"prefers-reduced-motion[^{]*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("pane-ping", m.group(1))


if __name__ == "__main__":
    unittest.main()
