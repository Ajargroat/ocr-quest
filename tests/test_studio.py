"""Phase-1 tests for the OCR Studio subsystem.

Offline and DB-free: the studio works on plain files, so these tests exercise
workspace I/O, the probe, the profile merge and the FastAPI endpoints with a
real, tiny PDF built in-process. No network, no Postgres, no Gemini.

Run either way:
    python tests/test_studio.py
    pytest tests/test_studio.py
"""
import io
import json
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
        self.src = tempfile.mkdtemp(prefix="studio-ws-src-")
        self._old = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_SOURCE_ROOT"] = self.src

    def tearDown(self):
        if self._old is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

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
        self.assertTrue(workspace.source_path(jid).endswith("exam.pdf"))
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


class StudioSourceTreeTests(unittest.TestCase):
    """B2 — the source lands once in the folder tree, not the job dir (Q8)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-tree-")
        self.src = tempfile.mkdtemp(prefix="studio-tree-src-")
        self._old_ws = os.environ.get("STUDIO_WORKSPACE")
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_WORKSPACE"] = self.root
        os.environ["STUDIO_SOURCE_ROOT"] = self.src

    def tearDown(self):
        if self._old_ws is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old_ws
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def test_save_source_writes_only_into_the_source_root(self):
        jid = workspace.new_id()
        stored = workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"),
                                       "exam.pdf", "exam.pdf")
        self.assertTrue(stored.startswith(self.src))
        self.assertTrue(os.path.isfile(os.path.join(self.src, jid, "exam.pdf")))
        leftovers = [n for n in os.listdir(workspace.job_dir(jid))
                     if n.startswith("source.")]
        self.assertEqual(leftovers, [])
        self.assertEqual(workspace.source_path(jid), stored)

    def test_source_meta_carries_rel_path_and_folder_id(self):
        from pipeline.utils import sha1_hex
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        meta = workspace.read_artifact(jid, "source_meta")
        self.assertEqual(meta["rel_path"], jid + "/exam.pdf")
        self.assertEqual(meta["folder_id"], sha1_hex(jid + "/exam.pdf"))
        self.assertEqual(meta["name"], "exam.pdf")


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

    def test_answer_key_heuristic_keeps_option_pages(self):
        # A real question page: four "۱)" option lines per stem, no key grid.
        page = ("۱- داده‌های جدول زیر مربوط به گاز آمونیاک حاصل از فرایند هابر "
                "است. سرعت متوسط مصرف هیدروژن کدام مقدار می‌تواند باشد؟\n"
                "۱) ۲/۶\n۲) ۲/۴\n۳) ۲/۸\n۴) ۳/۶\n"
                "۲- گرمای حاصل از سوختن کامل این گاز چند کیلوژول است؟\n"
                "۱) ۱۰۰\n۲) ۷۵\n۳) ۲۵\n۴) ۵۰\n")
        self.assertFalse(probe_mod._looks_like_answer_key(page))

    def test_answer_key_heuristic_reads_a_real_grid(self):
        grid = "\n".join("%d %d" % (i, (i % 4) + 1) for i in range(1, 21))
        self.assertTrue(probe_mod._looks_like_answer_key(grid))
        # The hint word alone still wins, sentence lines or not.
        self.assertTrue(probe_mod._looks_like_answer_key(
            "پاسخ کلیدی آزمون\n" + grid))


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
        # Every listed variant folds to whatever canonical the list declares
        # (the Tags panel owns the wording, so this test must not pin it).
        groups = (("قلمچی",
                   ("قلمچی", "قلم‌چی", "قلم چی")),
                  ("دیاز",
                   ("دیار", "دیاز", "دیاژ")))
        for variant, spellings in groups:
            canon = tags_mod.match_corp(variant, data)
            self.assertTrue(canon, variant)
            for spelling in spellings:
                self.assertEqual(tags_mod.match_corp(spelling, data), canon,
                                 spelling)

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
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_SOURCE_ROOT"] = self.tmp
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
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
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
        # load_config() reads os.environ, which the real .env populated at
        # import — scrub every STUDIO_* leak so this test runs pristine.
        for k in [k for k in os.environ if k.startswith("STUDIO_")]:
            os.environ.pop(k, None)
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

    def test_settings_save_persists_the_request_batching(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        import main  # noqa: E402
        saved_env = dict(os.environ)
        fd, env_path = tempfile.mkstemp(suffix=".env", prefix="studio-batch-")
        os.close(fd)
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("# temp env for studio batching test\n")
        for k in [k for k in os.environ if k.startswith("STUDIO_")]:
            os.environ.pop(k, None)
        old_path, old_cfg = main.ENV_PATH, main.cfg
        main.ENV_PATH = env_path
        try:
            main.cfg = main.load_config()
            res = self.client.post("/api/studio/settings",
                                   json={"pages_per_call": 5,
                                         "band_batch": 4})
            self.assertEqual(res.status_code, 200, res.text)
            body = res.json()
            self.assertEqual(body["pages_per_call"], 5)
            self.assertEqual(body["band_batch"], 4)
            main.cfg = main.load_config()
            self.assertEqual(main.cfg.studio_pages_per_call, 5)
            self.assertEqual(main.cfg.studio_chunk_images, 5)
            self.assertEqual(main.cfg.studio_band_batch, 4)
            with open(env_path, encoding="utf-8") as fh:
                env_text = fh.read()
            self.assertIn("STUDIO_PAGES_PER_CALL=5", env_text)
            self.assertIn("STUDIO_BAND_BATCH=4", env_text)
            # Out-of-range values clamp instead of reaching the lane.
            res = self.client.post("/api/studio/settings",
                                   json={"pages_per_call": 99,
                                         "band_batch": 0})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(res.json()["pages_per_call"], 8)
            self.assertEqual(res.json()["band_batch"], 1)
        finally:
            main.ENV_PATH = old_path
            os.environ.clear()
            os.environ.update(saved_env)
            try:
                os.unlink(env_path)
            except OSError:
                pass
            main.cfg = old_cfg

    def test_stage_route_chain_extract_enrich_review_import(self):
        """The UI's exact call sequence: extract -> enrich -> review -> import."""
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        from pipeline.studio import llm
        old = llm.CALL_OVERRIDE
        llm.CALL_OVERRIDE = _echo_stub
        try:
            with open(self.pdf, "rb") as fh:
                res = self.client.post(
                    "/api/studio/jobs",
                    files={"file": ("exam.pdf", fh, "application/pdf")})
            job_id = res.json()["job"]["id"]

            res = self.client.post("/api/studio/jobs/%s/extract" % job_id)
            self.assertTrue(res.json()["ok"], res.text)
            res = self.client.post("/api/studio/jobs/%s/enrich" % job_id)
            self.assertTrue(res.json()["ok"], res.text)

            bulk = self.client.get("/api/studio/jobs/%s/bulk" % job_id).json()
            self.assertTrue(bulk["ok"], bulk)
            blocks = bulk["view"]["blocks"]
            self.assertTrue(blocks)
            key = blocks[0]["key"]
            self.assertEqual(bulk["targets"][0]["id"], "supabase")

            # review: promote the page's prose to a question, fill the fields
            res = self.client.post("/api/studio/jobs/%s/review" % job_id, json={
                "edits": {key: {"kind": "question", "number": 1,
                                "text": "HELLO WORLD",
                                "options": ["A) one", "B) two"],
                                "subject": "S", "topic": "T", "corp": "C",
                                "year": "1402", "difficulty": "D",
                                "grade": "G"}},
                "crops": {}})
            self.assertTrue(res.json()["ok"], res.text)
            blocks = self.client.get(
                "/api/studio/jobs/%s/bulk" % job_id).json()["view"]["blocks"]
            self.assertEqual(blocks[0]["kind"], "question")
            self.assertEqual(blocks[0]["text"], "HELLO WORLD")
            self.assertEqual(blocks[0]["status"], "ready")

            dry = self.client.post("/api/studio/jobs/%s/import" % job_id,
                                   json={"target": "file",
                                         "dry_run": True}).json()
            self.assertTrue(dry["ok"], dry)
            self.assertEqual(dry["report"]["questions"], 1)
            self.assertEqual(dry["report"]["answers"], 0)
            self.assertFalse(os.path.exists(os.path.join(
                workspace.job_dir(job_id), "export.json")))

            real = self.client.post("/api/studio/jobs/%s/import" % job_id,
                                    json={"target": "file",
                                          "dry_run": False}).json()
            self.assertTrue(real["ok"], real)
            self.assertTrue(os.path.exists(os.path.join(
                workspace.job_dir(job_id), "export.json")))
        finally:
            llm.CALL_OVERRIDE = old

    def _crop_job(self):
        """A job with one approved text-layer crop, ready for the re-crop route."""
        jid = workspace.new_id()
        with open(self.pdf, "rb") as fh:
            workspace.save_source(jid, fh, os.path.basename(self.pdf))
        block = _blk("p1-b1", "question", number=1, text="متن سؤال",
                     bbox=[10, 10, 50, 90], bbox_source="text_layer")
        workspace.write_artifact(jid, "layer2", {
            "version": 1, "job_id": jid, "profile": profile_mod.blank(),
            "blocks": [block], "pages": [], "assets": [], "stats": {}})
        crop = workspace.crop_path(jid, "p1-b1")
        with open(crop, "wb") as fh:
            fh.write(b"jpeg")
        workspace.write_artifact(jid, "geometry", {
            "version": 1, "job_id": jid, "stats": {}, "boxes": {"p1-b1": {
                "bbox": [10, 10, 50, 90], "source": "text_layer",
                "crop_bbox": [10, 10, 50, 90], "crop_page": 1,
                "crop_method": "text", "crop_image": None,
                "crop_approved": True, "crop": None, "crop_bytes": None,
                "spec": "old"}}})
        return jid

    def test_re_crop_endpoint_uploads_the_same_object(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        from unittest import mock
        from pipeline.studio import geometry as geometry_mod
        jid = self._crop_job()
        meta = workspace.read_artifact(jid, "source_meta")
        obj = "crops/%s/p1-b1.jpg" % meta["folder_id"]
        workspace.write_artifact(jid, "imports", {
            "version": 1, "uploads": {"source": None, "crops": {
                "p1-b1": {"object": obj, "url": "u0", "spec": "old"}}}})
        entry = {"spec": "new", "crop_bbox": [1, 2, 3, 4], "crop_page": 1}
        with mock.patch.object(geometry_mod, "render_crop",
                               return_value=entry), \
                mock.patch.object(self.main.storage, "upload_file",
                                  return_value="https://cdn/new.jpg") as up, \
                mock.patch.object(self.main.storage, "delete_file",
                                  return_value=True) as dl:
            res = self.client.post("/api/studio/jobs/" + jid + "/crop/p1-b1",
                                   json={"bbox": [1, 2, 3, 4]})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["object"], obj)
        dl.assert_called_once_with(self.main.cfg, obj)
        self.assertEqual(up.call_args[0][1], obj)
        imports = workspace.read_artifact(jid, "imports")
        self.assertEqual(imports["uploads"]["crops"]["p1-b1"]["object"], obj)
        self.assertEqual(imports["uploads"]["crops"]["p1-b1"]["spec"], "new")

    def test_re_crop_unknown_job_is_404(self):
        if self.client is None:
            self.skipTest("fastapi TestClient unavailable")
        res = self.client.post("/api/studio/jobs/does-not-exist/crop/p1-b1",
                               json={"bbox": [1, 2, 3, 4]})
        self.assertEqual(res.status_code, 404)

