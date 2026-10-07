"""Offline tests for the OpenAI-compatible OCR lane (pipeline.vision).

No network — requests.get/post are scripted fakes. Covers the free
provider health check (`check_provider`), the retry/event contract of
`call_ocr_openai`, and the display label helper.

Run them either way:
    python tests/test_vision.py
    pytest tests/test_vision.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import faults, vision
from pipeline.config import Config, DEFAULT_MODEL_LADDER, load_config


def make_cfg(provider="openai", base="https://9router.example/v1",
             key="sk-or-test", model="ocr-model"):
    return Config(
        input_root="", min_age_seconds=0, supabase_url="", supabase_key="",
        supabase_bucket="", postgres_host="", postgres_port=0, postgres_db="",
        postgres_user="", postgres_password="",
        gemini_key_pool=(), gemini_key_names=(),
        gemini_model_ladder=DEFAULT_MODEL_LADDER,
        host="", port=0, router_provider="local", router_base_url="",
        router_api_key="", router_model="",
        ocr_provider=provider, ocr_base_url=base, ocr_api_key=key,
        ocr_model=model,
        extraction_providers=(), extraction_active="",
        revision_providers=(), revision_active="",
        proxy_profiles=(), proxy_active="",
        postgres_sslmode="",
        revision_batch_limit=0, revision_chunk_size=0, revision_scan_chunk=50,
    )


class Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status, json_body=None, text=""):
        self.status_code = status
        self.text = text or ("" if json_body is None else str(json_body))
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class CheckProviderTest(unittest.TestCase):
    def setUp(self):
        self._real_get = vision.requests.get

    def tearDown(self):
        vision.requests.get = self._real_get

    def _use(self, resp=None, exc=None):
        def fake(*a, **k):
            if exc is not None:
                raise exc
            return resp
        vision.requests.get = fake

    def test_success_lists_models(self):
        self._use(Resp(200, json_body={
            "data": [{"id": "ocr-model"}, {"id": "other-model"}]}))
        v = vision.check_provider(make_cfg())
        self.assertTrue(v["ok"])
        self.assertEqual(v["status"], 200)
        self.assertEqual(v["models"], ["ocr-model", "other-model"])
        self.assertIsNone(v["fault"])

    def test_404_is_a_classified_failure(self):
        self._use(Resp(404, text="no such route"))
        v = vision.check_provider(make_cfg())
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 404)
        self.assertEqual(v["fault"]["kind"], "model_unavailable")

    def test_500_is_overload(self):
        self._use(Resp(500, text="boom"))
        v = vision.check_provider(make_cfg())
        self.assertFalse(v["ok"])
        self.assertEqual(v["fault"]["kind"], "overload")

    def test_401_is_bad_key(self):
        self._use(Resp(401, text="bad key"))
        v = vision.check_provider(make_cfg())
        self.assertFalse(v["ok"])
        self.assertEqual(v["fault"]["kind"], "bad_key")

    def test_offline_never_raises(self):
        self._use(exc=vision.requests.ConnectionError("refused"))
        v = vision.check_provider(make_cfg())
        self.assertFalse(v["ok"])
        self.assertEqual(v["status"], 0)
        self.assertIn("fault", v)
        self.assertIsNotNone(v["fault"])

    def test_non_json_200_still_counts_as_reachable(self):
        """Some minimal servers answer /models with plain text —
        the endpoint is up, it just does not implement the listing."""
        self._use(Resp(200, text="<html>hi</html>"))
        v = vision.check_provider(make_cfg())
        self.assertTrue(v["ok"])
        self.assertEqual(v["models"], [])

    def test_empty_url_fails_cleanly_without_a_request(self):
        self._use()          # would explode if called
        v = vision.check_provider(make_cfg(base=""))
        self.assertFalse(v["ok"])
        self.assertIn("OCR_BASE_URL", v["detail"])


class ProviderLabelTest(unittest.TestCase):
    def test_loopback_urls_are_local(self):
        for url in ("http://localhost:20128/v1",
                    "http://127.0.0.1:8080/v1",
                    "http://[::1]:8080/v1"):
            self.assertEqual(vision.provider_label(make_cfg(base=url)), "local")

    def test_hosted_urls_show_the_host(self):
        self.assertEqual(
            vision.provider_label(make_cfg(base="https://9router.example/v1")),
            "9router.example")

    def test_missing_url_gets_a_placeholder(self):
        self.assertEqual(vision.provider_label(make_cfg(base="")),
                         "OCR endpoint")


class CallOcrOpenaiTest(unittest.TestCase):
    def setUp(self):
        self._real_post = vision.requests.post
        self._saved_env = {k: os.environ.get(k)
                           for k in ("NET_RETRIES", "NET_RETRY_WAIT")}

    def tearDown(self):
        vision.requests.post = self._real_post
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _use(self, resp=None, exc=None):
        def fake(*a, **k):
            if exc is not None:
                raise exc
            return resp
        vision.requests.post = fake

    def test_success_returns_parsed_json(self):
        body = {"choices": [{"message": {"content": '{"answer": 42}'}}]}
        self._use(Resp(200, json_body=body))
        parsed = vision.call_ocr_openai(make_cfg(), "p", "", "image/jpeg")
        self.assertEqual(parsed, {"answer": 42})

    def test_on_route_fires_before_a_retry(self):
        """The runner's '⇄ rerouted' event contract must hold for the
        OpenAI lane too — on_route fires on every retry with the
        endpoint's host as the label."""
        os.environ["NET_RETRIES"] = "2"
        os.environ["NET_RETRY_WAIT"] = "0"      # no real sleeping
        self._use(exc=vision.requests.ConnectionError("reset"))
        routes = []
        with self.assertRaises(faults.FaultError):
            vision.call_ocr_openai(
                make_cfg(), "p", "", "image/jpeg",
                on_route=lambda *a: routes.append(a))
        # one retry happened → one route event, labelled by host
        self.assertEqual(len(routes), 1)
        label, masked, model, attempt = routes[0]
        self.assertEqual(label, "9router.example")
        self.assertEqual(model, "ocr-model")
        self.assertEqual(attempt, 1)

    def test_401_raises_immediately_without_retry(self):
        """bad_key is not retryable — the retry loop must give up
        on the first attempt and never fire on_route."""
        os.environ["NET_RETRIES"] = "3"
        self._use(Resp(401, text="bad key"))
        routes = []
        with self.assertRaises(faults.FaultError):
            vision.call_ocr_openai(
                make_cfg(), "p", "", "image/jpeg",
                on_route=lambda *a: routes.append(a))
        self.assertEqual(routes, [])

    def test_empty_model_reply_raises_model_empty(self):
        body = {"choices": [{"message": {"content": "   "}}]}
        os.environ["NET_RETRIES"] = "1"
        self._use(Resp(200, json_body=body))
        with self.assertRaises(faults.FaultError) as ctx:
            vision.call_ocr_openai(make_cfg(), "p", "", "image/jpeg")
        self.assertEqual(ctx.exception.fault["kind"], "model_empty")


