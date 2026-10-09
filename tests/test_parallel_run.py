"""Offline tests for the parallel OCR engine (no network, no database).

Covers the round-based, key-pinned dispatch (Q1=A), the global file-order
import (Q2=B), the per-file "blocked" fallback (Q3=B), and the router's
key-pinned call path.

Run them either way:
    python tests/test_parallel_run.py
    pytest tests/test_parallel_run.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import gemini_router, parallel, runner                    # noqa: E402
from pipeline.gemini_router import GeminiRouter                          # noqa: E402
from pipeline.runner import blocked_remaining                            # noqa: E402


class RoundPlanTests(unittest.TestCase):
    """`parallel.round_plan` — one file per lane per round, in file order."""

    def test_round_plan_one_file_per_lane_in_file_order(self):
        items = list("ABCDEFG")                       # 7 files, 3 lanes
        rounds = parallel.round_plan(items, ["a", "b", "c"])
        self.assertEqual([len(chunk) for _no, chunk in rounds], [3, 3, 1])
        # lane n takes the n-th file of each round, file order preserved
        self.assertEqual(rounds[0][1][0], (0, "A", "a"))
        self.assertEqual(rounds[0][1][1], (1, "B", "b"))
        self.assertEqual(rounds[1][1][0], (3, "D", "a"))
        self.assertEqual(rounds[2][1][0], (6, "G", "a"))
        for _no, chunk in rounds:                     # <=1 file per key per round
            keys = [k for _i, _it, k in chunk]
            self.assertEqual(len(keys), len(set(keys)), chunk)

    def test_round_plan_empty_lanes_is_no_rounds(self):
        self.assertEqual(parallel.round_plan(list("ABC"), []), [])


class ImportOrderTests(unittest.TestCase):
    """`parallel.import_order` restores 01, 02, 03 … (Q2=B)."""

    def test_import_order_is_global_file_order(self):
        arrived = [(2, "x"), (0, "y"), (3, "z"), (1, "w")]
        ordered = parallel.import_order(arrived)
        self.assertEqual([i for i, _o in ordered], [0, 1, 2, 3])
        self.assertEqual([o for _i, o in ordered], ["y", "w", "x", "z"])


class LaneKeysTests(unittest.TestCase):
    """`parallel.lane_keys` — a lane needs an enabled key that is usable now."""

    def test_lane_keys_excludes_disabled_and_cooldown(self):
        cfg = SimpleNamespace(gemini_key_pool=("k1", "k2", "k3"))
        # k2 is cooling down (absent from the plan); k3 is switched off
        plan = [(0, "k1", ["m"], 0.0, 0.0), (2, "k3", ["m"], 0.0, 0.0)]
        lanes = parallel.lane_keys(cfg, enabled_keys=["k1", "k2"], plan=plan)
        self.assertEqual(lanes, ["k1"])

    def test_lane_keys_none_means_every_key_is_on(self):
        cfg = SimpleNamespace(gemini_key_pool=("k1", "k2"))
        plan = [(0, "k1", ["m"], 0.0, 0.0), (1, "k2", ["m"], 0.0, 0.0)]
        self.assertEqual(parallel.lane_keys(cfg, None, plan=plan), ["k1", "k2"])


class BlockedCountTests(unittest.TestCase):
    def test_parallel_blocked_counts_failing_file_plus_queue(self):
        # stop at the failing file (index 4 of 10): 4..10 = 7 files blocked
        self.assertEqual(blocked_remaining(10, 4), 7)
        self.assertEqual(blocked_remaining(10, 10), 1)


class StartRunOptionsTests(unittest.TestCase):
    """`start_run` threads the page-level mode + enabled keys to the run."""

    def test_start_run_records_mode_and_enabled_keys(self):
        captured = {}

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                captured["target"] = target
                captured["args"] = args
                captured["daemon"] = daemon

            def start(self):
                captured["started"] = True

        hub = runner.Hub()
        with mock.patch.object(runner.threading, "Thread", FakeThread):
            ok = runner.start_run(hub, None, None, mode="parallel",
                                  enabled_keys=["k1", "k2"])
        self.assertTrue(ok)
        self.assertTrue(captured.get("started"))
        _hub, _cfg, _db, options = captured["args"]
        self.assertEqual(options["mode"], "parallel")
        self.assertEqual(options["enabled_keys"], ["k1", "k2"])

    def test_start_run_defaults_to_serial_with_no_key_filter(self):
        captured = {}

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                captured["args"] = args

            def start(self):
                pass

        hub = runner.Hub()
        with mock.patch.object(runner.threading, "Thread", FakeThread):
            runner.start_run(hub, None, None)
        options = captured["args"][3]
        self.assertEqual(options["mode"], "serial")
        self.assertIsNone(options["enabled_keys"])


class CallOcrPinTests(unittest.TestCase):
    """`_call_ocr(key_pin=…)` routes to the pinned path; no pin = serial."""

    def test_call_ocr_pins_to_the_router_pinned_path(self):
        seen = {}
        real = runner.router.call_pinned
        runner.router.call_pinned = lambda cfg, key, *a, **k: (
            seen.update(key=key) or ({"q": 1}, "raw", {}))
        try:
            out = runner._call_ocr(None, "p", "b", "image/jpeg",
                                   key_pin="k2222222222")
        finally:
            runner.router.call_pinned = real
        self.assertEqual(seen.get("key"), "k2222222222")
        self.assertEqual(out, {"q": 1})

    def test_call_ocr_without_pin_uses_the_serial_router(self):
        seen = {}
        real = runner.router.call
        runner.router.call = lambda cfg, *a, **k: (
            seen.update(called=True) or ({"q": 1}, "raw", {}))
        try:
            out = runner._call_ocr(None, "p", "b", "image/jpeg")
        finally:
            runner.router.call = real
        self.assertTrue(seen.get("called"))
        self.assertEqual(out, {"q": 1})


class CallPinnedTests(unittest.TestCase):
    """`GeminiRouter.call_pinned` walks ONLY the pinned key's ladder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._real_usage = gemini_router.USAGE_PATH
        gemini_router.USAGE_PATH = os.path.join(self._tmp.name, "usage.json")
        self._real_call = gemini_router.call_gemini

    def tearDown(self):
        gemini_router.call_gemini = self._real_call
        gemini_router.USAGE_PATH = self._real_usage
        self._tmp.cleanup()

    @staticmethod
    def _cfg(keys=("k1111111111", "k2222222222")):
        from tests.test_gemini_router import make_cfg
        return make_cfg(keys=keys)

    def _record(self):
        calls = []

        def fake(api_key, model, *args, **kwargs):
            calls.append((api_key, model))
            return {"answer": 1}, "raw", {"tokens_in": 0, "tokens_out": 0}

        gemini_router.call_gemini = fake
        return calls

    def test_call_pinned_walks_only_the_given_key(self):
        calls = self._record()
        r = GeminiRouter()
        parsed, _text, route = r.call_pinned(
            self._cfg(), "k2222222222", "p", "b", "image/jpeg")
        self.assertEqual(parsed, {"answer": 1})
        self.assertTrue(all(key == "k2222222222" for key, _m in calls), calls)
        self.assertEqual(route["model"], "gemini-3.5-flash")

    def test_call_pinned_marks_exhausted_when_parked(self):
        self._record()
        r = GeminiRouter()
        cfg = self._cfg()
        key = "k1111111111"
        for model in cfg.gemini_model_ladder:         # park every rung
            r._mark(key, "model_unavailable", model)
        with self.assertRaises(gemini_router.faults.FaultError) as ctx:
            r.call_pinned(cfg, key, "p", "b", "image/jpeg")
        self.assertTrue(ctx.exception.fault.get("exhausted") is True)

    def test_call_pinned_does_not_advance_the_serial_rotation(self):
        self._record()
        r = GeminiRouter()
        r._rotate = 1
        r.call_pinned(self._cfg(), "k2222222222", "p", "b", "image/jpeg")
        self.assertEqual(r._rotate, 1)                # a lane never steals it


