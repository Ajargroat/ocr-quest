"""Studio log panel - the studio's own event stream and its faults.

Three layers make the studio log, and this module pins all three:

* ``pipeline/studio/faults.py`` - what went wrong, in studio words. The
  studio kinds come first (no source, wrong stage, broken PDF); everything
  network-ish falls through to the pipeline kinds (tunnel down, quota, bad
  key) so both surfaces chip a failure the same way.
* ``pipeline/studio/jobs.py`` - every error emit carries that fault, both in
  the live websocket event and in the job's own ``log.jsonl``.
* ``ui/`` - the log panel itself: the same surface as the pipeline log, wired
  to the ids the HTML carries.

Offline: no network, no database, no model.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.studio import faults as studio_faults  # noqa: E402
from pipeline.studio import jobs as studio_jobs  # noqa: E402
from pipeline.studio import workspace  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def _read(name):
    with open(os.path.join(UI, name), encoding="utf-8") as fh:
        return fh.read()


def _view(html, view_id):
    body = html.split('id="%s"' % view_id, 1)[1]
    nxt = body.find('<section id="view')
    return body[:nxt] if nxt != -1 else body


class _Hub:
    """The websocket hub, reduced to the one method the studio uses."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class StudioFaultTests(unittest.TestCase):
    """The studio's own fault names, and the fall-through to the pipeline's."""

    def test_studio_kinds_win_over_the_pipeline_ones(self):
        cases = {
            "job has no source file": "no_source",
            "run extract first": "stage_order",
            "the pdf is encrypted": "pdf_broken",
            "unsupported file type: .txt": "bad_file",
            "unknown import target: csv": "bad_target",
            "job has nothing to import yet": "import_empty",
            "no page render for page 3": "no_render",
            "the model replied with no text content": "empty_page",
        }
        for text, want in cases.items():
            got = studio_faults.classify_text(text)
            self.assertEqual(got["kind"], want, text)

    def test_network_faults_fall_through_to_the_pipeline_kinds(self):
        cases = {
            "Connection reset by peer": ("recv_dropped", "net"),
            "HTTP 429 Too Many Requests": ("rate_limit", "bill"),
            "RESOURCE_EXHAUSTED quota exceeded": ("quota", "bill"),
            "API key not valid": ("bad_key", "auth"),
        }
        for text, (kind, group) in cases.items():
            got = studio_faults.classify_text(text)
            self.assertEqual(got["kind"], kind, text)
            self.assertEqual(got["group"], group, text)

    def test_every_kind_carries_a_label_a_group_and_advice(self):
        for kind in studio_faults.KINDS:
            meta = studio_faults.fault(kind)
            self.assertTrue(meta["label"], kind)
            self.assertTrue(meta["hint"], kind)
            self.assertIn(meta["group"],
                          ("net", "bill", "auth", "srv", "model", "store",
                           "local", "misc"), kind)
            self.assertIn("retryable", meta)

    def test_the_payload_is_flat_and_json_safe(self):
        import json
        flt = studio_faults.classify_text("job has no source file")
        payload = studio_faults.payload(flt)
        self.assertEqual(set(payload), {"kind", "label", "emoji", "group",
                                        "retryable", "hint", "raw"})
        json.dumps(payload)

    def test_an_exception_classifies_like_its_text(self):
        try:
            raise ValueError("job has no source file")
        except ValueError as exc:
            got = studio_faults.classify(exc)
        self.assertEqual(got["kind"], "no_source")

    def test_a_rejected_database_write_is_a_database_fault(self):
        # Phase-1 live import: a Persian grade reached a smallint column and
        # the reply the log kept was a bare, unnamed failure string.
        cases = (
            'invalid input syntax for type smallint: "\u062f\u0648\u0627\u0632\u062f\u0647\u0645"',
            'duplicate key value violates unique constraint "questions_legacy_id_key"',
            'relation "public.questions" does not exist',
            "terminating connection due to administrator command",
        )
        for text in cases:
            got = studio_faults.classify_text(text)
            self.assertEqual(got["kind"], "db_down", text)
            self.assertTrue(got["hint"], text)


