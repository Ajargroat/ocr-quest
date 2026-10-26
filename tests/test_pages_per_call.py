"""Offline tests for the "Pages / call" OCR batching (no network, no database).

`OCR_PAGES_PER_CALL` (the pipeline toolbar's picker) must put N page images of
ONE paper into a SINGLE request — not merely cap how many pages are rendered.
The regression these pin: the setting was saved and read, but the OCR loop
still sent one image per request, so a run made exactly as many calls as there
were files.

Run them either way:
    python tests/test_pages_per_call.py
    pytest tests/test_pages_per_call.py
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import runner                                            # noqa: E402
from pipeline import scanner                                            # noqa: E402
from pipeline.runner import _ocr_pages, _page_batches                    # noqa: E402

# The real router, captured before any test swaps it out — every cleanup
# restores THIS, so a fake can never leak into another module's tests.
_REAL_ROUTER = runner.router

QUESTION = SimpleNamespace(type="سوال", rel_path="riazi/q.jpg")


class CallRecorder:
    """Stands in for `gemini_router.router` and records each request's shape."""

    def __init__(self, parsed=None):
        self.parsed = parsed
        self.replies = []          # consumed per call when given
        self.calls = []

    def call(self, cfg, prompt, data_b64, mime, on_problem=None,
             on_route=None, labels=None, **kwargs):
        self.calls.append({"prompt": prompt,
                           "images": (list(data_b64)
                                      if isinstance(data_b64, (list, tuple))
                                      else [data_b64]),
                           "labels": labels,
                           "mime": mime})
        reply = self.replies.pop(0) if self.replies else self.parsed
        return reply, {}, {}

    call_pinned = call


def cfg_for(pages_per_call):
    return SimpleNamespace(ocr_pages_per_call=pages_per_call,
                           ocr_provider="gemini")


def pages(count):
    return [(n, ("img-%d" % n).encode()) for n in range(1, count + 1)]


def page_file(name, folder="c:/papers/c4", kind="سوال", mime="image/jpeg"):
    """A SourceItem-shaped file named like the converter's page output."""
    return SimpleNamespace(
        source_id=name, rel_path=name, file_name=name,
        file_path=folder + "/" + name, type=kind, mime_type=mime,
        subject="shimi", grade=12, topic="c4", file_size=1)


class RecorderCase(unittest.TestCase):
    def setUp(self):
        self._router = _REAL_ROUTER
        self.addCleanup(setattr, runner, "router", self._router)

    def use(self, recorder):
        runner.router = recorder
        return recorder