# ── Stage 1–4 · extract / enrich / geometry / import (all offline) ──────────

def _write_pdf(path, objs):
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


def _jpeg(w, h, color):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _figure_pdf(path):
    """One text page — marker, four options, a metadata line, one figure."""
    content = (
        "q 200 0 0 200 300 400 cm /Im1 Do Q\n"
        "BT /F1 12 Tf 40 800 Td (1\\) Which option matches the figure?) Tj ET\n"
        "BT /F1 12 Tf 40 770 Td (A\\) first option) Tj ET\n"
        "BT /F1 12 Tf 40 750 Td (B\\) second option) Tj ET\n"
        "BT /F1 12 Tf 40 730 Td (C\\) third option) Tj ET\n"
        "BT /F1 12 Tf 40 710 Td (D\\) fourth option) Tj ET\n"
        "BT /F1 10 Tf 40 660 Td (\\(Sanjesh 1402 average\\)) Tj ET"
    ).encode("latin-1")
    jpg = _jpeg(24, 24, (200, 30, 30))
    return _write_pdf(path, [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 6 0 R >> /XObject << /Im1 5 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 24 /Height 24 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(jpg)).encode() + b" >>\nstream\n" + jpg + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"])


def _image_pdf(path):
    """One page with no text layer — the whole page is one scan image."""
    scan = _jpeg(600, 800, (240, 240, 240))
    content = b"q 595 0 0 842 0 0 cm /Im1 Do Q"
    return _write_pdf(path, [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 600 /Height 800 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(scan)).encode() + b" >>\nstream\n" + scan + b"\nendstream"])


def _image_pdf2(path):
    """Two pages with no text layer — two scan images in one request."""
    scan = _jpeg(600, 800, (238, 238, 238))
    content = b"q 595 0 0 842 0 0 cm /Im1 Do Q"
    return _write_pdf(path, [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 600 /Height 800 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(scan)).encode() + b" >>\nstream\n" + scan + b"\nendstream",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /XObject << /Im2 8 0 R >> >> /Contents 7 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
        + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 600 /Height 800 "
        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
        + str(len(scan)).encode() + b" >>\nstream\n" + scan + b"\nendstream"])


class _StudioStageCase(unittest.TestCase):
    """Workspace-per-test + a stubbed model lane (never a network call)."""

    def setUp(self):
        from pipeline.studio import llm
        self.llm = llm
        self.root = tempfile.mkdtemp(prefix="studio-stage-")
        self.src = tempfile.mkdtemp(prefix="studio-stage-src-")
        self._old_ws = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_SOURCE_ROOT"] = self.src
        self._old_override = llm.CALL_OVERRIDE

    def tearDown(self):
        self.llm.CALL_OVERRIDE = self._old_override
        if self._old_ws is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old_ws
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def _job(self, path):
        jid = workspace.new_id()
        with open(path, "rb") as fh:
            workspace.save_source(jid, fh, os.path.basename(path))
        return jid


class StudioChunkerTests(unittest.TestCase):
    """page-aligned request windows (Q10) — pure, no PDF and no model."""

    def test_windows_stay_page_aligned_under_the_char_budget(self):
        from pipeline.studio import chunker
        windows = chunker.chunk_pages([900, 900, 900], max_chars=2000)
        self.assertEqual([w["pages"] for w in windows], [[1, 2], [3]])
        self.assertEqual([w["index"] for w in windows], [1, 2])
        self.assertEqual([w["chunks"] for w in windows], [2, 2])

    def test_an_oversized_page_is_its_own_window(self):
        from pipeline.studio import chunker
        windows = chunker.chunk_pages([10, 99999, 10], max_chars=100)
        self.assertEqual([w["pages"] for w in windows], [[1], [2], [3]])

    def test_the_image_cap_bounds_text_free_pages(self):
        from pipeline.studio import chunker
        windows = chunker.chunk_pages([0] * 7, max_pages=3)
        self.assertEqual([len(w["pages"]) for w in windows], [3, 3, 1])

    def test_every_page_appears_once_in_order(self):
        from pipeline.studio import chunker
        windows = chunker.chunk_pages(list(range(1, 41)), max_chars=2000,
                                      max_pages=4)
        pages = [page for window in windows for page in window["pages"]]
        self.assertEqual(pages, list(range(1, 41)))


