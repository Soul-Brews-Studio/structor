"""The old console on the Python backend.

``app/ui`` (the PocketBase console) talks to its server with RELATIVE urls:
``api/structor/status``, ``api/collections/_superusers/auth-with-password``,
``api/realtime``. Served under ``/console/<target>/`` those become
``/console/<target>/api/…``, which this module answers from the LanceDB replica
of ``<target>``. Same contract as the Bun edition (``app/lance/src/facade.ts``,
header comment), reproduced here so both editions can be checked against it:

  POST collections/_superusers/auth-with-password  {identity, password}
       → 200 {token} when they match the target's admin credentials, else
       400 {error}. Tokens are process-local ("lance." + uuid4, 7-day TTL,
       at most 200 kept, bound to the target that issued them).
  every other path needs ``Authorization: <token>`` → else 401 {error};
  GET realtime is the one exception (EventSource cannot send a header; the
  stream is inert until a POST realtime, token required, subscribes it).

  GET structor/status      projects, sessions, events, session_weeks (row
       counts), last_ingest = max(sessions.updated), last_event_ts =
       max(sessions.last_ts), weeks = top 16 iso_week DESC from session_weeks
       as {week, sessions, events, user_msgs}, version, time (RFC3339 now,
       no fraction), tz "Asia/Bangkok".
  GET structor/projects?limit   {projects: [{id, path, cwd, encoded_dir, name,
       host, sessions, events, last_ts}]} last_ts DESC; default 200, max 1000.
  GET structor/search?q&project&project_id&week&session&role&limit
       {hits, limit, truncated}; rows with role <> '' and (text <> '' or tools
       not '[]'/''); q is a CASE-INSENSITIVE SUBSTRING match (the Go handler's
       LIKE %q%, escape %, _ and \\; NOT the BM25 index); project = substring of
       the project's cwd-or-path, project_id exact, session = session_id
       prefix, week = iso_week, role exact; newest first (ts DESC); the
       scope is pushed into the predicate as ``session IN (...)`` up to 2000
       ids. hit = {session_id, project, ts, iso_week, role, type, snippet
       (text[:600]), tools, line_no, uuid}. default 30, max 200; truncated =
       len(hits) >= limit.
  GET structor/days?from&to&project&project_id&limit
       {days, tz, truncated}; from/to real YYYY-MM-DD dates (400 otherwise),
       inclusive, capped at 31 days; a day is Asia/Bangkok = UTC + 7h; one row
       per (day, session) over events with role <> '': {day, session_id,
       project, events, user_msgs, first_ts, last_ts, preview (earliest user
       text that day [:200]), git_branch}; day DESC then last_ts DESC; default
       500, max 5000; truncated = more rows than limit.
  GET structor/read?session&offset&limit   {events: [{ts, role, type, text
       ([:4000]), tools, line_no}], offset}; session = session_id or unique
       prefix, case-insensitive (404 none, 400 ambiguous); conversational rows
       only, ts ASC; default 100, max 500.
  GET structor/sessions?project&project_id&week&limit   {sessions:
       [{session_id, project, tier, first_ts, last_ts, event_count,
       first_prompt, git_branch, file_path}]} last_ts DESC; default 50, max 500.
  GET structor/weeks?week&session&limit   {weeks: [{iso_week, session_id,
       project, events, user_msgs, assistant_msgs, tool_calls, first_ts,
       last_ts}]} from session_weeks.
  GET structor/intake?limit&pending   {summary: {files, bytes_tracked,
       bytes_indexed, pending_files, pending_bytes, last_ingest, runs_today,
       inserted_today (last 24h of import_runs), hosts (distinct projects.host,
       comma-joined)}, runs: newest first over ALL import_runs [{created,
       session_id, project, file_path, from_offset, to_offset, lines, inserted,
       skipped, host, writer}], files: [{session_id, project, file_path, tier,
       file_size, byte_offset, lines_seen, event_count, file_mtime, updated}]
       updated DESC (pending=1 keeps file_size > byte_offset), writers: per
       (host, writer) over ALL import_runs [{host, writer, last_run, runs_24h,
       inserted_24h, files}], connections: {oauth_clients: 0, active_tokens: 0},
       scan_dir "", scan_interval "", server_scan: {enabled: false, dir: "",
       interval: ""}, upload_dir "", host: target name, tz, superuser: true}.
  GET structor/state   404 {error}.   POST structor/scan|reconcile|upload|ingest
       405 {error: "... not available on the LanceDB backend; use the
       PocketBase console"}.
  GET realtime    proxy the target's ``/api/realtime`` SSE body unchanged
       (content-type text/event-stream); POST realtime  forward the JSON body
       with Authorization = replica.pb.bearer(); retry once after
       invalidate() on 401; return the upstream status.

LanceDB has no ORDER BY, GROUP BY or JOIN: fetch the needed columns with
``select``, aggregate in Python, cache aggregates for 30s keyed by table
version. Bad requests are always JSON ``{error}``, never a traceback.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import math
import os
import queue
import re
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .schema import BY_NAME
from .sync import Replica

JSON_HEADERS = {"content-type": "application/json; charset=utf-8", "cache-control": "no-store"}


@dataclass
class Reply:
    """A framework-neutral response: admin.py turns it into a FastAPI Response."""

    status: int
    body: bytes | Iterator[bytes]  # an iterator streams (the realtime proxy)
    headers: dict[str, str] = field(default_factory=lambda: dict(JSON_HEADERS))


def json_reply(value: object, status: int = 200) -> Reply:
    return Reply(status, json.dumps(value).encode())


def error(message: str, status: int = 400) -> Reply:
    return json_reply({"error": message}, status)


# ---------------------------------------------------------------- constants

#: Zone the Go server stamps ISO weeks and day buckets in. A fixed offset, so a day boundary is UTC + 7h.
TZ = "Asia/Bangkok"
TZ_OFFSET_MS = 7 * 3_600_000
DAY_MS = 86_400_000

#: Shown in the console's status strip; the replica has no PocketBase build stamp of its own.
VERSION = os.environ.get("STRUCTOR_LANCE_VERSION", "lance")

REALTIME_TIMEOUT = httpx.Timeout(10.0, read=None)

# ---------------------------------------------------------------- tokens

TOKEN_TTL_S = 7 * 24 * 3600.0
MAX_TOKENS = 200
_tokens: OrderedDict[str, tuple[str, float]] = OrderedDict()  # token -> (target name, issued)
_tokens_lock = threading.Lock()


def issue(target: str) -> str:
    """A process-local token for one target: expired ones swept, the oldest dropped past the cap."""
    now = time.time()
    with _tokens_lock:
        for k in [k for k, (_, at) in _tokens.items() if now - at > TOKEN_TTL_S]:
            _tokens.pop(k, None)
        while len(_tokens) >= MAX_TOKENS:
            _tokens.popitem(last=False)
        token = f"lance.{uuid.uuid4()}"
        _tokens[token] = (target, now)
    return token


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive lookup; FastAPI's Headers already are, a plain dict is not."""
    v = headers.get(name)
    if v is None:
        for k, val in headers.items():
            if k.lower() == name:
                v = val
                break
    return v or ""