class LaneEventTests(unittest.TestCase):
    """`_produce_round` emits `lane` progress WHILE the pool works (cycle-2
    visibility): start → route → done, or start → failed. The raw key never
    leaves the process — the event carries the pool index + the masked form."""

    KEY = "k1111111111"

    @staticmethod
    def _cfg():
        return SimpleNamespace(gemini_key_pool=(LaneEventTests.KEY, "k2222222222"))

    @staticmethod
    def _item(tmp, name="01-q.pdf"):
        path = os.path.join(tmp, name)
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 test")
        return SimpleNamespace(file_path=path, type="سوال", source_id="sid-" + name,
                               mime_type="application/pdf",
                               rel_path="math/10/algebra/question/" + name)

    def _run(self, fake_ocr, tmp):
        events = []
        hub = SimpleNamespace(emit=events.append)
        item = self._item(tmp)
        with mock.patch.object(runner, "upload_file", lambda *a, **k: "url"), \
             mock.patch.object(runner, "_call_ocr", fake_ocr):
            bundles = runner._produce_round(self._cfg(), None,
                                            [(0, item, self.KEY)], hub=hub)
        return events, bundles

    def test_produce_round_emits_lane_start_route_and_done(self):
        def fake_ocr(cfg, prompt, data_b64, mime_type, on_problem=None,
                     on_route=None, key_pin=None):
            if on_route:
                on_route("label", "masked", "gemini-3.5-flash", 1)
            return {"q": 1}

        with tempfile.TemporaryDirectory() as tmp:
            events, _bundles = self._run(fake_ocr, tmp)
        self.assertEqual([e["type"] for e in events],
                         ["lane", "lane", "lane", "lane"])
        self.assertEqual([e["state"] for e in events],
                         ["start", "start", "route", "done"])
        self.assertEqual([e["phase"] for e in events],
                         ["uploading", "ocr", "ocr", "importing"])
        self.assertEqual(events[0]["key"], 0)                 # pool index
        self.assertEqual(events[0]["index"], 1)
        self.assertEqual(events[2]["model"], "gemini-3.5-flash")
        self.assertTrue(all(e["masked"] == gemini_router.mask(self.KEY)
                            for e in events))

    def test_produce_round_reports_a_failed_lane(self):
        def boom(*a, **k):
            raise RuntimeError("OCR blew up")

        with tempfile.TemporaryDirectory() as tmp:
            events, bundles = self._run(boom, tmp)
        self.assertEqual([e["state"] for e in events],
                         ["start", "start", "failed"])
        self.assertIn("reason", events[-1])
        self.assertIsInstance(bundles[0]["error"], RuntimeError)

    def test_lane_events_carry_no_raw_key(self):
        def fake_ocr(cfg, prompt, data_b64, mime_type, on_problem=None,
                     on_route=None, key_pin=None):
            if on_route:
                on_route("l", "m", "gemini-3.5-flash", 1)
            return {"q": 1}

        with tempfile.TemporaryDirectory() as tmp:
            events, _bundles = self._run(fake_ocr, tmp)
        self.assertNotIn(self.KEY, json.dumps(events))

    def test_lane_emits_a_wait_phase_when_on_problem_fires(self):
        """A router backoff reaches the lane as a readable `waiting Ns`
        phase, and the callback tolerates the 4-arg call shape used by
        storage/vision (no `where` positional)."""
        def fake_ocr(cfg, prompt, data_b64, mime_type, on_problem=None,
                     on_route=None, key_pin=None):
            if on_problem:
                on_problem("gemini-3.5-flash", 2, 3, 12)     # 4 args
            return {"q": 1}

        with tempfile.TemporaryDirectory() as tmp:
            events, _bundles = self._run(fake_ocr, tmp)
        waits = [e for e in events if e["state"] == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["phase"], "waiting 12s")

    def test_wait_lane_carries_the_deadline(self):
        """C4 (item 1.4): the wait lane event carries `until`/`total` so the
        dashboard can draw an exact countdown ring."""
        def fake_ocr(cfg, prompt, data_b64, mime_type, on_problem=None,
                     on_route=None, key_pin=None):
            if on_problem:
                on_problem("gemini-3.5-flash", 2, 3, 12)
            return {"q": 1}

        with tempfile.TemporaryDirectory() as tmp:
            events, _bundles = self._run(fake_ocr, tmp)
        wait = [e for e in events if e["state"] == "wait"][0]
        self.assertEqual(wait["total"], 12)
        self.assertGreater(wait["until"], time.time())