class CrossFilePageRunTests(unittest.TestCase):
    """The setting must batch ACROSS FILES: the converter writes one paper as
    `base-1.jpg`, `base-2.jpg`, ..., so a folder of scans is many files of one
    paper. A single-image file cannot batch inside itself."""

    def test_scanner_reads_pages_in_numeric_order(self):
        """A folder of scans must be scanned as -2, -3, -10, -20, not as the
        string order -10, -2, -20, -3. The wrong order breaks every page
        number in the run, so no two consecutive pages are ever batched."""
        names = ["paper-10.jpg", "paper-2.jpg", "paper-20.jpg", "paper-3.jpg"]
        entries = [SimpleNamespace(name=n) for n in names]
        self.assertEqual([e.name for e in
                          sorted(entries, key=scanner._natural_key)],
                         ["paper-2.jpg", "paper-3.jpg",
                          "paper-10.jpg", "paper-20.jpg"])

    def test_numeric_scan_order_makes_consecutive_runs(self):
        """End-to-end on the reported shape: string order split page 2 away
        from its neighbours, so the batch setting changed nothing."""
        names = ["paper-20.jpg", "paper-21.jpg", "paper-3.jpg", "paper-4.jpg"]
        entries = sorted((SimpleNamespace(name=n) for n in names),
                         key=scanner._natural_key)
        items = [page_file(e.name) for e in entries]
        runs = runner.page_runs(items, 2)
        self.assertEqual([[runner._page_of(i)[1] for i in r] for r in runs],
                         [[3, 4], [20, 21]])

    def test_consecutive_pages_of_one_paper_share_a_run(self):
        items = [page_file("paper-1.jpg"), page_file("paper-2.jpg"),
                 page_file("paper-3.jpg")]
        runs = runner.page_runs(items, 2)
        self.assertEqual([[i.file_name for i in r] for r in runs],
                         [["paper-1.jpg", "paper-2.jpg"], ["paper-3.jpg"]])

    def test_one_page_per_call_never_groups(self):
        items = [page_file("paper-1.jpg"), page_file("paper-2.jpg")]
        runs = runner.page_runs(items, 1)
        self.assertEqual([[i.file_name for i in r] for r in runs],
                         [["paper-1.jpg"], ["paper-2.jpg"]])

    def test_a_run_never_crosses_papers_folders_or_branches(self):
        items = [page_file("a-1.jpg"), page_file("a-2.jpg"),
                 page_file("b-1.jpg"),                       # other paper
                 page_file("a-3.jpg", folder="c:/other"),    # other folder
                 page_file("a-4.jpg", kind="پاسخ")]          # other branch
        runs = runner.page_runs(items, 8)
        for run in runs:
            self.assertEqual(len({runner._page_of(i)[0] for i in run}), 1)
            self.assertEqual(len({i.file_path.rsplit("/", 1)[0] for i in run}), 1)
            self.assertEqual(len({i.type for i in run}), 1)

    def test_a_run_is_contiguous_never_page_2_with_page_20(self):
        """Arrival order is lexicographic (-1, -10, -2), so a group must not
        pair 2 with 20: the model would read them out of sequence."""
        names = ["paper-1.jpg", "paper-10.jpg", "paper-11.jpg",
                 "paper-2.jpg", "paper-3.jpg"]
        runs = runner.page_runs([page_file(n) for n in names], 3)
        for run in runs:
            numbers = [runner._page_of(i)[1] for i in run]
            self.assertEqual(numbers, list(range(numbers[0],
                                                 numbers[0] + len(numbers))))

    def test_a_run_is_contiguous_so_reading_order_is_arrival_order(self):
        """Because a run is contiguous by construction, its pages are already
        in reading order: 1,2 group and 10 stands alone."""
        names = ["paper-1.jpg", "paper-2.jpg", "paper-10.jpg"]
        runs = runner.page_runs([page_file(n) for n in names], 8)
        self.assertEqual([[runner._page_of(i)[1] for i in r] for r in runs],
                         [[1, 2], [10]])

    def test_a_pdf_is_not_grouped_across_files(self):
        items = [page_file("scan-1.pdf", mime="application/pdf"),
                 page_file("scan-2.pdf", mime="application/pdf")]
        runs = runner.page_runs(items, 4)
        self.assertEqual([len(r) for r in runs], [1, 1])

    def test_a_name_without_a_page_suffix_is_its_own_run(self):
        items = [page_file("paper-1.jpg"), page_file("cover.jpg")]
        runs = runner.page_runs(items, 4)
        self.assertEqual([[i.file_name for i in r] for r in runs],
                         [["paper-1.jpg"], ["cover.jpg"]])


