"""The LanceDB admin: a JSON API over every replica plus the static UI.

Loopback only; there is no auth because nothing here can reach a password
(targets are resolved from ~/.config, the API never echoes them).

  GET  /api/status                                       all targets, table counts, sync state
  GET  /api/{t}/tables                                    [{name, rows, version, indices, fts, stamp}]
  GET  /api/{t}/tables/{n}/schema                         {fields: [{name, type, nullable}]}
  GET  /api/{t}/tables/{n}/rows?where&limit&offset&select  {rows, total, limit, offset}
       (no ORDER BY: Lance scans in storage order)
  GET  /api/{t}/tables/{n}/search?q&limit&where            {rows} with _score (FTS tables only)
  GET  /api/{t}/tables/events/vsearch?q&limit&where&mode   {rows} with _distance or _relevance_score
  GET  /api/{t}/tables/{n}/stats                           {rows, version, versions, indices, stats}
  GET  /api/{t}/sync   (and every method but POST)         {state, lag}
  POST /api/{t}/sync                                       pull now → {pulled, state}
  POST /api/{t}/tables/{n}/optimize                        compact + index new rows
  POST /api/{t}/tables/{n}/fts                             (re)build the FTS index

Route for route, and JSON shape for shape, this is the Bun edition's
``app/lance/src/admin.ts``: the same dependency-free admin UI (``app/lance/ui``)
is served by both, so the two backends must answer it identically. Stats keys
are camel-cased on the way out for that reason — the Node bindings hand the UI
``totalBytes``/``fragmentStats``, the Python ones ``total_bytes``.

The old PocketBase console (``app/ui``) is mounted per target at
``/console/<target>/``; its relative ``api/…`` calls go to ``facade.handle``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import mimetypes
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, StreamingResponse
from lancedb.index import FTS
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import facade, vectors
from .schema import BY_NAME, TABLES, Event, Table
from .sync import PRUNE_AFTER, Replica, table_names

APP_DIR = Path(__file__).resolve().parents[3]  # …/app
DEFAULT_UI_DIR = APP_DIR / "lance" / "ui"  # the admin UI, shared with the Bun edition
DEFAULT_CONSOLE_DIR = APP_DIR / "ui"  # the old PocketBase console

MAX_LIMIT = 500
MAX_WHERE = 2000
MAX_Q = 500

NO_HOSTS = ("no Ollama hosts configured: put \"ollama_urls\" in ~/.config/structor/lance.json "
            "or set STRUCTOR_OLLAMA_URLS")

#: What a vector hit carries back, in this order; the score column is appended by the route.
VECTOR_COLUMNS = ("event_id", "session", "ts", "role", "text")

JSON_CT = "application/json; charset=utf-8"
JSON_HEADERS = {"content-type": JSON_CT, "cache-control": "no-store"}
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'"
)

# Hosts a browser may address this server as. Anything else (a DNS-rebound
# hostname, a LAN name) is refused, which is what makes "loopback only" an
# access control rather than just a bind address.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Scalar functions a `where` may call. LanceDB hands the predicate to
# DataFusion, whose full library includes things like repeat() that can
# allocate without bound; the admin is unauthenticated on loopback, so keep the
# callable surface to what a table filter needs.
ALLOWED_FUNCS = frozenset({
    "lower", "upper", "length", "char_length", "character_length", "octet_length", "substr", "substring",
    "starts_with", "ends_with", "contains", "strpos", "position", "trim", "ltrim", "rtrim", "btrim",
    "regexp_like", "regexp_match", "coalesce", "nullif", "abs", "round", "floor", "ceil",
    "in", "not", "exists", "any", "all", "cast", "date_part", "date_trunc", "to_timestamp",
})

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_COMMENT_OR_SEMI = re.compile(r";|--|/\*")
# The word boundary is spelled out as an ASCII lookbehind because Python's \b is
# Unicode-aware and JavaScript's is not: with \b, "érepeat(x)" has no boundary
# before "repeat" (é is a word character here) and the call would slip past the
# allow-list that the Bun edition refuses it by.
_CALL = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.ASCII)
_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Arrow-JS spellings for the types the Node bindings hand the shared UI; pyarrow
# prints its own ("string", "double"). Only the ones these schemas use are
# mapped — anything else goes out as pyarrow printed it.
ARROW_JS_TYPES = {"string": "Utf8", "double": "Float64", "bool": "Bool"}

EXTRA_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".mjs": "text/javascript", ".map": "application/json"}


# ---- validation ------------------------------------------------------------


def safe_where(w: str) -> tuple[str | None, str | None]:
    """Validate a ``where`` predicate; returns ``(predicate, None)`` or ``(None, message)``.

    String literals are blanked before the checks so text like ``'npm i --save'``
    is fine; comments, semicolons and functions outside ALLOWED_FUNCS are refused.
    """
    s = (w or "").strip()
    if not s:
        return "", None
    if len(s) > MAX_WHERE:
        return None, "where: too long"
    bare = _STRING_LITERAL.sub("''", s)
    if _COMMENT_OR_SEMI.search(bare):
        return None, "where: comments and semicolons are not allowed"
    for m in _CALL.finditer(bare):
        if m.group(1).lower() not in ALLOWED_FUNCS:
            return None, f"where: function {m.group(1)}() is not allowed"
    return s, None


def safe_select(raw: str) -> list[str] | None:
    """Column names only: letters, digits, underscore. ``None`` when anything else appears."""
    cols = [c.strip() for c in (raw or "").split(",") if c.strip()]
    return cols if all(_COLUMN.match(c) for c in cols) else None


def _number_or(raw: str | None, default: int, maximum: int | None = None) -> int:
    """JavaScript's ``Number(raw) || default``: unparseable, empty and zero all fall back.

    ``Number("Infinity")`` is a finite-looking answer in JavaScript and the Bun
    edition simply clamps it with ``Math.min``/``Math.max``; Python's ``int()``
    raises OverflowError on it instead. So an infinity saturates here: ``+inf``
    at ``maximum`` when the caller has one (a limit), otherwise at ``default``
    (an offset of Infinity is no offset), and ``-inf`` at 0, which the caller's
    own ``max(1, …)`` / ``max(0, …)`` then floors exactly as JavaScript does.
    """
    try:
        v = float(raw) if raw not in (None, "") else 0.0
    except (ValueError, OverflowError):
        v = 0.0
    if math.isnan(v) or not v:  # NaN and 0 are both falsy to Number()
        return default
    if math.isinf(v):
        return (maximum if maximum is not None else default) if v > 0 else 0
    try:
        return int(v)
    except (ValueError, OverflowError):  # a float too large for a Python int
        return default


# ---- JSON ------------------------------------------------------------------


def plain(v: Any) -> Any:
    """Anything Arrow, numpy or LanceDB hands back, as something ``json.dumps`` accepts."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, Mapping):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "as_py"):  # pyarrow scalar
        return plain(v.as_py())
    if hasattr(v, "item") and hasattr(v, "dtype") and getattr(v, "shape", None) == ():  # numpy scalar
        return plain(v.item())
    if hasattr(v, "tolist"):  # numpy array / Arrow vector
        return plain(v.tolist())
    if isinstance(v, (list, tuple, set, frozenset)):
        return [plain(x) for x in v]
    if isinstance(v, Iterable):
        return [plain(x) for x in v]
    return str(v)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def camel_keys(v: Any) -> Any:
    """``{'total_bytes': 1}`` → ``{'totalBytes': 1}``, recursively: the UI reads the Node spelling."""
    if isinstance(v, Mapping):
        return {_camel(str(k)): camel_keys(x) for k, x in v.items()}
    if isinstance(v, list):
        return [camel_keys(x) for x in v]
    return v


