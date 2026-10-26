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


class ProxiesForTests(unittest.TestCase):
    """Bucket traffic must ride the active proxy profile.

    The storage host is exit-blocked on this network, so a direct connection
    never completes a TLS handshake and every crop renders as a broken image.
    """

    def _cfg(self, active, profiles):
        return SimpleNamespace(supabase_url="http://sb", supabase_key="k",
                               supabase_bucket="b", proxy_active=active,
                               proxy_profiles=profiles)

    PROFILES = (
        {"name": "Direct", "host": "127.0.0.1", "port": "1",
         "scheme": "http", "enabled": True},
        {"name": "V2ray/pattM", "host": "127.0.0.1", "port": "10808",
         "scheme": "http", "enabled": True},
    )

    def test_active_profile_becomes_both_proxies(self):
        px = storage.proxies_for(self._cfg("V2ray/pattM", self.PROFILES))
        self.assertEqual(px, {"http": "http://127.0.0.1:10808",
                              "https": "http://127.0.0.1:10808"})

    def test_direct_means_no_proxy(self):
        self.assertIsNone(storage.proxies_for(self._cfg("", self.PROFILES)))

    def test_unknown_or_missing_profile_falls_back_to_direct(self):
        self.assertIsNone(storage.proxies_for(self._cfg("gone", self.PROFILES)))
        self.assertIsNone(storage.proxies_for(
            SimpleNamespace(supabase_url="http://sb", proxy_active="x")))

    def test_credentials_are_url_quoted(self):
        profiles = ({"name": "p", "host": "10.0.0.1", "port": "8080",
                     "scheme": "http", "user": "a b", "password": "p@ss"},)
        px = storage.proxies_for(self._cfg("p", profiles))
        self.assertEqual(px["https"], "http://a%20b:p%40ss@10.0.0.1:8080")

    def _patch(self, response):
        return mock.patch.object(storage.requests, "delete",
                                 return_value=response)

    def test_delete_rides_the_active_lane(self):
        cfg = self._cfg("V2ray/pattM", self.PROFILES)
        with self._patch(_Resp(200)) as delete:
            storage.delete_file(cfg, "x.jpg")
        self.assertEqual(delete.call_args[1]["proxies"]["https"],
                         "http://127.0.0.1:10808")

    def test_list_rides_the_active_lane(self):
        cfg = self._cfg("V2ray/pattM", self.PROFILES)
        with mock.patch.object(storage.requests, "post",
                               return_value=_Resp(200)) as post:
            post.return_value.json = lambda: []
            storage.list_objects(cfg, "crops/", 5, 0)
        self.assertEqual(post.call_args[1]["proxies"]["https"],
                         "http://127.0.0.1:10808")


if __name__ == "__main__":
    unittest.main()