class StudioLayoutTests(_StudioStageCase):
    def test_split_text_reads_persian_questions_and_metadata(self):
        from pipeline.studio import layout
        text = "\n".join([
            "۱) کدام گزینه دربارهٔ یاخته صحیح است؟",
            "الف) گزینهٔ نخست",
            "ب) گزینهٔ دوم",
            "ج) گزینهٔ سوم",
            "د) گزینهٔ چهارم",
            "(سنجش ۱۴۰۲ داخل - متوسط)",
        ])
        blocks = layout.split_text(text)
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block["kind"], "question")
        self.assertEqual(block["number"], 1)
        self.assertEqual(len(block["options"]), 4)
        self.assertEqual(block["options"][0]["label"], "الف")
        self.assertEqual(block["metadata_line"], "سنجش ۱۴۰۲ داخل - متوسط")
        meta = layout.metadata_fields(block["metadata_line"])
        self.assertEqual(meta["year"], "1402")
        self.assertEqual(meta["difficulty"], "متوسط")
        self.assertIn("سنجش", meta["corp"] or "")

    def test_key_page_entries_read_number_label_pairs(self):
        from pipeline.studio import layout
        entries = layout.key_entries("۱ ۲   ۲ ۳   ۳ ۱   ۴ ۴")
        self.assertEqual([(e["number"], e["label"]) for e in entries],
                         [("1", "2"), ("2", "3"), ("3", "1"), ("4", "4")])

    def test_a_stems_own_numbered_list_is_not_its_options(self):
        """A stem that lists "۱) …" items keeps them as text; options are the
        second, ascending run (the real answer choices)."""
        from pipeline.studio import layout
        text = "\n".join([
            "۶۷- گرمای واکنش را حساب کنید؟",
            "(۱) A → B",
            "(۲) B → C",
            "(۳) C → D",
            "(۱) ۹ - ۱۵۷/۵",
            "(۲) ۱۲ - ۱۵۷/۵",
            "(۳) ۹ - ۱۷۵",
            "(۴) ۱۲ - ۱۷۵",
        ])
        blocks = layout.split_text(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual([o["text"] for o in blocks[0]["options"]],
                         ["۹ - ۱۵۷/۵", "۱۲ - ۱۵۷/۵", "۹ - ۱۷۵", "۱۲ - ۱۷۵"])
        self.assertIn("A → B", blocks[0]["text"])

    def test_options_glued_onto_one_line_split_into_four(self):
        from pipeline.studio import layout
        blocks = layout.split_text("۶۰- کدام است؟\n۱) ۱۰۰۰  ۲) ۲۰۰۰  ۳) ۱۵۰۰  ۴) ۸۰۰")
        self.assertEqual([o["text"] for o in blocks[0]["options"]],
                         ["۱۰۰۰", "۲۰۰۰", "۱۵۰۰", "۸۰۰"])

    def test_a_figure_caption_does_not_break_a_numbered_run(self):
        """ "(الف) (ب)" under a picture is a caption, not option 1."""
        from pipeline.studio import layout
        text = "\n".join([
            "۸۹- کدام مطلب نادرست است؟",
            "(C=۱۲، H=۱: g.mol⁻¹)",
            "۱) یک",
            "۲) دو",
            "۳) سه",
            "(الف) (ب)",
            "۴) چهار",
        ])
        blocks = layout.split_text(text)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(blocks[0]["options"]), 4)
        self.assertIn("(الف) (ب)", blocks[0]["options"][2]["text"])

    def test_an_option_orphan_folds_back_into_its_question(self):
        from pipeline.studio import layout
        question = {"kind": "question", "number": 61, "text": "متن سؤال",
                    "options": [], "metadata_line": None, "ranges": []}
        orphan = {"kind": "question", "number": 3, "text": "(C=12)",
                  "options": [{"label": "۱", "text": "۱۰"}],
                  "metadata_line": None, "ranges": []}
        out = layout._absorb_option_orphans([question, orphan])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["options"][0]["text"], "۱۰")
        self.assertIn("(C=12)", out[0]["text"])
        # A question that already has its four options keeps the next one.
        full = {"kind": "question", "number": 61, "text": "متن",
                "options": [{"label": str(i), "text": "x"} for i in range(1, 5)],
                "metadata_line": None, "ranges": []}
        self.assertEqual(len(layout._absorb_option_orphans([full, dict(orphan)])), 2)

    def test_split_markers_are_stripped_and_returned(self):
        from pipeline.studio import layout
        self.assertEqual(layout.parse_split("۱) الف\nSPLIT: tail"),
                         ("۱) الف", "tail"))
        self.assertEqual(layout.parse_split("SPLIT: head\n۱) الف")[1], "head")
        self.assertEqual(
            layout.parse_split("SPLIT: head\n۱) الف\nSPLIT: tail")[1], "both")
        self.assertEqual(layout.parse_split("۱) الف"), ("۱) الف", None))

    def test_a_page_that_opens_with_an_option_continues_the_previous_one(self):
        from pipeline.studio import layout
        tail = _blk("p1-b1", "question", page=1, text="۱) متن\nالف) یک")
        head = _blk("p2-b1", "prose", page=2, text="ب) دو")
        layout.mark_continuations([tail, head])
        self.assertEqual(tail["split"], "tail")
        self.assertEqual(head["split"], "head")

    def test_join_merges_the_two_halves_and_records_the_join(self):
        from pipeline.studio import layout
        tail = _blk("p1-b1", "question", page=1, text="۱) متن",
                    options=["الف) یک"], split="tail")
        head = _blk("p2-b1", "prose", page=2, text="ادامه",
                    options=["ب) دو"], split="head")
        blocks, joined = layout.join_continuations([tail, head])
        self.assertEqual(joined, 1)
        self.assertEqual(len(blocks), 1)
        merged = blocks[0]
        self.assertEqual(merged["text"], "۱) متن\nادامه")
        self.assertEqual(merged["options"], ["الف) یک", "ب) دو"])
        self.assertIsNone(merged["split"])
        self.assertTrue(merged["split_joined"])
        self.assertEqual(merged["page_end"], 2)

    def test_an_unmatched_half_keeps_its_split_flag(self):
        from pipeline.studio import layout
        lone = _blk("p1-b1", "question", page=1, split="tail")
        blocks, joined = layout.join_continuations([lone])
        self.assertEqual(joined, 0)
        self.assertEqual(blocks[0]["split"], "tail")
        self.assertFalse(blocks[0].get("split_joined"))


