"""Surgical .env writer used by the Credentials tab.

Only the keys we manage are touched: existing lines are replaced in
place, missing ones are appended, and every other line (comments,
passwords, unrelated settings) survives byte-for-byte so handing the
project around never scrambles someone else's .env.
"""
import os
import re

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# Characters that would break an unquoted dotenv line.
_NEEDS_QUOTES = re.compile(r'[\s#\'"`$\\"]')


def _quote(value) -> str:
    v = str(value)
    if v == "" or _NEEDS_QUOTES.search(v):
        esc = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return v


def update_env_file(path: str, values: dict):
    """Set each KEY=value pair in `values` inside the .env at `path`.

    Creates the file (and rewrites atomically via a temp file) so a
    half-written .env can never survive a crash."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()

    remaining = dict(values)
    out = []
    for line in lines:
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out.append(f"{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={_quote(val)}")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