def json_response(value: Any, status: int = 200) -> Response:
    return Response(json.dumps(plain(value)).encode(), status_code=status, headers=dict(JSON_HEADERS))


def bad(message: str, status: int = 400) -> Response:
    return json_response({"error": message}, status)


# ---- host / origin ---------------------------------------------------------


def host_of(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h[: h.find("]") + 1] if "]" in h else h
    return h.split(":")[0]


def origin_refusal(host_header: str, origin: str | None) -> tuple[str, int] | None:
    """``None`` when the request may proceed, otherwise ``(message, status)``."""
    if host_of(host_header) not in LOOPBACK_HOSTS:
        return "loopback only", 403
    if origin and origin != "null":
        try:
            u = urlsplit(origin)
        except ValueError:
            return "bad origin", 403
        if not u.scheme or not u.netloc:
            return "bad origin", 403
        if (u.hostname or "") not in LOOPBACK_HOSTS or u.netloc.lower() != (host_header or "").strip().lower():
            return "cross-origin request refused", 403
    return None


class AdminGuard:
    """Cross-cutting ASGI wrapper: loopback check, the read-only write ban, and JSON for every error.

    Raw ASGI rather than ``BaseHTTPMiddleware`` so the console's SSE proxy keeps
    streaming, and so a handler that raises produces ``{"error": …}`` instead of
    a traceback page — the UI parses every response as JSON.
    """

    def __init__(self, app: Any, read_only: bool = False):
        self.app = app
        self.read_only = read_only

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        refusal = origin_refusal(headers.get("host", ""), headers.get("origin"))
        if refusal:
            await _send_json(send, {"error": refusal[0]}, refusal[1])
            return
        path = scope.get("path", "")
        # the console's POSTs (sign-in, realtime subscribe) never write a table; the guard is for /api/ only
        if self.read_only and scope.get("method") == "POST" and path.startswith("/api/"):
            await _send_json(send, {"error": "read-only instance (started with --no-sync)"}, 405)
            return
        started = False

        async def watch(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, watch)
        except Exception as e:  # a bad predicate must be a 500 body, not a dead connection
            if started:
                raise
            await _send_json(send, {"error": str(e) or type(e).__name__}, 500)


async def _send_json(send: Any, value: Any, status: int) -> None:
    body = json.dumps(plain(value)).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", JSON_CT.encode()), (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


# ---- static ----------------------------------------------------------------


def serve_file(root: Path, rel: str) -> Response:
    """One file under ``root``: html is never cached, everything else for an hour."""
    if ".." in rel or rel.startswith("/"):
        return bad("not found", 404)
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return bad("not found", 404)
    if not target.is_file():
        return Response("not found", status_code=404, media_type="text/plain; charset=utf-8")
    ext = target.suffix.lower()
    ctype = EXTRA_TYPES.get(ext) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {"cache-control": "no-cache" if ext == ".html" else "public, max-age=3600"}
    if ext == ".html":
        headers["content-security-policy"] = CSP
    return Response(target.read_bytes(), headers=headers, media_type=ctype)


# ---- app -------------------------------------------------------------------


def create_app(
    replicas: dict[str, Replica],
    *,
    version: str = "dev",
    data_root: Path = APP_DIR / "lance_data_py",
    read_only: bool = False,
    ui_dir: Path = DEFAULT_UI_DIR,
    console_dir: Path = DEFAULT_CONSOLE_DIR,
    embedder: Callable[[Replica], Any] | None = None,
) -> FastAPI:
    """``embedder`` builds the vector search for one replica; the default is an
    Ollama pool read from ~/.config/structor/lance.json. Tests pass their own."""
    ui_dir, console_dir, data_root = Path(ui_dir), Path(console_dir), Path(data_root)
    app = FastAPI(title="structor-lance (python)", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AdminGuard, read_only=read_only)
    app.state.replicas = replicas
    app.state.read_only = read_only

    async def http_error(_r: Request, exc: StarletteHTTPException) -> Response:
        return json_response({"error": exc.detail}, exc.status_code)

    async def validation_error(_r: Request, exc: RequestValidationError) -> Response:
        return json_response({"error": str(exc)}, 400)

    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)

    def replica_of(name: str) -> Replica | None:
        return replicas.get(name)

    def table_info(r: Replica, model: type[Table]) -> dict:
        t = r.table(model)
        info = {
            "name": model.__table__,
            "rows": t.count_rows(),
            "version": t.version,
            "indices": [index_brief(i) for i in _indices(t)],
            "fts": model.__fts__,
            "stamp": model.__stamp__,
        }
        if model is Event:  # events are the only table with a vector sibling
            info["vectors"] = vector_rows(r)
        return info

    def vector_table(r: Replica) -> Any | None:
        """``event_vectors`` when it exists — never created here, so a store without vectors stays without them."""
        db = r.db()
        return db.open_table(vectors.TABLE) if vectors.TABLE in table_names(db) else None

    def vector_rows(r: Replica) -> int:
        try:
            t = vector_table(r)
        except Exception:  # noqa: BLE001 — a half-written vector table must not hide the events count
            return 0
        return t.count_rows() if t is not None else 0

    # ---- /api/status -------------------------------------------------------

    @app.get("/api/status")
    def status(_request: Request) -> Response:
        targets = []
        for r in replicas.values():
            tables: dict[str, Any] = {}
            for model in TABLES:
                try:
                    tables[model.__table__] = table_info(r, model)
                except Exception as e:  # noqa: BLE001 — one broken table must not hide the rest
                    tables[model.__table__] = {"name": model.__table__, "error": str(e)}
            # state_copy(), not the live dict: the follow thread mutates it while json.dumps walks it
            targets.append({"name": r.target.name, "url": r.target.url, "dir": str(r.dir), "tables": tables,
                            "sync": r.state_copy()})
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return json_response({"version": version, "dataRoot": str(data_root), "time": now, "targets": targets})

    # ---- /api/{t}/sync -----------------------------------------------------

    @app.api_route("/api/{t}/sync", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def sync(t: str, request: Request) -> Response:
        """POST pulls; every other method is the read path, which is what the Bun
        edition's one route does (it branches on POST and falls through)."""
        r = replica_of(t)
        if r is None:
            return bad("unknown target", 404)
        if request.method == "POST":
            pulled = r.sync_now()
            return json_response({"pulled": pulled, "state": r.state_copy()})
        return json_response({"state": r.state_copy(), "lag": r.lag()})

    # ---- /api/{t}/tables ---------------------------------------------------

    @app.get("/api/{t}/tables")
    def tables(t: str) -> Response:
        r = replica_of(t)
        if r is None:
            return bad("unknown target", 404)
        return json_response([table_info(r, model) for model in TABLES])

    def resolve(t: str, n: str) -> tuple[Replica, type[Table]] | Response:
        r = replica_of(t)
        if r is None:
            return bad("unknown target", 404)
        model = BY_NAME.get(n)
        if model is None:
            return bad("unknown table", 404)
        return r, model

    @app.get("/api/{t}/tables/{n}/schema")
    def table_schema(t: str, n: str) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        sc = r.table(model).schema  # iterating a schema keeps the table's own field order
        return json_response({"fields": [{"name": f.name, "type": arrow_js_type(f.type), "nullable": f.nullable} for f in sc]})

    @app.get("/api/{t}/tables/{n}/rows")
    def table_rows(t: str, n: str, request: Request) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        qp = request.query_params
        where, err = safe_where(qp.get("where", ""))
        if err:
            return bad(err)
        limit = min(MAX_LIMIT, max(1, _number_or(qp.get("limit"), 50, MAX_LIMIT)))
        offset = max(0, _number_or(qp.get("offset"), 0))
        select = safe_select(qp.get("select", ""))
        if select is None:
            return bad("select: column names only")
        tbl = r.table(model)
        q = tbl.search()
        if where:
            q = q.where(where)
        if select:
            q = q.select(select)
        rows = q.limit(limit).offset(offset).to_list()  # a bad predicate raises here → 500 {error}
        try:
            total = tbl.count_rows(where) if where else tbl.count_rows()
        except Exception:  # noqa: BLE001 — the predicate already survived the scan above
            total = -1
        return json_response({"rows": rows, "total": total, "limit": limit, "offset": offset})

    @app.get("/api/{t}/tables/{n}/search")
    def table_search(t: str, n: str, request: Request) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        if not model.__fts__:
            return bad("this table has no full-text index")
        qp = request.query_params
        qs = (qp.get("q") or "").strip()
        if not qs:
            return bad("q required")
        if len(qs) > MAX_Q:
            return bad("q: too long")
        where, err = safe_where(qp.get("where", ""))
        if err:
            return bad(err)
        limit = min(MAX_LIMIT, max(1, _number_or(qp.get("limit"), 50, MAX_LIMIT)))
        q = r.table(model).search(qs, query_type="fts", fts_columns=model.__fts__)
        if where:
            q = q.where(where)
        # no .select(): lance warns when a projection drops _score
        rows = q.limit(limit).to_list()
        return json_response({"rows": rows, "q": qs, "limit": limit})

    @app.get("/api/{t}/tables/events/vsearch")
    def events_vsearch(t: str, request: Request) -> Response:
        """Nearest events to ``q`` in the bge-m3 space; ``mode=hybrid`` fuses the vector hits with FTS."""
        r = replica_of(t)
        if r is None:
            return bad("unknown target", 404)
        qp = request.query_params
        qs = (qp.get("q") or "").strip()
        if not qs:
            return bad("q required")
        if len(qs) > MAX_Q:
            return bad("q: too long")
        where, err = safe_where(qp.get("where", ""))
        if err:
            return bad(err)
        limit = min(MAX_LIMIT, max(1, _number_or(qp.get("limit"), 20, MAX_LIMIT)))
        mode = "hybrid" if (qp.get("mode") or "").lower() == "hybrid" else "vector"
        vt = vector_table(r)
        if vt is None:
            return bad(f"no {vectors.TABLE} table on {t}: run 'structor-lance embed' first")
        if embedder is None and not vectors.ollama_urls():
            return bad(NO_HOSTS)
        # the first hybrid query builds the FTS index on event_vectors, which is a
        # write; a --no-sync instance promised not to make any
        if mode == "hybrid" and read_only and not any(i.name == "text_idx" for i in _indices(vt)):
            return bad(f"hybrid needs a full-text index on {vectors.TABLE}, and this is a "
                       "read-only instance (started with --no-sync)", 405)
        e = embedder(r) if embedder else vectors.Embedder(r)
        # the columns the UI shows, and whichever score the mode produced — never
        # the vector itself, which is 1024 floats per hit
        rows = [
            {k: row[k] for k in (*VECTOR_COLUMNS, "_distance", "_relevance_score", "_score") if k in row}
            for row in e.search(qs, limit=limit, where=where, mode=mode)
        ]
        return json_response({"rows": rows, "q": qs, "limit": limit, "mode": mode})

    @app.get("/api/{t}/tables/{n}/stats")
    def table_stats(t: str, n: str) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        tbl = r.table(model)
        try:
            versions = len(tbl.list_versions())
        except Exception:  # noqa: BLE001
            versions = 0
        try:
            stats = camel_keys(tbl.stats())
        except Exception:  # noqa: BLE001
            stats = None
        return json_response({
            "rows": tbl.count_rows(),
            "version": tbl.version,
            "versions": versions,
            "indices": [index_brief(i, node=True) for i in _indices(tbl)],
            "stats": stats,
        })

    @app.post("/api/{t}/tables/{n}/optimize")
    def table_optimize(t: str, n: str) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        tbl = r.table(model)
        before = table_snapshot(tbl)
        tbl.optimize(cleanup_older_than=PRUNE_AFTER)
        return json_response({"ok": True, "result": optimize_result(before, table_snapshot(tbl))})

    @app.post("/api/{t}/tables/{n}/fts")
    def table_fts(t: str, n: str) -> Response:
        got = resolve(t, n)
        if isinstance(got, Response):
            return got
        r, model = got
        if not model.__fts__:
            return bad("this table has no full-text column")
        r.table(model).create_index(model.__fts__, config=FTS(), replace=True)
        r.mark_fts_built(model.__table__)
        return json_response({"ok": True, "column": model.__fts__})

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def api_not_found(rest: str) -> Response:
        return bad("not found", 404)

    # ---- the old console, one copy per target ------------------------------

    @app.get("/console")
    def console_root() -> Response:
        first = next(iter(replicas), "local")
        return RedirectResponse(f"/console/{first}/", status_code=302)

    @app.api_route("/console/{t}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def console_no_slash(t: str) -> Response:
        if replica_of(t) is None:
            return bad("unknown target", 404)
        return RedirectResponse(f"/console/{t}/", status_code=302)

    @app.api_route("/console/{t}/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def console(t: str, rest: str, request: Request) -> Response:
        r = replica_of(t)
        if r is None:
            return bad("unknown target", 404)
        if rest.startswith("api/"):
            body = await request.body()
            reply = await run_in_threadpool(
                facade.handle, r, rest[4:], request.method, dict(request.query_params), dict(request.headers), body
            )
            return reply_to_response(reply)
        return serve_file(console_dir, rest or "index.html")

    # ---- the admin UI ------------------------------------------------------

    @app.get("/{rest:path}")
    def static(rest: str) -> Response:
        return serve_file(ui_dir, rest.lstrip("/") or "index.html")

    return app


# ---- helpers ---------------------------------------------------------------


def _indices(tbl: Any) -> list:
    try:
        return list(tbl.list_indices())
    except Exception:  # noqa: BLE001 — a table with no index manifest yet
        return []


def index_brief(i: Any, node: bool = False) -> dict:
    """One index as the UI reads it.

    ``/tables`` maps the kind to ``type`` (admin.ts does the mapping); ``/stats``
    passes the binding's own record through, which on Node is spelled
    ``indexType`` — ``node=True`` picks that spelling.
    """
    return {
        "name": getattr(i, "name", ""),
        "columns": plain(getattr(i, "columns", [])),
        "indexType" if node else "type": str(getattr(i, "index_type", "")),
    }


def arrow_js_type(t: Any) -> str:
    """A pyarrow type as Arrow-JS prints it, which is the spelling in the shared UI."""
    name = str(t)
    return ARROW_JS_TYPES.get(name, name)


def table_snapshot(tbl: Any) -> dict[str, int]:
    """fragments / bytes / versions of a table, each 0 when unreadable (a brand new table has no stats yet)."""
    out = {"fragments": 0, "bytes": 0, "versions": 0}
    with contextlib.suppress(Exception):
        st = tbl.stats() or {}
        out["fragments"] = int(((st.get("fragment_stats") or {}).get("num_fragments")) or 0)
        out["bytes"] = int(st.get("total_bytes") or 0)
    with contextlib.suppress(Exception):
        out["versions"] = len(tbl.list_versions())
    return out


def optimize_result(before: dict[str, int], after: dict[str, int]) -> dict:
    """What the compaction did, in the shape the Node binding returns and the shared UI prints.

    lancedb-python 0.38's ``Table.optimize()`` returns None, so the numbers are
    the difference between two snapshots: fragments before → after is the
    compaction, versions and bytes before → after is the prune.
    """
    return {
        "compaction": {
            "fragmentsRemoved": before["fragments"], "fragmentsAdded": after["fragments"],
            "filesRemoved": before["fragments"], "filesAdded": after["fragments"],
        },
        "prune": {
            "bytesRemoved": max(0, before["bytes"] - after["bytes"]),
            "oldVersionsRemoved": max(0, before["versions"] - after["versions"]),
        },
    }


def stream_off_threadpool(it: Iterator[bytes]) -> Any:
    """Serve a blocking iterator from a thread of its own, not the ASGI threadpool.

    Starlette iterates a sync body through ``iterate_in_threadpool``: every open
    SSE stream would sit on one of the pool's 40 workers for as long as the
    facade's generator blocks (up to its 5s tick), so forty open consoles
    stalled every other request. Here one daemon thread per stream pumps chunks
    into an asyncio queue and the event loop only ever awaits; when the client
    goes away the pump is told to stop and closes the iterator on its own
    thread, which in turn closes the upstream connection.
    """
    DONE = object()

    async def agen():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        stopped = threading.Event()

        def hand_over(item: object) -> bool:
            fut = asyncio.run_coroutine_threadsafe(q.put(item), loop)
            try:
                fut.result(timeout=30)
                return True
            except Exception:  # noqa: BLE001 — loop gone or consumer stuck: give up on this stream
                return False

        def pump() -> None:
            try:
                # an upstream failure just ends the stream; the facade already reported the upstream status
                with contextlib.suppress(Exception):
                    for chunk in it:
                        if stopped.is_set() or not hand_over(chunk):
                            break
            finally:
                with contextlib.suppress(Exception):
                    close = getattr(it, "close", None)
                    if close:
                        close()
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(q.put_nowait, DONE)

        threading.Thread(target=pump, name="sse-pump", daemon=True).start()
        try:
            while True:
                item = await q.get()
                if item is DONE:
                    break
                yield item
        finally:
            stopped.set()  # the pump notices on its next chunk (≤ one tick) and closes the upstream

    return agen()


def reply_to_response(reply: facade.Reply) -> Response:
    """A framework-neutral ``facade.Reply`` as a FastAPI response; an iterator body streams."""
    headers = dict(reply.headers)
    body = reply.body
    if isinstance(body, (bytes, bytearray)):
        return Response(bytes(body), status_code=reply.status, headers=headers)
    if hasattr(body, "__aiter__"):
        return StreamingResponse(body, status_code=reply.status, headers=headers)
    if isinstance(body, Iterator) or hasattr(body, "__next__"):
        return StreamingResponse(stream_off_threadpool(body), status_code=reply.status, headers=headers)
    return Response(str(body).encode(), status_code=reply.status, headers=headers)