class StudioExtractTests(_StudioStageCase):
    def test_text_lane_is_deterministic_and_stores_the_figure_once(self):
        from pipeline.studio import extract
        pdf = _figure_pdf(os.path.join(self.src, "fig.pdf"))
        jid = self._job(pdf)
        out = extract.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(out["stats"]["calls"], 0)   # no model in the text lane
        self.assertEqual(out["stats"]["text_pages"], 1)
        layer = workspace.read_artifact(jid, "layer1")
        questions = [b for b in layer["blocks"] if b["kind"] == "question"]
        self.assertEqual(len(questions), 1)
        q = questions[0]
        self.assertEqual(q["number"], 1)
        self.assertEqual(len(q["options"]), 4)
        self.assertTrue(q["options"][0].startswith("A)"), q["options"][0])
        self.assertEqual(q["bbox_source"], "text_layer")
        self.assertTrue(q["bbox"])
        self.assertEqual(q["year"], "1402")
        self.assertEqual(q["corp"], "Sanjesh average")
        self.assertEqual(q["images"], [layer["assets"][0]["id"]])
        self.assertTrue(os.path.exists(
            workspace.asset_path(jid, layer["assets"][0]["id"])))

    def test_image_lane_keeps_the_same_schema_over_the_raw_ocr(self):
        from pipeline.studio import extract
        self.llm.CALL_OVERRIDE = lambda **kw: {
            "text": "۲) پرسش دومی؟\nالف) یک\nب) دو",
            "parsed": None, "route": {"label": "stub"}, "lane": "stub"}
        pdf = _image_pdf(os.path.join(self.src, "scan.pdf"))
        jid = self._job(pdf)
        out = extract.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(out["stats"]["vision_pages"], 1)
        self.assertEqual(out["stats"]["calls"], 1)
        layer = workspace.read_artifact(jid, "layer1")
        page = layer["pages"][0]
        self.assertEqual(page["method"], "vision")
        image = os.path.join(workspace.job_dir(jid), page["image"])
        from PIL import Image
        with Image.open(image) as im:
            self.assertLessEqual(max(im.size), extract.VISION_LONG_EDGE)
        q = layer["blocks"][0]
        self.assertEqual(q["kind"], "question")
        self.assertEqual(q["number"], 2)
        self.assertEqual(len(q["options"]), 2)
        self.assertIsNone(q["bbox"])                 # geometry's job
        self.assertIn("پرسش", q["raw_ocr_text"])

    def test_the_ocr_prompt_states_the_continuation_contract(self):
        from pipeline.studio import extract
        self.assertIn("SPLIT: tail", extract.OCR_PAGE_PROMPT)
        self.assertIn("SPLIT: head", extract.OCR_PAGE_PROMPT)
        self.assertIn("=== PAGE", extract.OCR_PAGE_PROMPT)

    def test_text_lane_reports_the_chunk_range_on_every_block(self):
        from pipeline.studio import extract
        pdf = _figure_pdf(os.path.join(self.src, "fig.pdf"))
        jid = self._job(pdf)
        events = []
        cfg = type("Cfg", (), {"studio_chunk_chars": 100,
                               "studio_chunk_images": 1})()
        out = extract.run(jid, cfg,
                          emit=lambda *a, **k: events.append((a, k)))
        self.assertEqual(out["stats"]["calls"], 0)   # no model in the text lane
        spans = [k for _, k in events if "chunk" in k]
        self.assertEqual(len(spans), 1)
        self.assertEqual((spans[0]["chunk"], spans[0]["chunks"],
                          spans[0]["page_start"], spans[0]["page_end"]),
                         (1, 1, 1, 1))
        layer = workspace.read_artifact(jid, "layer1")
        self.assertTrue(layer["blocks"])
        for block in layer["blocks"]:
            self.assertEqual((block["page_start"], block["page_end"]), (1, 1))

    def test_image_chunk_attributes_blocks_to_their_pages(self):
        from pipeline.studio import extract
        calls = []

        def stub(**kw):
            calls.append(kw)
            return {"text": "=== PAGE 1 ===\n۱) الف\n"
                            "=== PAGE 2 ===\n۲) ب",
                    "parsed": None, "route": {}, "lane": "stub"}

        self.llm.CALL_OVERRIDE = stub
        pdf = _image_pdf2(os.path.join(self.src, "scan2.pdf"))
        jid = self._job(pdf)
        out = extract.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(out["stats"]["calls"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0]["data_b64"], list)
        self.assertEqual(len(calls[0]["data_b64"]), 2)
        layer = workspace.read_artifact(jid, "layer1")
        blocks = [b for b in layer["blocks"] if b["kind"] == "question"]
        self.assertEqual([b["page"] for b in blocks], [1, 2])
        for block in blocks:
            self.assertEqual((block["page_start"], block["page_end"]), (1, 2))


def _blk(key, kind, **kw):
    base = {"key": key, "kind": kind, "page": kw.pop("page", 1),
            "order": kw.pop("order", 1), "number": None, "label": None,
            "text": "", "options": [], "metadata_line": None, "corp": None,
            "year": None, "topic": None, "difficulty": None,
            "has_figure": False, "images": [], "bbox": None,
            "bbox_source": None, "raw_ocr_text": None,
            "source": {"method": "text", "page": 1}, "missing": [], "fixes": [],
            "stall": None, "needs_human": False}
    base.update(kw)
    return base


def _seed_layer1(jid, blocks, assets=None, pages=None):
    profile = {"version": 1, "source_kind": "text_pdf",
               "compilation": "multi_institute_multi_year",
               "static": {"subject": None, "corp": None, "year": None,
                          "grade": "دوازدهم", "topic": None, "difficulty": None},
               "extract_per_question": ["subject", "corp", "year", "topic",
                                        "difficulty"],
               "answer_layout": "key_pages_at_end", "tag_list_ref": None,
               "notes": "", "updated_at": "2026-10-17T00:00:00"}
    workspace.write_artifact(jid, "profile", profile)
    workspace.write_artifact(jid, "layer1", {
        "version": 1, "job_id": jid, "source": "exam.pdf", "kind": "text_pdf",
        "profile": profile, "blocks": blocks, "assets": assets or [],
        "pages": pages or [{"page": 1, "method": "text", "text": "",
                            "images": []}],
        "stats": {}})
    return jid


class StudioMergeTests(_StudioStageCase):
    """B7 — the merged view carries the figure marker and the folder id."""

    def test_text_lane_figure_gets_the_marker_and_vision_figure_does_not(self):
        from pipeline.studio import merge
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        blocks = [
            _blk("p1-b1", "question", has_figure=True,
                 source={"method": "text", "page": 1}),
            _blk("p1-b2", "question", page=1,
                 source={"method": "vision", "page": 1})]
        _seed_layer1(jid, blocks)
        workspace.write_artifact(jid, "layer2", {
            "version": 1, "job_id": jid, "profile": profile_mod.blank(),
            "blocks": blocks, "pages": [], "assets": [{"id": "img-1"}],
            "stats": {}})
        view = merge.merged(jid)
        by_key = {b["key"]: b for b in view["blocks"]}
        self.assertTrue(by_key["p1-b1"].get("figure_marker"))
        self.assertNotIn("figure_marker", by_key["p1-b2"])

    def test_merged_exposes_the_folder_id(self):
        from pipeline.studio import merge
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        _seed_layer1(jid, [])
        meta = workspace.read_artifact(jid, "source_meta")
        self.assertEqual(merge.merged(jid)["folder_id"], meta["folder_id"])
        legacy = workspace.new_id()
        workspace.job_dir(legacy, create=True)
        self.assertIsNone(merge.merged(legacy)["folder_id"])


class StudioRowsTests(unittest.TestCase):
    """B6 — the row carries one media_images JSON array beside the old columns."""

    def test_question_row_writes_the_media_images_array(self):
        from pipeline.studio.importers import rows as rows_mod
        block = _blk("p1-b1", "question", number=1, text="متن سؤال",
                     crop={"needed": True, "approved": True,
                           "bbox": [10, 10, 50, 90], "page": 1})
        image = {"object": "crops/abc/p1-b1.jpg", "bbox": [10, 10, 50, 90],
                 "page": 1, "url": "https://cdn/p1-b1.jpg"}
        row = rows_mod._question_row("studio-j", block, image)
        self.assertEqual(json.loads(row["media_images"]), [
            {"object": "crops/abc/p1-b1.jpg", "bbox": [10, 10, 50, 90],
             "page": 1}])
        self.assertEqual(row["diagram_url"], "https://cdn/p1-b1.jpg")
        self.assertEqual(row["diagram_bbox"], "[10, 10, 50, 90]")

    def test_figure_without_text_keeps_one_inline_marker(self):
        from pipeline.studio.importers import rows as rows_mod
        block = _blk("p1-b1", "question", text="متن", figure_marker=True,
                     crop={"needed": True, "approved": True,
                           "bbox": [10, 10, 50, 90]})
        image = {"object": "crops/abc/p1-b1.jpg", "bbox": [10, 10, 50, 90],
                 "page": 1, "url": "u"}
        first = rows_mod._question_row("studio-j", block, image)
        second = rows_mod._question_row("studio-j", block, image)
        self.assertTrue(first["question_text"].endswith("[[figure:1]]"))
        self.assertEqual(first["question_text"], second["question_text"])

    def test_bare_subscripts_inside_mathrm_are_braced(self):
        """MathJax reads C_nH_2 as a double subscript; braces render the same."""
        from pipeline.studio.importers import rows as rows_mod
        self.assertEqual(rows_mod.brace_subscripts(r"\mathrm{C_nH_{2n+2}}"),
                         r"\mathrm{C_{n}H_{2n+2}}")
        self.assertEqual(rows_mod.brace_subscripts(r"\mathrm{C_6H_{10}}"),
                         r"\mathrm{C_{6}H_{10}}")
        self.assertEqual(rows_mod.brace_subscripts(r"\mathrm{kJ.mol^{-1}}"),
                         r"\mathrm{kJ.mol^{-1}}")
        self.assertEqual(rows_mod.brace_subscripts("بدون ریاضی"),
                         "بدون ریاضی")
        self.assertIsNone(rows_mod.brace_subscripts(None))

    def test_math_spans_get_latin_digits_and_upright_formulas(self):
        """The OCR mixes notations; math must print like the book."""
        from pipeline.studio.importers import rows as rows_mod
        self.assertEqual(rows_mod.normalise_math(r"$C_2H_6$"),
                         r"$\mathrm{C}_{2}\mathrm{H}_{6}$")
        self.assertEqual(rows_mod.normalise_math(r"$2N_2(g) + 5O_2(g)$"),
                         r"$2\mathrm{N}_{2}\mathrm{(g)} + 5\mathrm{O}_{2}\mathrm{(g)}$")
        self.assertEqual(rows_mod.normalise_math(r"$۲/۵۶\text{ g.L}^{-1}$"),
                         r"$2.56\text{ g.L}^{-1}$")
        self.assertEqual(rows_mod.normalise_math(r"$۱۳/۵\text{ kJ}$"),
                         r"$13.5\text{ kJ}$")
        self.assertEqual(rows_mod.normalise_math(r"$1/2$"), r"$1/2$")
        self.assertEqual(rows_mod.normalise_math(r"$\Delta H = ۱۱۴\text{kJ}$"),
                         r"$\Delta H = 114\text{kJ}$")
        self.assertEqual(rows_mod.normalise_math(r"$C-C$"), r"$\mathrm{C-C}$")
        self.assertEqual(rows_mod.normalise_math(r"$O=O$"), r"$\mathrm{O=O}$")

    def test_upright_formulas_and_bare_variables_are_left_alone(self):
        from pipeline.studio.importers import rows as rows_mod
        for untouched in (r"$\mathrm{NaHCO_{3}(s)}$", r"$\mathrm{H_{2}O}$",
                          r"$\mathrm{C_{6}H_{10}}$", r"$\Delta H = ?$",
                          r"$X$", "بدون ریاضی"):
            self.assertEqual(rows_mod.normalise_math(untouched), untouched)

    def test_question_row_braces_stem_and_option_subscripts(self):
        from pipeline.studio.importers import rows as rows_mod
        block = _blk("p1-b1", "question", number=1,
                     text=r"$\mathrm{C_nH_{2n+2}}$ چند است؟",
                     options=[{"label": "1", "text": r"$\mathrm{C_6H_{10}}$"}])
        row = rows_mod._question_row("studio-j", block, None)
        self.assertIn(r"\mathrm{C_{n}H_{2n+2}}", row["question_text"])
        self.assertIn(r"\mathrm{C_{6}H_{10}}", json.loads(row["options"])[0]["text"])


