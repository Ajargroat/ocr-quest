"""Stage 3 - geometry: one box per block + the crop each figure becomes.

Text-lane blocks already carry exact text-layer boxes from Stage 1; this stage
only unions the figure boxes a block owns into its crop box. Vision blocks get
their question boxes from a page-batched locate pass, then a second band pass
sends several question slices as separate image parts in one request and asks
for the picture inside each slice.

Bands are not stitched into one sheet: each keeps its own scale and its own
retry, and the reply is mapped back through the band rectangle.

Crops are rendered lazily into the job's ``crops/`` folder and cached: a
text-lane crop is exact, so it is auto-approved; a vision crop waits for the
human gate before anything can upload.
"""
import base64
import json
import os
import time

from PIL import Image

from pipeline import converter, gemini as gemini_mod

from pipeline.studio import bands, extract, llm, workspace

# A box smaller than this (of 1000) is treated as a bad reply, not a crop.
MIN_BOX = 8

# Band padding, in page-normalized units; the retry doubles it.
BAND_PAD_Y = 25
BAND_PAD_X = 40

LOCATE_PROMPT = """You locate question regions on scanned exam pages.

The request carries one page image after each "PAGE <n>:" label, and a JSON
list of the questions on each page. Return ONE JSON object:
{"pages": [{"page": <n>, "boxes": [{"key": "...", "bbox": [y0, x0, y1, x1], "figure": [y0, x0, y1, x1] | null}]}]}
- one entry per requested question, in the same order, boxes normalized to
0-1000 relative to THAT page (y from the top, x from the left).

"bbox" is tight around everything that belongs to the question (stem, options,
its figure) and clear of its neighbours.

"figure" is the box of the PICTURE that belongs to that question - a chart,
plot, diagram, molecular structure, apparatus drawing or photo - or null when
the question carries no picture. Text, formulas, tables and option lists are
not pictures: a text-only question has "figure": null.

A question you cannot locate gets "bbox": null.

pages:
"""

GEOMETRY_PROMPT = """You locate question regions on one scanned exam page.

Return ONE JSON object: {"boxes": [{"key": "...", "bbox": [y0, x0, y1, x1], "figure": [y0, x0, y1, x1] | null}]}
- one entry per requested question, in the same order, boxes normalized to
0-1000 relative to the page (y from the top, x from the left).

"bbox" is tight around everything that belongs to the question (stem, options,
its figure) and clear of its neighbours.

"figure" is the box of the PICTURE that belongs to that question - a chart,
plot, diagram, molecular structure, apparatus drawing or photo - or null when
the question carries no picture. Text, formulas, tables and option lists are
not pictures: a text-only question has "figure": null.

A question you cannot locate gets "bbox": null.

questions:
"""

BAND_PROMPT = """You find the picture inside pictured question bands.

The request carries one band image after each "BAND <key>:" label. A band is a
horizontal slice of an exam page around ONE question, and the list below names
the bands in the same order.

Return ONE JSON object:
{"bands": [{"key": "...", "figures": [[y0, x0, y1, x1], ...]}]}
- one entry per band, boxes normalized to 0-1000 relative to THAT band (y from
the top, x from the left).

"figures" lists every chart, plot, diagram, molecular structure, apparatus
drawing or photo inside the band, each tight around the picture. A band with
no picture gets "figures": [].

Do not return the question text, its options, formulas or tables as a figure.

bands:
"""


def _cfg_int(cfg, name, default):
    """Read one cfg integer, tolerating a bare dict/None in tests."""
    try:
        return int(getattr(cfg, name, default) or default)
    except (TypeError, ValueError):
        return int(default)


