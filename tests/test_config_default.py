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

    def test_env_example_documents_30(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".env.example")
        with open(path, encoding="utf-8") as fh:
            self.assertIn("MIN_AGE_SECONDS=30", fh.read())


if __name__ == "__main__":
    unittest.main()
