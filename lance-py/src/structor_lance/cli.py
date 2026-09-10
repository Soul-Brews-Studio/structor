"""structor-lance CLI — the backend without the browser.

Reads go straight to the Lance directory, which is safe alongside the running
replica; writes (sync, optimize, fts) go through the admin API when it is up so
only one process mutates a table, and fall back to direct access when it is not.

    structor-lance targets
    structor-lance status   [--target local]
    structor-lance tables   [--target local]
    structor-lance schema   <table> [--target local]
    structor-lance rows     <table> [--where "role = 'user'"] [--limit 20] [--offset 0] [--select id,ts,text] [--json]
    structor-lance search   <query> [--table events] [--where …] [--limit 20] [--json]
    structor-lance lag      [--target local]
    structor-lance sync     [--target local]
    structor-lance optimize <table> [--target local]
    structor-lance fts      [--table events] [--target local]
    structor-lance embed    [--limit N] [--batch 128] [--where PRED] [--target local]
    structor-lance vsearch  <query> [--limit 20] [--where …] [--mode vector|hybrid] [--json]
    structor-lance vectors  [--target local]
    structor-lance wiki-index  <dir> [--target local] [--json]
    structor-lance wiki-search <query> [--mode hybrid|vector|fts] [--limit 10] [--json]
    structor-lance wiki     [--target local]   # rows, files, newest last text change
    structor-lance serve    [--http 127.0.0.1:8094] [--data …] [--targets local,kvmlab1] [--interval 15] [--no-sync]
    structor-lance once     [--targets local]

Env: STRUCTOR_LANCE_PY_DATA (default app/lance_data_py), STRUCTOR_LANCE_PY_HTTP
(default 127.0.0.1:8094), STRUCTOR_LANCE_TARGETS and STRUCTOR_LANCE_INTERVAL
(defaults for ``serve``), STRUCTOR_OLLAMA_URLS (the embedding pool, otherwise
``ollama_urls`` in ~/.config/structor/lance.json). Passwords are never printed:
only a target's name and url ever leave this process.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from lancedb.index import FTS

from .schema import TABLES, Table, model_for
from .sync import Replica
from .targets import load_targets

HTTP_ENV = "STRUCTOR_LANCE_PY_HTTP"
DATA_ENV = "STRUCTOR_LANCE_PY_DATA"
TARGETS_ENV = "STRUCTOR_LANCE_TARGETS"
INTERVAL_ENV = "STRUCTOR_LANCE_INTERVAL"
API_TIMEOUT_S = 120.0
DEFAULT_INTERVAL_S = 15.0
SEARCH_COLUMNS = "_score,id,session,ts,role,text"
NO_HOSTS_EXIT = 78  # EX_CONFIG: the pool is not configured, so there is nothing to retry
TABLE_TEXT = 120  # characters of a wiki section shown in the table; --json carries all of it

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__.split("\n\n")[0])

TargetOpt = Annotated[str, typer.Option("--target", "-t", help="target name (see 'structor-lance targets')")]
JsonOpt = Annotated[bool, typer.Option("--json", help="print JSON instead of a text table")]


# ---- environment ----------------------------------------------------------


def admin_http() -> str:
    """host:port of the admin API — the process allowed to write these tables."""
    from .server import DEFAULT_HTTP

    return os.environ.get(HTTP_ENV) or DEFAULT_HTTP


def data_root() -> Path:
    """Directory holding one Lance store per target."""
    from .server import DEFAULT_DATA

    return Path(os.environ.get(DATA_ENV) or DEFAULT_DATA)


def env_targets() -> list[str] | None:
    """``$STRUCTOR_LANCE_TARGETS`` — what scripts/agent.sh hands the launchd copy of ``serve``."""
    return columns(os.environ.get(TARGETS_ENV, "")) or None


def env_interval() -> float:
    """``$STRUCTOR_LANCE_INTERVAL`` seconds between pulls; the built-in default when it is unset or junk."""
    try:
        return float(os.environ.get(INTERVAL_ENV) or DEFAULT_INTERVAL_S)
    except ValueError:
        return DEFAULT_INTERVAL_S


# ---- output ---------------------------------------------------------------


def fail(message: str, code: int = 64) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def echo_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


def render(value: object) -> str:
    """One cell as text, newlines and runs of spaces flattened."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return re.sub(r"\s+", " ", text)


def fit(text: str, width: int) -> str:
    return text[: width - 1] + "…" if len(text) > width else text.ljust(width)


