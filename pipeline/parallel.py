"""Parallel OCR engine helpers — pure logic, no I/O, no DB, no network.

The parallel run splits every file into three phases:

  produce  (lane pool, concurrent)   read + Supabase upload + OCR with a
                                     PINNED key (see GeminiRouter.call_pinned)
  import   (run thread, file order)  Postgres writes + archive to done/

Only the OCR calls run concurrently. The single Postgres connection a
`Database` holds is only ever *written* on the run thread, so Q2=B's
"completed results are imported in original file order" holds by
construction — lane finish order never reaches the DB.

Everything in this module is import-free and unit-testable (no cfg, no
router, no threads). The threaded worker itself lives in `runner.py`,
which owns the cfg/hub/db it needs.
"""


def lane_keys(cfg, enabled_keys=None, plan=None):
    """The healthy lanes for a parallel run, in pool order.

    A key gets a lane only when it is BOTH:
      * enabled — `enabled_keys` is the Credentials tab's session on/off
        selection (a list of key strings; None means "all keys are on"); and
      * usable right now — present in `plan`, the router's ordered list of
        keys not in cooldown and with a non-empty model ladder.

    Returns a list of key strings (possibly empty — the caller falls back to
    one-by-one)."""
    pool = list(getattr(cfg, "gemini_key_pool", ()) or ())
    if enabled_keys is not None:
        allowed = set(enabled_keys)
        pool = [k for k in pool if k in allowed]
    if plan is not None:
        usable = {key for _i, key, _models, _lat, _pen in plan}
        pool = [k for k in pool if k in usable]
    return pool


def round_plan(items, lanes):
    """[(round_no, [(index0, item, key), …]), …] — one file per lane per
    round, in original file order; a trailing round may hold fewer files.

    `index0` is the item's position in `items` (0-based), which is the global
    file order the import phase restores. Lane `n` always takes the n-th file
    of each round, so every round gives each key at most one file."""
    lanes = list(lanes)
    rounds = []
    if not lanes:
        return rounds
    pairs = list(enumerate(items))
    width = len(lanes)
    for round_no, start in enumerate(range(0, len(pairs), width), 1):
        chunk = pairs[start:start + width]
        rounds.append((round_no,
                       [(idx, item, lanes[i])
                        for i, (idx, item) in enumerate(chunk)]))
    return rounds


def import_order(results):
    """[(index0, outcome), …] sorted by index0 — the global file order.

    `results` is any iterable of (index0, outcome) pairs in whatever order the
    lanes finished; this restores 01, 02, 03 … before any DB write."""
    return sorted(results, key=lambda pair: pair[0])


def type_segments(items):
    """Contiguous runs of same-`type` items, in order.

    The scanner sorts questions before answers, so this yields one question
    segment then one answer segment. Running the rounds per segment means an
    answer file's OCR never starts before the run's questions are imported —
    its `fetch_context_questions` read then sees exactly what the serial loop
    would (answers link to questions written earlier in the same run)."""
    segments = []
    for item in items:
        if segments and segments[-1][0].type == item.type:
            segments[-1].append(item)
        else:
            segments.append([item])
    return segments