class StudioCropStorageTests(_StudioStageCase):
    """B3/B5 — crops upload under a stable object id and re-crop replaces it."""

    def _view(self):
        return {"folder_id": "f" * 40, "blocks": [{
            "key": "p1-b1", "kind": "question", "status": "ready",
            "crop": {"needed": True, "approved": True,
                     "bbox": [1, 2, 3, 4], "page": 1}}]}

    def _crop_file(self, jid):
        path = workspace.crop_path(jid, "p1-b1")
        with open(path, "wb") as fh:
            fh.write(b"jpeg")

    def test_crop_object_is_stable_and_has_no_version_part(self):
        from pipeline.studio import geometry
        obj = geometry.crop_object({}, "a" * 40, "p1-b1")
        self.assertEqual(obj, "crops/" + "a" * 40 + "/p1-b1.jpg")
        self.assertNotIn("v2", obj)

    def test_re_crop_deletes_then_uploads_the_same_object(self):
        from unittest import mock
        from pipeline import storage
        from pipeline.studio import geometry
        from pipeline.studio.importers import supabase
        jid = workspace.new_id()
        self._crop_file(jid)
        obj = geometry.crop_object({}, "f" * 40, "p1-b1")
        imports = {"uploads": {"source": None, "crops": {
            "p1-b1": {"object": obj, "url": "u0", "spec": "old"}}}}
        events = []
        with mock.patch.object(geometry, "render_crop",
                               return_value={"spec": "new",
                                             "crop_bbox": [1, 2, 3, 4],
                                             "crop_page": 1}), \
                mock.patch.object(storage, "upload_file",
                                  return_value="u1") as up, \
                mock.patch.object(storage, "delete_file",
                                  return_value=True) as dl:
            blob, errors = supabase._upload_crops(
                {}, jid, self._view(), imports,
                lambda *a, **k: events.append((a, k)))
        self.assertEqual(errors, [])
        self.assertEqual(dl.call_args[0], ({}, obj))
        self.assertEqual(up.call_args[0][1], obj)
        self.assertEqual(blob["p1-b1"]["object"], obj)
        self.assertEqual(imports["uploads"]["crops"]["p1-b1"]["spec"], "new")

    def test_first_upload_never_deletes(self):
        from unittest import mock
        from pipeline import storage
        from pipeline.studio import geometry
        from pipeline.studio.importers import supabase
        jid = workspace.new_id()
        self._crop_file(jid)
        imports = {"uploads": {"source": None, "crops": {}}}
        with mock.patch.object(geometry, "render_crop",
                               return_value={"spec": "s",
                                             "crop_bbox": [1, 2, 3, 4],
                                             "crop_page": 1}), \
                mock.patch.object(storage, "upload_file",
                                  return_value="u1") as up, \
                mock.patch.object(storage, "delete_file",
                                  return_value=True) as dl:
            blob, errors = supabase._upload_crops(
                {}, jid, self._view(), imports, lambda *a, **k: None)
        self.assertEqual(errors, [])
        dl.assert_not_called()
        self.assertTrue(up.call_args[0][1].endswith("/p1-b1.jpg"))
        self.assertEqual(blob["p1-b1"]["object"], up.call_args[0][1])


class StudioEnrichTests(_StudioStageCase):
    def _patch(self, **kw):
        base = {"text": "متن سؤال", "options": [], "corp": None, "year": None,
                "difficulty": None, "subject": None, "topic": None,
                "missing": []}
        base.update(kw)
        return base

    def test_sorts_links_the_key_and_flags_what_is_missing(self):
        from pipeline.studio import enrich
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        blocks = [
            _blk("p1-b1", "question", order=1, number=1, text="متن سئوال یک",
                 options=["الف) یک", "ب) دو", "ج) سه", "د) چهار"]),
            _blk("p1-b2", "question", order=2, number=2, text="متن سؤال دو",
                 options=["الف) یک", "ب) دو"]),
            _blk("p2-k1", "answer_key_entry", order=3, page=2, number=1,
                 label="ب"),
            _blk("p2-k9", "answer_key_entry", order=4, page=2, number=9,
                 label="۱"),
        ]
        _seed_layer1(jid, blocks)
        self.llm.CALL_OVERRIDE = lambda **kw: {
            "text": "", "parsed": {"blocks": [
                self._patch(key="p1-b1", text="متن سؤال یک",
                            options=["الف) یک", "ب) دو", "ج) سه", "د) چهار"],
                            corp="سنجش", year="1402", difficulty="متوسط",
                            subject="زیست‌شناسی",
                            topic="سطوح سازمانیابی حیات"),
                self._patch(key="p1-b2", text="متن سؤال دو",
                            options=["الف) یک", "ب) دو"], topic="بردارها",
                            missing=["topic"]),
            ]}, "route": {}, "lane": "stub"}
        out = enrich.run(jid, {}, emit=lambda *a, **k: None)
        layer = workspace.read_artifact(jid, "layer2")
        by_key = {b["key"]: b for b in layer["blocks"]}
        first = by_key["p1-b1"]
        self.assertEqual(first["status"], "ready")
        self.assertEqual(first["answer"], "ب")
        self.assertEqual(first["subject"], "زیست‌شناسی")
        self.assertIn("stem repaired", first["fixes"])
        second = by_key["p1-b2"]
        self.assertEqual(second["status"], "error")
        self.assertIn("topic", second["missing"])
        self.assertEqual(second["proposed"],
                         [{"field": "topic", "value": "بردارها"}])
        orphan = by_key["p2-k9"]
        self.assertIn("no question with this number", orphan["issues"])
        self.assertEqual(out["stats"]["answers_linked"], 1)

    def test_a_failed_call_keeps_the_block_queued(self):
        from pipeline.studio import enrich

        def boom(**kw):
            raise RuntimeError("no lane")
        self.llm.CALL_OVERRIDE = boom
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        _seed_layer1(jid, [_blk("p1-b1", "question", number=1, text="متن",
                                options=["الف) یک", "ب) دو"])])
        out = enrich.run(jid, {}, emit=lambda *a, **k: None)
        layer = workspace.read_artifact(jid, "layer2")
        self.assertIn("sorting call returned nothing",
                      layer["blocks"][0]["issues"])
        self.assertTrue(out["stats"]["errors"])