def _clean_bbox(raw):
    """Any reply box -> 4 ints clamped to 0-1000, or None when degenerate."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        y0, x0, y1, x1 = (int(round(float(v))) for v in raw)
    except (TypeError, ValueError):
        return None
    y0, x0 = max(0, min(1000, y0)), max(0, min(1000, x0))
    y1, x1 = max(0, min(1000, y1)), max(0, min(1000, x1))
    if y1 - y0 < MIN_BOX or x1 - x0 < MIN_BOX:
        return None
    return [y0, x0, y1, x1]


def _questions(blocks):
    return [{"key": b["key"], "number": b.get("number"),
             "text": (b.get("text") or "")[:120]} for b in blocks]


def _read_b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


# -- locate pass ------------------------------------------------------------

def _parse_locate(result, page_nos):
    """{(page_no, key): {"bbox", "figure"}} from a batch or single reply."""
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        try:
            parsed = gemini_mod.extract_json(result.get("text") or "")
        except ValueError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    out = {}
    if isinstance(parsed.get("pages"), list):
        for page_entry in parsed["pages"]:
            if not isinstance(page_entry, dict):
                continue
            try:
                page_no = int(page_entry.get("page"))
            except (TypeError, ValueError):
                continue
            if page_no not in page_nos:
                continue
            _take_locate(out, page_no, page_entry.get("boxes"))
        return out
    # A one-page request may answer with the old {"boxes": [...]} shape.
    if len(page_nos) == 1:
        _take_locate(out, page_nos[0], parsed.get("boxes"))
    return out


def _take_locate(out, page_no, entries):
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        box = _clean_bbox(entry.get("bbox"))
        if not box:
            continue
        out[(page_no, entry["key"])] = {
            "bbox": box, "figure": _clean_bbox(entry.get("figure"))}


def _vision_boxes(cfg, job_id, page_no, image_rel, blocks):
    """One-page locate fallback -> {key: {"bbox", "figure"}}.

    Raises on failure. ``bbox`` is the question region; ``figure`` is the
    picture inside it, and stays None for a text-only question.
    """
    path = os.path.join(workspace.job_dir(job_id), image_rel)
    questions = _questions(blocks)
    result = llm.call_stage(
        cfg, "vision", GEOMETRY_PROMPT + json.dumps(questions,
                                                    ensure_ascii=False),
        _read_b64(path), "image/jpeg")
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        parsed = gemini_mod.extract_json(result.get("text") or "")
    if not isinstance(parsed, dict):
        parsed = {}
    out = {}
    for entry in (parsed or {}).get("boxes") or []:
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        box = _clean_bbox(entry.get("bbox"))
        if not box:
            continue
        out[entry["key"]] = {"bbox": box,
                             "figure": _clean_bbox(entry.get("figure"))}
    return out


def _locate_batch(cfg, job_id, page_nos, by_page, pages):
    """Locate several pages in one request -> {(page, key): entry}."""
    payloads, labels, request = [], [], []
    for page_no in page_nos:
        page = pages.get(page_no) or {}
        image_rel = page.get("image") or ""
        if not image_rel:
            continue
        payloads.append(_read_b64(
            os.path.join(workspace.job_dir(job_id), image_rel)))
        labels.append("PAGE %d:" % page_no)
        request.append({"page": page_no,
                        "questions": _questions(by_page[page_no])})
    if not payloads:
        return {}
    prompt = LOCATE_PROMPT + json.dumps(request, ensure_ascii=False)
    result = llm.call_stage(cfg, "vision", prompt, images=payloads,
                            labels=labels, json_mode=True, thinking=0)
    return _parse_locate(result, [entry["page"] for entry in request])


# -- band pass --------------------------------------------------------------

def _band_page(job_id, block, cfg, cache):
    """A high-quality render of the block's page, cached per batch.

    Prefers the source PDF over the stored page so an old job's q20 page is
    not what the model squeezes the figure out of.
    """
    page_no = int(block.get("page"))
    if page_no in cache:
        return cache[page_no]
    edge = _cfg_int(cfg, "studio_vision_long_edge", 3000)
    src = workspace.source_path(job_id) or ""
    pil = None
    if src.lower().endswith(".pdf"):
        try:
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(src)
            try:
                pil = extract._render_pil(doc[page_no - 1])
            finally:
                doc.close()
        except Exception:
            pil = None
    if pil is None:
        rel = (block.get("source") or {}).get("image") or ""
        path = os.path.join(workspace.job_dir(job_id), rel) if rel else src
        if path and os.path.exists(path):
            with Image.open(path) as im:
                im.load()
                pil = im.convert("RGB")
    if pil is None:
        raise ValueError("no page render for page %s" % page_no)
    pil = extract._fit_long_edge(pil, edge)
    cache[page_no] = pil
    return pil


def _cut_bands(cfg, job_id, blocks, pad_y=BAND_PAD_Y, pad_x=BAND_PAD_X):
    """Cut one JPEG band per pictured block; unrenderable pages are skipped."""
    quality = _cfg_int(cfg, "studio_vision_quality", 88)
    cache, items = {}, []
    for block in blocks:
        try:
            page_no = int(block.get("page"))
            pil = _band_page(job_id, block, cfg, cache)
        except Exception:
            continue
        band = bands.cut(pil, block.get("bbox"), block["key"], page_no,
                         pad_y=pad_y, pad_x=pad_x)
        if not band:
            continue
        jpeg = bands.jpeg_bytes(pil, band, quality)
        try:
            with open(workspace.band_path(job_id, block["key"]), "wb") as fh:
                fh.write(jpeg)
        except OSError:
            pass
        items.append({"block": block, "band": band, "jpeg": jpeg})
    return items


def _band_call(cfg, items):
    labels = ["BAND %s:" % item["band"].key for item in items]
    request = [{"key": item["band"].key,
                "hint": (item["block"].get("text") or "")[:120]}
               for item in items]
    prompt = BAND_PROMPT + json.dumps(request, ensure_ascii=False)
    return llm.call_stage(cfg, "vision", prompt,
                          images=[base64.b64encode(item["jpeg"]).decode("ascii")
                                   for item in items],
                          labels=labels, json_mode=True, thinking=0)


def _parse_bands(result, items):
    """({key: [box]}, relative) from a band reply; relative=False is page coords."""
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        try:
            parsed = gemini_mod.extract_json(result.get("text") or "")
        except ValueError:
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if isinstance(parsed.get("bands"), list):
        out = {}
        for entry in parsed["bands"]:
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            out[str(entry["key"])] = [
                box for box in (_clean_bbox(raw)
                                for raw in entry.get("figures") or [])
                if box]
        return out, True
    # A one-band request may answer in the old single-page shape. Those
    # coordinates are page-relative (the locate contract), not band-relative.
    if len(items) == 1 and isinstance(parsed.get("boxes"), list):
        key = items[0]["band"].key
        for entry in parsed["boxes"]:
            if isinstance(entry, dict) and entry.get("key") == key:
                figure = _clean_bbox(entry.get("figure"))
                return ({key: [figure]} if figure else {key: []}), False
    return {}, True


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _pick_figure(figures):
    """One crop box for a block with several pictures.

    Side-by-side parts of one figure union cleanly; two unrelated pictures
    separated by text stay separate, and the largest one becomes the crop.
    """
    if len(figures) <= 1:
        return figures[0] if figures else None
    union = [min(b[0] for b in figures), min(b[1] for b in figures),
             max(b[2] for b in figures), max(b[3] for b in figures)]
    total = sum(_area(box) for box in figures)
    if total and _area(union) <= total * 1.8:
        return union
    return max(figures, key=_area)


def _store_figures(block, figures):
    clean = [box for box in (_clean_bbox(raw) for raw in figures or []) if box]
    block["figure_bboxes"] = clean
    block["figure_bbox"] = _pick_figure(clean) if clean else None


def _apply_band_reply(items, found, relative, stats):
    """Write the valid figures; return the bands that need one more cut."""
    retry = []
    for item in items:
        key = item["band"].key
        block = item["block"]
        if key not in found:
            continue                       # no answer: keep the locate figure
        figs = found[key]
        if not relative:
            _store_figures(block, figs)
            continue
        if not figs:
            _store_figures(block, [])
            stats["figure_missing"] += 1
            continue
        good = []
        for fig in figs:
            ok, _reason = bands.qa(fig, item["band"])
            if ok:
                good.append(item["band"].to_page(fig))
        if good:
            _store_figures(block, good)
            stats["qa_rejects"] += len(figs) - len(good)
        else:
            retry.append(item)
    return retry


def _retry_band(cfg, job_id, item, stats):
    """Re-cut one failed band with double padding, then give up honestly."""
    key = item["band"].key
    block = item["block"]
    stats["band_calls"] += 1
    try:
        wider = _cut_bands(cfg, job_id, [block], pad_y=BAND_PAD_Y * 2,
                           pad_x=BAND_PAD_X * 2)
        if not wider:
            _store_figures(block, [])
            stats["figure_missing"] += 1
            return
        found, relative = _parse_bands(_band_call(cfg, wider), wider)
    except Exception as exc:
        stats["errors"].append({"key": key, "error": "band retry: %s" % exc})
        return                             # keep the locate figure
    if key not in found:
        _store_figures(block, [])
        stats["figure_missing"] += 1
        return
    figs = found[key]
    if not relative:
        _store_figures(block, figs)
        return
    good = [wider[0]["band"].to_page(fig) for fig in figs
            if bands.qa(fig, wider[0]["band"])[0]]
    _store_figures(block, good)
    if not good:
        stats["qa_rejects"] += 1
        stats["figure_missing"] += 1


def _figure_pass(cfg, job_id, chunk, stats):
    """One band batch; falls back to single bands when a lane refuses many."""
    items = _cut_bands(cfg, job_id, chunk)
    if not items:
        return
    stats["bands"] += len(items)
    stats["band_calls"] += 1
    try:
        found, relative = _parse_bands(_band_call(cfg, items), items)
    except Exception as exc:
        if len(items) == 1:
            stats["errors"].append({"key": items[0]["band"].key,
                                    "error": "band pass: %s" % exc})
            return
        # Some OpenAI-compatible providers reject several images per request;
        # one band at a time still gets the better crop, just more calls.
        found, relative = {}, True
        for item in items:
            stats["band_calls"] += 1
            try:
                one, rel = _parse_bands(_band_call(cfg, [item]), [item])
                found.update(one)
                if not rel:
                    relative = False
            except Exception:
                pass
    retry = _apply_band_reply(items, found, relative, stats)
    for item in retry:
        _retry_band(cfg, job_id, item, stats)


# -- crops ------------------------------------------------------------------

def _crop_spec(block, assets, figure=None):
    """(crop_bbox, source) for one block: its picture, or nothing at all.

    A question with no picture gets no crop. Cropping the whole question
    region would ship a picture the row never had (and an invented bbox).
    """
    boxes = [assets[i]["bbox"] for i in block.get("images") or []
             if i in assets and assets[i].get("bbox")]
    if boxes:
        union = boxes[0]
        for box in boxes[1:]:
            union = [min(union[0], box[0]), min(union[1], box[1]),
                     max(union[2], box[2]), max(union[3], box[3])]
        return union, "figure"
    figure = _clean_bbox(figure)
    if figure:
        return figure, "figure"
    return None, None


def _norm_to_pixels(bbox, width, height):
    y0, x0, y1, x1 = bbox
    return (max(0, int(x0 / 1000.0 * width) - 2),
            max(0, int(y0 / 1000.0 * height) - 2),
            min(width, int(x1 / 1000.0 * width) + 2),
            min(height, int(y1 / 1000.0 * height) + 2))


def _page_pil(job_id, entry):
    """The page an entry's crop comes from, as an independent RGB image."""
    rel = entry.get("crop_image") or ""
    if rel:
        with Image.open(os.path.join(workspace.job_dir(job_id), rel)) as im:
            im.load()
            return im.convert("RGB")
    src = workspace.source_path(job_id)
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(src)
    try:
        return extract._render_pil(doc[entry["crop_page"] - 1])
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _crop_key(entry, bbox):
    """Cache identity of one crop: page + method + the box it was cut with."""
    return "%s|%s|%s" % (entry.get("crop_page"), entry.get("crop_method"),
                          ",".join(str(int(v)) for v in bbox))


