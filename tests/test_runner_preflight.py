"""Q1 run pre-flight: a run whose ACTIVE provider section is switched off is
blocked, with the flag named in the error. The database switches never gate a
run (presentation-only) — the pre-flight still requires SUPABASE_SERVICE_KEY.

No network, no database: missing_credentials() is a pure function of Config.

Run it either way:
    python tests/test_runner_preflight.py
    pytest tests/test_runner_preflight.py
"""
import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.runner import missing_credentials          # noqa: E402
from tests.test_gemini_router import make_cfg            # noqa: E402


def _cfg(**kw):
    """A Gemini-pool Config with a Supabase key, so only the flag under test
    can produce a missing entry."""
    keys = kw.pop("keys", ("k1111111111",))
    return replace(make_cfg(keys=keys), supabase_key="svc-key", **kw)


class PreflightGateTests(unittest.TestCase):
    def test_defaults_pass_when_flags_are_absent(self):
        self.assertEqual(missing_credentials(_cfg()), [])

    def test_disabled_gemini_pool_blocks_run(self):
        missing = missing_credentials(_cfg(gemini_pool_enabled=False))
        self.assertTrue(any("GEMINI_POOL_ENABLED" in m for m in missing), missing)

    def test_disabled_ninerouter_blocks_run(self):
        cfg = _cfg(keys=(), ocr_provider="custom",
                   ocr_base_url="http://127.0.0.1:20128/v1",
                   ninerouter_enabled=False)
        missing = missing_credentials(cfg)
        self.assertTrue(any("NINEROUTER_ENABLED" in m for m in missing), missing)

    def test_disabled_revision_provider_blocks_run(self):
        cfg = _cfg(revision_active="9router", revision_provider_enabled=False)
        missing = missing_credentials(cfg)
        self.assertTrue(any("REVISION_PROVIDER_ENABLED" in m for m in missing),
                        missing)

    def test_supabase_disabled_does_not_block(self):
        self.assertEqual(missing_credentials(_cfg(supabase_enabled=False)), [])


if __name__ == "__main__":
    unittest.main()
