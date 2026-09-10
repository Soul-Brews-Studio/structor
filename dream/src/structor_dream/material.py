"""Choosing what the model reads: the sessions of a week, a session's turns within a budget, hits by horizon.

Nothing in this module talks to a model. It is the sampling layer, and it is
deterministic on purpose: the same replica and the same arguments pick the
same sessions and the same turns, so a re-run digests nothing new and a test
can assert exact choices.

The two stratifiers are the session-dream lab's rule with one axis swapped:

- ``stratify`` (week mode) round-robins across **projects** — inside a
  project the sessions with the most human turns come first, because human
  turns are intent — until ``max_sessions`` is reached;
- ``topic_material`` (topic mode) round-robins across **time horizons**
  (short ≤ 7 days, mid ≤ 30, long ≤ 90, archive beyond — computed from the
  event timestamp at run time, never stored), and inside a horizon across
  projects, after a relevance floor.

``budget_sample`` picks a session's turns for one digest call: every human
turn first, in order, then assistant turns spaced evenly through the session
to fill what is left of the budget.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from structor_lance.rag import MIN_TEXT
from structor_lance.schema import Event, SessionWeek
from structor_lance.sync import Replica, stamp_to_dt

from .config import parse_week

MIN_USER_TURNS = 2  # a session with one human turn is a one-shot prompt, not a conversation
MIN_EVENTS = 10
DEFAULT_MAX_SESSIONS = 40
DIGEST_BUDGET = 6000  # characters of turn text per digest call (fences come on top)
ITEM_CAP = 700  # characters kept of one turn; a 46k-char session cannot be read whole anyway
HORIZONS = ("short", "mid", "long", "archive")
HORIZON_DAYS = {"short": 7, "mid": 30, "long": 90}
RELEVANCE_FLOOR = 0.5  # a hit scoring under this fraction of the best hit is noise for the theme
SESSION_COLUMNS = ["session", "project", "iso_week", "event_count", "user_count", "assistant_count",
                   "first_ts", "last_ts", "updated"]


def quoted(value: str) -> str:
    """One SQL string literal for a Lance predicate."""
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------- week mode: sessions


def week_rows(replica: Replica, week: str) -> list[dict[str, Any]]:
    """Every ``session_weeks`` row of ``week`` (one per session that had events in it)."""
    parse_week(week)  # a bad label is an error here, never a predicate
    tbl = replica.table(SessionWeek)
    if tbl.count_rows() == 0:
        return []
    return tbl.search().where(f"iso_week = {quoted(week)}").select(SESSION_COLUMNS).limit(1_000_000).to_list()


def row_stamp(row: dict[str, Any]) -> str:
    """When one ``session_weeks`` row last moved: the newer of its ``updated`` and ``last_ts`` (same text format)."""
    return max(str(row.get("updated") or ""), str(row.get("last_ts") or ""))


def ledger_stamp(rows: list[dict[str, Any]]) -> str:
    """When a week's ledger last moved: the newest ``row_stamp`` among its rows; ``""`` for no rows.

    A week page records this as ``ledger_at``; the nightly job compares it
    with the same number computed fresh, so the two must be built alike.
    """
    return max((row_stamp(r) for r in rows), default="")


def weeks_present(replica: Replica) -> dict[str, str]:
    """``iso_week`` → its ``ledger_stamp``: when that week's ledger last moved."""
    tbl = replica.table(SessionWeek)
    if tbl.count_rows() == 0:
        return {}
    out: dict[str, str] = {}
    for r in tbl.search().select(["iso_week", "updated", "last_ts"]).limit(1_000_000).to_list():
        week = str(r.get("iso_week") or "")
        if not week:
            continue
        stamp = row_stamp(r)
        if stamp > out.get(week, ""):
            out[week] = stamp
    return out


def conversational(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if float(r.get("user_count") or 0) >= MIN_USER_TURNS
            and float(r.get("event_count") or 0) >= MIN_EVENTS]


