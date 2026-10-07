"""Pure-Python PDF->JPEG converter watcher, embedded in the FastAPI server.

Replicates converter/watch.sh behaviour without Docker: sweep the
konkour-ocr/ tree every SCAN_INTERVAL_SECONDS, render each mature PDF
page to JPEG next to the PDF, then move the PDF to <dir>/done/ (or
<dir>/failed/ on render error). Maturity (MIN_AGE_SECONDS) and the
done/failed skip keep scans from racing a live run — same gates as
pipeline/scanner.py (SKIP_DIRS + min_age_seconds).
"""
import os
import threading
import time

SCAN_INTERVAL_SECONDS = 15

_done = threading.Event()
_thread = None
_lock = threading.Lock()


def start(cfg):
    """Start the background sweep thread (idempotent). Returns the thread."""
    global _thread
    with _lock:
        _done.clear()
        if _thread is not None and _thread.is_alive():
            return _thread
        from pipeline import converter as _self  # kept flat, avoid circulars
        _thread = threading.Thread(target=_self._loop, args=(cfg,), daemon=True)
        _thread.start()
        return _thread


def stop():
    """Signal the sweep loop to exit."""
    _done.set()


def reset_for_tests():
    """Clear the stop event so unit tests can re-run sweeps."""
    _done.clear()


def _mature(path, min_age_seconds):
    try:
        return (time.time() - os.stat(path).st_mtime) >= min_age_seconds
    except OSError:
        return False


def _under_skip_dir(path, root):
    """True when path sits under any done/ or failed/ directory."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False
    parts = rel.split(os.sep)
    return "done" in parts or "failed" in parts


def _iter_pdfs(root):
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune done/failed subtrees so archived PDFs are never re-scanned.
        dirnames[:] = [d for d in dirnames if d not in ("done", "failed")]
        for name in filenames:
            if name.lower().endswith(".pdf"):
                full = os.path.join(dirpath, name)
                if _under_skip_dir(full, root):
                    continue
                yield full


# --- per-page size cap + concurrent page workers (see _render_pdf) ---------
PAGE_SIZE_CAP = 200 * 1024   # every page JPEG must fit in 200 KB
QUALITY_START = 78           # first-pass quality (CONVERTER_RASTER_QUALITY default)
QUALITY_STEP = 10
QUALITY_FLOOR = 20           # stop stepping down here; the DPI is never lowered
RENDER_WORKERS = 3           # page threads (patchable in tests)
# PDFium is not thread-safe: "you may still use pdfium in a threaded context
# if it is ensured that only a single pdfium call can be made at a time
# (e.g. via mutex)" - pypdfium2 docs. One process-wide lock, render only.
_PDFIUM_LOCK = threading.Lock()


def _save_capped(pil, path, quality):
    """Save path as JPEG, re-encoding at a lower quality until it fits.

    Returns the quality actually written. Quality never drops below
    QUALITY_FLOOR and the DPI is never touched (PREFERENCE section 5).
    """
    q = max(QUALITY_FLOOR, int(quality))
    while True:
        pil.save(path, "JPEG", quality=q)
        if q <= QUALITY_FLOOR or os.path.getsize(path) <= PAGE_SIZE_CAP:
            return q
        q = max(QUALITY_FLOOR, q - QUALITY_STEP)


def _render_pdf(pdf_path, out_base, dpi, quality):
    """Render every PDF page to <out_base>-<n>.jpg. Returns page count.

    Pages are rendered by RENDER_WORKERS threads (pdfium calls serialised
    behind _PDFIUM_LOCK) and each page is capped at PAGE_SIZE_CAP bytes.
    Raises on any render error so the caller can archive to failed/.
    """
    import pypdfium2 as pdfium
    from PIL import Image

    doc = pdfium.PdfDocument(pdf_path)
    try:
        scale = dpi / 72.0
        total = len(doc)
        if total == 0:
            raise ValueError("PDF has no renderable pages")

        def one(i):
            with _PDFIUM_LOCK:      # pdfium: one call at a time, ever
                bitmap = doc[i].render(scale=scale)
                pil = bitmap.to_pil()
                # to_pil() shares the bitmap's buffer for RGBA/RGBX/L, so
                # keep the bitmap alive until the encode below is done.
            if pil.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", pil.size, (255, 255, 255))
                background.paste(pil, mask=pil.split()[-1] if pil.mode in ("RGBA", "LA") else None)
                pil = background
            elif pil.mode != "RGB":
                pil = pil.convert("RGB")
            try:
                _save_capped(pil, f"{out_base}-{i + 1}.jpg", quality)
            finally:
                del bitmap          # buffer must outlive the encode (shared)

        if total == 1 or RENDER_WORKERS <= 1:
            for i in range(total):
                one(i)
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(RENDER_WORKERS, total)) as pool:
                list(pool.map(one, range(total)))   # re-raises the first error
        return total
    finally:
        doc.close()


def _archive(pdf_path, ok):
    """Move the source PDF to <dir>/done/ (ok) or <dir>/failed/ (error)."""
    target_dir = os.path.join(os.path.dirname(pdf_path), "done" if ok else "failed")
    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, os.path.basename(pdf_path))
    try:
        os.replace(pdf_path, dest)
    except OSError:
        pass
    return dest


def _sweep_once(cfg):
    """One watch.sh-equivalent pass. Returns (converted, failed) counts."""
    root = cfg.input_root
    min_age = getattr(cfg, "min_age_seconds", 30)
    dpi = int(os.getenv("CONVERTER_DPI", "170"))
    quality = int(os.getenv("CONVERTER_RASTER_QUALITY", str(QUALITY_START)))
    converted, failed = 0, 0
    if not os.path.isdir(root):
        return converted, failed
    for pdf in sorted(_iter_pdfs(root)):
        if _done.is_set():
            break
        if not _mature(pdf, min_age):
            continue
        directory = os.path.dirname(pdf)
        base = os.path.splitext(os.path.basename(pdf))[0]
        uid = base.split("-", 1)[0] if "-" in base else ""
        if len(uid) == 12:
            try:
                from pipeline import uploads as _uploads
                _uploads.mark(uid, "extracting", "Converter is extracting pages")
            except Exception:
                pass
        print(f"[converter] rasterizing: {pdf}")
        try:
            _render_pdf(pdf, os.path.join(directory, base), dpi, quality)
        except Exception as exc:
            print(f"[converter] FAILED (moved to failed/): {pdf} ({exc})")
            _archive(pdf, False)
            failed += 1
            _mark_upload(uid, "failed", f"Render failed: {exc}")
        else:
            _archive(pdf, True)
            print(f"[converter] finished: {base}")
            converted += 1
            _mark_upload(uid, "done", "Pages extracted to the watched tree")
    return converted, failed


def _mark_upload(uid, status, detail=""):
    if not uid or len(uid) != 12:
        return
    try:
        from pipeline import uploads as _uploads
        _uploads.mark(uid, status, detail)
    except Exception:
        pass


def _loop(cfg):
    interval = int(os.getenv("CONVERTER_SCAN_INTERVAL_SECONDS", "15") or "15")
    while not _done.is_set():
        try:
            _sweep_once(cfg)
        except Exception as exc:  # a sweep crash must never kill the server
            print(f"[converter] sweep failed: {exc}")
        _done.wait(max(1, interval))
