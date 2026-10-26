"""Tests for the editable prompts store and the floating assistant relay.

Both are local-first and network-free: prompt overrides redirect to a temp JSON
store, and the assistant's `STREAM_OVERRIDE` seam stands in for the upstream
SSE endpoint, so no test reaches the internet.

Every literal here is ASCII on purpose: the console this suite runs under is
cp1256 and must never be asked to print Persian text.

Run either way:
    python tests/test_prompts_assistant.py
    pytest tests/test_prompts_assistant.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import assistant, prompts, questions  # noqa: E402


class StoreCase(unittest.TestCase):
    """Redirect the override store into a temp file for every test."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prompt-store-")
        self._old = prompts.STORE_PATH
        prompts.STORE_PATH = os.path.join(self.dir, "overrides.json")

    def tearDown(self):
        prompts.STORE_PATH = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


class PromptsStoreTests(StoreCase):
    def test_an_override_reaches_the_next_call_without_a_restart(self):
        self.assertEqual(questions.question_prompt(), questions.QUESTION_PROMPT)
        prompts.save("pipeline.questions", "EDITED INSTRUCTIONS")
        # read at CALL time, so the very next request sees the new wording
        self.assertEqual(questions.question_prompt(), "EDITED INSTRUCTIONS")
        prompts.clear("pipeline.questions")
        self.assertEqual(questions.question_prompt(), questions.QUESTION_PROMPT)

    def test_a_blank_text_clears_the_override(self):
        prompts.save("pipeline.questions", "X")
        prompts.save("pipeline.questions", "   ")
        self.assertEqual(prompts.load(), {})

    def test_view_marks_what_is_overridden(self):
        prompts.save("pipeline.questions", "X")
        rows = {row["id"]: row for row in prompts.view()}
        self.assertTrue(rows["pipeline.questions"]["overridden"])
        self.assertEqual(rows["pipeline.questions"]["text"], "X")
        self.assertFalse(rows["pipeline.answers"]["overridden"])
        self.assertEqual(rows["pipeline.answers"]["text"],
                         rows["pipeline.answers"]["default"])

    def test_every_catalog_prompt_has_a_default_in_the_code(self):
        ids = [row["id"] for row in prompts.catalog()]
        self.assertIn("pipeline.questions", ids)
        self.assertIn("pipeline.answers", ids)
        self.assertIn("assistant.system", ids)
        for row in prompts.catalog():
            self.assertFalse(row["id"].startswith("studio."),
                             "Studio is retired; its prompts are gone")
            self.assertTrue(row["default"].strip(), row["id"])

    def test_an_unknown_id_has_no_default(self):
        self.assertEqual(prompts.default_of("nope"), "")

    def test_a_corrupt_store_is_treated_as_no_overrides(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(prompts.STORE_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(prompts.load(), {})
        self.assertEqual(questions.question_prompt(), questions.QUESTION_PROMPT)


class PromptsEndpointTests(StoreCase):
    def setUp(self):
        super().setUp()
        import main
        try:
            from fastapi.testclient import TestClient
            self.client = TestClient(main.app)
        except Exception:      # pragma: no cover - fastapi present in the suite
            self.client = None

    def test_the_view_lists_every_prompt(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.get("/api/prompts")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        ids = {row["id"] for row in body["prompts"]}
        self.assertIn("pipeline.questions", ids)
        self.assertEqual(len(ids), len(prompts.CATALOG))

    def test_save_and_reset_round_trip(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/prompts",
                               json={"id": "pipeline.questions",
                                     "text": "SAVED"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(questions.question_prompt() == "SAVED")
        self.assertEqual(
            self.client.get("/api/prompts").json()["prompts"][0].get("id"),
            prompts.CATALOG[0][0])
        res = self.client.post("/api/prompts/reset", json={})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(prompts.load(), {})

    def test_saving_the_default_wording_stores_no_override(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/prompts",
                               json={"id": "pipeline.questions",
                                     "text": questions.QUESTION_PROMPT})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(prompts.load(), {})

    def test_an_unknown_id_is_refused(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/prompts", json={"id": "nope", "text": "x"})
        self.assertEqual(res.status_code, 404)


class AssistantTests(unittest.TestCase):
    def _cfg(self, url="http://relay.local/v1", model="chat-x", key="sk-test"):
        # the legacy single endpoint: no saved provider list, one bare model
        return SimpleNamespace(assistant_url=url, assistant_model=model,
                               assistant_key=key, assistant_providers=(),
                               assistant_effort="")

    def test_a_base_url_gets_the_chat_path(self):
        self.assertEqual(assistant.endpoint("http://relay.local/v1"),
                         "http://relay.local/v1/chat/completions")
        self.assertEqual(assistant.endpoint("http://relay.local/v1/"),
                         "http://relay.local/v1/chat/completions")
        self.assertEqual(
            assistant.endpoint("http://relay.local/x/chat/completions"),
            "http://relay.local/x/chat/completions")
        self.assertEqual(assistant.endpoint(""), "")

    def test_openai_deltas_become_thinking_and_text_events(self):
        chunk = {"choices": [{"delta": {"reasoning_content": "hmm"}}]}
        self.assertEqual(list(assistant._events(chunk)),
                         [{"type": "thinking", "text": "hmm"}])
        chunk = {"choices": [{"delta": {"content": "hello"}}]}
        self.assertEqual(list(assistant._events(chunk)),
                         [{"type": "text", "text": "hello"}])
        self.assertEqual(list(assistant._events({})), [])
        # a relay that streams a bare object still yields text
        self.assertEqual(list(assistant._events({"output_text": "hi"})),
                         [{"type": "text", "text": "hi"}])

    def test_the_override_seam_is_relayed_untouched(self):
        def fake(cfg=None, payload=None):
            self.assertEqual(payload["model"], "chat-x")
            self.assertEqual(payload["messages"][0]["role"], "system")
            yield {"type": "thinking", "text": "why"}
            yield {"type": "text", "text": "because"}
            yield {"type": "done"}

        events = list(assistant.stream(self._cfg(), [{"role": "user",
                                                      "content": "q"}],
                                       override=fake))
        self.assertEqual([e["type"] for e in events],
                         ["thinking", "text", "done"])

    def test_an_unconfigured_assistant_explains_itself(self):
        events = list(assistant.stream(self._cfg(url="", model=""), []))
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("ASSISTANT_URL", events[0]["message"])
        self.assertEqual(events[-1], {"type": "done"})

    def test_the_system_prompt_is_editable(self):
        self.assertIn({"role": "system"},
                      [{"role": msg["role"]}
                       for msg in assistant._messages(self._cfg(), [])])
        messages = assistant._messages(self._cfg(), [])
        self.assertEqual(messages[0]["content"], assistant.SYSTEM_PROMPT)

    def test_the_key_is_never_returned(self):
        view = assistant.config_view(self._cfg())
        self.assertTrue(view["configured"])
        self.assertTrue(view["key_set"])
        self.assertNotIn("sk-test", json.dumps(view))


class AssistantEndpointTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self._old_cfg = main.cfg
        self._old_stream = assistant.STREAM_OVERRIDE
        main.cfg = SimpleNamespace(assistant_url="http://relay.local/v1",
                                   assistant_model="chat-x",
                                   assistant_key="sk-test")
        self.addCleanup(self._restore)
        try:
            from fastapi.testclient import TestClient
            self.client = TestClient(main.app)
        except Exception:      # pragma: no cover
            self.client = None

    def _restore(self):
        self.main.cfg = self._old_cfg
        assistant.STREAM_OVERRIDE = self._old_stream

    def test_the_config_view_is_masked(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        body = self.client.get("/api/assistant/config").json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["model"], "chat-x")
        self.assertEqual(body["url_host"], "relay.local")
        self.assertNotIn("sk-test", json.dumps(body))

    def test_the_chat_route_streams_our_own_events(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")

        def fake(cfg=None, payload=None):
            yield {"type": "thinking", "text": "why"}
            yield {"type": "text", "text": "because"}
            yield {"type": "done"}

        assistant.STREAM_OVERRIDE = fake
        res = self.client.post("/api/assistant/chat",
                               json={"messages": [{"role": "user",
                                                   "content": "q"}]})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers["content-type"].startswith(
            "text/event-stream"))
        lines = [line for line in res.text.split("\n") if line.startswith("data: ")]
        events = [json.loads(line[6:]) for line in lines]
        self.assertEqual([e["type"] for e in events],
                         ["thinking", "text", "done"])


THINKING = [{"type": "thinking", "text": "why "}]


class MultipleProvidersTests(unittest.TestCase):
    """More than one provider, each with its own models, one active pick."""

    def _cfg(self, **over):
        base = dict(assistant_url="", assistant_key="",
                    assistant_model="local::chat-x",
                    assistant_effort="low",
                    assistant_providers=(
                        {"label": "or", "base_url": "https://openrouter.ai/api/v1",
                         "api_key": "sk-or-secret", "models": ["a/one", "a/two"]},
                        {"label": "local", "base_url": "http://127.0.0.1:20128/v1",
                         "api_key": "sk-local", "models": ["chat-x", "chat-y"]}))
        base.update(over)
        return SimpleNamespace(**base)

    def test_the_active_pick_names_the_provider_and_its_model(self):
        cfg = self._cfg()
        provider, model = assistant.active(cfg)
        self.assertEqual(provider["label"], "local")
        self.assertEqual(model, "chat-x")
        self.assertEqual(assistant.model_id("local", "chat-y"), "local::chat-y")

    def test_a_missing_label_still_resolves_an_unambiguous_model(self):
        cfg = self._cfg(assistant_model="gone::chat-y")
        self.assertEqual(assistant.active(cfg)[0]["label"], "local")
        self.assertEqual(assistant.active(self._cfg(
            assistant_model="gone::nowhere"))[0], {})

    def test_the_payload_speaks_openrouter_reasoning(self):
        payload = assistant.payload_for(self._cfg(), [{"role": "user",
                                                       "content": "hi"}])
        self.assertEqual(payload["model"], "chat-x")
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertTrue(payload["stream_options"]["include_usage"])
        none = assistant.payload_for(self._cfg(assistant_effort=""),
                                     [{"role": "user", "content": "hi"}])
        self.assertNotIn("reasoning", none)

    def test_every_key_is_masked_in_the_view(self):
        body = json.dumps(assistant.config_view(self._cfg()))
        self.assertNotIn("sk-or-secret", body)
        self.assertNotIn("sk-local", body)
        self.assertEqual(len(json.loads(body)["providers"]), 2)

    def test_usage_becomes_a_token_and_speed_event(self):
        events = assistant._usage_events(
            {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160,
             "completion_tokens_details": {"reasoning_tokens": 12}},
            __import__("time").monotonic() - 2.0, "abc", "d")
        event = events[0]
        self.assertEqual(event["type"], "usage")
        self.assertEqual(event["prompt_tokens"], 120)
        self.assertEqual(event["reasoning_tokens"], 12)
        self.assertGreater(event["tokens_per_second"], 0)

    def test_a_403_is_explained_not_echoed_bare(self):
        text = assistant._readable_403('{"error":{"message":"no access to '
                                       'openrouter.ai free models"}}')
        self.assertIn("403", text)
        self.assertIn("9router", text)


class AssistantConfigSaveTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self._old_cfg = main.cfg
        self._old_path = main.ENV_PATH
        fd, self.env = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write("ASSISTANT_PROVIDERS=[]\n")
        main.ENV_PATH = self.env
        main.cfg = SimpleNamespace(assistant_url="", assistant_key="",
                                   assistant_model="",
                                   assistant_effort="",
                                   assistant_providers=())
        self.addCleanup(self._restore)
        try:
            from fastapi.testclient import TestClient
            self.client = TestClient(main.app)
        except Exception:      # pragma: no cover
            self.client = None

    def _restore(self):
        self.main.cfg = self._old_cfg
        self.main.ENV_PATH = self._old_path
        for key in list(os.environ):
            if key.startswith("ASSISTANT_"):
                os.environ.pop(key, None)
        try:
            os.unlink(self.env)
        except OSError:
            pass

    def test_a_saved_list_keeps_keys_on_the_second_save(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        first = self.client.post("/api/assistant/config", json={
            "providers": [{"label": "or", "base_url": "https://openrouter.ai/api/v1",
                           "api_key": "sk-live", "models": ["a", "b"]}],
            "active": "or::b", "effort": "minimal"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["model"], "b")
        self.assertEqual(first.json()["effort"], "minimal")
        # the second save sends the masked form back: the secret must survive
        second = self.client.post("/api/assistant/config", json={
            "providers": [{"label": "or", "base_url": "https://openrouter.ai/api/v1",
                           "api_key": "__KEEP__", "models": ["a", "b"]},
                          {"label": "local", "base_url": "http://127.0.0.1:20128/v1",
                           "api_key": "sk-local", "models": ["chat-x"]}]})
        self.assertEqual(second.status_code, 200, second.text)
        body = second.json()
        self.assertTrue(body["providers"][0]["api_key_set"])
        self.assertEqual(len(body["providers"]), 2)
        with open(self.env, encoding="utf-8") as fh:
            self.assertIn("sk-live", fh.read())
        self.assertNotIn("sk-live", json.dumps(body))

    def test_an_active_pick_outside_the_list_falls_back(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        body = self.client.post("/api/assistant/config", json={
            "providers": [{"label": "local", "base_url": "http://127.0.0.1:20128/v1",
                           "api_key": "k", "models": ["chat-x"]}],
            "active": "ghost::model"}).json()
        self.assertEqual(body["model"], "chat-x")

    def test_an_unknown_effort_is_cleared(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        body = self.client.post("/api/assistant/config",
                                json={"effort": "turbo"}).json()
        self.assertEqual(body["effort"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