class HubLaneTests(unittest.TestCase):
    """The hub seeds the last lane state per key into `snapshot()` so a
    reconnecting browser redraws the lanes, and clears it on a new run."""

    def test_lane_event_is_seeded_into_the_snapshot_and_cleared_on_run(self):
        hub = runner.Hub()
        hub.emit({"type": "lane", "key": 1, "masked": "k222…2222",
                  "state": "route", "index": 3, "file": "a/b.pdf",
                  "model": "gemini-3.5-flash", "attempt": 1,
                  "phase": "imported"})
        snap = hub.snapshot()
        self.assertEqual(snap["lanes"][1]["state"], "route")
        self.assertEqual(snap["lanes"][1]["model"], "gemini-3.5-flash")
        self.assertEqual(snap["lanes"][1]["phase"], "imported")
        hub.emit({"type": "run_started"})
        self.assertEqual(hub.snapshot()["lanes"], {})


class RoundBoundaryTests(unittest.TestCase):
    """`_run_parallel` emits one `round` event per round, numbered 1..N."""

    def test_run_parallel_emits_a_round_event_per_round(self):
        hub = runner.Hub()
        events = []
        real_emit = hub.emit

        def cap(ev):
            events.append(ev)
            return real_emit(ev)

        hub.emit = cap
        cfg = SimpleNamespace(gemini_key_pool=("k1", "k2"), ocr_provider="gemini")
        items = [SimpleNamespace(type="سوال") for _ in range(5)]   # 5 files, 2 lanes

        def fake_produce(cfg, db, assignments, hub=None, on_bundle=None):
            bundles = {i: {"index0": i, "item": item, "key": k}
                       for i, item, k in assignments}
            if on_bundle is not None:
                for b in bundles.values():
                    on_bundle(b)
            return bundles

        with mock.patch.object(runner.router, "_plan",
                               lambda *a, **k: [(0, "k1", ["m"], 0.0, 0.0),
                                                (1, "k2", ["m"], 0.0, 0.0)]), \
             mock.patch.object(runner, "_produce_round", side_effect=fake_produce), \
             mock.patch.object(runner, "_process_item", lambda *a, **k: "clean"), \
             mock.patch.object(runner, "_flush_parked", lambda *a, **k: None), \
             mock.patch.object(runner.deferrals, "max_consecutive_failures",
                               lambda: 3):
            runner._run_parallel(hub, cfg, None, items,
                                 {"enabled_keys": None}, lambda *a, **k: None)
        rounds = [e for e in events if e.get("type") == "round"]
        self.assertEqual([e["no"] for e in rounds], [1, 2, 3])
        self.assertTrue(all(e["lanes"] == 2 for e in rounds))