class StudioImportFailureLogTests(unittest.TestCase):
    """A failed import leaves a classified line in the job log."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-log-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self.job = workspace.new_id()
        workspace.job_dir(self.job, create=True)
        workspace.write_artifact(self.job, "layer2", {
            "version": 1, "job_id": self.job, "profile": {},
            "blocks": [{"key": "p1-b1", "kind": "question", "page": 1,
                        "number": 1, "text": "x", "options": [], "status": "ready",
                        "issues": [], "missing": [], "bbox": None, "images": [],
                        "source": {"method": "text", "page": 1}}],
            "pages": [], "assets": [], "stats": {}})

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_failing_supabase_target_logs_chip_advice_and_raw_text(self):
        from unittest import mock
        from pipeline.studio import importers
        from pipeline.studio.importers import supabase

        boom = Exception('invalid input syntax for type smallint: "12"')

        def fail(*args, **kwargs):
            raise boom
        with mock.patch.object(supabase, "run", side_effect=fail):
            with self.assertRaises(Exception):
                importers.run(object(), self.job, target="supabase")
        rows = workspace.read_log(self.job)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stage"], "import")
        self.assertEqual(row["level"], "error")
        self.assertEqual(row["fault"]["kind"], "db_down")
        self.assertTrue(row["fault"]["hint"])
        self.assertIn("invalid input syntax", row["message"])


class StudioLogTests(unittest.TestCase):
    """Every error emit carries a fault; the history keeps it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-log-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self.job = workspace.new_id()
        self.hub = _Hub()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_error_emit_is_classified_and_tagged(self):
        studio_jobs._emit(self.hub, self.job, "extract", "error",
                          "pages 3-5: Connection reset by peer")
        self.assertEqual(len(self.hub.events), 1)
        event = self.hub.events[0]
        self.assertEqual(event["type"], "studio")
        self.assertEqual(event["level"], "error")
        self.assertEqual(event["fault"]["kind"], "recv_dropped")
        self.assertEqual(event["fault"]["group"], "net")
        self.assertTrue(event["fault"]["hint"])
        self.assertTrue(event["ts"])

    def test_an_info_emit_carries_no_fault(self):
        studio_jobs._emit(self.hub, self.job, "probe", "info", "probing")
        self.assertNotIn("fault", self.hub.events[0])

    def test_fail_classifies_the_exception_itself(self):
        studio_jobs._fail(self.hub, self.job, "enrich",
                          ValueError("job has no source file"))
        self.assertEqual(self.hub.events[0]["fault"]["kind"], "no_source")

    def test_the_log_file_keeps_the_fault_and_the_timestamp(self):
        studio_jobs._emit(self.hub, self.job, "import", "error",
                          "unknown import target: csv")
        rows = workspace.read_log(self.job)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stage"], "import")
        self.assertEqual(row["level"], "error")
        self.assertEqual(row["fault"]["kind"], "bad_target")
        self.assertIn("T", row["ts"])

    def test_the_log_endpoint_returns_the_history(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi TestClient unavailable")
        import main  # noqa: E402  (offline-safe import)
        client = TestClient(main.app)
        studio_jobs._emit(self.hub, self.job, "probe", "info", "probing")
        studio_jobs._fail(self.hub, self.job, "extract",
                          ValueError("job has no source file"))
        res = client.get("/api/studio/jobs/%s/log" % self.job)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["lines"]), 2)
        self.assertEqual(body["lines"][1]["fault"]["kind"], "no_source")

    def test_the_log_endpoint_404s_for_an_unknown_job(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi TestClient unavailable")
        import main  # noqa: E402
        client = TestClient(main.app)
        res = client.get("/api/studio/jobs/does-not-exist/log")
        self.assertEqual(res.status_code, 404)


class StudioLogUiTests(unittest.TestCase):
    """The panel: same surface as the pipeline log, same house controls."""

    def test_the_studio_view_has_a_live_log_panel(self):
        html = _read("index.html")
        view = _view(html, "viewStudio")
        self.assertIn('id="stLogPanel"', view)
        self.assertIn('id="stLog"', view)
        self.assertIn('id="stLogCount"', view)
        self.assertIn('id="stDiagCount"', view)
        self.assertIn('id="stLogCopyBtn"', view)
        self.assertIn('id="stLogClearBtn"', view)
        self.assertIn('id="stLogToggle"', view)
        # the pipeline's own log panel stays where it was
        self.assertIn('id="log"', html)
        self.assertIn('id="logClearBtn"', html)

    def test_the_panel_wears_the_pipeline_log_skin(self):
        css = _read("styles.css")
        for rule in (".st-logpanel{", ".st-logpanel .log{",
                     ".st-logpanel.collapsed .log{"):
            self.assertIn(rule, css, rule)
        # the fault chips are the pipeline's own classes
        for group in ("net", "bill", "auth", "model", "store", "local"):
            self.assertIn(".ek.%s" % group, css, group)

    def test_studio_js_wires_the_log_panel(self):
        js = _read("studio.js")
        for fn in ("function stLogLine(", "function stLogLoad(",
                   "function stLogWire(", "function stLogFaultTally(",
                   "function stLogFail(", "function stLogReset("):
            self.assertIn(fn, js, fn)
        # the fault chip and the diagnostic line are the pipeline's classes
        self.assertIn("'ek ' + (flt.group || 'misc')", js)
        self.assertIn("'line diag ' + (flt.group || 'misc')", js)
        # the live feed writes into the log
        self.assertIn("stLogLine(ev)", js)

    def test_the_log_endpoint_is_used_by_the_panel(self):
        js = _read("studio.js")
        self.assertIn("/log?limit=300", js)

    def test_the_log_route_is_registered(self):
        import main  # noqa: E402  (offline-safe import)
        paths = {r.path for r in main.app.routes
                 if getattr(r, "path", "").startswith("/api/studio")}
        self.assertIn("/api/studio/jobs/{job_id}/log", paths)


if __name__ == "__main__":
    unittest.main(verbosity=2)
