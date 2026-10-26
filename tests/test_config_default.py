"""The MIN_AGE_SECONDS default is 30 s (TASK acceptance 4)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import config


class MinAgeDefaultTests(unittest.TestCase):
    def test_default_is_30_when_the_key_is_absent(self):
        saved = os.environ.pop("MIN_AGE_SECONDS", None)
        try:
            self.assertEqual(config.load_config().min_age_seconds, 30)
        finally:
            if saved is not None:
                os.environ["MIN_AGE_SECONDS"] = saved

    def test_enable_flags_default_to_on(self):
        """Q1: absent flag keys → every section is enabled, so an untouched
        .env runs exactly as before."""
        names = ("GEMINI_POOL_ENABLED", "NINEROUTER_ENABLED",
                 "REVISION_PROVIDER_ENABLED", "SUPABASE_ENABLED")
        saved = {n: os.environ.pop(n, None) for n in names}
        try:
            cfg = config.load_config()
            self.assertTrue(cfg.gemini_pool_enabled)
            self.assertTrue(cfg.ninerouter_enabled)
            self.assertTrue(cfg.revision_provider_enabled)
            self.assertTrue(cfg.supabase_enabled)
        finally:
            for n, v in saved.items():
                if v is not None:
                    os.environ[n] = v

    def test_enable_flags_read_the_off_spellings(self):
        os.environ["GEMINI_POOL_ENABLED"] = "0"
        os.environ["NINEROUTER_ENABLED"] = "false"
        try:
            cfg = config.load_config()
            self.assertFalse(cfg.gemini_pool_enabled)
            self.assertFalse(cfg.ninerouter_enabled)
        finally:
            os.environ.pop("GEMINI_POOL_ENABLED", None)
            os.environ.pop("NINEROUTER_ENABLED", None)

    def test_env_example_documents_30(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".env.example")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("MIN_AGE_SECONDS=30", fh.read())


class LaneModelToggleTests(unittest.TestCase):
    """_lane_model — a provider's own models_enabled toggle steers the lane."""

    def test_no_toggle_keeps_the_configured_model(self):
        p = {"model": "m-b", "models": ["m-a", "m-b"], "models_enabled": None}
        self.assertEqual(config._lane_model(p), "m-b")

    def test_disabled_configured_model_falls_back_to_first_enabled(self):
        p = {"model": "m-b", "models": ["m-a", "m-b", "m-c"],
             "models_enabled": ["m-a", "m-c"]}
        self.assertEqual(config._lane_model(p), "m-a")

    def test_enabled_configured_model_is_kept(self):
        p = {"model": "m-c", "models": ["m-a", "m-c"],
             "models_enabled": ["m-a", "m-c"]}
        self.assertEqual(config._lane_model(p), "m-c")

    def test_all_disabled_keeps_the_configured_model_as_last_resort(self):
        p = {"model": "m-b", "models": ["m-a", "m-b"], "models_enabled": []}
        self.assertEqual(config._lane_model(p), "m-b")

    def test_empty_provider_resolves_to_empty_string(self):
        self.assertEqual(config._lane_model({}), "")


if __name__ == "__main__":
    unittest.main()

