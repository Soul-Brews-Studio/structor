"""structor-dream CLI — dream pages over a structor-lance replica.

    structor-dream week    [2026-W37] [--max-sessions 40] [--target local] [--force] [--no-index] [--json]
    structor-dream topic   "<theme>"  [--k 48] [--out PATH] [--target local] [--no-index] [--json]
    structor-dream nightly [--target local] [--max-sessions 40]

The replica, the embedding pool and the chat host are the ones ``structor-lance``
uses (``~/.config/structor/*.json``, ``lance.json``: ``ollama_urls``, ``chat_url``,
``chat_model``, ``wiki_dir``). The dream directory is ``STRUCTOR_DREAM_DIR``, else
``dream_dir`` in ``lance.json``, else ``<wiki_dir>/dreams``; with none of those the
command exits 78 (EX_CONFIG), as ``structor-lance`` does for a missing pool.

Pages land in the dream directory; when it sits under the wiki directory (the
default) the wiki table is re-indexed afterwards so ``structor-lance ask`` can
cite the new page at once. Paths of private directories never appear in this
package; they are configuration.

One run per replica at a time: every command takes the replica's run lock
first and exits 75 (EX_TEMPFAIL) when another ``structor-dream`` holds it —
the digest cache is written whole, so two runs would erase each other's work.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from structor_lance import cli as lance_cli
from structor_lance import wiki as wiki_table
from structor_lance.rag import Asker
from structor_lance.sync import Replica, stamp_to_dt
from structor_lance.vectors import Embedder

from . import config, material, page, reduce
from .digest import MAX_ATTEMPTS, NoJson, digest_week
from .state import LOCK_FILE, DigestState, RunLock

NO_CONF_EXIT = 78  # EX_CONFIG: nothing to retry until someone edits lance.json
MODEL_EXIT = 70  # EX_SOFTWARE: the chat host answered, but not with anything usable
LOCK_EXIT = 75  # EX_TEMPFAIL: another run holds this replica's lock; the same command later will work
DEFAULT_TOPIC_K = 48
SEARCH_WHERE = f"length(text) >= {material.MIN_TEXT} AND role IN ('user', 'assistant')"

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__.split("\n\n")[0])

TargetOpt = Annotated[str, typer.Option("--target", "-t", help="target name (see 'structor-lance targets')")]
JsonOpt = Annotated[bool, typer.Option("--json", help="print one JSON object instead of progress lines")]
NoIndexOpt = Annotated[bool, typer.Option("--no-index", help="write the page but do not re-index the wiki")]
ModelOpt = Annotated[str, typer.Option("--model", help="Ollama chat model (default: chat_model in lance.json)")]
MaxOpt = Annotated[int, typer.Option("--max-sessions", help="sessions digested per week, spread across projects")]


# ---- plumbing (module functions so tests can replace them) ----------------


def fail(message: str, code: int = 64) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def open_replica(target: str) -> Replica:
    return lance_cli.replica(target)


def open_embedder(target: str) -> Embedder:
    """The pool-backed embedder, or exit 78 when no Ollama host is configured."""
    return lance_cli.embedder(target)


def asker_for(replica: Replica, embedder: Embedder | None, model: str) -> Asker:
    asker = Asker(replica, embedder, model=model or None)
    if not asker.url:
        fail('no chat host: put "chat_url" (or "ollama_urls") in ~/.config/structor/lance.json, or set STRUCTOR_CHAT_URL',
             NO_CONF_EXIT)
    return asker


HOST_TRIES = 10     # a nightly run waits up to ~10 minutes for the chat host: launchd fires at 03:30, and on the
HOST_DELAY_S = 60   # first night (2026-09-11) the mesh's DNS was not back yet — "nodename nor servname provided"


def probe_host(asker: Asker) -> str:
    """``""`` when the chat host answers, else the error in one line."""
    try:
        import ollama

        ollama.Client(host=asker.url, timeout=10).list()
        return ""
    except Exception as e:  # noqa: BLE001 — any failure to reach the host reads the same to the caller
        return f"{type(e).__name__}: {str(e)[:120]}"


def wait_for_host(asker: Asker, log: Any, tries: int | None = None, delay: float | None = None) -> str:
    """Probe the chat host up to ``tries`` times, ``delay`` seconds apart; ``""`` once it answers, else the last error.

    An unattended run must not spend a night marking every session failed
    because the mesh was still waking up; a hand-started ``week`` fails fast
    instead, its user is there to read the error. Defaults are read at call
    time (``HOST_TRIES`` / ``HOST_DELAY_S``), so a test can shorten them.
    """
    tries = HOST_TRIES if tries is None else tries
    delay = HOST_DELAY_S if delay is None else delay
    error = ""
    for i in range(tries):
        error = probe_host(asker)
        if not error:
            return ""
        log(f"chat host {asker.url} not answering ({error}); try {i + 1}/{tries}" + (f", waiting {delay:g}s" if i + 1 < tries else ""))
        if i + 1 < tries:
            time.sleep(delay)
    return error


def run_lock(replica: Replica) -> RunLock:
    """The replica's run lock, acquired or not (``held``); see ``state.RunLock`` for why there is one."""
    lock = RunLock(config.cache_dir(replica) / LOCK_FILE)
    lock.acquire()
    return lock


