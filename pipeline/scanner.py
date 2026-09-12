"""Port of the 'Scan Folder' code node."""
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config
from .utils import parse_number, sha1_hex

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
SKIP_DIRS = {"done", "failed"}
TYPE_DIRS = {"question": "سوال", "answer": "پاسخ"}

# Accepted layouts, relative to INPUT_ROOT. Anything else is reported and skipped
# instead of aborting the whole run, so one malformed folder cannot stop the batch.
EXPECTED_LAYOUTS = ("{subject}/{grade}/{topic}/{question|answer}/{file}",
                    "{subject}/{topic}/{question|answer}/{file}")

_noop_warn: Callable[[str], None] = lambda message: None


@dataclass
class SourceItem:
    source_id: str
    rel_path: str
    subject: str
    grade: object
    topic: str
    type: str
    file_name: str
    file_path: str
    file_size: int
    mime_type: str


def _split_layout(rel_parts):
    """Map path parts onto (subject, grade, topic).

    rel_parts always looks like [subject, ..., question|answer, file_name], so the
    folders between the subject and the type folder decide the shape:
    two of them means a grade level is present, one means it is not.
    """
    middle = rel_parts[1:-2]
    if len(middle) == 2:
        return rel_parts[0], middle[0], middle[1]
    if len(middle) == 1:
        return rel_parts[0], None, middle[0]
    return None


def _load_meta(meta_path: str, where: str, warn: Callable[[str], None]) -> dict:
    if not os.path.isfile(meta_path):
        # meta.json is only a label carrier; folder names still describe the item.
        return {}
    try:
        # utf-8-sig: the files are written by PowerShell, which prefixes a BOM
        # that plain utf-8 would reject.
        with open(meta_path, encoding="utf-8-sig") as fh:
            meta = json.load(fh)
    except Exception as exc:
        warn(f"Bad meta.json in {where} ({exc}) - falling back to folder names.")
        return {}
    return meta if isinstance(meta, dict) else {}


def scan(cfg: Config, warn: Callable[[str], None] | None = None) -> list[SourceItem]:
    """Collect every pending input file. `warn` receives one line per folder skipped."""
    warn = warn or _noop_warn
    root = cfg.input_root
    if not os.path.isdir(root):
        raise FileNotFoundError("ROOT NOT FOUND: " + root)

    now = time.time()
    found: list[SourceItem] = []

    def walk(directory: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        entries = list(entries)

        # 1) Recurse into sub-directories (skip done/failed, like the JS).
        for entry in entries:
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                walk(entry.path)

        # 2) Only folders named 'question' or 'answer' that actually hold input
        #    files are considered.
        dir_name = os.path.basename(directory)
        if dir_name not in TYPE_DIRS:
            return

        candidates = [
            f for f in entries
            if f.is_file() and f.name != "meta.json"
            and os.path.splitext(f.name)[1].lower() in ALLOWED_EXTENSIONS
        ]
        if not candidates:
            return

        where = os.path.relpath(directory, root).replace(os.sep, "/")
        meta = _load_meta(os.path.join(directory, "meta.json"), where, warn)
        item_type = meta.get("type") or TYPE_DIRS[dir_name]

        for f in candidates:
            stat = f.stat()
            # Skip files still being written (MIN_AGE_MS in the JS).
            if now - stat.st_mtime < cfg.min_age_seconds:
                continue

            rel_path = os.path.relpath(f.path, root).replace(os.sep, "/")
            parts = rel_path.split("/")
            layout = _split_layout(parts) if len(parts) >= 4 else None
            if layout is None:
                warn(f"Skipped {rel_path}: folder depth does not match "
                     + " or ".join(EXPECTED_LAYOUTS))
                continue

            subject, raw_grade, topic = layout
            grade = parse_number(raw_grade) if raw_grade is not None else None
            ext = os.path.splitext(f.name)[1].lower()
            found.append(SourceItem(
                source_id=sha1_hex(rel_path),
                rel_path=rel_path,
                # Metadata comes from the folder, never from meta.json's labels.
                subject=subject,
                grade=grade if grade is not None else raw_grade,
                topic=topic,
                type=item_type,
                file_name=f.name,
                file_path=f.path,
                file_size=stat.st_size,
                mime_type=ALLOWED_EXTENSIONS[ext],
            ))

    walk(root)
    # Questions first, so answers can link to them within the same run.
    found.sort(key=lambda item: 0 if item.type == "سوال" else 1)
    return found