def stratify(rows: list[dict[str, Any]], max_sessions: int = DEFAULT_MAX_SESSIONS) -> list[dict[str, Any]]:
    """Up to ``max_sessions`` rows, round-robin across projects, best-by-human-turns first inside each.

    Projects take turns in the order of their strongest session, so a project
    with one intense session is heard before a project with many small ones —
    but every project gets its first pick before any gets a second.
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_project.setdefault(str(r.get("project") or ""), []).append(r)
    def rank(r: dict[str, Any]) -> tuple[float, float, str]:
        return (-float(r.get("user_count") or 0), -float(r.get("event_count") or 0), str(r.get("session") or ""))

    queues = [sorted(group, key=rank) for group in by_project.values()]
    queues.sort(key=lambda q: (rank(q[0]), str(q[0].get("project") or "")))
    chosen: list[dict[str, Any]] = []
    while queues and len(chosen) < max_sessions:
        remaining = []
        for q in queues:
            if len(chosen) >= max_sessions:
                break
            chosen.append(q.pop(0))
            if q:
                remaining.append(q)
        queues = remaining
    return chosen


# ---------------------------------------------------------------- week mode: one session's turns


def session_events(replica: Replica, session: str, week: str) -> list[dict[str, Any]]:
    """The conversational turns of one session inside one week, in time order: user/assistant, ≥ MIN_TEXT chars."""
    parse_week(week)
    tbl = replica.table(Event)
    if tbl.count_rows() == 0:
        return []
    pred = (f"session = {quoted(session)} AND iso_week = {quoted(week)} "
            f"AND role IN ('user', 'assistant') AND length(text) >= {MIN_TEXT}")
    rows = tbl.search().where(pred).select(["id", "session", "ts", "role", "text"]).limit(100_000).to_list()
    rows.sort(key=lambda r: (str(r.get("ts") or ""), str(r.get("id") or "")))
    return rows


def clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 2].rstrip() + " …"


def evenly_spaced(items: list[Any], n: int) -> list[Any]:
    """``n`` items spread through the list (first and last included when n ≥ 2); all of them when n ≥ len."""
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    if n == 1:
        return [items[len(items) // 2]]
    picks = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in picks]


def budget_sample(events: list[dict[str, Any]], budget: int = DIGEST_BUDGET, item_cap: int = ITEM_CAP) -> list[dict[str, Any]]:
    """The turns one digest call reads: all human turns first, then assistant turns evenly spaced to fill.

    Every turn is capped at ``item_cap`` characters (a 40k-character tool dump
    is still one turn). When the human turns alone overflow the budget they
    are themselves thinned evenly, so a very long session still shows its
    start, middle and end. The result is in time order.
    """
    items = [{**e, "text": clip(str(e.get("text") or "").strip(), item_cap)} for e in events]
    users = [e for e in items if e.get("role") == "user"]
    assistants = [e for e in items if e.get("role") == "assistant"]

    def size(e: dict[str, Any]) -> int:
        return len(e["text"]) + 1  # the newline between fenced items

    if sum(size(e) for e in users) > budget and users:
        average = sum(size(e) for e in users) / len(users)
        users = evenly_spaced(users, max(1, int(budget // average)))
    chosen: list[dict[str, Any]] = []
    left = budget
    for e in users:
        if size(e) <= left:
            chosen.append(e)
            left -= size(e)
    if assistants and left > 0:
        average = sum(size(e) for e in assistants) / len(assistants)
        for e in evenly_spaced(assistants, min(len(assistants), int(left // average))):
            if size(e) <= left:
                chosen.append(e)
                left -= size(e)
    chosen.sort(key=lambda e: (str(e.get("ts") or ""), str(e.get("id") or "")))
    return chosen


# ---------------------------------------------------------------- topic mode: hits by horizon


def horizon(ts: str, at: datetime) -> str:
    """short ≤ 7 days old, mid ≤ 30, long ≤ 90, archive beyond (or undated) — relative to ``at``, never stored."""
    then = stamp_to_dt(str(ts or ""))
    if then is None:
        return "archive"
    age_days = (at - then).total_seconds() / 86400.0
    for name in ("short", "mid", "long"):
        if age_days <= HORIZON_DAYS[name]:
            return name
    return "archive"


def score_of(hit: dict[str, Any]) -> float:
    """The relevance a search returned: RRF ``_relevance_score`` (hybrid), else BM25 ``_score``, else 0."""
    for key in ("_relevance_score", "_score"):
        if hit.get(key) is not None:
            return float(hit[key])
    return 0.0


# A line that a tool wrote, not a person: a line-numbered listing ("397 def read_lines(" / "409: const body"),
# a diff hunk, a JSON blob, a tuple list ("[(523, 567)] OK"), a "=====" banner.
DUMP_LINE = re.compile(r"^\s*(?:\d{1,5}(?::|\s{2,}|\s\d)|\d{1,5}:? {1}[A-Za-z_{\[(#/<]|[-+]{3}\s|@@ |diff --git|=====|\{\s*\"|\[\()")


def looks_like_tool_output(text: str) -> bool:
    """True when a hit reads as a tool's output pasted into the transcript rather than a turn someone wrote.

    ``role = user`` is not provenance — most user-role lines are tool results
    (the contract's 870,023 vs 134,745) — and a theme like "409 offset" pulls
    line-numbered code dumps first. Measured on the first topic dream
    (2026-09-10): 12 of 21 material rows were such dumps. The rule: of the
    first eight non-empty lines, three look tool-written, or the very first
    one does and there is more than one line. A numbered list a person wrote
    ("1. do this") does not match; a single quoted line of code does not.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:8]
    if not lines:
        return False
    dumpy = [bool(DUMP_LINE.match(ln)) for ln in lines]
    return sum(dumpy) >= 3 or (dumpy[0] and len(lines) > 1)