class PrepassTests(RecorderCase):
    """`_page_ocr_prepass`: ONE call per run, each file gets its own slice."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="pages-test-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)

    def files(self, *names):
        """Real files on disk — the prepass reads each page's bytes."""
        out = []
        for name in names:
            item = page_file(name, folder=self.tmp)
            with open(item.file_path, "wb") as fh:
                fh.write(b"jpeg-bytes-" + name.encode())
            out.append(item)
        return out

    def test_one_call_covers_a_two_file_run_and_splits_per_file(self):
        rec = self.use(CallRecorder({"pages": [
            {"page": 1, "questions": [{"question_number": 1}]},
            {"page": 2, "questions": [{"question_number": 2}]}]}))
        logs = []
        pre = runner._page_ocr_prepass(
            SimpleNamespace(stop_flag=__import__("threading").Event()),
            cfg_for(2), None, self.files("paper-1.jpg", "paper-2.jpg"), 2,
            lambda level, text, *a: logs.append(text))
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]["labels"], ["PAGE 1:", "PAGE 2:"])
        self.assertEqual(len(rec.calls[0]["images"]), 2)
        self.assertEqual(sorted(pre), ["paper-1.jpg", "paper-2.jpg"])
        self.assertEqual([r["question_number"]
                          for r in pre["paper-1.jpg"]["parsed"]["questions"]],
                         [1])
        self.assertEqual([r["question_number"]
                          for r in pre["paper-2.jpg"]["parsed"]["questions"]],
                         [2])
        self.assertTrue(any("one call" in t for t in logs))

    def test_a_failed_batch_yields_nothing_so_files_use_their_own_call(self):
        class Boom:
            def call(self, *a, **k):
                raise RuntimeError("batch blew up")
            call_pinned = call

        runner.router = Boom()
        pre = runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-1.jpg", "paper-2.jpg"),
            2, lambda *a: None)
        self.assertEqual(pre, {})

    def test_single_file_runs_make_no_prepass_call(self):
        rec = self.use(CallRecorder({"questions": []}))
        pre = runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("solo.jpg"), 2,
            lambda *a: None)
        self.assertEqual(pre, {})
        self.assertEqual(rec.calls, [])

    def test_an_unreadable_page_skips_the_whole_batch(self):
        rec = self.use(CallRecorder({"pages": []}))
        items = self.files("paper-1.jpg") + [page_file("paper-2.jpg",
                                                       folder=self.tmp)]
        pre = runner._page_ocr_prepass(None, cfg_for(2), None, items, 2,
                                       lambda *a: None)
        self.assertEqual(pre, {})
        self.assertEqual(rec.calls, [])

    def test_a_reply_that_echoes_page_1_is_refused_not_misfiled(self):
        """A reply that cannot be attributed must not be filed on page 3.

        A batch of pages 3-4 whose questions all say ``"page": 1`` used to be
        dumped whole onto page 3: page 3's source got page 4's rows as well,
        and page 4's file staged ZERO rows and was archived anyway. That is
        dropped data that looks like a clean run. The batch is now refused, so
        every file falls back to its own call and lands on its own page.
        """
        rec = self.use(CallRecorder({"pages": [
            {"page": 1, "questions": [{"question_number": 1},
                                      {"question_number": 2},
                                      {"question_number": 3}]}]}))
        logs, faults = [], []
        pre = runner._page_ocr_prepass(
            SimpleNamespace(stop_flag=__import__("threading").Event()),
            cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"), 2,
            lambda level, text, kind=None, *a: (logs.append(text),
                                                faults.append(kind)))
        self.assertEqual(len(rec.calls), 1)
        # Nothing is handed over, so both files take their own single-image
        # call rather than one of them silently receiving the other's rows.
        self.assertEqual(pre, {})
        self.assertIn("bad_page_label", faults)
        self.assertTrue(any("could not be attributed" in t for t in logs))
        # It must NOT claim to have kept the rows somewhere.
        self.assertFalse(any("kept whole" in t for t in logs))

    def test_a_dict_keyed_by_the_page_label_splits_per_page(self):
        """{"pages": {"PAGE 3:": ...}} — the page is the KEY, not a field.

        The batch prompt labels every image "PAGE n:", so the model often
        keys its answer by that label. ``page_number`` only understood bare
        integers, so every page read empty and a perfectly good 2-image reply
        was thrown away.
        """
        rec = self.use(CallRecorder({"pages": {
            "PAGE 3:": {"questions": [{"question_number": 1}]},
            "PAGE 4:": {"questions": [{"question_number": 2}]}}}))
        logs, faults = [], []
        pre = runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"),
            2, lambda level, text, kind=None, *a: (logs.append(text),
                                                   faults.append(kind)))
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual([r["question_number"] for r in
                          pre["paper-3.jpg"]["parsed"]["questions"]], [1])
        self.assertEqual([r["question_number"] for r in
                          pre["paper-4.jpg"]["parsed"]["questions"]], [2])
        self.assertNotIn("bad_page_label", faults)
        self.assertTrue(any("2 of 2 page(s)" in t for t in logs))

    def test_a_label_spelled_page_n_is_understood(self):
        """Every spelling of a page key resolves to the same page number."""
        from pipeline.questions import page_number
        for value in ("PAGE 3:", "PAGE 3", "page 3", "3", 3, 3.0):
            self.assertEqual(page_number(value), 3, value)
        # ...without inventing a page where there is no number at all.
        for value in (None, True, "", "abc"):
            self.assertIsNone(page_number(value), value)

    def test_a_blank_page_does_not_swallow_the_other_pages_rows(self):
        """One genuinely empty page must not look like a mislabel."""
        rec = self.use(CallRecorder({"pages": [
            {"page": 3, "questions": [{"question_number": 1}]},
            {"page": 4, "questions": []}]}))
        logs, faults = [], []
        pre = runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"),
            2, lambda level, text, kind=None, *a: (logs.append(text),
                                                   faults.append(kind)))
        self.assertEqual(len(pre["paper-3.jpg"]["parsed"]["questions"]), 1)
        self.assertEqual(pre["paper-4.jpg"]["parsed"]["questions"], [])
        self.assertNotIn("bad_page_label", faults)
        self.assertTrue(any("1 of 2 page(s)" in t for t in logs))

    def test_real_page_labels_split_cleanly_and_flag_nothing(self):
        rec = self.use(CallRecorder({"pages": [
            {"page": 3, "questions": [{"question_number": 1}]},
            {"page": 4, "questions": [{"question_number": 2},
                                      {"question_number": 3}]}]}))
        faults = []
        pre = runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"),
            2, lambda level, text, kind=None, *a: faults.append(kind))
        self.assertEqual(len(pre["paper-3.jpg"]["parsed"]["questions"]), 1)
        self.assertEqual(len(pre["paper-4.jpg"]["parsed"]["questions"]), 2)
        self.assertNotIn("bad_page_label", faults)

    def test_the_labels_carry_the_real_suffix_page_not_the_position(self):
        """A batch of pages 3 and 4 must be labelled PAGE 3/PAGE 4.

        An earlier version collected the page numbers into a list *before*
        the read loop, so the loop iterated an empty list and every batch
        retried with a mismatched image count. The label is how the model
        knows which page a row belongs to, so it is pinned here.
        """
        rec = self.use(CallRecorder({"pages": [
            {"page": 3, "questions": [{"question_number": 1}]},
            {"page": 4, "questions": [{"question_number": 2}]}]}))
        runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"),
            2, lambda *a, **k: None)
        self.assertEqual(rec.calls[0]["labels"], ["PAGE 3:", "PAGE 4:"])
        self.assertEqual(len(rec.calls[0]["images"]), 2)

    def test_the_coverage_line_counts_pages_not_a_running_total(self):
        """`covered` is a per-batch tally, not the file total.

        The same name was assigned the run-wide total first, so the log line
        could read "2 of 2" and then print the total again - a number that
        cannot reveal a batch whose rows all landed on one page.
        """
        self.use(CallRecorder({"pages": [
            {"page": 3, "questions": [{"question_number": 1}]},
            {"page": 4, "questions": [{"question_number": 2}]}]}))
        logs = []
        runner._page_ocr_prepass(
            None, cfg_for(2), None, self.files("paper-3.jpg", "paper-4.jpg"),
            2, lambda level, text, *a: logs.append(text))
        line = next(t for t in logs if "read in one call" in t)
        self.assertIn("2 questions on 2 of 2 page(s)", line)