class ImportTerminalLaneTests(unittest.TestCase):
    """The import loop emits a TERMINAL lane event per bundle, so a finished
    lane stops reading "importing" (cycle-3 item 1)."""

    def _drive(self, outcome):
        hub = runner.Hub()
        events = []
        real_emit = hub.emit

        def cap(ev):
            events.append(ev)
            return real_emit(ev)

        hub.emit = cap
        cfg = SimpleNamespace(gemini_key_pool=("k1", "k2"), ocr_provider="gemini")
        items = [SimpleNamespace(type="سوال") for _ in range(5)]

        def fake_produce(cfg, db, assignments, hub=None, on_bundle=None):
            bundles = {i: {"index0": i, "item": item, "key": k}
                       for i, item, k in assignments}
            if on_bundle is not None:
                for b in bundles.values():
                    on_bundle(b)
            return bundles

        with mock.patch.object(runner.router, "_plan",
                               lambda *a, **k: [(0, "k1", ["m"], 0.0, 0.0),
                                                (1, "k2", ["m"], 0.0, 0.0)]), \
             mock.patch.object(runner, "_produce_round", side_effect=fake_produce), \
             mock.patch.object(runner, "_process_item", lambda *a, **k: outcome), \
             mock.patch.object(runner, "_flush_parked", lambda *a, **k: None), \
             mock.patch.object(runner.deferrals, "max_consecutive_failures",
                               lambda: 3):
            runner._run_parallel(hub, cfg, None, items,
                                 {"enabled_keys": None}, lambda *a, **k: None)
        return events

    def test_import_loop_emits_a_terminal_lane_event_per_bundle(self):
        events = self._drive("clean")
        lanes = [e for e in events if e.get("type") == "lane"]
        self.assertEqual(len(lanes), 5)                       # one per bundle
        self.assertTrue(all(e["state"] == "done" for e in lanes))
        self.assertTrue(all(e["phase"] == "imported" for e in lanes))
        rounds = [e for e in events if e.get("type") == "round"]
        self.assertEqual([e["no"] for e in rounds], [1, 2, 3])  # mechanics intact

    def test_import_loop_marks_a_not_imported_bundle_failed(self):
        events = self._drive("blocked")
        lanes = [e for e in events if e.get("type") == "lane"]
        self.assertTrue(lanes)
        self.assertTrue(all(e["state"] == "failed" for e in lanes))
        self.assertTrue(all(e["phase"] == "not imported" for e in lanes))


