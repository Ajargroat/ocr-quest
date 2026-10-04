"""Offline tests for the credential / profile / chat-test endpoints in main.py.

No network, no database: main.ENV_PATH is patched at a temp file and
os.environ is restored in tearDown, so the real .env is never touched.
HTTP calls are stubbed at the seams (vision.test_chat, gemini.call_gemini).

Run them either way:
    python tests/test_credential_endpoints.py
    pytest tests/test_credential_endpoints.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402  (import is offline-safe: db/console are lazy)
from pipeline import faults  # noqa: E402

KEEP = "__KEEP__"
_POOL_KEYS = ("GEMINI_API_KEYS", "GEMINI_API_KEY",
              "GEMINI_API_KEY_QUESTIONS", "GEMINI_API_KEY_ANSWERS")


class EndpointCase(unittest.TestCase):
    """Shared harness: temp .env, no Gemini pool, restored globals."""

    def setUp(self):
        self._env = dict(os.environ)
        fd, self._env_path = tempfile.mkstemp(suffix=".env",
                                              prefix="konkour-test-")
        os.close(fd)
        with open(self._env_path, "w", encoding="utf-8") as fh:
            fh.write("# temp env for endpoint tests\n")
        self._old_env_path = main.ENV_PATH
        main.ENV_PATH = self._env_path
        for k in _POOL_KEYS:
            os.environ.pop(k, None)
        main.cfg = main.load_config()
        self._old_test_chat = main.vision.test_chat
        self._old_set_proxy = main.gemini_mod.set_proxy

    def tearDown(self):
        main.ENV_PATH = self._old_env_path
        main.vision.test_chat = self._old_test_chat
        main.gemini_mod.set_proxy = self._old_set_proxy
        os.environ.clear()
        os.environ.update(self._env)
        try:
            os.unlink(self._env_path)
        except OSError:
            pass
        main.cfg = main.load_config()

    def client(self):
        from fastapi.testclient import TestClient
        # No `with`: the lifespan (backup loop) is not needed by any test.
        return TestClient(main.app)

    @staticmethod
    def base():
        return {"gemini_keys": [], "gemini_names": [], "model_ladder": []}


class CredentialsSaveTest(EndpointCase):
    def test_provider_list_round_trips_and_masks_the_key(self):
        res = self.client().post("/api/credentials", json={
            **self.base(),
            "extraction_providers": [
                {"label": "9router", "base_url": "http://localhost:20128/v1",
                 "api_key": "sk-secret-123", "model": "qwen2.5-vl"}],
            "extraction_active": "9router",
            "revision_providers": [
                {"label": "rev", "base_url": "http://localhost:20128/v1",
                 "api_key": "sk-rev-456", "model": "OCR-Quest"}],
            "revision_active": "rev",
        })
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["extraction_active"], "9router")
        p = body["extraction_providers"][0]
        self.assertEqual(p["label"], "9router")
        self.assertTrue(p["api_key_set"])
        # the raw secret never appears in the API view …
        self.assertNotIn("sk-secret-123", json.dumps(body))
        self.assertNotIn("sk-rev-456", json.dumps(body))
        # … but really round-trips into the temp .env as JSON
        stored = json.loads(os.environ["EXTRACTION_PROVIDERS"])
        self.assertEqual(stored[0]["api_key"], "sk-secret-123")
        self.assertEqual(os.environ["EXTRACTION_ACTIVE"], "9router")
        rev = json.loads(os.environ["REVISION_PROVIDERS"])
        self.assertEqual(rev[0]["api_key"], "sk-rev-456")
        self.assertEqual(os.environ["REVISION_ACTIVE"], "rev")
        # GET (the load path) agrees, still masked
        view = self.client().get("/api/credentials").json()
        self.assertEqual(len(view["extraction_providers"]), 1)
        self.assertTrue(view["extraction_providers"][0]["api_key_set"])
        self.assertNotIn("sk-secret-123", json.dumps(view))

    def test_adding_a_provider_appends_and_the_sections_stay_independent(self):
        c = self.client()
        r1 = c.post("/api/credentials", json={
            **self.base(),
            "extraction_providers": [
                {"label": "A", "base_url": "http://a/v1",
                 "api_key": "sk-aaa", "model": "m-a"}],
            "extraction_active": "A"})
        self.assertEqual(r1.status_code, 200, r1.text)
        # second save: A kept (KEEP sentinel at its position), B appended,
        # revision gets its own one-entry list — the two never mix.
        r2 = c.post("/api/credentials", json={
            **self.base(),
            "extraction_providers": [
                {"label": "A", "base_url": "http://a/v1",
                 "api_key": KEEP, "model": "m-a"},
                {"label": "B", "base_url": "http://b/v1",
                 "api_key": "sk-bbb", "model": "m-b"}],
            "extraction_active": "B",
            "revision_providers": [
                {"label": "R", "base_url": "http://r/v1",
                 "api_key": "sk-rrr", "model": "m-r"}],
            "revision_active": "R"})
        self.assertEqual(r2.status_code, 200, r2.text)
        ext = json.loads(os.environ["EXTRACTION_PROVIDERS"])
        self.assertEqual([p["label"] for p in ext], ["A", "B"])
        self.assertEqual(ext[0]["api_key"], "sk-aaa")     # KEEP kept the secret
        self.assertEqual(ext[1]["api_key"], "sk-bbb")
        self.assertEqual(os.environ["EXTRACTION_ACTIVE"], "B")
        rev = json.loads(os.environ["REVISION_PROVIDERS"])
        self.assertEqual([p["label"] for p in rev], ["R"])
        view = c.get("/api/credentials").json()
        self.assertEqual([p["label"] for p in view["extraction_providers"]],
                         ["A", "B"])
        self.assertEqual([p["label"] for p in view["revision_providers"]],
                         ["R"])

    def test_postgres_fields_persist_across_reload(self):
        res = self.client().post("/api/credentials", json={
            **self.base(),
            "postgres_host": "db.local", "postgres_port": "5433",
            "postgres_db": "konkour", "postgres_user": "u",
            "postgres_password": "pw-secret", "postgres_sslmode": "require",
        })
        self.assertEqual(res.status_code, 200, res.text)
        view = self.client().get("/api/credentials").json()
        self.assertEqual(view["postgres_host"], "db.local")
        self.assertEqual(view["postgres_port"], "5433")
        self.assertEqual(view["postgres_sslmode"], "require")
        self.assertTrue(view["postgres_password_set"])
        self.assertNotIn("pw-secret", json.dumps(view))
        # hot-reloaded config + .env agree (restart reads the same file)
        self.assertEqual(main.cfg.postgres_host, "db.local")
        self.assertEqual(main.cfg.postgres_sslmode, "require")
        self.assertEqual(os.environ["POSTGRES_PASSWORD"], "pw-secret")

    def test_non_numeric_postgres_port_is_rejected(self):
        res = self.client().post("/api/credentials", json={
            **self.base(), "postgres_port": "not-a-port"})
        self.assertEqual(res.status_code, 400, res.text)

    def test_unknown_extraction_active_is_rejected(self):
        res = self.client().post("/api/credentials", json={
            **self.base(),
            "extraction_providers": [
                {"label": "A", "base_url": "http://a/v1",
                 "api_key": "sk", "model": "m"}],
            "extraction_active": "ghost"})
        self.assertEqual(res.status_code, 400, res.text)


class ChatTestEndpoint(EndpointCase):
    def test_chat_endpoint_returns_snippet_and_latency(self):
        main.vision.test_chat = lambda *a, **k: ("pong", 12.5)
        res = self.client().post("/api/credentials/test", json={
            "section": "extraction", "base_url": "http://localhost:20128/v1",
            "api_key": "sk", "model": "qwen2.5-vl"})
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["snippet"], "pong")
        self.assertEqual(d["model"], "qwen2.5-vl")
        self.assertIsInstance(d["latency_ms"], float)

    def test_chat_endpoint_reports_an_unreachable_provider(self):
        def boom(*a, **k):
            raise faults.FaultError(faults.fault("rate_limit"),
                                    "connection refused")
        main.vision.test_chat = boom
        res = self.client().post("/api/credentials/test", json={
            "section": "extraction", "base_url": "http://127.0.0.1:9/v1",
            "api_key": "", "model": "m"})
        self.assertEqual(res.status_code, 502)
        d = res.json()
        self.assertFalse(d["ok"])
        self.assertTrue(d.get("fault"))
        self.assertIn("latency_ms", d)

    def test_gemini_pool_test_makes_exactly_one_real_call(self):
        os.environ["GEMINI_API_KEYS"] = "AIzaFakeKeyForTest"
        os.environ["GEMINI_MODEL_LADDER"] = "gemini-test-flash"
        main.cfg = main.load_config()
        called = {}

        def fake_call(key, model, prompt, *a, **k):
            called.update(key=key, model=model, prompt=prompt)
            return ({"candidates": []}, "{}")

        old = main.gemini_mod.call_gemini
        main.gemini_mod.call_gemini = fake_call
        try:
            res = self.client().post("/api/credentials/test",
                                     json={"section": "extraction"})
        finally:
            main.gemini_mod.call_gemini = old
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["model"], "gemini-test-flash")
        self.assertEqual(called.get("model"), "gemini-test-flash")
        self.assertEqual(called.get("prompt"), 'Reply with exactly: {"pong": true}')

    def test_gemini_test_without_a_key_is_a_clean_400(self):
        res = self.client().post("/api/credentials/test",
                                 json={"section": "extraction"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Gemini key", res.json()["error"])


class ProfileEndpointsTest(EndpointCase):
    def test_profiles_save_persists_and_masks_the_password(self):
        calls = []
        main.gemini_mod.set_proxy = lambda c: calls.append(c)
        res = self.client().post("/api/profiles", json={
            "profiles": [{"name": "home", "host": "127.0.0.1",
                          "port": "10808", "scheme": "socks5", "user": "u",
                          "password": "pw-secret"}],
            "active": "home"})
        self.assertEqual(res.status_code, 200, res.text)
        d = res.json()
        self.assertTrue(d["saved"])
        self.assertEqual(d["active"], "home")
        self.assertTrue(d["profiles"][0]["pass_set"])
        self.assertNotIn("pw-secret", json.dumps(d))
        stored = json.loads(os.environ["PROXY_PROFILES"])
        self.assertEqual(stored[0]["password"], "pw-secret")
        self.assertEqual(os.environ["PROXY_ACTIVE"], "home")
        self.assertEqual(len(calls), 1)        # live lane swap happened
        # KEEP keeps the stored password on a re-save
        r2 = self.client().post("/api/profiles", json={
            "profiles": [{"name": "home", "host": "127.0.0.1",
                          "port": "10808", "scheme": "socks5", "user": "u",
                          "password": KEEP}],
            "active": "home"})
        self.assertEqual(r2.status_code, 200, r2.text)
        stored = json.loads(os.environ["PROXY_PROFILES"])
        self.assertEqual(stored[0]["password"], "pw-secret")
        # GET (the load path) agrees, still masked
        view = self.client().get("/api/profiles").json()
        self.assertEqual(view["active"], "home")
        self.assertNotIn("pw-secret", json.dumps(view))

    def test_direct_is_the_explicit_off_value(self):
        main.gemini_mod.set_proxy = lambda c: None
        res = self.client().post("/api/profiles",
                                 json={"profiles": [], "active": ""})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(os.environ["PROXY_ACTIVE"], "")
        view = self.client().get("/api/profiles").json()
        self.assertEqual(view["profiles"], [])
        self.assertEqual(view["active"], "")


if __name__ == "__main__":
    unittest.main()