def authorized(r: Replica, headers: Mapping[str, str]) -> bool:
    """True when the request carries a token this process issued for this target."""
    raw = _header(headers, "authorization").strip()
    token = raw[7:].strip() if raw[:7].lower() == "bearer " else raw
    if not token:
        return False
    with _tokens_lock:
        rec = _tokens.get(token)
        if rec is None:
            return False
        target, at = rec
        if time.time() - at > TOKEN_TTL_S:
            _tokens.pop(token, None)
            return False
    return target == r.target.name


def same_secret(a: str, b: str) -> bool:
    """Constant-time comparison, so a wrong password leaks nothing through timing. Never logs either side."""
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))


# ---------------------------------------------------------------- cache

CACHE_TTL_S = 30.0
CACHE_MAX = 32
_caches: weakref.WeakKeyDictionary[Replica, OrderedDict[str, tuple[str, float, Any]]] = weakref.WeakKeyDictionary()
_cache_lock = threading.Lock()


def _tbl(r: Replica, name: str) -> Any:
    """The replica's table handle, re-opened every call so its version is the current one."""
    return r.table(BY_NAME[name])


def cached(r: Replica, tables: list[str], key: str, make: Callable[[], Any]) -> Any:
    """Memoise one derived value per replica.

    An entry is reused while every table it was built from is still at the same
    version and it is younger than 30s — the version keeps a stale aggregate
    from outliving an import, the age keeps wall-clock windows (last 24h,
    "today") from drifting.
    """
    version = ".".join(str(_tbl(r, n).version) for n in tables)
    with _cache_lock:
        m = _caches.get(r)
        hit = m.get(key) if m is not None else None
        if hit is not None and hit[0] == version and time.time() - hit[1] < CACHE_TTL_S:
            return hit[2]
    value = make()
    with _cache_lock:
        m = _caches.get(r)
        if m is None:
            m = OrderedDict()
            _caches[r] = m
        m.pop(key, None)
        while len(m) >= CACHE_MAX:
            m.popitem(last=False)
        m[key] = (version, time.time(), value)
    return value


# ---------------------------------------------------------------- helpers


def lit(s: object) -> str:
    """Quote a literal for a LanceDB (DataFusion) predicate: single quotes, doubled inside."""
    return "'" + str(s).replace("'", "''") + "'"


def sstr(v: object) -> str:
    return "" if v is None else str(v)