def locked_message(lock: RunLock) -> str:
    return (f"another structor-dream run holds {lock.path}" + (f" (pid {lock.holder})" if lock.holder else "")
            + "; wait for it to finish, then run this again")


def hold_lock(replica: Replica) -> RunLock:
    """Take the replica's run lock or exit 75."""
    lock = run_lock(replica)
    if not lock.held:
        fail(locked_message(lock), LOCK_EXIT)
    return lock


def dream_root() -> Path:
    """The configured dream directory, created on first use; exit 78 when nothing points anywhere."""
    configured = config.dream_dir()
    if not configured:
        fail('no dream directory: put "wiki_dir" (dreams go to <wiki_dir>/dreams) or "dream_dir" in '
             "~/.config/structor/lance.json, or set STRUCTOR_DREAM_DIR", NO_CONF_EXIT)
    root = Path(configured).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def stamp_now() -> str:
    return config.now().isoformat(timespec="seconds")


def reindex(replica: Replica, target: str, root: Path, log) -> dict[str, Any] | None:
    """Re-index the wiki when the dream directory is inside it; otherwise say how, and return ``None``.

    The wiki table mirrors exactly one directory, so ``wiki-index <dream_dir>``
    on its own would empty it of every other page — the advice is to put the
    dream directory under the wiki, not to index it alone.
    """
    wiki_root = wiki_table.wiki_dir()
    if not wiki_root or not config.under(root, Path(wiki_root)):
        log(f"not indexed: {root} is outside the wiki directory. The wiki table mirrors one directory per "
            "replica, so move (or symlink) the dream directory under wiki_dir and run 'structor-lance wiki-index'.")
        return None
    counts = wiki_table.index_dir(replica, Path(wiki_root).expanduser(), func=open_embedder(target).func, log=log)
    return counts


def index_after(replica: Replica, target: str, root: Path, log, wanted: bool) -> tuple[dict[str, Any] | None, str]:
    """``reindex`` that never takes a written page down with it: ``(counts, error)``.

    A page on disk is the run's product; a pool that is not configured (exit
    78 from ``open_embedder``) or a wiki directory that does not exist yet is
    reported beside it, as text, and the command still prints its summary.
    """
    if not wanted:
        return None, ""
    try:
        return reindex(replica, target, root, log), ""
    except typer.Exit as e:  # open_embedder's own message is already on stderr
        return None, f"not indexed: the embedding pool is not configured (exit {e.exit_code}); the page is written"
    except Exception as e:  # noqa: BLE001 — a missing wiki_dir or a dead pool is a message, not a traceback
        return None, f"not indexed: {type(e).__name__}: {str(e)[:200]}; the page is written"