def crop_object(cfg, folder, key):
    """The bucket object a block's crop lives at - stable per source + key.

    Pure string math (no I/O), so the importer, the re-crop endpoint and any
    future consumer always agree on the id a row points at.
    """
    prefix = (getattr(cfg, "studio_crop_prefix", "") or "crops").strip("/")
    return "%s/%s/%s.jpg" % (prefix, folder, key)


def render_crop(job_id, key, crop_bbox=None, force=False):
    """Render one block's crop (cached). Returns the geometry entry.

    ``crop_bbox`` overrides the stored box - that is how a human's reviewed
    box reaches the renderer without ever editing the stage artifacts.
    """
    geometry = workspace.read_artifact(job_id, "geometry") or {}
    entry = (geometry.get("boxes") or {}).get(key)
    if not entry:
        return None
    bbox = _clean_bbox(crop_bbox or entry.get("crop_bbox"))
    if not bbox:
        return None
    spec = _crop_key(entry, bbox)
    dest = workspace.crop_path(job_id, key)
    if not force and entry.get("spec") == spec and os.path.exists(dest):
        return entry
    pil = _page_pil(job_id, entry)
    box = _norm_to_pixels(bbox, pil.width, pil.height)
    if box[2] - box[0] < 12 or box[3] - box[1] < 12:
        return None
    converter._save_capped(pil.crop(box), dest, converter.QUALITY_START)
    entry["crop"] = os.path.relpath(dest, workspace.job_dir(job_id))
    entry["crop_bytes"] = os.path.getsize(dest)
    entry["crop_bbox"] = bbox
    entry["spec"] = spec
    workspace.write_artifact(job_id, "geometry", geometry)
    return entry