def nnum(v: object) -> int | float:
    """Lance stores these columns as float64; Go and the console print them as integers."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if math.isnan(f) or math.isinf(f):
        return 0
    return int(f) if f.is_integer() else f


def cut(s: object, n: int) -> str:
    v = sstr(s)
    return v[:n] if len(v) > n else v


def stamp_at(ms: float) -> str:
    """PocketBase's timestamp shape, which is also what the ts/created columns hold: 2026-09-09 15:00:00.000Z."""
    dt = datetime.fromtimestamp(ms / 1000, UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_stamp_ms(ts: str) -> float | None:
    """A stored timestamp back to epoch milliseconds, or None when it is not one."""
    s = ts.strip().replace("T", " ").removesuffix("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC).timestamp() * 1000
        except ValueError:
            continue
    return None


YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_real_ymd(s: str) -> bool:
    """A real calendar date: the shape, and no rollover (2026-02-31 is refused, as Go's ParseInLocation refuses it)."""
    if not YMD.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return False
    return True


def day_start_ms(ymd: str) -> float:
    """Midnight UTC of a YYYY-MM-DD, in epoch milliseconds."""
    return datetime.strptime(ymd, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000


def limit_of(raw: str | None, default: int, maximum: int) -> int:
    """Go's rule for every limit: out of range falls back to the default, it is not clamped to the maximum."""
    if raw is None or raw == "":
        return default
    try:
        n = math.floor(float(raw))
    except (TypeError, ValueError, OverflowError):
        return default
    return n if 0 < n <= maximum else default


def offset_of(raw: str | None) -> int:
    if raw is None or raw == "":
        return 0
    try:
        n = math.floor(float(raw))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n)


def now_rfc3339() -> str:
    """RFC3339 without fractional seconds, the way Go's time.RFC3339 prints it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def qs(query: Mapping[str, str], name: str) -> str:
    v = query.get(name)
    return "" if v is None else str(v)


Row = dict[str, Any]  # rows come back as plain dicts; only strings and float64 are in these schemas


# ---------------------------------------------------------------- loaders


@dataclass(slots=True)
class SessionRec:
    id: str
    session_id: str
    project: str
    tier: str
    file_path: str
    first_ts: str
    last_ts: str
    event_count: int | float
    first_prompt: str
    git_branch: str
    file_size: int | float
    byte_offset: int | float
    lines_seen: int | float
    file_mtime: int | float
    updated: str
    cwd: str


@dataclass(slots=True)
class ProjectRec:
    id: str
    path: str
    cwd: str
    encoded_dir: str
    name: str
    host: str
    created: str
    updated: str


@dataclass(slots=True)
class WeekRec:
    session: str
    project: str
    iso_week: str
    event_count: int | float
    user_count: int | float
    assistant_count: int | float
    tool_count: int | float
    first_ts: str
    last_ts: str


@dataclass(slots=True)
class RunRec:
    session: str
    project: str
    from_offset: int | float
    to_offset: int | float
    lines: int | float
    inserted: int | float
    skipped: int | float
    host: str
    writer: str
    created: str


@dataclass(slots=True)
class Store:
    sessions: list[SessionRec]
    by_record: dict[str, SessionRec]  # sessions.id (what events.session points at)
    projects: list[ProjectRec]
    by_project: dict[str, ProjectRec]
    path_of: dict[str, str]  # sessions.id → the project's real cwd when known, else the decoded guess


SESSION_COLS = ["id", "session_id", "project", "tier", "file_path", "first_ts", "last_ts", "event_count",
                "first_prompt", "git_branch", "file_size", "byte_offset", "lines_seen", "file_mtime", "updated", "cwd"]
PROJECT_COLS = ["id", "path", "cwd", "encoded_dir", "name", "host", "created", "updated"]


def scan_all(r: Replica, name: str, cols: list[str], limit: int = 1_000_000) -> list[Row]:
    return _tbl(r, name).search().select(cols).limit(limit).to_list()


def store(r: Replica) -> Store:
    """sessions + projects, the join the console needs on every page. A few thousand rows, cached per version."""

    def make() -> Store:
        projects = [
            ProjectRec(sstr(p["id"]), sstr(p["path"]), sstr(p["cwd"]), sstr(p["encoded_dir"]),
                       sstr(p["name"]), sstr(p["host"]), sstr(p["created"]), sstr(p["updated"]))
            for p in scan_all(r, "projects", PROJECT_COLS, 100_000)
        ]
        by_project = {p.id: p for p in projects}
        sessions = [
            SessionRec(sstr(s["id"]), sstr(s["session_id"]), sstr(s["project"]), sstr(s["tier"]), sstr(s["file_path"]),
                       sstr(s["first_ts"]), sstr(s["last_ts"]), nnum(s["event_count"]), sstr(s["first_prompt"]),
                       sstr(s["git_branch"]), nnum(s["file_size"]), nnum(s["byte_offset"]), nnum(s["lines_seen"]),
                       nnum(s["file_mtime"]), sstr(s["updated"]), sstr(s["cwd"]))
            for s in scan_all(r, "sessions", SESSION_COLS)
        ]
        by_record = {s.id: s for s in sessions}
        path_of: dict[str, str] = {}
        for s in sessions:
            p = by_project.get(s.project)
            path_of[s.id] = (p.cwd or p.path) if p else ""
        return Store(sessions, by_record, projects, by_project, path_of)

    return cached(r, ["sessions", "projects"], "store", make)


WEEK_COLS = ["session", "project", "iso_week", "event_count", "user_count", "assistant_count",
             "tool_count", "first_ts", "last_ts"]
RUN_COLS = ["session", "project", "from_offset", "to_offset", "lines", "inserted", "skipped", "host", "writer", "created"]


def week_rows(r: Replica) -> list[WeekRec]:
    def make() -> list[WeekRec]:
        return [
            WeekRec(sstr(w["session"]), sstr(w["project"]), sstr(w["iso_week"]), nnum(w["event_count"]),
                    nnum(w["user_count"]), nnum(w["assistant_count"]), nnum(w["tool_count"]),
                    sstr(w["first_ts"]), sstr(w["last_ts"]))
            for w in scan_all(r, "session_weeks", WEEK_COLS)
        ]

    return cached(r, ["session_weeks"], "weeks", make)


def run_rows(r: Replica) -> list[RunRec]:
    """Every import_runs row: writers and the run ledger are computed over all history, like the Go handlers."""

    def make() -> list[RunRec]:
        return [
            RunRec(sstr(x["session"]), sstr(x["project"]), nnum(x["from_offset"]), nnum(x["to_offset"]),
                   nnum(x["lines"]), nnum(x["inserted"]), nnum(x["skipped"]), sstr(x["host"]), sstr(x["writer"]),
                   sstr(x["created"]))
            for x in scan_all(r, "import_runs", RUN_COLS)
        ]

    return cached(r, ["import_runs"], "runs", make)


# ---------------------------------------------------------------- event scans

#: Rows the console calls conversational: a role, and something to show.
CONVERSATIONAL = "role <> '' AND (text <> '' OR (tools <> '[]' AND tools <> ''))"

EVENT_COLS = ["session", "uuid", "ts", "iso_week", "role", "type", "text", "tools", "line_no"]

#: Beyond this many ids an IN list is not worth building and the caller keeps the Python-side filter.
SCOPE_PUSHDOWN_MAX = 2000

#: Windows tried, newest first, when looking for the most recent N events: 14 days, then 90, then everything.
WINDOWS_MS = (14 * DAY_MS, 90 * DAY_MS, 0)


def session_scope(s: Store, project: str = "", project_id: str = "", session: str = "") -> set[str] | None:
    """The session record ids a project/session filter allows, or None when no such filter is set.

    Projects and session ids live in other tables, so they are matched here and
    applied to event rows either as a pushed-down predicate or in Python.
    """
    proj = project.lower()
    sid = session.lower()
    if not proj and not project_id and not sid:
        return None
    out: set[str] = set()
    for x in s.sessions:
        if project_id and x.project != project_id:
            continue
        if proj and proj not in s.path_of.get(x.id, "").lower():
            continue
        if sid and not x.session_id.lower().startswith(sid):
            continue
        out.add(x.id)
    return out


def scope_pred(scope: set[str] | None) -> str | None:
    """A session scope as a predicate, so Lance filters instead of Python walking rows it already read."""
    if scope is None:
        return None
    if not scope:
        return "session = ''"  # matches nothing
    if len(scope) > SCOPE_PUSHDOWN_MAX:
        return None
    return "session IN (" + ", ".join(lit(x) for x in sorted(scope)) + ")"


def like_contains(column: str, q: str) -> str:
    """``%q%`` for a case-insensitive substring match, the Go backend's ``LIKE {:q} ESCAPE '\\'`` semantics."""
    esc = re.sub(r"([\\%_])", r"\\\1", q.lower())
    return f"lower({column}) LIKE {lit('%' + esc + '%')}"


def newest_events(r: Replica, base: list[str], scope: set[str] | None, limit: int,
                  matches: Callable[[str], bool] | None = None) -> list[Row]:
    """The newest ``limit`` event rows matching ``base``, honouring a Python-side scope and text matcher.

    Two passes: a narrow (ts, session[, text]) scan finds the timestamp of the
    limit-th eligible row, then only rows at or after it are read with every
    column. Widening from 14 days to 90 to all time stops as soon as a window
    holds enough rows.
    """
    t = _tbl(r, "events")
    cols = ["ts", "session", "text"] if matches else ["ts", "session"]
    threshold = ""
    for w in WINDOWS_MS:
        frm = stamp_at(time.time() * 1000 - w) if w else ""
        pred = " AND ".join([*base, *([f"ts >= {lit(frm)}"] if frm else [])])
        rows = t.search().where(pred).select(cols).limit(2_000_000).to_list()
        eligible = [
            sstr(row["ts"]) for row in rows
            if (scope is None or sstr(row["session"]) in scope) and (matches is None or matches(sstr(row.get("text"))))
        ]
        if not eligible and w:
            continue  # nothing here: widen
        eligible.sort(reverse=True)
        threshold = eligible[limit - 1] if len(eligible) >= limit else frm
        if len(eligible) >= limit or not w:
            break  # enough, or the last window
    pred = " AND ".join([*base, *([f"ts >= {lit(threshold)}"] if threshold else [])])
    rows = t.search().where(pred).select(EVENT_COLS).limit(2_000_000).to_list()
    keep = [
        row for row in rows
        if (scope is None or sstr(row["session"]) in scope) and (matches is None or matches(sstr(row.get("text"))))
    ]
    keep.sort(key=lambda row: sstr(row["ts"]), reverse=True)
    return keep[:limit]


# ---------------------------------------------------------------- endpoints


def status(r: Replica) -> Reply:
    def make() -> dict:
        counts = {n: _tbl(r, n).count_rows() for n in ("projects", "sessions", "events", "session_weeks")}
        s = store(r)
        last_ingest = ""
        last_event_ts = ""
        for x in s.sessions:
            last_ingest = max(last_ingest, x.updated)
            last_event_ts = max(last_event_ts, x.last_ts)
        agg: dict[str, dict] = {}
        for w in week_rows(r):
            a = agg.setdefault(w.iso_week, {"week": w.iso_week, "sessions": 0, "events": 0, "user_msgs": 0})
            a["sessions"] += 1
            a["events"] += w.event_count
            a["user_msgs"] += w.user_count
        weeks = sorted(agg.values(), key=lambda a: a["week"], reverse=True)[:16]
        return {**counts, "last_ingest": last_ingest, "last_event_ts": last_event_ts,
                "weeks": weeks, "version": VERSION, "tz": TZ}

    body = cached(r, ["projects", "sessions", "events", "session_weeks"], "status", make)
    return json_reply({**body, "time": now_rfc3339()})


def projects(r: Replica, query: Mapping[str, str]) -> Reply:
    limit = limit_of(query.get("limit"), 200, 1000)

    def make() -> list[dict]:
        s = store(r)
        agg: dict[str, dict] = {}
        for x in s.sessions:
            a = agg.setdefault(x.project, {"sessions": 0, "events": 0, "last_ts": ""})
            a["sessions"] += 1
            a["events"] += x.event_count
            a["last_ts"] = max(a["last_ts"], x.last_ts)
        rows = [
            {"id": p.id, "path": p.path, "cwd": p.cwd, "encoded_dir": p.encoded_dir, "name": p.name, "host": p.host,
             **agg.get(p.id, {"sessions": 0, "events": 0, "last_ts": ""})}
            for p in s.projects
        ]
        rows.sort(key=lambda a: a["last_ts"], reverse=True)
        return rows

    return json_reply({"projects": cached(r, ["sessions", "projects"], "projects", make)[:limit]})


def search(r: Replica, query: Mapping[str, str]) -> Reply:
    q = qs(query, "q").strip()
    limit = limit_of(query.get("limit"), 30, 200)
    s = store(r)
    scope = session_scope(s, project=qs(query, "project"), project_id=qs(query, "project_id"),
                          session=qs(query, "session"))
    base = [CONVERSATIONAL]
    if qs(query, "role"):
        base.append(f"role = {lit(qs(query, 'role'))}")
    if qs(query, "week"):
        base.append(f"iso_week = {lit(qs(query, 'week'))}")
    # Same semantics as the Go handler: a case-insensitive substring match (LIKE
    # %q%) over the scope, newest first. The BM25 index is the admin's tool; the
    # console's search box promises "text contains", and a mid-word fragment
    # that LIKE finds would be invisible to a tokeniser.
    if q:
        base.append(like_contains("text", q))
    pushed = scope_pred(scope)
    if pushed:
        base.append(pushed)
    rows = newest_events(r, base, None if pushed else scope, limit)
    hits = [
        {"session_id": (s.by_record[sstr(row["session"])].session_id if sstr(row["session"]) in s.by_record else ""),
         "project": s.path_of.get(sstr(row["session"]), ""),
         "ts": sstr(row["ts"]), "iso_week": sstr(row["iso_week"]), "role": sstr(row["role"]), "type": sstr(row["type"]),
         "snippet": cut(row["text"], 600), "tools": sstr(row["tools"]), "line_no": nnum(row["line_no"]),
         "uuid": sstr(row["uuid"])}
        for row in rows
    ]
    return json_reply({"hits": hits, "limit": limit, "truncated": len(hits) >= limit})


def day_of(ts: str, memo: dict[str, str]) -> str:
    """YYYY-MM-DD of a stored timestamp in Asia/Bangkok, memoised on the 'YYYY-MM-DD HH' prefix (≤ 31×24 keys)."""
    key = ts[:13]
    seen = memo.get(key)
    if seen is not None:
        return seen
    ms = parse_stamp_ms(ts)
    day = ts[:10] if ms is None else datetime.fromtimestamp((ms + TZ_OFFSET_MS) / 1000, UTC).strftime("%Y-%m-%d")
    memo[key] = day
    return day


def days(r: Replica, query: Mapping[str, str]) -> Reply:
    frm, to = qs(query, "from"), qs(query, "to")
    if not is_real_ymd(frm):
        return error("days: from must be a real YYYY-MM-DD date")
    if not is_real_ymd(to):
        return error("days: to must be a real YYYY-MM-DD date")
    limit = limit_of(query.get("limit"), 500, 5000)
    project, pid = qs(query, "project"), qs(query, "project_id")
    s = store(r)
    scope = session_scope(s, project=project, project_id=pid)

    # Fixed +07:00: the local day starts 7h before the UTC day, so day = UTC ts + 7h.
    start = day_start_ms(frm) - TZ_OFFSET_MS
    end = day_start_ms(to) - TZ_OFFSET_MS + DAY_MS
    if end - start > 31 * DAY_MS:
        end = start + 31 * DAY_MS
    if end <= start:
        return json_reply({"days": [], "tz": TZ, "truncated": False})

    def make() -> list[dict]:
        pred = f"ts >= {lit(stamp_at(start))} AND ts < {lit(stamp_at(end))} AND role <> ''"
        rows = _tbl(r, "events").search().where(pred).select(["session", "ts", "role", "text"]).limit(2_000_000).to_list()
        memo: dict[str, str] = {}
        acc: dict[tuple[str, str], dict] = {}
        for row in rows:
            sid = sstr(row["session"])
            if scope is not None and sid not in scope:
                continue
            ts = sstr(row["ts"])
            day = day_of(ts, memo)
            a = acc.get((day, sid))
            if a is None:
                a = {"day": day, "session": sid, "events": 0, "user_msgs": 0,
                     "first_ts": ts, "last_ts": ts, "preview": "", "preview_ts": ""}
                acc[(day, sid)] = a
            a["events"] += 1
            a["first_ts"] = min(a["first_ts"], ts)
            a["last_ts"] = max(a["last_ts"], ts)
            if sstr(row["role"]) == "user":
                a["user_msgs"] += 1
                text = sstr(row["text"])
                if text and (a["preview_ts"] == "" or ts < a["preview_ts"]):
                    a["preview_ts"] = ts
                    a["preview"] = cut(text, 200)
        out = [
            {"day": a["day"],
             "session_id": s.by_record[a["session"]].session_id if a["session"] in s.by_record else "",
             "project": s.path_of.get(a["session"], ""),
             "events": a["events"], "user_msgs": a["user_msgs"], "first_ts": a["first_ts"], "last_ts": a["last_ts"],
             "preview": a["preview"],
             "git_branch": s.by_record[a["session"]].git_branch if a["session"] in s.by_record else ""}
            for a in acc.values()
        ]
        # day DESC, then last_ts DESC, the Go handler's order (most recently active session first)
        out.sort(key=lambda x: (x["day"], x["last_ts"]), reverse=True)
        return out

    key = f"days:{frm}:{to}:{project}:{pid}"
    every = cached(r, ["events", "sessions", "projects"], key, make)
    return json_reply({"days": every[:limit], "tz": TZ, "truncated": len(every) > limit})


def resolve_session(s: Store, prefix: str) -> tuple[str, str, int]:
    """Exact session_id, else the one session it is a prefix of. Returns (record id, error, status)."""
    # case-insensitive, like the SQLite LIKE the Go handler resolves with
    want = prefix.strip().lower()
    if not want:
        return "", "no such session", 404
    for x in s.sessions:
        if x.session_id.lower() == want:
            return x.id, "", 0
    hits = [x for x in s.sessions if x.session_id.lower().startswith(want)]
    if not hits:
        return "", "no such session", 404
    if len(hits) > 1:
        return "", "session id prefix is ambiguous, give more characters", 400
    return hits[0].id, "", 0


def read(r: Replica, query: Mapping[str, str]) -> Reply:
    want = qs(query, "session")
    if not want:
        return error("session query param required")
    limit = limit_of(query.get("limit"), 100, 500)
    offset = offset_of(query.get("offset"))
    s = store(r)
    record_id, err, status_code = resolve_session(s, want)
    if err:
        return error(err, status_code)

    def make() -> list[dict]:
        pred = f"session = {lit(record_id)} AND {CONVERSATIONAL}"
        raw = _tbl(r, "events").search().where(pred).select(
            ["ts", "role", "type", "text", "tools", "line_no"]).limit(2_000_000).to_list()
        out = [{"ts": sstr(x["ts"]), "role": sstr(x["role"]), "type": sstr(x["type"]), "text": cut(x["text"], 4000),
                "tools": sstr(x["tools"]), "line_no": nnum(x["line_no"])} for x in raw]
        out.sort(key=lambda x: (x["ts"], x["line_no"]))
        return out

    rows = cached(r, ["events"], f"read:{record_id}", make)
    return json_reply({"events": rows[offset:offset + limit], "offset": offset})


def sessions(r: Replica, query: Mapping[str, str]) -> Reply:
    limit = limit_of(query.get("limit"), 50, 500)
    week = qs(query, "week")
    s = store(r)
    scope = session_scope(s, project=qs(query, "project"), project_id=qs(query, "project_id"))
    in_week: set[str] | None = None
    if week:
        in_week = {w.session for w in week_rows(r) if w.iso_week == week}
    keep = [x for x in s.sessions
            if (scope is None or x.id in scope) and (in_week is None or x.id in in_week)]
    keep.sort(key=lambda x: x.last_ts, reverse=True)
    rows = [
        {"session_id": x.session_id, "project": s.path_of.get(x.id, ""), "tier": x.tier,
         "first_ts": x.first_ts, "last_ts": x.last_ts, "event_count": x.event_count,
         "first_prompt": cut(x.first_prompt, 300), "git_branch": x.git_branch, "file_path": x.file_path}
        for x in keep[:limit]
    ]
    return json_reply({"sessions": rows})


def weeks(r: Replica, query: Mapping[str, str]) -> Reply:
    limit = limit_of(query.get("limit"), 100, 1000)
    week = qs(query, "week")
    prefix = qs(query, "session").lower()
    s = store(r)
    keep = [w for w in week_rows(r) if (not week or w.iso_week == week) and w.session in s.by_record]
    if prefix:
        keep = [w for w in keep if s.by_record[w.session].session_id.lower().startswith(prefix)]
    keep.sort(key=lambda w: (w.iso_week, w.last_ts), reverse=True)
    rows = []
    for w in keep[:limit]:
        proj = s.by_project.get(w.project)
        rows.append({
            "iso_week": w.iso_week,
            "session_id": s.by_record[w.session].session_id,
            "project": (proj.cwd or proj.path) if proj else s.path_of.get(w.session, ""),
            "events": w.event_count, "user_msgs": w.user_count, "assistant_msgs": w.assistant_count,
            "tool_calls": w.tool_count, "first_ts": w.first_ts, "last_ts": w.last_ts,
        })
    return json_reply({"weeks": rows})


def intake(r: Replica, query: Mapping[str, str]) -> Reply:
    runs_limit = limit_of(query.get("limit"), 100, 500)
    files_limit = limit_of(query.get("limit"), 100, 1000)
    pending_only = query.get("pending") == "1"
    s = store(r)
    since = stamp_at(time.time() * 1000 - DAY_MS)
    all_runs = run_rows(r)
    runs_24 = [x for x in all_runs if x.created >= since]

    bytes_tracked = bytes_indexed = pending_files = pending_bytes = 0
    last_ingest = ""
    for x in s.sessions:
        bytes_tracked += x.file_size
        bytes_indexed += x.byte_offset
        if x.file_size > x.byte_offset:
            pending_files += 1
            pending_bytes += x.file_size - x.byte_offset
        last_ingest = max(last_ingest, x.updated)
    inserted_today = sum(x.inserted for x in runs_24)
    hosts = sorted({p.host for p in s.projects if p.host})

    ledger = sorted((x for x in all_runs if x.session in s.by_record), key=lambda x: x.created, reverse=True)
    runs = []
    for x in ledger[:runs_limit]:
        sess = s.by_record[x.session]
        runs.append({"created": x.created, "session_id": sess.session_id, "project": s.path_of.get(x.session, ""),
                     "file_path": sess.file_path, "from_offset": x.from_offset, "to_offset": x.to_offset,
                     "lines": x.lines, "inserted": x.inserted, "skipped": x.skipped, "host": x.host,
                     "writer": x.writer})

    tracked = [x for x in s.sessions if not pending_only or x.file_size > x.byte_offset]
    tracked.sort(key=lambda x: x.updated, reverse=True)
    files = [
        {"session_id": x.session_id, "project": s.path_of.get(x.id, ""), "file_path": x.file_path, "tier": x.tier,
         "file_size": x.file_size, "byte_offset": x.byte_offset, "lines_seen": x.lines_seen,
         "event_count": x.event_count, "file_mtime": x.file_mtime, "updated": x.updated}
        for x in tracked[:files_limit]
    ]

    # every (host, writer) that ever wrote, like Go's ListWriters; the 24h counters are a window on top
    wagg: dict[tuple[str, str], dict] = {}
    for x in all_runs:
        a = wagg.get((x.host, x.writer))
        if a is None:
            a = {"host": x.host, "writer": x.writer, "last_run": "", "runs_24h": 0, "inserted_24h": 0, "files": set()}
            wagg[(x.host, x.writer)] = a
        if x.created >= since:
            a["runs_24h"] += 1
            a["inserted_24h"] += x.inserted
        a["files"].add(x.session)
        a["last_run"] = max(a["last_run"], x.created)
    writers = [
        {"host": a["host"], "writer": a["writer"], "last_run": a["last_run"], "runs_24h": a["runs_24h"],
         "inserted_24h": a["inserted_24h"], "files": len(a["files"])}
        for a in sorted(wagg.values(), key=lambda a: a["last_run"], reverse=True)
    ]

    return json_reply({
        "summary": {
            "files": len(s.sessions), "bytes_tracked": bytes_tracked, "bytes_indexed": bytes_indexed,
            "pending_files": pending_files, "pending_bytes": pending_bytes, "last_ingest": last_ingest,
            "runs_today": len(runs_24), "inserted_today": inserted_today, "hosts": ",".join(hosts),
        },
        "runs": runs, "files": files, "writers": writers,
        # Go's keys; the replica has no OAuth tables, so both are 0
        "connections": {"oauth_clients": 0, "active_tokens": 0},
        "scan_dir": "", "scan_interval": "",
        # Go's keys, which the console reads without guarding
        "server_scan": {"enabled": False, "dir": "", "interval": ""},
        "upload_dir": "", "host": r.target.name, "tz": TZ, "superuser": True,
    })


# ---------------------------------------------------------------- realtime proxy

SSE_HEADERS = {"content-type": "text/event-stream", "cache-control": "no-cache",
               "connection": "keep-alive", "x-accel-buffering": "no"}

#: How long the generator waits for upstream bytes before yielding a comment line.
SSE_TICK_S = 5.0
#: Chunks held for a slow browser before the reader thread starts blocking on the queue.
SSE_QUEUE = 64


def realtime_get(r: Replica) -> Reply:
    """Open the target's SSE stream and hand its body back through a queue.

    The upstream read has to happen on a thread of its own. ASGI runs a sync
    generator body in the server's threadpool, and ``iter_raw()`` blocks for as
    long as the target has nothing to say — an idle stream would hold that
    worker forever, so a few dozen opened-and-closed EventSources exhaust the
    pool and the whole admin stops answering. Here a reader thread owns the
    blocking read and the generator only ever waits ``SSE_TICK_S`` on a queue,
    yielding a comment line when nothing arrived, so control comes back to the
    server regularly and a disconnect is noticed. Closing the generator sets the
    stop flag and closes the response, which ends the reader thread.
    """
    client = httpx.Client(timeout=REALTIME_TIMEOUT)
    ctx = None
    try:
        ctx = client.stream("GET", f"{r.target.url}/api/realtime", headers={"Accept": "text/event-stream"})
        upstream = ctx.__enter__()
        if upstream.status_code != 200:
            raise RuntimeError(f"realtime upstream {upstream.status_code}")
    except Exception as e:  # noqa: BLE001 — every upstream failure is a 502, never a traceback
        _close(ctx, client)
        message = str(e) if str(e).startswith("realtime upstream") else f"realtime upstream: {e or type(e).__name__}"
        return error(message, 502)

    chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=SSE_QUEUE)
    stop = threading.Event()

    def read() -> None:
        try:
            # every failure ends the stream: upstream died, or the generator closed it under us
            with contextlib.suppress(Exception):
                for chunk in upstream.iter_raw():
                    if stop.is_set():
                        break
                    while not stop.is_set():
                        try:
                            chunks.put(chunk, timeout=SSE_TICK_S)
                            break
                        except queue.Full:  # the browser is not reading; check the stop flag and wait again
                            continue
        finally:
            with contextlib.suppress(queue.Full):
                chunks.put_nowait(None)  # the sentinel is best-effort; the generator also watches the thread
            _close(ctx, client)

    reader = threading.Thread(target=read, name=f"realtime-{r.target.name}", daemon=True)
    reader.start()

    def stream() -> Iterator[bytes]:
        try:
            while True:
                try:
                    chunk = chunks.get(timeout=SSE_TICK_S)
                except queue.Empty:
                    if not reader.is_alive():
                        return
                    yield b": ping\n\n"  # an SSE comment: keeps the connection warm, ignored by EventSource
                    continue
                if chunk is None:
                    return
                yield chunk
        finally:
            # GeneratorExit (the consumer stopped iterating) lands here too
            stop.set()
            _close(ctx, client)  # unblocks the reader thread's iter_raw()

    return Reply(200, stream(), dict(SSE_HEADERS))


def _close(ctx: Any, client: httpx.Client) -> None:
    """Closing a half-read stream is allowed to fail; there is nothing left to report it to."""
    if ctx is not None:
        with contextlib.suppress(Exception):
            ctx.__exit__(None, None, None)
    with contextlib.suppress(Exception):
        client.close()


def realtime_post(r: Replica, body: bytes) -> Reply:
    """Forward a subscribe body with the replica's own PocketBase credentials, once retried on a stale token."""

    def send(token: str) -> httpx.Response:
        return httpx.post(f"{r.target.url}/api/realtime", content=body, timeout=REALTIME_TIMEOUT,
                          headers={"content-type": "application/json", "Authorization": token})

    try:
        upstream = send(r.pb.bearer())
        if upstream.status_code == 401:
            r.pb.invalidate()
            upstream = send(r.pb.bearer())
    except Exception as e:  # noqa: BLE001
        return error(f"realtime upstream: {e or type(e).__name__}", 502)
    if upstream.status_code in (204, 205, 304):
        return Reply(upstream.status_code, b"", {})
    ct = upstream.headers.get("content-type") or "application/json"
    return Reply(upstream.status_code, upstream.content, {"content-type": ct})


# ---------------------------------------------------------------- dispatch

WRITE_PATHS = frozenset({"structor/scan", "structor/reconcile", "structor/upload", "structor/ingest"})
ROUTES: dict[str, Callable[[Replica, Mapping[str, str]], Reply]] = {
    "structor/projects": projects,
    "structor/search": search,
    "structor/days": days,
    "structor/read": read,
    "structor/sessions": sessions,
    "structor/weeks": weeks,
    "structor/intake": intake,
}


def login(r: Replica, body: bytes) -> Reply:
    try:
        parsed = json.loads(body or b"")
    except ValueError:
        return error("invalid login body")
    if not isinstance(parsed, dict):
        return error("invalid login body")
    identity = parsed.get("identity")
    password = parsed.get("password")
    identity = identity if isinstance(identity, str) else ""
    password = password if isinstance(password, str) else ""
    # Compared against the target's own admin credentials; neither side is ever logged or echoed.
    if not same_secret(identity, r.target.email) or not same_secret(password, r.target.password):
        return error("Failed to authenticate.")
    return json_reply({"token": issue(r.target.name)})


def handle(r: Replica, path: str, method: str, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> Reply:
    """Answer one console API request. ``path`` is relative to /console/<target>/api/ (no leading slash)."""
    p = path.lstrip("/")
    try:
        if p == "collections/_superusers/auth-with-password":
            return login(r, body) if method == "POST" else error("method not allowed", 405)

        # EventSource cannot send Authorization, and the stream is inert until a
        # POST (token required) subscribes it; see the note in the header.
        if p == "realtime" and method == "GET":
            return realtime_get(r)

        if not authorized(r, headers):
            return error("unauthorized", 401)

        if p == "realtime":
            return realtime_post(r, body) if method == "POST" else error("method not allowed", 405)
        if p in WRITE_PATHS:
            return error(f"{p} is not available on the LanceDB backend; use the PocketBase console", 405)
        if method != "GET":
            return error("method not allowed", 405)

        if p == "structor/status":
            return status(r)
        if p == "structor/state":
            return error("tail state lives in PocketBase; the LanceDB replica does not serve it", 404)
        route = ROUTES.get(p)
        if route is None:
            return error(f"not found: {p}", 404)
        return route(r, query)
    except Exception as e:  # noqa: BLE001 — a handler failure is JSON, never a traceback
        return error(str(e) or type(e).__name__, 500)
