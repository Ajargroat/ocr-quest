"""Offline tests for storage.delete_file (B4) — no network, no bucket.

The Supabase storage delete posts the object prefixes as JSON; a 404 means the
object is already gone and is not an error.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import storage


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class DeleteFileTests(unittest.TestCase):
    def _cfg(self):
        return SimpleNamespace(supabase_url="http://sb", supabase_key="k",
                               supabase_bucket="b")

    def _patch(self, response):
        return mock.patch.object(storage.requests, "delete",
                                 return_value=response)

    def test_delete_posts_the_object_path_as_a_prefix(self):
        with self._patch(_Resp(200)) as delete:
            self.assertTrue(storage.delete_file(self._cfg(), "crops/f/x.jpg"))
        self.assertEqual(delete.call_args[0][0],
                         "http://sb/storage/v1/object/b")
        self.assertEqual(delete.call_args[1]["json"],
                         {"prefixes": ["crops/f/x.jpg"]})
        self.assertEqual(delete.call_args[1]["headers"]["apikey"], "k")

    def test_missing_object_is_not_an_error(self):
        with self._patch(_Resp(404)):
            self.assertFalse(storage.delete_file(self._cfg(), "x.jpg"))

    def test_http_error_raises(self):
        with self._patch(_Resp(400)):
            with self.assertRaises(RuntimeError):
                storage.delete_file(self._cfg(), "x.jpg")


if __name__ == "__main__":
    unittest.main()