def clip(text: str, width: int) -> str:
    """``fit`` without the padding: shortens for a table cell, never for machine-readable output."""
    return text[: width - 1] + "…" if len(text) > width else text


def print_rows(rows: list[dict[str, Any]], as_json: bool = False, wide: str = "text") -> None:
    """A plain column layout, like the Bun edition's printRows. ``wide`` names the roomy column."""
    if as_json:
        echo_json(rows)
        return
    if not rows:
        typer.echo("(no rows)")
        return
    cols = list(rows[0].keys())
    cells = [[render(r.get(c)) for c in cols] for r in rows]
    widths = [min(60 if c == wide else 28, max(len(c), *(len(row[i]) for row in cells))) for i, c in enumerate(cols)]
    typer.echo("  ".join(fit(c, w) for c, w in zip(cols, widths, strict=True)).rstrip())
    for row in cells:
        typer.echo("  ".join(fit(v, w) for v, w in zip(row, widths, strict=True)).rstrip())


# ---- lookups --------------------------------------------------------------


def replica(target: str) -> Replica:
    known = load_targets()
    for t in known:
        if t.name == target:
            return Replica(t, data_root())
    fail(f"unknown target {target!r} (have: {', '.join(t.name for t in known)})")


def model(name: str) -> type[Table]:
    try:
        return model_for(name)
    except KeyError as e:
        fail(str(e.args[0]))


def fts_model(name: str) -> type[Table]:
    m = model(name)
    if not m.__fts__:
        fail(f"{name} has no full-text column")
    return m


def non_negative(value: int) -> int:
    if value < 0:
        raise typer.BadParameter("must be a non-negative integer")
    return value


def at_least_one(value: int) -> int:
    """A limit of 0 is a typo, not a request for nothing: Lance would return an empty page."""
    if value < 1:
        raise typer.BadParameter("must be 1 or more")
    return value


def at_least_one_or_all(value: int | None) -> int | None:
    """The same, for a limit whose absence means "everything"."""
    return None if value is None else at_least_one(value)


def columns(select: str) -> list[str]:
    return [c.strip() for c in select.split(",") if c.strip()]


def via_api(path: str, method: str = "POST") -> Any | None:
    """Ask the admin API. ``None`` means nothing answers there, so do it directly."""
    import httpx

    url = f"http://{admin_http()}{path}"
    try:
        r = httpx.request(method, url, timeout=API_TIMEOUT_S)
    except httpx.TimeoutException:
        fail(f"admin API timed out on {path}", 70)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        fail(f"{url} → {r.status_code} {r.text[:300]}", 70)
    try:
        return r.json()
    except ValueError:
        fail(f"{url} → {r.status_code} but not JSON", 70)


def indices_of(tbl: Any) -> str:
    try:
        return ",".join(i.name for i in tbl.list_indices())
    except Exception:  # noqa: BLE001 — a brand new table has no index manifest yet
        return ""


# ---- read commands --------------------------------------------------------


@app.command()
def targets(as_json: JsonOpt = False) -> None:
    """Every replicable store found in ~/.config/structor (name and url only)."""
    print_rows([{"name": t.name, "url": t.url} for t in load_targets()], as_json, wide="url")


