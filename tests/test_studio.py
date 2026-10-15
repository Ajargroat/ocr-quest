"""Phase-1 tests for the OCR Studio subsystem.

Offline and DB-free: the studio works on plain files, so these tests exercise
workspace I/O, the probe, the profile merge and the FastAPI endpoints with a
real, tiny PDF built in-process. No network, no Postgres, no Gemini.

Run either way:
    python tests/test_studio.py
    pytest tests/test_studio.py
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.studio import profile as profile_mod  # noqa: E402
from pipeline.studio import probe as probe_mod  # noqa: E402
from pipeline.studio import tags as tags_mod  # noqa: E402
from pipeline.studio import workspace  # noqa: E402


def _tiny_pdf(path):
    """A 1-page PDF with a real text layer, built with pypdfium2's writer if
    available; otherwise a hand-written minimal PDF with a text object."""
    # A minimal, valid PDF with one page and a text run. Content stream draws
    # "HELLO" so the probe sees a text layer (>= TEXT_PAGE_MIN_CHARS chars it
    # will not reach with one word — so we repeat it).
    text = ("BT /F1 24 Tf 72 700 Td (HELLO WORLD HELLO WORLD HELLO WORLD) Tj ET "
            "BT /F1 12 Tf 72 650 Td (more text here to pass the char threshold "
            "and make this page clearly text-selectable for the probe) Tj ET")
    content = text.encode("latin-1")
    objs = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>")
    objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
                + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += (b"trailer\n<< /Size " + str(len(objs) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n")
    with open(path, "wb") as fh:
        fh.write(out)
    return path


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-ws-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def test_id_validation_blocks_traversal(self):
        self.assertFalse(workspace.valid_id(".."))
        self.assertFalse(workspace.valid_id("a/b"))
        self.assertFalse(workspace.valid_id(""))
        self.assertFalse(workspace.valid_id("a" * 65))
        self.assertTrue(workspace.valid_id(workspace.new_id()))

    def test_job_dir_refuses_bad_id(self):
        with self.assertRaises(ValueError):
            workspace.job_dir("../etc")

    def test_artifact_roundtrip_is_atomic(self):
        jid = workspace.new_id()
        workspace.write_artifact(jid, "profile", {"a": 1, "فارسی": "متن"})
        self.assertEqual(workspace.read_artifact(jid, "profile")["a"], 1)
        # no stray .tmp left behind
        self.assertFalse(os.path.exists(
            os.path.join(workspace.job_dir(jid), "profile.json.tmp")))

    def test_save_source_and_summary(self):
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 fake"), "exam.pdf", "exam.pdf")
        self.assertTrue(workspace.source_path(jid).endswith("source.pdf"))
        summary = workspace.summary(jid)
        self.assertEqual(summary["name"], "exam.pdf")
        self.assertTrue(summary["has_source"])
        self.assertFalse(summary["artifacts"]["profile"])

    def test_save_source_rejects_unknown_ext(self):
        jid = workspace.new_id()
        with self.assertRaises(ValueError):
            workspace.save_source(jid, io.BytesIO(b"x"), "notes.txt", "notes.txt")

    def test_log_appends_and_reads(self):
        jid = workspace.new_id()
        workspace.log_event(jid, "probe", "info", "hello", n=1)
        workspace.log_event(jid, "probe", "success", "done")
        lines = workspace.read_log(jid)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["message"], "hello")
        self.assertEqual(lines[0]["n"], 1)

    def test_list_jobs_newest_first(self):
        a = workspace.new_id()
        b = workspace.new_id()
        workspace.save_source(a, io.BytesIO(b"%PDF-1.4 a"), "a.pdf")
        workspace.save_source(b, io.BytesIO(b"%PDF-1.4 b"), "b.pdf")
        # force distinct mtimes
        os.utime(workspace.job_dir(a), (1000, 1000))
        os.utime(workspace.job_dir(b), (2000, 2000))
        ids = [j["id"] for j in workspace.list_jobs()]
        self.assertEqual(ids[0], b)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="studio-probe-")
        self.pdf = os.path.join(self.tmp, "t.pdf")
        _tiny_pdf(self.pdf)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_reports_a_text_pdf(self):
        report = probe_mod.probe(self.pdf)
        self.assertTrue(report["ok"])
        self.assertEqual(report["page_count"], 1)
        # the tiny PDF carries a real text layer
        self.assertIn(report["source_kind"], ("text_pdf", "mixed"))
        self.assertGreaterEqual(report["text_pages"], 1)

    def test_probe_missing_file_is_graceful(self):
        report = probe_mod.probe(os.path.join(self.tmp, "nope.pdf"))
        self.assertFalse(report["ok"])
        self.assertIn("error", report)

    def test_probe_image_kind(self):
        img = os.path.join(self.tmp, "p.png")
        with open(img, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
        report = probe_mod.probe(img)
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_kind"], "images")

    def test_suggest_profile_shape(self):
        report = probe_mod.probe(self.pdf)
        suggestion = probe_mod.suggest_profile(report)
        self.assertIn(suggestion["source_kind"], ("text_pdf", "mixed"))
        self.assertIn("static", suggestion)
        self.assertIn("corp", suggestion["extract_per_question"])


class ProfileTests(unittest.TestCase):
    def test_normalize_drops_unknown_enums(self):
        p = profile_mod.normalize({"source_kind": "banana", "compilation": "nope"})
        self.assertEqual(p["source_kind"], "unknown")
        self.assertEqual(p["compilation"], "unknown")

    def test_extract_defaults_to_unpinned_fields(self):
        p = profile_mod.normalize({"static": {"subject": "زیست‌شناسی"}})
        self.assertNotIn("subject", p["extract_per_question"])
        self.assertIn("corp", p["extract_per_question"])

    def test_apply_overrides_merges_static_per_field(self):
        base = profile_mod.normalize({"static": {"subject": "شیمی", "corp": "ماز"}})
        merged = profile_mod.apply_overrides(base, {"static": {"year": "1401"}})
        self.assertEqual(merged["static"]["subject"], "شیمی")
        self.assertEqual(merged["static"]["corp"], "ماز")
        self.assertEqual(merged["static"]["year"], "1401")

    def test_apply_overrides_clears_with_empty_string(self):
        base = profile_mod.normalize({"static": {"subject": "شیمی"}})
        merged = profile_mod.apply_overrides(base, {"static": {"subject": ""}})
        self.assertIsNone(merged["static"]["subject"])

    def test_required_missing(self):
        p = profile_mod.normalize({"static": {"subject": "شیمی"},
                                   "extract_per_question": ["corp", "year"]})
        self.assertEqual(profile_mod.required_missing(p), ["corp", "year"])


class TagTests(unittest.TestCase):
    def test_seed_loads(self):
        data = tags_mod.load()
        self.assertIn("زیست‌شناسی", data["subjects"])
        self.assertTrue(data["corps"])

    def test_corp_folding_merges_variants(self):
        data = tags_mod.load()
        # قلمچی / قلم‌چی / قلم چی all fold to the canonical tag
        for variant in ("قلمچی", "قلم‌چی", "قلم چی"):
            self.assertEqual(tags_mod.match_corp(variant, data), "قلم‌چی")
        # دیار / دیاز / دیاژ
        for variant in ("دیار", "دیاز", "دیاژ"):
            self.assertEqual(tags_mod.match_corp(variant, data), "دیار")

    def test_corp_arabic_forms_fold(self):
        data = tags_mod.load()
        # Arabic yeh/kaf spellings fold to the Persian canonical
        self.assertEqual(tags_mod.match_corp("قلمچی", data), "قلم‌چی")

    def test_unknown_corp_returns_none(self):
        self.assertIsNone(tags_mod.match_corp("مؤسسه ناشناخته", tags_mod.load()))

    def test_match_subject(self):
        self.assertEqual(tags_mod.match_subject("زیست‌شناسی", tags_mod.load()),
                         "زیست‌شناسی")

    def test_topics_for_filters(self):
        topics = tags_mod.topics_for("شیمی", grade="دهم", chapter=1)
        self.assertTrue(any("آب" in t for t in topics))

    def test_vocab_for_prompt_is_compact(self):
        vocab = tags_mod.vocab_for_prompt()
        self.assertIn("subjects", vocab)
        self.assertIn("corps", vocab)
        # corps in the prompt are canonical tags, not the variant table
        self.assertIn("قلم‌چی", vocab["corps"])
        self.assertNotIn("قلمچی", vocab["corps"])


class StudioTagsValidateTests(unittest.TestCase):
    def test_valid_seed_passes(self):
        self.assertEqual(tags_mod.validate(tags_mod.load()), [])

    def test_bad_shapes_fail(self):
        self.assertTrue(tags_mod.validate(["x"]))
        self.assertTrue(tags_mod.validate({"subjects": "nope"}))
        self.assertTrue(tags_mod.validate({"subjects": [], "corps": "x"}))
        self.assertTrue(tags_mod.validate({"subjects": [], "corps": [{"tag": ""}]}))
        self.assertTrue(tags_mod.validate({"subjects": [], "corps": [],
                                           "chapters": [{"topics": "x"}]}))
        self.assertTrue(tags_mod.validate(
            {"subjects": ["s"], "corps": [{"tag": "c", "variants": [""]}],
             "chapters": []}))

    def test_normalize_strips_and_dedupes(self):
        clean = tags_mod.normalize(
            {"subjects": [" a ", "a", ""],
             "corps": [{"tag": " x ", "variants": ["y", " y ", ""]}],
             "chapters": []})
        self.assertEqual(clean["subjects"], ["a"])
        self.assertEqual(clean["corps"], [{"tag": "x", "variants": ["y"]}])

    def test_save_load_roundtrip_to_tmp(self):
        tmp = tempfile.mkdtemp(prefix="studio-tags-")
        try:
            path = os.path.join(tmp, "tags.json")
            doc = {"version": 1, "subjects": ["s"],
                   "corps": [{"tag": "c", "variants": ["v"]}], "chapters": []}
            tags_mod.save(doc, path)
            self.assertEqual(tags_mod.load(path)["subjects"], ["s"])
            self.assertFalse(os.path.exists(path + ".tmp"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

class StudioEndpointTests(unittest.TestCase):
    """Exercise the FastAPI routes with a temp workspace (offline)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-ep-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self.tmp = tempfile.mkdtemp(prefix="studio-src-")
        self.pdf = os.path.join(self.tmp, "exam.pdf")
        _tiny_pdf(self.pdf)

        import main  # noqa: E402  (offline-safe import)
        self.main = main
        self.client = self._client()

    def _client(self):
        try:
            from fastapi.testclient import TestClient
        except Exception:
            return None
        return TestClient(self.main.app)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_jobs_endpoints_roundtrip(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        with open(self.pdf, "rb") as fh:
            res = self.client.post("/api/studio/jobs",
                                   files={"file": ("exam.pdf", fh, "application/pdf")})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        job_id = body["job"]["id"]
        self.assertTrue(body["probe"]["ok"])
        self.assertIn("profile", body)

        # list
        jobs = self.client.get("/api/studio/jobs").json()["jobs"]
        self.assertIn(job_id, [j["id"] for j in jobs])

        # fetch one
        one = self.client.get("/api/studio/jobs/" + job_id).json()
        self.assertTrue(one["ok"])
        self.assertIn("profile", one)

        # save profile
        res = self.client.post(
            "/api/studio/jobs/" + job_id + "/profile",
            json={"static": {"subject": "زیست‌شناسی"}, "compilation": "single_institute_multi_year"})
        self.assertEqual(res.status_code, 200, res.text)
        saved = res.json()["profile"]
        self.assertEqual(saved["static"]["subject"], "زیست‌شناسی")
        self.assertEqual(saved["compilation"], "single_institute_multi_year")

        # tags
        tags = self.client.get("/api/studio/tags").json()
        self.assertTrue(tags["ok"])
        self.assertIn("زیست‌شناسی", tags["tags"]["subjects"])

        # delete
        res = self.client.delete("/api/studio/jobs/" + job_id)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/api/studio/jobs/" + job_id).status_code, 404)

    def test_missing_job_is_404(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        self.assertEqual(self.client.get("/api/studio/jobs/does-not-exist").status_code, 404)

    def test_tags_save_roundtrip(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        # Invalid first: rejected, nothing is written.
        res = self.client.post("/api/studio/tags", json={"tags": {"subjects": "nope"}})
        self.assertEqual(res.status_code, 400)
        # Valid: the live seed round-trips through a PATCHED path so the
        # shipped seed.json is never touched by the suite.
        tmp = tempfile.mkdtemp(prefix="studio-seed-")
        try:
            seed = os.path.join(tmp, "seed.json")
            current = self.client.get("/api/studio/tags").json()["tags"]
            old = tags_mod.SEED_PATH
            tags_mod.SEED_PATH = seed
            try:
                res = self.client.post("/api/studio/tags", json={"tags": current})
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(tags_mod.load(seed)["subjects"], current["subjects"])
            finally:
                tags_mod.SEED_PATH = old
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_models_endpoints_reject_bad_values_without_writing(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        view = self.client.get("/api/studio/models").json()
        self.assertTrue(view["ok"])
        self.assertIn("providers", view)
        self.assertIn("extract_active", view)
        self.assertIn("enrich_active", view)
        self.assertIn("proxy", view)
        res = self.client.post("/api/studio/models",
                               json={"providers": [], "extract_active": "nope"})
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/studio/models", json={"proxy": "no-such-profile"})
        self.assertEqual(res.status_code, 400)

    def test_models_save_persists_to_env(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        import main  # noqa: E402  (same offline-safe module as setUp)
        saved_env = dict(os.environ)
        fd, env_path = tempfile.mkstemp(suffix=".env", prefix="studio-models-")
        os.close(fd)
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("# temp env for studio models test\n")
        old_path, old_cfg = main.ENV_PATH, main.cfg
        main.ENV_PATH = env_path
        try:
            main.cfg = main.load_config()
            res = self.client.post("/api/studio/models",
                                   json={"providers": [], "extract_active": "",
                                         "enrich_active": "", "proxy": ""})
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertTrue(body["saved"])
            with open(env_path, encoding="utf-8") as fh:
                env_text = fh.read()
            self.assertIn("STUDIO_PROVIDERS", env_text)
            self.assertIn("STUDIO_PROXY", env_text)
        finally:
            main.ENV_PATH = old_path
            os.environ.clear()
            os.environ.update(saved_env)
            try:
                os.unlink(env_path)
            except OSError:
                pass
            main.cfg = old_cfg


if __name__ == "__main__":
    unittest.main(verbosity=2)