# -- the stage entry point --------------------------------------------------

def run(job_id, cfg, emit=None):
    """Locate every block and plan its crop; writes geometry.json."""
    emit = emit or (lambda *args, **kwargs: None)
    layer2 = workspace.read_artifact(job_id, "layer2")
    if not layer2:
        raise ValueError("job has no sorted bulk yet - run enrich first")

    assets = {a["id"]: a for a in layer2.get("assets") or []
              if isinstance(a, dict) and a.get("id")}
    pages = {p.get("page"): p for p in layer2.get("pages") or []
             if isinstance(p, dict)}
    blocks = layer2.get("blocks") or []

    stats = {"blocks": len(blocks), "boxes": 0, "vision_pages": 0,
             "missing": 0, "crops": 0, "calls": 0, "bands": 0,
             "band_calls": 0, "figure_missing": 0, "qa_rejects": 0,
             "kept_previous": 0, "errors": []}
    previous = ((workspace.read_artifact(job_id, "geometry") or {}).get("boxes")
                or {})

    # Vision blocks: locate in page batches; one page per call on failure.
    by_page = {}
    for block in blocks:
        if block.get("bbox") and block.get("bbox_source") == "text_layer":
            continue
        if (block.get("source") or {}).get("method") != "vision":
            continue
        by_page.setdefault(block.get("page"), []).append(block)

    per_call = max(1, _cfg_int(cfg, "studio_pages_per_call", 3))
    page_nos = sorted(by_page, key=lambda p: p or 0)
    for start in range(0, len(page_nos), per_call):
        batch = page_nos[start:start + per_call]
        usable = []
        for page_no in batch:
            if (pages.get(page_no) or {}).get("image"):
                usable.append(page_no)
            else:
                stats["errors"].append({"page": page_no,
                                        "error": "no page image stored"})
        if not usable:
            continue
        emit("geometry", "info",
             "locating page(s) %s" % ", ".join(str(p) for p in usable))
        stats["calls"] += 1
        try:
            found = _locate_batch(cfg, job_id, usable, by_page, pages)
        except Exception as exc:
            found = {}
            fault = getattr(exc, "fault", {}) or {}
            if fault.get("exhausted") or fault.get("kind") in ("quota",
                                                               "bad_key"):
                # A dead pool must not be retried page by page: that burns the
                # remaining keys on the same refusal.
                stats["errors"].append({"pages": usable, "error": str(exc)})
                emit("geometry", "error", "pages %s: %s" % (usable, exc))
            else:
                for page_no in usable:
                    stats["calls"] += 1
                    try:
                        one = _vision_boxes(
                            cfg, job_id, page_no,
                            (pages.get(page_no) or {}).get("image") or "",
                            by_page[page_no])
                    except Exception as inner:
                        stats["errors"].append({"page": page_no,
                                                "error": str(inner)})
                        emit("geometry", "error",
                             "page %s: %s" % (page_no, inner))
                        continue
                    for key, hit in one.items():
                        found[(page_no, key)] = hit
                if not found:
                    stats["errors"].append({"pages": usable,
                                            "error": str(exc)})
                    emit("geometry", "error", "pages %s: %s"
                         % (usable, exc))
        stats["vision_pages"] += len(usable)
        for page_no in usable:
            for block in by_page[page_no]:
                hit = found.get((page_no, block["key"])) or {}
                if hit.get("bbox"):
                    block["bbox"] = hit["bbox"]
                    block["bbox_source"] = "vision"
                if hit.get("figure"):
                    block["figure_bbox"] = hit["figure"]
                    block["figure_bboxes"] = [hit["figure"]]

    # A failed page (quota, tunnel down) must not erase a box we already have:
    # keep the previous locate result for any block this run could not place.
    for block in blocks:
        old = previous.get(block["key"]) or {}
        if not block.get("bbox") and old.get("bbox"):
            block["bbox"] = old["bbox"]
            block["bbox_source"] = old.get("source") or "vision"
            stats["kept_previous"] += 1

    # Pictured questions: one band pass, several bands per request.
    pictured = [block for block in blocks
                if block.get("bbox_source") == "vision"
                and not block.get("images")
                and block.get("figure_bbox")]
    band_batch = max(1, _cfg_int(cfg, "studio_band_batch", 6))
    for start in range(0, len(pictured), band_batch):
        chunk = pictured[start:start + band_batch]
        emit("geometry", "info",
             "checking %d pictured question(s)" % len(chunk))
        _figure_pass(cfg, job_id, chunk, stats)

    entries = {}
    for block in blocks:
        box = _clean_bbox(block.get("bbox"))
        figures = [box for box in
                   (_clean_bbox(raw) for raw in
                    block.get("figure_bboxes") or []) if box]
        crop_bbox, crop_source = _crop_spec(block, assets,
                                            block.get("figure_bbox"))
        if not box:
            stats["missing"] += 1
        else:
            stats["boxes"] += 1
        if crop_bbox:
            stats["crops"] += 1
        # Only the text lane's figure boxes are exact enough to skip review.
        exact = (crop_source == "figure"
                 and block.get("bbox_source") == "text_layer")
        entry = {"bbox": box, "source": block.get("bbox_source"),
                 "crop_bbox": crop_bbox, "crop_source": crop_source,
                 "figure_bboxes": figures,
                 "crop_page": block.get("page"),
                 "crop_method": (block.get("source") or {}).get("method"),
                 "crop_image": (block.get("source") or {}).get("image"),
                 "crop_approved": True if exact else None,
                 "crop": None, "crop_bytes": None, "spec": None}
        entries[block["key"]] = entry

    geometry = {
        "version": 1,
        "job_id": job_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "boxes": entries,
        "stats": stats,
    }
    workspace.write_artifact(job_id, "geometry", geometry)
    workspace.log_event(
        job_id, "geometry", "success",
        "located %d/%d block(s) - %d crop(s) planned"
        % (stats["boxes"], stats["blocks"], stats["crops"]))
    return {"job": workspace.summary(job_id), "stats": stats}