# ---- the week engine ------------------------------------------------------


def nothing_reason(talk: list[dict[str, Any]], mapped: dict[str, Any]) -> str:
    """One sentence for a week that had ledger rows and yielded no digest — the empty page's reason."""
    if not talk:
        return (f"no session in the week was conversational (at least {material.MIN_USER_TURNS} human turns "
                f"and {material.MIN_EVENTS} events).")
    empty, gave_up = mapped["empty"], mapped["gave_up"]
    if empty and not gave_up:
        return (f"the {len(empty)} chosen session(s) had no user or assistant turn of at least "
                f"{material.MIN_TEXT} characters to read.")
    if gave_up and not empty:
        return (f"the {len(gave_up)} chosen session(s) were given up on after {MAX_ATTEMPTS} replies "
                f"without JSON each.")
    return (f"the chosen sessions had nothing to read ({len(empty)}) or were given up on after {MAX_ATTEMPTS} "
            f"replies without JSON ({len(gave_up)}).")


def run_week(replica: Replica, asker: Asker, week: str, root: Path, max_sessions: int, force: bool,
             log, reduce_budget: int = reduce.REDUCE_BUDGET) -> dict[str, Any]:
    """Digest, reduce and write one week's page. Returns the numbers the CLI prints.

    A week with ledger rows that yields no digest for a reason that will not
    change by itself (nothing conversational, nothing to read, every session
    given up on) gets an *empty page* carrying ``ledger_at``, so the nightly
    job does not dream it again until its ledger moves. A week where a session
    failed for a reason that may pass (the host was down) gets no page and is
    tried again.
    """
    t0 = time.time()
    rows = material.week_rows(replica, week)
    talk = material.conversational(rows)
    chosen_rows = material.stratify(talk, max_sessions)
    names = asker.names()
    state = DigestState(config.state_path(replica))
    log(f"{week}: {len(rows)} sessions, {len(talk)} conversational, {len({r['project'] for r in rows})} projects; "
        f"{len(chosen_rows)} chosen (cap {max_sessions})")

    mapped = digest_week(asker, replica, week, chosen_rows, state, names, force=force, log=log)
    by_session = {str(r["session"]): r for r in chosen_rows}
    chosen = [(s, {**mapped["digests"][s], "first_ts": by_session[s].get("first_ts"), "last_ts": by_session[s].get("last_ts")})
              for s in (str(r["session"]) for r in chosen_rows) if s in mapped["digests"]]

    previous_label = config.previous_week(week)
    previous_path = root / f"{previous_label}.md"
    previous = reduce.previous_insights(previous_path.read_text(encoding="utf-8")) if previous_path.is_file() else ""

    out: dict[str, Any] = {
        "week": week, "sessions_in_week": len(rows), "conversational": len(talk), "events_in_week": int(sum(float(r.get("event_count") or 0) for r in rows)),
        "projects": len({str(r.get("project") or "") for r in rows}), "chosen": len(chosen_rows),
        "digested": mapped["digested"], "skipped": mapped["skipped"], "failed": mapped["failed"], "empty": mapped["empty"],
        "gave_up": mapped["gave_up"], "ledger_at": material.ledger_stamp(rows),
        "model": asker.model, "page": str(root / f"{week}.md"), "written": False, "reduced": 0,
    }
    if not chosen:
        if rows and not mapped["failed"]:
            reason = nothing_reason(talk, mapped)
            made = {**out, "generated_at": stamp_now()}
            out["written"] = page.write_if_changed(root / f"{week}.md", page.empty_week_page(week, made, reason))
            out["reason"] = reason
            log(f"{week}: nothing to reduce — {reason} Empty page {'written' if out['written'] else 'unchanged'} → {out['page']}")
        else:
            log(f"{week}: nothing to reduce (no digest" + (f"; {len(mapped['failed'])} failed, will retry" if mapped["failed"] else "") + ")")
        out["elapsed_s"] = round(time.time() - t0, 1)
        return out

    log(f"reducing {len(chosen)} digest(s)" + (f" with the Insights of {previous_label}" if previous else ""))
    reduced = reduce.reduce_week(asker, week, chosen, names, previous=previous, previous_label=previous_label, budget=reduce_budget)
    made = {**out, "generated_at": stamp_now(), "max_sessions": max_sessions, "reduce_budget": reduce_budget,
            "previous": previous_label if previous else "", "image": page.existing_image(root / f"{week}.md")}
    text = page.week_page(week, made, reduced, dict(chosen), by_session, names)
    out["written"] = page.write_if_changed(root / f"{week}.md", text)
    out["reduced"] = len(reduced["sessions_reduced"])
    out["cited_sessions"] = len(page.sources_of(reduced, reduce.WEEK_SECTIONS))
    out["elapsed_s"] = round(time.time() - t0, 1)
    log(f"{week}: page {'written' if out['written'] else 'unchanged'} → {out['page']} "
        f"(digested {out['digested']}, skipped {out['skipped']}, reduced {out['reduced']}, {out['elapsed_s']}s)")
    return out