class PerLaneImportTests(unittest.TestCase):
    """C1 (item 1.1): each lane's file is imported the instant ITS OCR
    returns — a fast lane never waits for the round's slowest lane."""

    def test_fast_lane_imports_before_slow_lane(self):
        hub = runner.Hub()
        trace = []
        cfg = SimpleNamespace(gemini_key_pool=("k1", "k2"), ocr_provider="gemini")
        items = [SimpleNamespace(type="سوال") for _ in range(2)]

        def fake_produce(cfg, db, assignments, hub=None, on_bundle=None):
            bundles = {i: {"index0": i, "item": item, "key": k}
                       for i, item, k in assignments}
            for i in sorted(bundles):          # completion order: lane 0 first
                if on_bundle is not None:
                    on_bundle(bundles[i])
            trace.append("produce_returned")   # only AFTER both lanes imported
            return bundles

        def fake_process(hub, cfg, db, item, index, total, pre=None):
            trace.append("import_%d" % index)
            return "clean"

        with mock.patch.object(runner.router, "_plan",
                               lambda *a, **k: [(0, "k1", ["m"], 0.0, 0.0),
                                                (1, "k2", ["m"], 0.0, 0.0)]), \
             mock.patch.object(runner, "_produce_round", side_effect=fake_produce), \
             mock.patch.object(runner, "_process_item", side_effect=fake_process), \
             mock.patch.object(runner, "_flush_parked", lambda *a, **k: None), \
             mock.patch.object(runner.deferrals, "max_consecutive_failures",
                               lambda: 3):
            runner._run_parallel(hub, cfg, None, items,
                                 {"enabled_keys": None}, lambda *a, **k: None)
        # both files import INSIDE the round (completion order), not after it
        self.assertEqual(trace, ["import_1", "import_2", "produce_returned"])


if __name__ == "__main__":
    unittest.main()