@app.command()
def status(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """Row counts, table versions and the last sync of one target."""
    r = replica(target)
    rows_out = []
    for m in TABLES:
        tbl = r.table(m)
        rows_out.append({"table": m.__table__, "rows": tbl.count_rows(), "version": tbl.version, "indices": indices_of(tbl)})
    if as_json:
        echo_json({"target": r.target.name, "url": r.target.url, "dir": str(r.dir), "tables": rows_out, "sync": r.state})
        return
    typer.echo(f"{r.target.name}  {r.target.url}  →  {r.dir}")
    print_rows(rows_out)
    error = r.state.get("lastError") or ""
    typer.echo(f"last run {r.state.get('lastRun') or 'never'}  {r.state.get('lastDurationMs', 0)}ms" + (f"  ERROR {error}" if error else ""))


@app.command()
def tables(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """The five replicated tables: rows, the column each is paged by, its full-text column."""
    r = replica(target)
    print_rows(
        [{"table": m.__table__, "rows": r.table(m).count_rows(), "stamp": m.__stamp__, "fts": m.__fts__ or ""} for m in TABLES],
        as_json,
    )


@app.command()
def schema(table: str, target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """Arrow fields of one table, as stored."""
    tbl = replica(target).table(model(table))
    print_rows([{"field": f.name, "type": str(f.type), "nullable": f.nullable} for f in tbl.schema], as_json, wide="type")


@app.command()
def rows(
    table: str,
    where: Annotated[str, typer.Option("--where", help="SQL predicate, e.g. \"role = 'user'\"")] = "",
    limit: Annotated[int, typer.Option("--limit", callback=at_least_one)] = 20,
    offset: Annotated[int, typer.Option("--offset", callback=non_negative)] = 0,
    select: Annotated[str, typer.Option("--select", help="comma-separated columns")] = "",
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Rows in storage order (LanceDB has no ORDER BY): filter, then page with --limit/--offset."""
    q = replica(target).table(model(table)).search()
    if where:
        q = q.where(where)
    if select:
        q = q.select(columns(select))
    print_rows(q.limit(limit).offset(offset).to_list(), as_json)


@app.command()
def search(
    query: str,
    table: Annotated[str, typer.Option("--table")] = "events",
    where: Annotated[str, typer.Option("--where")] = "",
    limit: Annotated[int, typer.Option("--limit", callback=at_least_one)] = 20,
    select: Annotated[str, typer.Option("--select")] = SEARCH_COLUMNS,
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Full-text search over the table's indexed column, best match first."""
    m = fts_model(table)
    q = replica(target).table(m).search(query, query_type="fts")
    if where:
        q = q.where(where)
    # no .select(): lance warns when a projection drops _score, so trim the columns afterwards
    hits = q.limit(limit).to_list()
    keep = [c for c in columns(select) if c != "_score"]
    print_rows([{"_score": f"{float(h.get('_score') or 0):.3f}", **{c: h.get(c) for c in keep}} for h in hits], as_json)


@app.command()
def lag(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """Rows on the PocketBase side against rows here. A remote of -1 means that store did not answer."""
    counts = replica(target).lag()
    if as_json:
        echo_json(counts)
        return
    print_rows(
        [
            {"table": name, "remote": v["remote"], "local": v["local"], "behind": "?" if v["remote"] < 0 else v["remote"] - v["local"]}
            for name, v in counts.items()
        ]
    )


# ---- write commands (through the admin API when it is up) -----------------


@app.command()
def sync(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """Pull everything new for every table."""
    via = via_api(f"/api/{target}/sync")
    if via is not None:
        if as_json:
            echo_json(via)
        else:
            typer.echo(f"pulled {via.get('pulled', 0)} rows (via admin)")
        return
    r = replica(target)
    pulled = r.sync_now()
    if as_json:
        echo_json({"pulled": pulled, "state": r.state})
        return
    error = r.state.get("lastError") or ""
    typer.echo(f"pulled {pulled} rows" + (f"  ERROR {error}" if error else ""))


@app.command()
def optimize(table: str, target: TargetOpt = "local") -> None:
    """Compact fragments and index the rows added since the last optimize."""
    m = model(table)
    via = via_api(f"/api/{target}/tables/{m.__table__}/optimize")
    if via is not None:
        echo_json(via)
        return
    tbl = replica(target).table(m)
    tbl.optimize()
    echo_json({"ok": True, "table": m.__table__, "rows": tbl.count_rows(), "version": tbl.version})


@app.command()
def fts(table: Annotated[str, typer.Option("--table")] = "events", target: TargetOpt = "local") -> None:
    """(Re)build the full-text index on that table's text column."""
    m = fts_model(table)
    via = via_api(f"/api/{target}/tables/{m.__table__}/fts")
    if via is not None:
        echo_json(via)
        return
    r = replica(target)
    r.table(m).create_index(m.__fts__, config=FTS(), replace=True)
    r.mark_fts_built(m.__table__)
    echo_json({"ok": True, "table": m.__table__, "column": m.__fts__})


# ---- vectors (optional: FTS is the default search) ------------------------


def embedder(target: str) -> Any:
    """An ``Embedder`` for one target, or a clear exit when no Ollama host is configured."""
    from .vectors import Embedder, ollama_urls

    hosts = ollama_urls()
    if not hosts:
        fail('no Ollama hosts configured: put "ollama_urls": ["http://host:11434"] in '
             "~/.config/structor/lance.json, or set STRUCTOR_OLLAMA_URLS", NO_HOSTS_EXIT)
    return Embedder(replica(target))


@app.command()
def embed(
    limit: Annotated[int | None, typer.Option("--limit", help="stop after this many rows (default: everything pending)", callback=at_least_one_or_all)] = None,
    batch: Annotated[int, typer.Option("--batch", help="rows per add(); each is sharded across the pool", callback=at_least_one)] = 128,
    where: Annotated[str, typer.Option("--where", help="extra predicate on events, e.g. \"iso_week = '2026-W37'\"")] = "",
    target: TargetOpt = "local",
) -> None:
    """Embed conversational events into event_vectors, newest first. Resumable: rerun to continue."""
    e = embedder(target)
    written = e.run(limit=limit, batch=batch, where=where, log=lambda line: typer.echo(line))
    typer.echo(f"{written} rows embedded")


@app.command()
def vsearch(
    query: str,
    limit: Annotated[int, typer.Option("--limit", callback=at_least_one)] = 20,
    where: Annotated[str, typer.Option("--where")] = "",
    mode: Annotated[str, typer.Option("--mode", help="vector | hybrid (vector + FTS, RRF-fused)")] = "vector",
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Nearest events to a query in the embedding space, closest first."""
    if mode not in ("vector", "hybrid"):
        fail("--mode must be vector or hybrid")
    hits = embedder(target).search(query, limit=limit, where=where, mode=mode)
    score = "_relevance_score" if mode == "hybrid" else "_distance"
    print_rows(
        [{score: f"{float(h.get(score) or 0):.4f}", "ts": h.get("ts"), "role": h.get("role"), "text": h.get("text")} for h in hits],
        as_json,
    )


@app.command()
def vectors(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """How much of events is embedded: rows in event_vectors, and rows still waiting."""
    from .sync import table_names
    from .vectors import CONVERSATIONAL, TABLE, Embedder

    r = replica(target)
    db = r.db()
    # counting needs no Ollama host — pending() compares ids, it does not embed —
    # and a store with no vectors is left without them: nothing is created here
    if TABLE in table_names(db):
        embedded = db.open_table(TABLE).count_rows()
        waiting = len(Embedder(r).pending())
    else:
        embedded = 0
        waiting = r.table(model("events")).count_rows(CONVERSATIONAL)
    if as_json:
        echo_json({"target": r.target.name, "table": TABLE, "embedded": embedded, "pending": waiting})
        return
    print_rows([{"table": TABLE, "embedded": embedded, "pending": waiting}])


# ---- the wiki: a markdown directory in the same store ---------------------


@app.command("wiki-index")
def wiki_index(
    directory: Annotated[str, typer.Argument(help="directory of *.md to index (walked recursively)")],
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Index a markdown directory into the replica's wiki table; only changed sections are embedded."""
    from . import wiki as wiki_mod

    root = Path(directory).expanduser()
    if not root.is_dir():
        fail(f"{root} is not a directory")
    counts = wiki_mod.index_dir(replica(target), root, func=embedder(target).func,
                                log=(lambda _line: None) if as_json else (lambda line: typer.echo(line)))
    if as_json:
        echo_json({"target": target, "dir": str(root.resolve()), **counts})
        return
    print_rows([counts])


@app.command("wiki-search")
def wiki_search(
    query: str,
    mode: Annotated[str, typer.Option("--mode", help="hybrid (vector + FTS, RRF-fused) | vector | fts")] = "hybrid",
    limit: Annotated[int, typer.Option("--limit", callback=at_least_one)] = 10,
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Search the wiki sections: title, section, file, score and the text (whole section under --json)."""
    from . import wiki as wiki_mod

    if mode not in ("hybrid", "vector", "fts"):
        fail("--mode must be hybrid, vector or fts")
    hits = wiki_mod.search(replica(target), query, limit=limit, mode=mode)
    score = {"hybrid": "_relevance_score", "fts": "_score", "vector": "_distance"}[mode]
    rows = [{"title": h.get("title"), "section": h.get("section"), "path": h.get("path"),
             score: f"{float(h.get(score) or 0):.4f}", "text": str(h.get("text") or "")} for h in hits]
    if as_json:  # the whole section, like vsearch --json: a truncated hit is not a usable answer
        echo_json(rows)
        return
    print_rows([{**r, "text": clip(r["text"], TABLE_TEXT)} for r in rows])


@app.command()
def wiki(target: TargetOpt = "local", as_json: JsonOpt = False) -> None:
    """What the wiki table holds: rows, distinct files, and the newest last text change (a touch alone moves nothing)."""
    from . import wiki as wiki_mod

    counts = wiki_mod.stats(replica(target))
    if as_json:
        echo_json({"target": target, **counts})
        return
    print_rows([counts])


# ---- the process itself ---------------------------------------------------


def bind(http: str) -> str:
    """``--http`` through the server's own parser, so ``8094`` is a port here too and not a hostname."""
    from . import server

    host, port = server.split_http(http or admin_http())
    return f"{host}:{port}"


@app.command()
def serve(
    http: Annotated[str, typer.Option("--http", help="host:port to bind (default $STRUCTOR_LANCE_PY_HTTP or 127.0.0.1:8094)")] = "",
    data: Annotated[str, typer.Option("--data", help="Lance data root (default $STRUCTOR_LANCE_PY_DATA or app/lance_data_py)")] = "",
    only: Annotated[str, typer.Option("--targets", help="comma-separated subset of targets (default $STRUCTOR_LANCE_TARGETS)")] = "",
    interval: Annotated[float, typer.Option("--interval", help="seconds between polls (default $STRUCTOR_LANCE_INTERVAL)")] = 0.0,
    no_sync: Annotated[bool, typer.Option("--no-sync", help="admin only: read the store without writing to it")] = False,
) -> None:
    """Replicas plus the admin API and UI, in the foreground."""
    from . import server

    server.serve(http=bind(http), data=Path(data) if data else data_root(),
                 targets=columns(only) or env_targets(), interval=interval or env_interval(), no_sync=no_sync)


@app.command()
def once(
    http: Annotated[str, typer.Option("--http")] = "",
    data: Annotated[str, typer.Option("--data")] = "",
    only: Annotated[str, typer.Option("--targets")] = "",
) -> None:
    """One pull for every target, then exit (a running admin on that port does the pull)."""
    from . import server

    server.serve(http=bind(http), data=Path(data) if data else data_root(),
                 targets=columns(only) or env_targets(), once=True)




@app.command()
def ask(
    question: str,
    k: Annotated[int, typer.Option("--k", help="events retrieved for the context", callback=at_least_one)] = 10,
    mode: Annotated[str, typer.Option("--mode", help="hybrid (vector + FTS) | vector")] = "hybrid",
    where: Annotated[str, typer.Option("--where", help="predicate on event_vectors, e.g. \"role = 'user'\"")] = "",
    chat_model: Annotated[str, typer.Option("--model", help="Ollama chat model (default: chat_model in lance.json, else gemma3:27b)")] = "",
    no_stream: Annotated[bool, typer.Option("--no-stream", help="print the answer once it is complete")] = False,
    no_plan: Annotated[bool, typer.Option("--no-plan", help="search with the question as typed instead of model-written keyword queries")] = False,
    min_text: Annotated[int, typer.Option("--min-text", help="skip events shorter than this many characters (0 = search everything)", callback=non_negative)] = 80,
    no_wiki: Annotated[bool, typer.Option("--no-wiki", help="transcripts only: leave the wiki sections out of the context")] = False,
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Answer a question from the transcripts and the wiki: retrieve, ask the chat model on the GPU box, cite sources."""
    from .rag import Asker

    if mode not in ("vector", "hybrid"):
        fail("--mode must be vector or hybrid")
    asker = Asker(replica(target), embedder(target), model=chat_model or None)
    if not asker.url:
        fail('no chat host: put "chat_url" (or "ollama_urls") in ~/.config/structor/lance.json, or set STRUCTOR_CHAT_URL', NO_HOSTS_EXIT)
    stream = None if (as_json or no_stream) else (lambda tok: typer.echo(tok, nl=False))
    try:
        result = asker.ask(question, k=k, where=where, mode=mode, on_token=stream, plan=not no_plan,
                           min_text=min_text, wiki=not no_wiki)
    except Exception as e:  # noqa: BLE001 — a dead GPU box is a message, not a traceback
        fail(f"ask failed on {asker.url} ({asker.model}): {e}", 70)
    if as_json:
        echo_json(result)
        return
    if stream:
        typer.echo("" if result["answer"] else result.get("note", "no answer"))
    else:
        typer.echo(result["answer"] or result.get("note", ""))
    if result["sources"]:
        typer.echo("")
        plan_note = ", ".join(result.get("plan", {}).get("queries") or [])
        since = result.get("plan", {}).get("since")
        typer.echo(f"sources ({result['model']} on {result['chat_url']}; searched: {plan_note}{f'; since {since}' if since else ''}):")
        for s in result["sources"]:
            if s.get("kind") == "wiki":
                where_from = " · ".join(x for x in (s.get("title"), s.get("section"), s.get("path")) if x)
                typer.echo(f"  [{s['n']}] wiki             {fit(where_from, 34)}  {fit(s['text'], 70)}")
            else:
                typer.echo(f"  [{s['n']}] {str(s['ts'])[:16]} {s['role']:9} {s['session_id'][:8]} {s['project'].rsplit('/', 1)[-1]}  {fit(s['text'], 70)}")


if __name__ == "__main__":  # pragma: no cover
    app()
