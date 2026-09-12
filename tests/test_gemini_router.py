"""Offline tests for the Gemini key×model router, env writer and config.

No network, no database — every Gemini touchpoint is a scripted fake.

Run them either way:
    python tests/test_gemini_router.py
    pytest tests/test_gemini_router.py
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import envfile, faults, gemini, gemini_router
from pipeline.config import (Config, DEFAULT_MODEL_LADDER, _gemini_pool,
                             _model_ladder)
from pipeline.gemini_router import GeminiRouter, mask


def make_cfg(keys=("k1111111111", "k2222222222"), ladder=None, names=()):
    return Config(
        input_root="", min_age_seconds=0, supabase_url="", supabase_key="",
        supabase_bucket="", postgres_host="", postgres_port=0, postgres_db="",
        postgres_user="", postgres_password="",
        gemini_key_pool=tuple(keys), gemini_key_names=tuple(names),
        gemini_model_ladder=tuple(ladder or DEFAULT_MODEL_LADDER),
        host="", port=0, router_provider="local", router_base_url="", router_api_key="",
        router_model="",
        revision_batch_limit=0, revision_chunk_size=0, revision_scan_chunk=50,
    )


def fault_error(kind):
    return faults.FaultError(faults.fault(kind), f"boom {kind}")


class ScriptedGemini:
    """Fake gemini.call_gemini: outcomes maps 'key|model' → scripted result(s).
    A list is consumed one item per call (last item repeats); 'ok' returns,
    anything else raises that fault kind."""

    def __init__(self, outcomes):
        self.outcomes = {k: (v if isinstance(v, list) else [v])
                         for k, v in outcomes.items()}
        self.calls = []                     # [(key, model)]

    def __call__(self, api_key, model, *args, **kwargs):
        self.calls.append((api_key, model))
        seq = self.outcomes.get(f"{api_key}|{model}", ["ok"])
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if outcome == "ok":
            return {"answer": 1}, "raw json text"
        raise fault_error(outcome)


class RouterTest(unittest.TestCase):
    def setUp(self):
        self._real_call = gemini_router.call_gemini
        self._real_list = gemini_router.list_models
        self._real_gw = gemini_router.gateway_probe
        self._tmp = tempfile.TemporaryDirectory()
        self._real_usage = gemini_router.USAGE_PATH
        gemini_router.USAGE_PATH = os.path.join(self._tmp.name, "usage.json")
        # default: the path to Google is up, so health checks reach the keys
        gemini_router.gateway_probe = lambda *a, **k: {
            "reachable": True, "google_err": False, "kind": "ok",
            "detail": "test gateway"}

    def tearDown(self):
        gemini_router.call_gemini = self._real_call
        gemini_router.list_models = self._real_list
        gemini_router.gateway_probe = self._real_gw
        gemini_router.USAGE_PATH = self._real_usage
        self._tmp.cleanup()

    def _use(self, outcomes):
        fake = ScriptedGemini(outcomes)
        gemini_router.call_gemini = fake
        return fake

    # ---------------------------------------------------------- happy paths
    def test_healthy_pair_costs_exactly_one_call(self):
        """No dedicated probes: real work goes straight to key1 · 3.5."""
        fake = self._use({})
        r = GeminiRouter()
        parsed, _text, route = r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(parsed, {"answer": 1})
        self.assertEqual(fake.calls, [("k1111111111", "gemini-3.5-flash")])
        self.assertEqual(route["model"], "gemini-3.5-flash")
        self.assertEqual(route["attempts"], 1)

    def test_second_call_reuses_cached_pair(self):
        fake = self._use({})
        r = GeminiRouter()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(len(fake.calls), 2)   # one per file, no extra probing

    # ---------------------------------------------------- model ladder steps
    def test_model_steps_up_within_the_same_key(self):
        fake = self._use({
            "k1111111111|gemini-3.5-flash": "model_unavailable",
        })
        r = GeminiRouter()
        _p, _t, route = r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(route["model"], "gemini-3.6-flash")
        self.assertEqual(route["attempts"], 2)
        # the dead model is cached — the next file must not pay for it again
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls, [("k1111111111", "gemini-3.6-flash")])

    def test_empty_reply_steps_ladder_without_caching_death(self):
        """A model that blanks on one page is not 'dead' — but the sticky
        last-good model keeps the next file starting one rung higher."""
        fake = self._use({
            "k1111111111|gemini-3.5-flash": "model_empty",
        })
        r = GeminiRouter()
        _p, _t, route = r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(route["model"], "gemini-3.6-flash")
        self.assertNotIn("gemini-3.5-flash", r._entry("k1111111111")["blocked"])
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls[0], ("k1111111111", "gemini-3.6-flash"))
        # 3.5 is still in the plan (not blocked), just behind the sticky pick
        self.assertIn("gemini-3.5-flash",
                      [m for m in r._plan(make_cfg(), time.time())[0][2]])

    # ------------------------------------------------------------ key failover
    def test_bad_key_fails_over_to_next_key(self):
        fake = self._use({"k1111111111|gemini-3.5-flash": "bad_key"})
        seen = []
        r = GeminiRouter()
        _p, _t, route = r.call(
            make_cfg(), "p", "b", "image/jpeg",
            on_route=lambda *a: seen.append(a))
        self.assertEqual(route["key"], mask("k2222222222"))
        self.assertEqual(fake.calls, [("k1111111111", "gemini-3.5-flash"),
                                      ("k2222222222", "gemini-3.5-flash")])
        # key #1 is now dead-cached: the next call must not touch it at all
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls, [("k2222222222", "gemini-3.5-flash")])

    def test_rate_limit_cools_the_key_and_the_next_key_serves(self):
        fake = self._use({
            "k1111111111|gemini-3.5-flash": "rate_limit",
        })
        r = GeminiRouter()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls,
                         [("k1111111111", "gemini-3.5-flash"),
                          ("k2222222222", "gemini-3.5-flash")])
        # key #1 is in its ~60 s window: the next file must not touch it
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls, [("k2222222222", "gemini-3.5-flash")])
        # once the window passes, it is eligible again
        st = r._entry("k1111111111")
        st["unavailable_until"] = 0.0
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls[0][0], "k1111111111")

    def test_quota_spends_the_model_not_the_key(self):
        """Free-tier day quota is per model: 3.5 being exhausted must step
        to 3.6 on the SAME key, not cold-down the whole key for hours."""
        fake = self._use({"k1111111111|gemini-3.5-flash": "quota"})
        r = GeminiRouter()
        _p, _t, route = r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(route["model"], "gemini-3.6-flash")
        st = r._entry("k1111111111")
        self.assertFalse(st["dead"])
        u = r._usage_of("k1111111111")
        self.assertEqual(u["models"]["gemini-3.5-flash"], u["limit"])
        # the next file must start above the spent rung
        fake.calls.clear()
        r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls[0], ("k1111111111", "gemini-3.6-flash"))

    def test_whole_pool_exhausted_raises_after_bounded_attempts(self):
        dead = {f"{k}|{m}": "model_unavailable"
                for k in ("k1111111111", "k2222222222")
                for m in DEFAULT_MODEL_LADDER}
        fake = self._use(dead)
        r = GeminiRouter()
        with self.assertRaises(faults.FaultError) as ctx:
            r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(len(fake.calls), 2 * len(DEFAULT_MODEL_LADDER))
        self.assertEqual(ctx.exception.fault["kind"], "model_unavailable")
        # everything is cached now: a second file fails without new calls
        fake.calls.clear()
        with self.assertRaises(faults.FaultError):
            r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(fake.calls, [])

    # ------------------------------------------------------------- global faults
    def test_tunnel_down_aborts_without_burning_the_pool(self):
        fake = self._use({"k1111111111|gemini-3.5-flash": "tunnel_down"})
        r = GeminiRouter()
        with self.assertRaises(faults.FaultError) as ctx:
            r.call(make_cfg(), "p", "b", "image/jpeg")
        self.assertEqual(ctx.exception.fault["kind"], "tunnel_down")
        self.assertEqual(len(fake.calls), 1)   # key #2 untouched, same exit IP

    def test_geo_block_aborts_and_does_not_kill_the_key(self):
        fake = self._use({"k1111111111|gemini-3.5-flash": "geo_block"})
        r = GeminiRouter()
        with self.assertRaises(faults.FaultError):
            r.call(make_cfg(), "p", "b", "image/jpeg")
        st = r._entry("k1111111111")
        self.assertFalse(st["dead"])           # the KEY is fine — the IP isn't
        self.assertEqual(len(fake.calls), 1)

    def test_missing_pool_raises_immediately(self):
        r = GeminiRouter()
        with self.assertRaises(faults.FaultError) as ctx:
            r.call(make_cfg(keys=()), "p", "b", "image/jpeg")
        self.assertEqual(ctx.exception.fault["kind"], "bad_key")

    # ----------------------------------------------------------- health check
    def test_check_all_uses_only_the_free_listing_call(self):
        """The health check is a gateway probe + one list_models per key. It
        must never call a model — no generation tokens, ever."""
        gen = []

        def fake_call(*a, **k):
            gen.append(a)
            return {"x": 1}, "ok"

        def fake_list(key):
            if key == "k1111111111":
                return {"gemini-3.5-flash": True, "gemini-3.6-flash": True,
                        "gemini-2.5-flash": True}
            raise fault_error("bad_key")

        gemini_router.call_gemini = fake_call
        gemini_router.list_models = fake_list
        r = GeminiRouter()
        report = r.check_all(make_cfg())
        first, second = report["keys"]
        self.assertTrue(report["gateway"]["reachable"])
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["models"]["gemini-3.5-flash"], "ok")
        self.assertEqual(first["models"]["gemini-3.7-flash"], "missing")
        self.assertEqual(second["status"], "dead")
        self.assertEqual(second["fault"]["cause"], "auth")   # not our tunnel
        self.assertEqual(gen, [])                             # no model was used

        # the matrix feeds the plan: 3.5 is skipped next time it is dead,
        # and for key #1 the unseen 3.7/3.8 are pre-blocked
        plan = {i: ms for i, _k, ms in r._plan(make_cfg(), 0)}
        self.assertEqual(plan[0], ["gemini-3.5-flash", "gemini-3.6-flash"])

    def test_check_all_walks_keys_one_by_one_in_order(self):
        seen = []

        def fake_list(key):
            seen.append(key)
            return {"gemini-3.5-flash": True}

        gemini_router.list_models = fake_list
        r = GeminiRouter()
        report = r.check_all(make_cfg(keys=("k1111111111", "k2222222222",
                                             "k3333333333")))
        self.assertEqual(seen, ["k1111111111", "k2222222222", "k3333333333"])
        self.assertEqual([row["index"] for row in report["keys"]], [0, 1, 2])
        self.assertTrue(all(row["status"] == "ok" for row in report["keys"]))

    def test_gateway_outage_blames_no_key_and_skips_listing(self):
        """When googleapis itself is unreachable the pool must not be marked
        dead — every key reports 'unreachable' and no key-level cooldown."""
        gemini_router.gateway_probe = lambda *a, **k: {
            "reachable": False, "google_err": False, "kind": "tunnel_down",
            "detail": "TUNNEL DOWN — no route"}
        listed = []
        gemini_router.list_models = lambda key: listed.append(key) or {}
        r = GeminiRouter()
        report = r.check_all(make_cfg())
        self.assertFalse(report["gateway"]["reachable"])
        self.assertEqual(listed, [])                          # never blamed
        for row in report["keys"]:
            self.assertEqual(row["status"], "unreachable")
            self.assertEqual(row["fault"]["cause"], "tunnel")  # says whose fault
        for k in ("k1111111111", "k2222222222"):
            self.assertFalse((r._state.get(k) or {}).get("dead", False))

    def test_google_erroring_is_reported_apart_from_tunnel(self):
        gemini_router.gateway_probe = lambda *a, **k: {
            "reachable": True, "google_err": True, "kind": "overload",
            "detail": "Google answered HTTP 503"}
        gemini_router.list_models = lambda key: {"gemini-3.5-flash": True}
        r = GeminiRouter()
        report = r.check_all(make_cfg())
        self.assertTrue(report["gateway"]["reachable"])
        self.assertTrue(report["gateway"]["google_err"])
        self.assertEqual(report["keys"][0]["status"], "ok")    # keys still fine

    # ------------------------------------------------------------ usage bars
    def test_quota_failure_fills_the_model_bar_to_the_cap(self):
        r = GeminiRouter()
        r._mark("k1111111111", "quota", "gemini-3.7-flash")
        u = r._usage_of("k1111111111")
        self.assertEqual(u["models"]["gemini-3.7-flash"], u["limit"])

    def test_a_spent_model_is_dropped_from_the_plan(self):
        r = GeminiRouter()
        r._count_send("k1111111111", "gemini-3.5-flash", quota_hit=True)
        ladder = dict((i, ms) for i, _k, ms in
                      r._plan(make_cfg(keys=("k1111111111",)), 0))[0]
        self.assertNotIn("gemini-3.5-flash", ladder)
        self.assertEqual(ladder[0], "gemini-3.6-flash")

    def test_usage_survives_a_new_router_and_never_stores_the_key(self):
        r = GeminiRouter()
        r._count_send("supersecretkey", "gemini-3.5-flash")
        with open(gemini_router.USAGE_PATH) as fh:
            blob = fh.read()
        self.assertNotIn("supersecretkey", blob)
        self.assertEqual(GeminiRouter()._usage_of("supersecretkey")
                         ["models"]["gemini-3.5-flash"], 1)

    # ----------------------------------------------------------------- mask
    def test_mask_never_leaks_a_usable_key(self):
        m = mask("AIzaSyD1234567890abcdefghijklmnop")
        self.assertTrue(m.startswith("AIza"))
        self.assertTrue(m.endswith("mnop"))
        self.assertNotIn("1234567890abcde", m)
        self.assertEqual(mask(""), "")


class GatewayProbeTest(unittest.TestCase):
    """gateway_probe must tell Google's own refusals apart from a foreign
    page sitting where the tunnel should be."""

    class Resp:
        def __init__(self, status, body="", server="", json_body=None,
                     server_timing=None):
            self.status_code = status
            self.text = body
            self.headers = {}
            if server:
                self.headers["server"] = server
            if server_timing:
                self.headers["server-timing"] = server_timing
            self._json = json_body

        def json(self):
            if self._json is None:
                raise ValueError("no json")
            return self._json

    def _use(self, resp):
        real = gemini.requests.get
        gemini.requests.get = lambda *a, **k: resp
        self.addCleanup(lambda: setattr(gemini.requests, "get", real))

    def test_google_json_refusal_counts_as_reachable(self):
        self._use(self.Resp(403, json_body={"error": {"message": "no key"}}))
        self.assertTrue(gemini.gateway_probe()["reachable"])

    def test_google_html_error_template_counts_as_reachable(self):
        # exactly what googleapis.com serves keyless: no Server header, a
        # gfe Server-Timing and the robot.png template deep in the body
        self._use(self.Resp(403, server_timing="gfet4t7; dur=0", body='\n'
                            '  ' * 40 + '<title>Error 403 (Forbidden)!!1</title>'))
        self.assertTrue(gemini.gateway_probe()["reachable"])

    def test_google_www_body_reference_counts_as_reachable(self):
        self._use(self.Resp(403, body='\n' * 40 +
                            'url(//www.google.com/images/errors/robot.png)'))
        self.assertTrue(gemini.gateway_probe()["reachable"])

    def test_foreign_captive_page_is_not_reachable(self):
        self._use(self.Resp(403, server="Squid", body="Access denied by policy"))
        res = gemini.gateway_probe()
        self.assertFalse(res["reachable"])
        self.assertEqual(res["kind"], "tunnel_down")

    def test_google_5xx_is_their_side_not_the_tunnel(self):
        self._use(self.Resp(503, json_body={"error": {"message": "busy"}}))
        res = gemini.gateway_probe()
        self.assertTrue(res["reachable"])
        self.assertTrue(res["google_err"])


class FaultClassificationTest(unittest.TestCase):
    def test_model_404_is_model_unavailable_not_unknown(self):
        flt = faults.classify_text(
            'Gemini HTTP 404: {"error":{"message":"Model [models/gemini-3.8-flash] '
            'is not found for API version v1beta, or is not supported for '
            'generateContent."}}')
        self.assertEqual(flt["kind"], "model_unavailable")
        self.assertFalse(flt["retryable"])

    def test_real_auth_and_quota_paths_are_untouched(self):
        self.assertEqual(faults.classify_text(
            "Gemini HTTP 403: API_KEY_INVALID The API key not valid").
            get("kind"), "bad_key")
        self.assertEqual(faults.classify_text(
            "Gemini HTTP 429: RESOURCE_EXHAUSTED quota").get("kind"), "quota")


class ConfigPoolTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "GEMINI_API_KEYS", "GEMINI_API_KEY",
            "GEMINI_API_KEY_QUESTIONS", "GEMINI_API_KEY_ANSWERS",
            "GEMINI_MODEL_LADDER")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_pool_prefers_GEMINI_API_KEYS_and_dedupes(self):
        os.environ["GEMINI_API_KEYS"] = "a, b ,a"
        os.environ["GEMINI_API_KEY"] = "z"
        self.assertEqual(_gemini_pool(), ("a", "b"))

    def test_empty_GEMINI_API_KEYS_is_authoritative(self):
        """Deleting every key in the UI must not resurrect legacy ones."""
        os.environ["GEMINI_API_KEYS"] = ""
        os.environ["GEMINI_API_KEY"] = "z"
        self.assertEqual(_gemini_pool(), ())

    def test_legacy_keys_seed_the_pool(self):
        os.environ.pop("GEMINI_API_KEYS", None)
        os.environ["GEMINI_API_KEY"] = "z"
        os.environ["GEMINI_API_KEY_QUESTIONS"] = "q"
        os.environ["GEMINI_API_KEY_ANSWERS"] = "z"
        self.assertEqual(_gemini_pool(), ("z", "q"))

    def test_default_ladder_is_35_to_38_flash(self):
        os.environ.pop("GEMINI_MODEL_LADDER", None)
        self.assertEqual(_model_ladder(), DEFAULT_MODEL_LADDER)
        self.assertEqual(DEFAULT_MODEL_LADDER[0], "gemini-3.5-flash")
        self.assertEqual(DEFAULT_MODEL_LADDER[-1], "gemini-3.8-flash")


class EnvFileTest(unittest.TestCase):
    def test_updates_in_place_preserving_comments_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("# keep me\nSUPABASE_URL=https://old.supabase.co\n"
                         "PORT=8080\n# trailing note\n")
            envfile.update_env_file(path, {
                "SUPABASE_URL": "https://new.supabase.co",
                "GEMINI_API_KEYS": "a,b,c",
            })
            with open(path) as fh:
                text = fh.read()
            self.assertEqual(text,
                             "# keep me\nSUPABASE_URL=https://new.supabase.co\n"
                             "PORT=8080\n# trailing note\nGEMINI_API_KEYS=a,b,c\n")

    def test_values_with_specials_get_quoted(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            envfile.update_env_file(path, {"PASS": "a b#c"})
            with open(path) as fh:
                line = fh.read().strip()
            self.assertEqual(line, 'PASS="a b#c"')


class TextOnlyGeminiTest(unittest.TestCase):
    def test_empty_payload_omits_inline_data(self):
        """call_gemini builds a text-only body when data_b64 is ''."""
        captured = {}

        class Resp:
            status_code = 200

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": '{"ok":1}'}]}}]}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            return Resp()

        real_post = gemini.requests.post
        gemini.requests.post = fake_post
        try:
            gemini.call_gemini("k", "gemini-3.5-flash", "hi", "", "text/plain")
        finally:
            gemini.requests.post = real_post
        parts = captured["payload"]["contents"][0]["parts"]
        self.assertEqual(parts, [{"text": "hi"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