class SavedProviderDispatchTest(unittest.TestCase):
    """REPORT item 1: a saved custom provider that is active must
    receive the run's requests — config resolution → runner
    dispatch → the OpenAI-compatible call site, one chain."""

    _ENV_KEYS = ("EXTRACTION_PROVIDERS", "EXTRACTION_ACTIVE",
                 "OCR_PROVIDER", "OCR_BASE_URL", "OCR_API_KEY", "OCR_MODEL")

    def setUp(self):
        self._real_post = vision.requests.post
        self._saved_env = {k: os.environ.get(k) for k in self._ENV_KEYS}

    def tearDown(self):
        vision.requests.post = self._real_post
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _env_cfg(self, extraction_active):
        """What a real .env looks like after the Credentials tab saved
        one custom provider and selected it (or deselected it)."""
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["EXTRACTION_PROVIDERS"] = json.dumps([{
            "label": "9router", "base_url": "http://127.0.0.1:20128",
            "api_key": "sk-chain-test", "model": "chain-model"}])
        os.environ["EXTRACTION_ACTIVE"] = extraction_active
        return load_config()

    def test_saved_active_provider_receives_the_request(self):
        """load_config() resolves the saved selection into cfg.ocr_*,
        and the runner's dispatch posts exactly there."""
        cfg = self._env_cfg("9router")
        # config resolution (pipeline/config.py:228-231)
        self.assertEqual(cfg.ocr_provider, "custom")
        # stored URL is coerced to the /v1 base (config.py NINEROUTER_URL)
        self.assertEqual(cfg.ocr_base_url, "http://127.0.0.1:20128/v1")
        self.assertEqual(cfg.ocr_api_key, "sk-chain-test")
        self.assertEqual(cfg.ocr_model, "chain-model")
        # the call site posts to base + /chat/completions with the
        # selected model and key (pipeline/vision.py:76,83,90-91)
        seen = {}

        def fake_post(url, json=None, headers=None, **kw):
            seen.update(url=url, json=json, headers=headers)
            return Resp(200, json_body={"choices": [
                {"message": {"content": '{"answer": 7}'}}]})
        vision.requests.post = fake_post
        parsed = vision.call_ocr_openai(cfg, "p", "", "image/jpeg")
        self.assertEqual(parsed, {"answer": 7})
        self.assertEqual(seen["url"],
                         "http://127.0.0.1:20128/v1/chat/completions")
        self.assertEqual(seen["json"]["model"], "chain-model")
        self.assertEqual(seen["headers"]["Authorization"],
                         "Bearer sk-chain-test")

    def test_ninerouter_coercion_adds_v1_prefix(self):
        """A stored 9router URL without /v1 self-heals on load: the
        extraction lane must resolve the /v1 base, so chat + /models +
        the provider check all land on real routes (the cycle-3 404 fix)."""
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["EXTRACTION_PROVIDERS"] = json.dumps([{
            "label": "9router", "base_url": "http://127.0.0.1:20128",
            "api_key": "sk-v1-test", "model": "m"}])
        os.environ["EXTRACTION_ACTIVE"] = "9router"
        cfg = load_config()
        self.assertEqual(cfg.ocr_base_url, "http://127.0.0.1:20128/v1")

    def test_runner_dispatches_a_custom_provider_to_the_openai_lane(self):
        """runner._call_ocr honours cfg.ocr_provider == 'custom'
        (pipeline/runner.py:33-35) and never touches the Gemini pool."""
        from pipeline import runner
        cfg = self._env_cfg("9router")
        seen = {}
        real = runner.call_ocr_openai

        def fake(cfg_, prompt, *a, **k):
            seen["called"] = True
            seen["base"] = cfg_.ocr_base_url
            return {"answer": 7}
        runner.call_ocr_openai = fake
        try:
            parsed = runner._call_ocr(cfg, "p", "", "image/jpeg")
        finally:
            runner.call_ocr_openai = real
        self.assertEqual(parsed, {"answer": 7})
        self.assertEqual(seen["base"], "http://127.0.0.1:20128/v1")

    def test_runner_keeps_the_gemini_pool_when_nothing_is_selected(self):
        """The other half of REPORT item 1: with EXTRACTION_ACTIVE=''
        the dispatch must go to the Gemini lane, not the OpenAI one —
        this is the regression the rework exists to prevent."""
        from pipeline import runner
        from pipeline import gemini_router
        cfg = self._env_cfg("")
        self.assertEqual(cfg.ocr_provider, "gemini")
        calls = []
        real_call = gemini_router.router.call
        real_ocr = runner.call_ocr_openai

        def fake_pool(*a, **k):
            calls.append(a)
            return {}, "", []
        gemini_router.router.call = fake_pool
        runner.call_ocr_openai = (
            lambda *a, **k: self.fail("the OpenAI lane must not be called"))
        try:
            parsed = runner._call_ocr(cfg, "p", "", "image/jpeg")
        finally:
            gemini_router.router.call = real_call
            runner.call_ocr_openai = real_ocr
        self.assertEqual(parsed, {})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