class PagesPromptTests(unittest.TestCase):
    """The prompt must not hand the model a page number it can copy."""

    def test_the_skeleton_does_not_show_a_literal_page_1(self):
        from pipeline.questions import PAGES_PROMPT
        self.assertNotIn('"page": 1,', PAGES_PROMPT)
        self.assertIn("PAGE n:", PAGES_PROMPT)

    def test_the_prompt_reads_the_page_from_its_label(self):
        from pipeline.questions import PAGES_PROMPT
        self.assertIn("NUMBER PRINTED IN THAT LABEL", PAGES_PROMPT)
        self.assertIn("Never renumber the pages from 1", PAGES_PROMPT)




class BatchWindowTests(unittest.TestCase):
    """`_page_batches` — consecutive, never straddling a page-number gap."""

    def test_one_page_per_call_is_the_classic_sequence(self):
        self.assertEqual([[p for p, _d in b] for b in _page_batches(pages(3), 1)],
                         [[1], [2], [3]])

    def test_two_pages_per_call_groups_consecutive_pages(self):
        self.assertEqual([[p for p, _d in b] for b in _page_batches(pages(5), 2)],
                         [[1, 2], [3, 4], [5]])

    def test_a_window_never_straddles_a_gap(self):
        data = [(1, b"a"), (3, b"b"), (4, b"c")]
        self.assertEqual([[p for p, _d in b] for b in _page_batches(data, 2)],
                         [[1], [3, 4]])

    def test_a_bad_setting_still_sends_one_image_per_call(self):
        self.assertEqual([[p for p, _d in b] for b in _page_batches(pages(2), 0)],
                         [[1], [2]])