class StudioGeometryTests(_StudioStageCase):
    def _to_geometry(self, jid):
        from pipeline.studio import enrich
        self.llm.CALL_OVERRIDE = lambda **kw: {
            "text": "", "parsed": {"blocks": []}, "route": {}, "lane": "stub"}
        enrich.run(jid, {}, emit=lambda *a, **k: None)

    def test_text_figure_crop_is_auto_approved_and_cached(self):
        from pipeline.studio import extract, geometry
        pdf = _figure_pdf(os.path.join(self.src, "fig.pdf"))
        jid = self._job(pdf)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        self._to_geometry(jid)
        out = geometry.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(out["stats"]["crops"], 1)
        entry = workspace.read_artifact(jid, "geometry")["boxes"]["p1-b1"]
        self.assertEqual(entry["crop_source"], "figure")
        self.assertTrue(entry["crop_approved"])
        rendered = geometry.render_crop(jid, "p1-b1")
        path = workspace.crop_path(jid, "p1-b1")
        self.assertTrue(os.path.exists(path))
        self.assertLessEqual(os.path.getsize(path), 200 * 1024)
        stamp = os.path.getmtime(path)
        geometry.render_crop(jid, "p1-b1")                 # cache hit
        self.assertEqual(os.path.getmtime(path), stamp)
        geometry.render_crop(jid, "p1-b1", force=True)     # review re-render
        self.assertNotEqual(os.path.getmtime(path), stamp)
        self.assertEqual(rendered["crop_bbox"], entry["crop_bbox"])

    def _pinned_profile(self, jid):
        """Everything pinned: nothing is demanded, so only a crop can gate."""
        workspace.write_artifact(jid, "profile", {
            "version": 1, "source_kind": "image_pdf",
            "compilation": "single_institute_multi_year",
            "static": {"subject": "زیست‌شناسی", "corp": "سنجش",
                       "year": "1402", "grade": "دوازدهم",
                       "topic": "سطوح سازمانیابی حیات",
                       "difficulty": "متوسط"},
            "extract_per_question": [], "answer_layout": "none",
            "tag_list_ref": None, "notes": "",
            "updated_at": "2026-10-17T00:00:00"})

    def test_a_vision_question_with_a_picture_waits_for_the_human_gate(self):
        from pipeline.studio import enrich, extract, geometry, merge

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "۱) پرسش؟\nالف) یک\nب) دو", "parsed": None,
                        "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                return {"text": "", "parsed": {"boxes": [
                    {"key": "p1-b1", "bbox": [80, 60, 620, 940],
                     "figure": [300, 70, 600, 930]}]},
                    "route": {}, "lane": "stub"}
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}
        self.llm.CALL_OVERRIDE = stub

        pdf = _image_pdf(os.path.join(self.src, "scan.pdf"))
        jid = self._job(pdf)
        self._pinned_profile(jid)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        geometry.run(jid, {}, emit=lambda *a, **k: None)
        block = merge.merged(jid)["blocks"][0]
        self.assertEqual(block["bbox_source"], "vision")
        self.assertTrue(block["crop"]["needed"])
        self.assertEqual(block["crop"]["bbox"], [300, 70, 600, 930])
        self.assertIsNone(block["crop"]["approved"])       # human gate
        ok, reason = merge.importable(block)
        self.assertFalse(ok)
        self.assertEqual(reason, "crop awaiting approval")
        workspace.write_artifact(jid, "review", {
            "version": 1, "edits": {},
            "crops": {"p1-b1": {"bbox": [300, 70, 600, 930], "approved": True}}})
        block = merge.merged(jid)["blocks"][0]
        self.assertTrue(block["crop"]["approved"])
        ok, _ = merge.importable(block)
        self.assertTrue(ok)

    def test_a_text_only_vision_question_gets_no_crop(self):
        """No picture in the box => no crop, no bbox in the row."""
        from pipeline.studio import enrich, extract, geometry, merge
        from pipeline.studio.importers import rows as rows_mod

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "۱) پرسش؟\nالف) یک\nب) دو", "parsed": None,
                        "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                return {"text": "", "parsed": {"boxes": [
                    {"key": "p1-b1", "bbox": [80, 60, 620, 940],
                     "figure": None}]}, "route": {}, "lane": "stub"}
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}
        self.llm.CALL_OVERRIDE = stub

        pdf = _image_pdf(os.path.join(self.src, "text.pdf"))
        jid = self._job(pdf)
        self._pinned_profile(jid)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        out = geometry.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(out["stats"]["crops"], 0)
        entry = workspace.read_artifact(jid, "geometry")["boxes"]["p1-b1"]
        self.assertEqual(entry["bbox"], [80, 60, 620, 940])
        self.assertIsNone(entry["crop_bbox"])
        block = merge.merged(jid)["blocks"][0]
        self.assertFalse(block["crop"]["needed"])
        ok, _ = merge.importable(block)
        self.assertTrue(ok)
        row = rows_mod.build_rows({"job_id": jid, "blocks": [block]})["questions"][0]
        self.assertIsNone(row["diagram_url"])
        self.assertIsNone(row["diagram_bbox"])
        self.assertEqual(row["media_images"], "[]")

    def test_a_reviewer_can_clear_a_false_positive_picture(self):
        """bbox: null in review.json means "no picture here", not "keep mine"."""
        from pipeline.studio import enrich, extract, geometry, merge

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "۱) پرسش؟\nالف) یک\nب) دو", "parsed": None,
                        "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                return {"text": "", "parsed": {"boxes": [
                    {"key": "p1-b1", "bbox": [80, 60, 620, 940],
                     "figure": [300, 70, 600, 930]}]},
                    "route": {}, "lane": "stub"}
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}
        self.llm.CALL_OVERRIDE = stub

        pdf = _image_pdf(os.path.join(self.src, "false.pdf"))
        jid = self._job(pdf)
        self._pinned_profile(jid)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        geometry.run(jid, {}, emit=lambda *a, **k: None)
        self.assertTrue(merge.merged(jid)["blocks"][0]["crop"]["needed"])
        workspace.write_artifact(jid, "review", {
            "version": 1, "edits": {},
            "crops": {"p1-b1": {"bbox": None, "approved": False}}})
        block = merge.merged(jid)["blocks"][0]
        self.assertFalse(block["crop"]["needed"])
        ok, _ = merge.importable(block)
        self.assertTrue(ok)


    def test_two_pages_are_located_in_one_call(self):
        """pages/call is the locate batch: two pages, one request, two labels."""
        from pipeline.studio import enrich, extract, geometry
        calls = []

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "=== PAGE 1 ===\n۱) الف\nالف) یک\n"
                                "=== PAGE 2 ===\n۲) ب\nالف) دو",
                        "parsed": None, "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                calls.append(kw)
                return {"text": "", "parsed": {"pages": [
                    {"page": 1, "boxes": [{"key": "p1-b1",
                                           "bbox": [80, 60, 300, 940],
                                           "figure": None}]},
                    {"page": 2, "boxes": [{"key": "p2-b1",
                                           "bbox": [80, 60, 300, 940],
                                           "figure": None}]}]},
                    "route": {}, "lane": "stub"}
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}

        self.llm.CALL_OVERRIDE = stub
        pdf = _image_pdf2(os.path.join(self.src, "two.pdf"))
        jid = self._job(pdf)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        cfg = type("Cfg", (), {"studio_pages_per_call": 2,
                               "studio_band_batch": 6})()
        out = geometry.run(jid, cfg, emit=lambda *a, **k: None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["labels"], ["PAGE 1:", "PAGE 2:"])
        self.assertEqual(len(calls[0]["images"]), 2)
        boxes = workspace.read_artifact(jid, "geometry")["boxes"]
        self.assertEqual(boxes["p1-b1"]["bbox"], [80, 60, 300, 940])
        self.assertEqual(boxes["p2-b1"]["bbox"], [80, 60, 300, 940])
        self.assertEqual(out["stats"]["vision_pages"], 2)

    def _one_figure_stub(self, band_reply):
        from pipeline.studio import enrich, extract, geometry
        calls = []

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "۱) پرسش؟\nالف) یک\nب) دو",
                        "parsed": None, "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                calls.append(kw)
                if len(calls) == 1:
                    return {"text": "", "parsed": {"boxes": [
                        {"key": "p1-b1", "bbox": [80, 60, 620, 940],
                         "figure": [300, 70, 600, 930]}]},
                        "route": {}, "lane": "stub"}
                return {"text": "", "parsed": band_reply,
                        "route": {}, "lane": "stub"}
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}

        self.llm.CALL_OVERRIDE = stub
        pdf = _image_pdf(os.path.join(self.src, "band.pdf"))
        jid = self._job(pdf)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        return jid, calls

    def test_a_band_reply_maps_back_to_the_page(self):
        """Band coordinates are not page coordinates: the band rect maps them."""
        from unittest import mock
        from pipeline.studio import geometry
        jid, calls = self._one_figure_stub({"bands": [
            {"key": "p1-b1", "figures": [[250, 250, 750, 750]]}]})
        real_cut = geometry.bands.cut
        seen = {}

        def cut(page, box, key, page_no, **kwargs):
            band = real_cut(page, box, key, page_no, **kwargs)
            seen["band"] = band
            return band

        with mock.patch.object(geometry.bands, "cut", side_effect=cut):
            out = geometry.run(jid, {}, emit=lambda *a, **k: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["labels"], ["BAND p1-b1:"])
        self.assertEqual(len(calls[1]["images"]), 1)
        self.assertTrue(all(isinstance(img, str) for img in calls[1]["images"]))
        expected = seen["band"].to_page([250, 250, 750, 750])
        entry = workspace.read_artifact(jid, "geometry")["boxes"]["p1-b1"]
        self.assertEqual(entry["crop_bbox"], expected)
        self.assertEqual(entry["figure_bboxes"], [expected])
        self.assertIsNone(entry["crop_approved"])       # human gate
        self.assertEqual(out["stats"]["bands"], 1)
        self.assertEqual(out["stats"]["band_calls"], 1)

    def test_a_failed_page_keeps_its_previous_box(self):
        """Quota/tunnel failure must not erase a box the job already had."""
        from pipeline.studio import enrich, extract, geometry

        def stub(**kw):
            if kw["stage"] == "extract":
                return {"text": "۱) پرسش؟\nالف) یک\nب) دو",
                        "parsed": None, "route": {}, "lane": "stub"}
            if kw["stage"] == "vision":
                raise RuntimeError("quota down")
            return {"text": "", "parsed": {"blocks": []}, "route": {},
                    "lane": "stub"}

        self.llm.CALL_OVERRIDE = stub
        pdf = _image_pdf(os.path.join(self.src, "previous.pdf"))
        jid = self._job(pdf)
        extract.run(jid, {}, emit=lambda *a, **k: None)
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        workspace.write_artifact(jid, "geometry", {"boxes": {"p1-b1": {
            "bbox": [11, 22, 333, 444], "source": "vision"}}})
        out = geometry.run(jid, {}, emit=lambda *a, **k: None)
        entry = workspace.read_artifact(jid, "geometry")["boxes"]["p1-b1"]
        self.assertEqual(entry["bbox"], [11, 22, 333, 444])
        self.assertEqual(out["stats"]["kept_previous"], 1)
        self.assertIsNone(entry["crop_bbox"])

    def test_a_band_with_no_figure_clears_the_locate_box(self):
        """Gemini's empty band is authoritative; do not keep DeepSeek's guess."""
        from pipeline.studio import geometry
        jid, calls = self._one_figure_stub({"bands": [
            {"key": "p1-b1", "figures": []}]})
        out = geometry.run(jid, {}, emit=lambda *a, **k: None)
        entry = workspace.read_artifact(jid, "geometry")["boxes"]["p1-b1"]
        self.assertIsNone(entry["crop_bbox"])
        self.assertEqual(entry["figure_bboxes"], [])
        self.assertIsNone(entry["crop_approved"])
        self.assertEqual(out["stats"]["figure_missing"], 1)
        self.assertEqual(out["stats"]["crops"], 0)