def topic_material(hits: list[dict[str, Any]], k: int, at: datetime,
                   names: dict[str, tuple[str, str]], floor: float = RELEVANCE_FLOOR) -> list[dict[str, Any]]:
    """Up to ``k`` hits: a relevance floor, tool-output dumps dropped, then up to ``k/4`` per horizon,
    round-robin across projects inside it.

    A bucket that has fewer hits than its share does not hand the rest on —
    the point of the horizons is to hear from each of them, not to fill ``k``.
    Each returned item carries ``n`` (1-based, in horizon order), ``horizon``,
    ``session_id`` and ``project`` (from ``names``), and no vector.
    """
    scored = [(score_of(h), h) for h in hits]
    best = max((s for s, _ in scored), default=0.0)
    kept = [h for s, h in scored if (best <= 0 or s >= floor * best) and not looks_like_tool_output(str(h.get("text") or ""))]

    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {name: {} for name in HORIZONS}
    for h in kept:  # search order is relevance order, so each project queue stays best-first
        sid, project = names.get(str(h.get("session") or ""), ("", ""))
        item = {key: value for key, value in h.items() if key != "vector"}
        item.update({"horizon": horizon(str(h.get("ts") or ""), at), "session_id": sid, "project": project})
        buckets[item["horizon"]].setdefault(project, []).append(item)

    share = max(1, k // 4)
    out: list[dict[str, Any]] = []
    for name in HORIZONS:
        queues = [q for _, q in sorted(buckets[name].items())]
        queues.sort(key=lambda q: -score_of(q[0]))
        taken = 0
        while queues and taken < share:
            remaining = []
            for q in queues:
                if taken >= share:
                    break
                out.append(q.pop(0))
                taken += 1
                if q:
                    remaining.append(q)
            queues = remaining
    for n, item in enumerate(out, 1):
        item["n"] = n
    return out