class OcrPagesBatchingTests(RecorderCase):
    """The regression: N pages of one paper must ride ONE request."""

    def test_two_pages_make_one_call_not_two(self):
        rec = self.use(CallRecorder({"pages": [
            {"page": 1, "questions": [{"question_number": 1}]},
            {"page": 2, "questions": [{"question_number": 2}]}]}))
        merged = _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=True)
        self.assertEqual(len(rec.calls), 1, "one batch must be one HTTP call")
        self.assertEqual(len(rec.calls[0]["images"]), 2)
        self.assertEqual(rec.calls[0]["labels"], ["PAGE 1:", "PAGE 2:"])
        self.assertEqual(merged["ocr_calls"], 1)

    def test_one_page_per_call_keeps_one_request_each(self):
        rec = self.use(CallRecorder({"questions": [{"question_number": 1}]}))
        _ocr_pages(cfg_for(1), QUESTION, pages(3), questions=True)
        self.assertEqual(len(rec.calls), 3)
        # the classic lane sends a bare image: no batch labels
        self.assertTrue(all(c["labels"] is None for c in rec.calls))
        self.assertTrue(all(len(c["images"]) == 1 for c in rec.calls))

    def test_the_batch_prompt_asks_for_pages_not_a_single_file(self):
        rec = self.use(CallRecorder({"pages": []}))
        _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=True)
        prompt = rec.calls[0]["prompt"]
        self.assertIn('"pages"', prompt)
        self.assertNotIn("You will receive one file", prompt)

    def test_each_question_lands_on_the_page_it_was_printed_on(self):
        self.use(CallRecorder({"pages": [
            {"page": 1, "questions": [{"question_number": 1},
                                      {"question_number": 2}]},
            {"page": 2, "questions": [{"question_number": 3}]}]}))
        merged = _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=True)
        rows = merged["questions"]
        self.assertEqual([r["question_number"] for r in rows], [1, 2, 3])
        self.assertEqual([r["page"] for r in rows], [1, 1, 2])

    def test_three_pages_at_two_per_call_makes_two_calls(self):
        rec = self.use(CallRecorder())
        rec.replies = [
            {"pages": [{"page": 1, "questions": [{"question_number": 1}]},
                       {"page": 2, "questions": [{"question_number": 2}]}]},
            {"pages": [{"page": 3, "questions": [{"question_number": 3}]}]},
        ]
        merged = _ocr_pages(cfg_for(2), QUESTION, pages(3), questions=True)
        self.assertEqual(len(rec.calls), 2)
        self.assertEqual(merged["ocr_calls"], 2)
        # page 3 forms its own singular batch: one image, classic prompt,
        # no redundant "PAGE 3:" label (that page is the whole request)
        self.assertEqual(len(rec.calls[1]["images"]), 1)
        self.assertIsNone(rec.calls[1]["labels"])
        # Every page is read exactly once, each row on the page it came from.
        self.assertEqual([(r["question_number"], r["page"])
                          for r in merged["questions"]], [(1, 1), (2, 2), (3, 3)])

    def test_a_flat_reply_to_a_batch_is_kept_on_its_first_page(self):
        """A model that ignores the grouping must not lose every row."""
        self.use(CallRecorder({"questions": [{"question_number": 7}]}))
        merged = _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=True)
        self.assertEqual([(r["question_number"], r["page"])
                          for r in merged["questions"]], [(7, 1)])

    def test_answers_batch_too_and_use_the_pages_template(self):
        rec = self.use(CallRecorder({"pages": [
            {"page": 1, "answers": [{"question_number": 1}]}]}))
        merged = _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=False,
                            candidates=[])
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(len(rec.calls[0]["images"]), 2)
        self.assertIn('"pages"', rec.calls[0]["prompt"])
        self.assertEqual([a["page"] for a in merged["answers"]], [1])

    def test_a_lane_pin_reaches_the_call(self):
        """Parallel lanes must stay pinned to ONE key through batching."""
        seen = {}

        class Pinned:
            def call_pinned(self, cfg, key, prompt, data, mime, **kwargs):
                seen["key"] = key
                seen["images"] = len(data) if isinstance(data, list) else 1
                return {"pages": []}, {}, {}

        runner.router = Pinned()
        _ocr_pages(cfg_for(2), QUESTION, pages(2), questions=True,
                   key_pin="KEY-A")
        self.assertEqual(seen, {"key": "KEY-A", "images": 2})