def _echo_stub(**kw):
    """Enrich lane stub: echo every block back unchanged (offline)."""
    payload = json.loads(kw["prompt"].split("\nblocks: ", 1)[1])
    return {"text": "", "parsed": {"blocks": [
        {"key": b["key"], "kind": b["kind"], "text": b["text"],
         "options": b["options"], "missing": []} for b in payload]},
        "route": {}, "lane": "stub"}


class StudioImportTests(_StudioStageCase):
    def _seed(self, with_figure=True):
        from pipeline.studio import enrich
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        filled = {"subject": "زیست‌شناسی", "corp": "سنجش", "year": "1402",
                  "difficulty": "متوسط", "topic": "سطوح سازمانیابی حیات"}
        blocks = [
            _blk("p1-b1", "question", order=1, number=1, text="متن یک",
                 options=["الف) یک", "ب) دو", "ج) سه", "د) چهار"],
                 bbox=[10, 10, 50, 90], bbox_source="text_layer", **filled),
            _blk("p2-k1", "answer_key_entry", order=2, page=2, number=1,
                 label="ب", **filled),
            # topic is demanded and absent -> the row stays queued
            _blk("p1-b2", "question", order=3, number=2, text="متن دو",
                 options=["الف) یک", "ب) دو"], **{k: v for k, v in filled.items()
                                                 if k != "topic"}),
        ]
        _seed_layer1(jid, blocks)
        self.llm.CALL_OVERRIDE = _echo_stub
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        boxes = {"p1-b1": {
            "bbox": [10, 10, 50, 90], "source": "text_layer",
            "crop_bbox": [10, 10, 50, 90] if with_figure else None,
            "crop_source": "figure" if with_figure else None,
            "crop_page": 1, "crop_method": "text", "crop_image": None,
            "crop_approved": True if with_figure else None,
            "crop": None, "crop_bytes": None, "spec": None},
            "p2-k1": {"bbox": None, "source": None, "crop_bbox": None,
                      "crop_source": None, "crop_page": 2,
                      "crop_method": "text", "crop_image": None,
                      "crop_approved": None, "crop": None,
                      "crop_bytes": None, "spec": None},
            "p1-b2": {"bbox": None, "source": None, "crop_bbox": None,
                      "crop_source": None, "crop_page": 1,
                      "crop_method": "text", "crop_image": None,
                      "crop_approved": None, "crop": None,
                      "crop_bytes": None, "spec": None}}
        workspace.write_artifact(jid, "geometry", {
            "version": 1, "job_id": jid, "boxes": boxes, "stats": {}})
        return jid

    def test_rows_link_answers_and_gate_unapproved_crops(self):
        from pipeline.studio import merge
        from pipeline.studio.importers import rows as rows_mod
        jid = self._seed()
        view = merge.merged(jid)
        built = rows_mod.build_rows(view, {"p1-b1": "crops/p1-b1.jpg"})
        self.assertEqual(len(built["questions"]), 1)
        question = built["questions"][0]
        self.assertTrue(question["id"].startswith("studio-" + jid))
        self.assertTrue(question["id"].endswith("-p1-b1"))
        self.assertEqual(question["diagram_url"], "crops/p1-b1.jpg")
        self.assertEqual(question["diagram_bbox"], "[10, 10, 50, 90]")
        self.assertEqual(len(built["answers"]), 1)
        answer = built["answers"][0]
        self.assertEqual(answer["correct_option_label"], "ب")
        self.assertEqual(answer["correct_option_text"], "دو")
        self.assertEqual(answer["question_id"], question["id"])
        self.assertEqual(answer["review_status"], "pending")
        reasons = {s["key"]: s["reason"] for s in built["skipped"]}
        self.assertEqual(reasons["p1-b2"], "flagged: topic")
        strict = rows_mod.build_rows(view, {})
        self.assertIn("crop not uploaded",
                      {s["key"]: s["reason"]
                       for s in strict["skipped"]}["p1-b1"])

    def test_file_import_writes_rows_and_a_dry_run_marks_nothing(self):
        from pipeline.studio import importers
        jid = self._seed()
        dry = importers.run({}, jid, target="file", dry_run=True)
        self.assertTrue(dry["report"]["dry_run"])
        self.assertFalse(os.path.exists(
            os.path.join(workspace.job_dir(jid), "export.json")))
        self.assertFalse(workspace.read_artifact(jid, "imports")["results"])
        out = importers.run({}, jid, target="file", dry_run=False)
        self.assertEqual(out["report"]["questions"], 1)
        with io.open(os.path.join(workspace.job_dir(jid), "export.json"),
                     encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(len(payload["questions"]), 1)
        self.assertEqual(len(payload["answers"]), 1)
        imports = workspace.read_artifact(jid, "imports")
        self.assertEqual(imports["results"]["p1-b1"]["status"], "ok")
        self.assertEqual(imports["results"]["p1-b2"]["status"], "skipped")
        self.assertEqual(workspace.summary(jid)["imported"], 2)

    def test_review_edit_recomputes_health_and_unlocks_import(self):
        from pipeline.studio import merge
        from pipeline.studio.importers import rows as rows_mod
        jid = self._seed()
        workspace.write_artifact(jid, "review", {
            "version": 1,
            "edits": {"p1-b2": {"text": "متن دو",
                                "options": ["الف) یک", "ب) دو"],
                                "topic": "سطوح سازمانیابی حیات"}},
            "crops": {}})
        view = merge.merged(jid)
        edited = [b for b in view["blocks"] if b["key"] == "p1-b2"][0]
        self.assertEqual(edited["status"], "ready")
        built = rows_mod.build_rows(view, {"p1-b1": "crops/p1-b1.jpg"})
        self.assertEqual(len(built["questions"]), 2)

    def test_a_split_half_is_held_at_import(self):
        from pipeline.studio import merge
        head = _blk("p2-b1", "question", split="head")
        self.assertEqual(merge.importable(head),
                         (False, "split across a page boundary"))
        head["split"] = None
        self.assertEqual(merge.importable(head), (True, None))

    def _seed_split(self):
        from pipeline.studio import enrich
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf")
        filled = {"subject": "زیست‌شناسی", "corp": "سنجش", "year": "1402",
                  "difficulty": "متوسط", "topic": "سطوح سازمانیابی حیات"}
        _seed_layer1(jid, [
            _blk("p1-b1", "question", order=1, number=1, text="متن یک",
                 options=["الف) یک", "ب) دو"], split="head", **filled)])
        self.llm.CALL_OVERRIDE = _echo_stub
        enrich.run(jid, {}, emit=lambda *a, **k: None)
        return jid

    def test_a_joined_question_is_importable(self):
        from pipeline.studio import merge
        self.assertEqual(
            merge.importable(_blk("p1-b1", "question", split_joined=True)),
            (True, None))
        jid = self._seed_split()
        view = merge.merged(jid)
        self.assertEqual(merge.importable(view["blocks"][0]),
                         (False, "split across a page boundary"))
        workspace.write_artifact(jid, "review", {
            "version": 1, "crops": {"p1-b1": {"split_ok": True}}})
        view = merge.merged(jid)
        self.assertIsNone(view["blocks"][0]["split"])
        self.assertTrue(view["blocks"][0]["split_joined"])
        self.assertEqual(merge.importable(view["blocks"][0]), (True, None))

    def test_the_review_endpoint_split_ok_unlocks_import(self):
        from pipeline.studio import merge
        jid = self._seed_split()
        try:
            from fastapi.testclient import TestClient
        except Exception:
            self.skipTest("fastapi TestClient unavailable")
        import main
        client = TestClient(main.app)
        res = client.post("/api/studio/jobs/" + jid + "/review",
                          json={"crops": {"p1-b1": {"split_ok": True}}})
        self.assertEqual(res.status_code, 200, res.text)
        view = merge.merged(jid)
        self.assertIsNone(view["blocks"][0]["split"])
        self.assertEqual(merge.importable(view["blocks"][0]), (True, None))

    def test_import_never_uploads_the_source(self):
        from unittest import mock
        from pipeline import storage
        from pipeline.studio import geometry
        from pipeline.studio.importers import supabase
        jid = workspace.new_id()
        path = workspace.crop_path(jid, "p1-b1")
        with open(path, "wb") as fh:
            fh.write(b"jpeg")
        view = {"folder_id": "f" * 40, "source": "exam.pdf", "crops": {},
                "blocks": [{"key": "p1-b1", "kind": "question",
                            "status": "ready",
                            "crop": {"needed": True, "approved": True,
                                     "bbox": [1, 2, 3, 4], "page": 1}}]}
        imports = {"uploads": {"source": None, "crops": {}}}

        class _DB:
            def __init__(self, cfg):
                pass

            def upsert_questions(self, rows):
                pass

            def upsert_answers(self, rows):
                pass

        with mock.patch.object(supabase.db_mod, "Database", _DB), \
                mock.patch.object(geometry, "render_crop",
                                  return_value={"spec": "s",
                                                "crop_bbox": [1, 2, 3, 4],
                                                "crop_page": 1}), \
                mock.patch.object(storage, "upload_file",
                                  return_value="u1") as up, \
                mock.patch.object(storage, "delete_file",
                                  return_value=True):
            supabase.run({}, jid, view, imports,
                         emit=lambda *a, **k: None)
        names = [call[0][1] for call in up.call_args_list]
        self.assertEqual(names, ["crops/" + "f" * 40 + "/p1-b1.jpg"])
        self.assertFalse([n for n in names if "source" in n])
        self.assertIsNone(imports["uploads"]["source"])

    def test_file_export_carries_media_images_and_folder_id(self):
        from pipeline.studio import importers
        jid = self._seed()
        importers.run({}, jid, target="file", dry_run=False)
        with io.open(os.path.join(workspace.job_dir(jid), "export.json"),
                     encoding="utf-8") as fh:
            payload = json.load(fh)
        meta = workspace.read_artifact(jid, "source_meta")
        self.assertEqual(payload["folder_id"], meta["folder_id"])
        pictures = json.loads(payload["questions"][0]["media_images"])
        self.assertEqual(pictures[0]["object"], "crops/p1-b1.jpg")
        self.assertEqual(pictures[0]["bbox"], [10, 10, 50, 90])

    def test_option_text_matches_by_label_then_by_position(self):
        from pipeline.studio.importers import rows as rows_mod
        from pipeline import questions as questions_mod
        options = questions_mod.normalize_options(
            ["الف) یک", "ب) دو", "ج) سه", "د) چهار"])
        self.assertEqual(rows_mod.option_text(options, "ب"), "دو")
        self.assertEqual(rows_mod.option_text(options, "۲"), "دو")
        self.assertIsNone(rows_mod.option_text(options, "۹"))

class StudioLlmTests(unittest.TestCase):
    def test_vision_stage_pins_the_configured_gemini_model(self):
        """STUDIO_VISION_MODEL narrows the router ladder for crop calls only."""
        from types import SimpleNamespace
        from unittest import mock
        from pipeline.studio import llm

        cfg = SimpleNamespace(
            studio_vision_active="",
            studio_vision_model="gemini-3.5-flash",
            gemini_model_ladder=("gemini-3.5-flash-lite", "gemini-3.5-flash"),
            gemini_key_pool=("k",), studio_proxy="", studio_providers=())
        seen = {}

        def fake_router(lane_cfg, *args, **kwargs):
            seen["ladder"] = lane_cfg.gemini_model_ladder
            return {}, "", {}

        with mock.patch.object(llm.router, "call", side_effect=fake_router), \
                mock.patch.object(llm.gemini_mod, "_apply_profile"), \
                mock.patch.object(llm.gemini_mod, "_proxy_url",
                                  {"url": None}):
            llm.call_stage(cfg, "vision", "p", "a", "image/jpeg")
        self.assertEqual(seen["ladder"], ("gemini-3.5-flash",))
        self.assertEqual(cfg.gemini_model_ladder,
                         ("gemini-3.5-flash-lite", "gemini-3.5-flash"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
