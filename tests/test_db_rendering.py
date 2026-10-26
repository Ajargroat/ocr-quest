"""Database-tab rendering + /api/media route pins for workstream C4/C3b.

Two halves:

  MediaRouteTests  GET /api/media proxies a bucket object whose name contains
                   slashes (studio crops: ``studio/<job>/<key>.jpg``) and
                   rejects traversal before any outbound call; the legacy
                   single-segment route keeps serving page images.
  DbGridPins       the grid formats a table/formula cell through `mdHtml`
                   and typesets it, and the bucket drawer shows the source
                   row's `folder_id`.

Offline: the outbound `requests.get` is stubbed on `main.http_requests`; no
network and no database.
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (offline-safe import, same as the other endpoint tests)

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _func_body(js, name):
    start = js.index("function %s(" % name)
    nxt = js.find("\nfunction ", start + 1)
    return js[start:nxt if nxt != -1 else len(js)]


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status=200, content=b"IMG", ctype="image/png"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ctype}


class MediaRouteTests(unittest.TestCase):

    def setUp(self):
        self._cfg = main.cfg
        self._get = main.http_requests.get
        main.cfg = SimpleNamespace(supabase_url="https://example.invalid",
                                   supabase_bucket="test-bucket",
                                   supabase_key="service-key")
        self.calls = []

        def fake_get(url, headers=None, timeout=None, proxies=None):
            self.calls.append(url)
            return _Resp()

        main.http_requests.get = fake_get

    def tearDown(self):
        main.cfg = self._cfg
        main.http_requests.get = self._get

    @staticmethod
    def client():
        from fastapi.testclient import TestClient
        return TestClient(main.app)   # no `with`: the lifespan is not needed

    def test_media_route_rejects_traversal(self):
        res = self.client().get("/api/media", params={"object": "../x"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self.calls, [], "traversal must not reach the bucket")
        res = self.client().get("/api/media", params={"object": ""})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(self.calls, [])

    def test_media_route_passes_slashed_objects(self):
        res = self.client().get("/api/media", params={"object": "studio/j1/k3.jpg"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.content, b"IMG")
        self.assertIn("image/png", res.headers["content-type"])
        self.assertEqual(len(self.calls), 1)
        # the path is exact; ?v= is the edge-cache buster a re-crop needs
        self.assertIn("/object/test-bucket/studio/j1/k3.jpg?v=", self.calls[0],
                      self.calls[0])

    def test_legacy_route_still_serves_page_images(self):
        res = self.client().get("/api/proxy/image/abc123")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.content, b"IMG")
        self.assertIn("/object/test-bucket/abc123?v=", self.calls[0],
                      self.calls[0])

    def test_legacy_route_keeps_its_404_for_a_missing_object(self):
        main.http_requests.get = lambda *a, **k: _Resp(status=404)
        res = self.client().get("/api/proxy/image/missing")
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["error"], "Image not found")


class DbGridPins(unittest.TestCase):

    def setUp(self):
        self.js = _read("app.js")
        self.css = _read("styles.css")

    def test_cell_renders_rich_content(self):
        self.assertIn("function dbRichHtml(", self.js)
        cell = _func_body(self.js, "dbCellHtml")
        self.assertIn("const rich = dbRichHtml(s);", cell)
        self.assertIn('class="db-rich-wrap"', cell)
        # plain cells keep the escaped, truncated span (no raw HTML path)
        self.assertIn("s.slice(0, 240)", cell)
        rich = _func_body(self.js, "dbRichHtml")
        self.assertIn("'<table'", rich)
        self.assertIn("'$$'", rich)

    def test_grid_typesets_after_render(self):
        grid = _func_body(self.js, "dbRenderGrid")
        self.assertIn('class="db-grid"', grid)
        self.assertIn("typesetMath(g);", grid)
        self.assertGreater(grid.index("typesetMath(g);"), grid.index("g.innerHTML"))

    def test_rich_css_exists(self):
        self.assertIn(".db-rich-wrap", self.css)
        self.assertIn(".db-rich-wrap .md-table", self.css)

    def test_drawer_shows_the_folder_id(self):
        drawer = _func_body(self.js, "dbBucketOpen")
        self.assertIn("s.folder_id", drawer)
        self.assertIn("bucketDrawerName", drawer)
        self.assertIn("folder ?", drawer)


if __name__ == "__main__":
    unittest.main()