class ParallelLaneBatchingTests(RecorderCase):
    """The parallel lane must batch too: `_produce_round` used to OCR
    `pages[0]` and silently drop every later page of the file."""

    KEY = "k1111111111"

    def setUp(self):
        self._router = runner.router
        self.addCleanup(setattr, runner, "router", self._router)

    def test_a_lane_ocrs_every_page_in_one_batch(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call_pinned(cfg, key, prompt, data, mime, **kw):
            blobs = list(data) if isinstance(data, (list, tuple)) else [data]
            calls.append((key, len(blobs), kw.get("labels")))
            return ({"pages": [
                {"page": 1, "questions": [{"question_number": 1}]},
                {"page": 2, "questions": [{"question_number": 2}]}]}, {}, {})

        class Pinned:
            call_pinned = staticmethod(fake_call_pinned)

        runner.router = Pinned()
        cfg = SimpleNamespace(ocr_pages_per_call=2, ocr_provider="gemini",
                              gemini_key_pool=(self.KEY,))
        item = SimpleNamespace(file_path="p.pdf", type=QUESTION.type,
                               source_id="s1", mime_type="application/pdf",
                               rel_path="a/p.pdf", file_name="p.pdf",
                               subject="s", topic="t", grade=12)
        with tempfile.TemporaryDirectory() as tmp:
            item.file_path = os.path.join(tmp, "p.pdf")
            with open(item.file_path, "wb") as fh:
                fh.write(b"%PDF-1.4 test")
            with mock.patch.object(runner.stage, "page_images",
                                   lambda *a, **k: [(1, b"a"), (2, b"b")]):
                bundles = runner._produce_round(
                    cfg, None, [(0, item, self.KEY)],
                    hub=SimpleNamespace(emit=lambda ev: None))
        bundle = bundles[0]
        self.assertNotIn("error", bundle)
        self.assertEqual(calls, [(self.KEY, 2, ["PAGE 1:", "PAGE 2:"])])
        self.assertEqual([r["page"] for r in bundle["parsed"]["questions"]],
                         [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