# ---- commands -------------------------------------------------------------


@app.command()
def week(
    label: Annotated[str, typer.Argument(help="ISO week, e.g. 2026-W37 (default: this week, Asia/Bangkok)")] = "",
    max_sessions: MaxOpt = material.DEFAULT_MAX_SESSIONS,
    force: Annotated[bool, typer.Option("--force", help="re-digest every chosen session, ignoring the cache")] = False,
    no_index: NoIndexOpt = False,
    model: ModelOpt = "",
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Dream one week: digest its sessions (cached by event count), reduce, write <dream_dir>/<week>.md, re-index."""
    label = label or config.current_week()
    try:
        config.parse_week(label)
    except ValueError as e:
        fail(str(e))
    if max_sessions < 1:
        fail("--max-sessions must be 1 or more")
    log = (lambda line: typer.echo(line, err=True)) if as_json else (lambda line: typer.echo(line))
    root = dream_root()
    replica = open_replica(target)
    lock = hold_lock(replica)
    try:
        asker = asker_for(replica, None, model)
        try:
            result = run_week(replica, asker, label, root, max_sessions, force, log)
        except NoJson as e:
            fail(f"the reduce call never came back as JSON ({asker.model} on {asker.url}): {e}", MODEL_EXIT)
        except Exception as e:  # noqa: BLE001 — a dead GPU box is a message, not a traceback
            fail(f"week failed on {asker.url} ({asker.model}): {e}", MODEL_EXIT)
        result["indexed"], error = index_after(replica, target, root, log, wanted=result["written"] and not no_index)
        if error:
            result["index_error"] = error
            log(error)
        if as_json:
            typer.echo(json.dumps(result, default=str))
    finally:
        lock.release()


@app.command()
def topic(
    query: Annotated[str, typer.Argument(help="the theme, as a search query")],
    k: Annotated[int, typer.Option("--k", help="hits kept after the horizon split (k/4 per horizon)")] = DEFAULT_TOPIC_K,
    out: Annotated[str, typer.Option("--out", help="page path (default <dream_dir>/topic-<slug>.md)")] = "",
    no_index: NoIndexOpt = False,
    model: ModelOpt = "",
    target: TargetOpt = "local",
    as_json: JsonOpt = False,
) -> None:
    """Dream one theme: hybrid retrieval stratified by time horizon and project, one model call, a cited page."""
    if not query.strip():
        fail("the theme is empty")
    if k < 4:
        fail("--k must be 4 or more (one per horizon)")
    log = (lambda line: typer.echo(line, err=True)) if as_json else (lambda line: typer.echo(line))
    t0 = time.time()
    root = dream_root()
    replica = open_replica(target)
    lock = hold_lock(replica)
    try:
        embedder = open_embedder(target)
        asker = asker_for(replica, embedder, model)
        try:
            hits = embedder.search(query, limit=k * 2, where=SEARCH_WHERE, mode="hybrid")
            items = material.topic_material(hits, k, config.now(), asker.names())
            log(f"{len(hits)} hits → {len(items)} after the floor and the horizon split")
            if not items:
                fail("no matching events", MODEL_EXIT)
            reduced = reduce.dream_topic(asker, query, items)
        except typer.Exit:
            raise
        except NoJson as e:
            fail(f"the model never came back as JSON ({asker.model} on {asker.url}): {e}", MODEL_EXIT)
        except Exception as e:  # noqa: BLE001
            fail(f"topic failed on {asker.url} ({asker.model}): {e}", MODEL_EXIT)
        slug = page.topic_slug(query)
        path = Path(out).expanduser() if out else root / f"topic-{slug}.md"
        made = {"model": asker.model, "generated_at": stamp_now(), "retrieved": len(hits), "share": max(1, k // 4),
                "dumps": sum(1 for h in hits if material.looks_like_tool_output(str(h.get("text") or ""))),
                "image": page.existing_image(path)}
        written = page.write_if_changed(path, page.topic_page(query, made, reduced, items))
        result = {"query": query, "slug": slug, "hits": len(hits), "material": len(items), "read": len(reduced["used"]),
                  "cited_events": len(page.sources_of(reduced, reduce.TOPIC_SECTIONS)), "page": str(path),
                  "written": written, "model": asker.model, "elapsed_s": round(time.time() - t0, 1)}
        log(f"page {'written' if written else 'unchanged'} → {path} ({result['elapsed_s']}s)")
        result["indexed"], error = index_after(replica, target, path.parent, log, wanted=written and not no_index)
        if error:
            result["index_error"] = error
            log(error)
        if as_json:
            typer.echo(json.dumps(result, default=str))
    finally:
        lock.release()


def stale_weeks(replica: Replica, root: Path, at: datetime) -> list[str]:
    """The current week, plus every week whose ledger moved since its page was made (or that has no page).

    A page's ``ledger_at`` is the ledger stamp it was made from, so the test
    is stamp against stamp and a page whose text did not change still counts
    as made from the newer ledger. A page from before ``ledger_at`` existed
    is judged by its ``generated_at`` instead.
    """
    current = config.current_week(at)
    weeks = material.weeks_present(replica)
    due = {current} if current in weeks or not weeks else set()
    for label, moved in weeks.items():
        meta = page.read_frontmatter(root / f"{label}.md")
        ledger = str(meta.get("ledger_at") or "")
        if ledger:
            if moved > ledger:
                due.add(label)
            continue
        generated = str(meta.get("generated_at") or "")
        if not generated:
            due.add(label)
            continue
        try:
            written = datetime.fromisoformat(generated)
        except ValueError:
            due.add(label)
            continue
        last = stamp_to_dt(moved)
        if last is not None and last > written:
            due.add(label)
    return sorted(due)


@app.command()
def nightly(
    max_sessions: MaxOpt = material.DEFAULT_MAX_SESSIONS,
    model: ModelOpt = "",
    target: TargetOpt = "local",
    as_json: JsonOpt = True,
) -> None:
    """The launchd job: dream the current week and every week that changed since its page, then re-index once.

    Always ends with one JSON line ({"weeks", "digested", "skipped", "pages", …}); exits 0 unless nothing is
    configured, so a night with a dead chat host is a logged line, not a launchd retry storm — or 75 when
    another run (a hand-started one, say) still holds the replica's lock, which is also just a line.
    """
    def log(line: str) -> None:  # progress on stderr, the summary on stdout
        typer.echo(line, err=True)

    t0 = time.time()
    root = dream_root()
    replica = open_replica(target)
    summary: dict[str, Any] = {"weeks": [], "digested": 0, "skipped": 0, "pages": [], "failed": [], "gave_up": [],
                               "errors": []}
    lock = run_lock(replica)
    if not lock.held:
        summary["errors"].append(f"locked: {locked_message(lock)}")
        summary["elapsed_s"] = round(time.time() - t0, 1)
        typer.echo(json.dumps(summary, default=str))
        raise typer.Exit(LOCK_EXIT)
    try:
        asker = asker_for(replica, None, model)
        unreachable = wait_for_host(asker, log)
        if unreachable:
            summary["errors"].append(f"chat host unreachable after {HOST_TRIES} tries: {unreachable}")
            summary["indexed"] = None
            summary["elapsed_s"] = round(time.time() - t0, 1)
            typer.echo(json.dumps(summary, default=str))
            return  # nothing was tried, so every week stays due for the next night
        weeks = stale_weeks(replica, root, config.now())
        summary["weeks"] = weeks
        written_any = False
        for label in weeks:
            try:
                result = run_week(replica, asker, label, root, max_sessions, False, log)
            except Exception as e:  # noqa: BLE001 — the other weeks still get their turn
                summary["errors"].append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
                log(f"{label}: {type(e).__name__}: {e}")
                continue
            summary["digested"] += result["digested"]
            summary["skipped"] += result["skipped"]
            summary["failed"].extend(result["failed"])
            summary["gave_up"].extend(result["gave_up"])
            if result["written"]:
                summary["pages"].append(result["page"])
                written_any = True
        summary["indexed"], error = index_after(replica, target, root, log, wanted=written_any)
        if error:
            summary["errors"].append(f"index: {error}")
            log(error)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        typer.echo(json.dumps(summary, default=str))
        _ = as_json  # the summary is always JSON; the flag exists so the three commands read alike
    finally:
        lock.release()


# ---- draw: the page's image prompt → one illustration beside the page -------

DRAW_TIMEOUT_S = 900       # an image-generation turn in Codex took ~90 s in the probe (2026-09-11); nine minutes is the ceiling
IMAGE_MAX_BYTES = 6_000_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def draw_with_codex(prompt: str, workspace: Path, timeout: int, log) -> Path | None:
    """One illustration from the Codex CLI's image-generation tool, saved in ``workspace``; ``None`` when it made none.

    ``codex exec`` runs non-interactively in a workspace-write sandbox rooted
    at a scratch directory, so the only thing it can touch is that directory.
    Measured 2026-09-11: ``codex-cli 0.154`` answered IMAGE_TOOL_USED and wrote
    a 1200×630 PNG in about a minute and a half.
    """
    import shutil
    import subprocess

    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex not found on PATH (install the Codex CLI and run 'codex login')")
    out = workspace / "image.png"
    instruction = (
        f"Create one illustration with your image-generation tool and save it as {out} "
        "(PNG, landscape, about 1200x630). The scene: " + prompt.strip() + " "
        "No text, letters, logos or watermarks in the image. Write no other file. "
        "Reply with one line: the path you wrote, or NO_IMAGE_TOOL if you cannot generate images."
    )
    cmd = [codex, "exec", "-s", "workspace-write", "--skip-git-repo-check", "-C", str(workspace),
           "-o", str(workspace / "last.md"), instruction]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"codex did not finish in {timeout}s") from e
    tail = (proc.stdout or "").strip().split("\n")[-1:]
    log(f"codex exit {proc.returncode}" + (f": {tail[0][:160]}" if tail else ""))
    return out if out.is_file() else None


ENGINES = {"codex": draw_with_codex}


def png_problem(path: Path) -> str:
    """``""`` when the file is a PNG of a sane size, else what is wrong with it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(8)
    except OSError as e:
        return f"unreadable: {e}"
    if head != PNG_MAGIC:
        return "not a PNG"
    if size > IMAGE_MAX_BYTES:
        return f"{size} bytes is over the {IMAGE_MAX_BYTES}-byte cap"
    return ""


@app.command()
def draw(
    which: Annotated[str, typer.Argument(help="a week (2026-W37), a topic slug (topic-409-offset-mismatch) or a page path")],
    engine: Annotated[str, typer.Option("--engine", help="image engine: " + ", ".join(ENGINES))] = "codex",
    timeout: Annotated[int, typer.Option("--timeout", help="seconds to give the engine")] = DRAW_TIMEOUT_S,
    force: Annotated[bool, typer.Option("--force", help="draw again even when the page already has an image")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Draw the illustration a dream page asked for (its image_prompt) and put it beside the page.

    Costs whatever the engine costs (Codex is a paid account), so it is never
    part of nightly; run it by hand on the pages worth a picture. The page
    gets an ``image:`` frontmatter line and the image under its title; a
    later re-dream keeps the picture.
    """
    import shutil
    import tempfile

    def log(line: str) -> None:
        if not as_json:
            typer.echo(line, err=True)

    t0 = time.time()
    if engine not in ENGINES:
        fail(f"unknown engine {engine!r} (have: {', '.join(ENGINES)})", 64)
    root = dream_root()
    stem = Path(which).stem if which.endswith(".md") else which.strip()
    path = Path(which).expanduser() if which.endswith(".md") else root / f"{stem}.md"
    if not path.is_file():
        fail(f"no dream page at {path}", 64)
    meta = page.read_frontmatter(path)
    prompt = str(meta.get("image_prompt") or "").strip()
    if not prompt:
        fail(f"{path.name} carries no image_prompt — re-run 'week' or 'topic' for it (pages made before "
             "2026-09-11 have none), or the model gave none", 64)
    image = path.with_suffix(".png")
    if image.is_file() and not force:
        changed = page.attach_image(path, image.name, f"Illustration of {stem}, drawn from the image prompt below")
        result = {"page": str(path), "image": str(image), "bytes": image.stat().st_size, "engine": engine,
                  "drawn": False, "page_changed": changed, "elapsed_s": round(time.time() - t0, 1)}
        typer.echo(json.dumps(result) if as_json else f"{image.name} already exists — --force to draw again")
        return
    log(f"{stem}: drawing with {engine} — {prompt[:120]}{'…' if len(prompt) > 120 else ''}")
    workspace = Path(tempfile.mkdtemp(prefix="structor-dream-draw-"))
    try:
        try:
            produced = ENGINES[engine](prompt, workspace, timeout, log)
        except RuntimeError as e:
            fail(f"{engine}: {e}", MODEL_EXIT)
        if produced is None:
            note = (workspace / "last.md").read_text(encoding="utf-8", errors="replace")[:200] if (workspace / "last.md").is_file() else ""
            fail(f"{engine} produced no image" + (f" — its last words: {note.strip()}" if note else ""), MODEL_EXIT)
        problem = png_problem(produced)
        if problem:
            fail(f"{engine} wrote {produced.name} but it is {problem}", MODEL_EXIT)
        shutil.copyfile(produced, image)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    changed = page.attach_image(path, image.name, f"Illustration of {stem}, drawn from the image prompt below")
    result = {"page": str(path), "image": str(image), "bytes": image.stat().st_size, "engine": engine,
              "drawn": True, "page_changed": changed, "elapsed_s": round(time.time() - t0, 1)}
    if as_json:
        typer.echo(json.dumps(result))
    else:
        typer.echo(f"{stem}: drew {image.name} ({result['bytes']} bytes) in {result['elapsed_s']}s → {path}")


if __name__ == "__main__":  # pragma: no cover
    app()
