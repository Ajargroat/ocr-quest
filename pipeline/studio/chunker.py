"""Stage 1 helper - page-aligned request windows under one budget.

A window is a run of contiguous PDF pages: the unit for progress, for page
attribution and for the split-continuation check. A page is never split and
never dropped, so every block can name the window it came from; a single page
bigger than the budget still becomes a window of its own (honest overflow).
"""

def chunk_pages(page_chars, max_chars=None, max_pages=None):
    """Group contiguous page char counts into windows.

    ``page_chars`` is one non-space char count per page, in page order. Both
    bounds are optional but at least one must be set: the text lane bounds by
    ``max_chars``, the image lane (pages carry no text) bounds by ``max_pages``.
    Returns 1-based ``[{"index", "chunks", "page_start", "page_end", "pages",
    "chars"}]`` - every page in exactly one window, in order.
    """
    if max_chars is None and max_pages is None:
        raise ValueError("chunk_pages needs max_chars or max_pages")
    limit_chars = None if max_chars is None else max(1, int(max_chars))
    limit_pages = None if max_pages is None else max(1, int(max_pages))
    windows, current = [], []

    def flush():
        if not current:
            return
        windows.append({
            "index": len(windows) + 1,
            "page_start": current[0]["page"],
            "page_end": current[-1]["page"],
            "pages": [item["page"] for item in current],
            "chars": sum(item["chars"] for item in current),
        })
        current.clear()

    for number, chars in enumerate(page_chars or [], start=1):
        page = {"page": number, "chars": max(0, int(chars or 0))}
        if current:
            used = sum(item["chars"] for item in current)
            if ((limit_chars is not None and used + page["chars"] > limit_chars)
                    or (limit_pages is not None
                        and len(current) >= limit_pages)):
                flush()
        current.append(page)
    flush()
    for window in windows:
        window["chunks"] = len(windows)
    return windows
