"""The dashboard shell and its scripts must never be served from a stale cache.

`ui/` has no build step and therefore no content hash in the asset URLs. Before
this, a browser that had loaded the dashboard once kept running the OLD
`inspect.js` after a server restart, so a markup change looked like it had never
landed -- the new elements existed in the served HTML but not in the DOM.

Both the shell (`/`) and `/static/*` now say `no-cache`, which still allows a
cheap conditional 304 but forbids reusing a response without asking.

Run: python tests/test_ui_shell_cache.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(name):
    with open(os.path.join(ROOT, "ui", name), encoding="utf-8") as fh:
        return fh.read()


class ShellCacheTests(unittest.TestCase):
    def setUp(self):
        import main
        from starlette.testclient import TestClient
        self.client = TestClient(main.app)

    def test_the_shell_forbids_reusing_a_cached_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-cache", r.headers.get("cache-control", ""))

    def test_every_static_script_revalidates(self):
        for asset in ("inspect.js", "app.js", "styles.css"):
            r = self.client.get("/static/" + asset)
            self.assertEqual(r.status_code, 200, asset)
            self.assertIn("no-cache", r.headers.get("cache-control", ""), asset)

    def test_the_shell_loads_the_current_inspect_script(self):
        """The markup the new JS expects and the script itself ship together."""
        html = _read("index.html")
        js = _read("inspect.js")
        doc = _read("inspect_doc.js")
        self.assertIn("static/inspect.js", html)
        self.assertIn("static/inspect_doc.js", html)
        # every element the editor reaches for exists in the shell
        for element in ("insDocOpts", "insOptAdd", "insBboxBar", "insBboxYmin",
                        "insCropImg", "insDifficulty", "insZoomIn", "insEdit",
                        "insTags", "insDocEditor"):
            self.assertIn('id="%s"' % element, html, element)
            self.assertTrue("'%s'" % element in js or "'%s'" % element in doc, element)


if __name__ == "__main__":
    unittest.main(verbosity=2)
