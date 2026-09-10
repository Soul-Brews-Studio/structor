"""The wiki: a markdown directory as one more Lance table, searchable like the events.

A replica's ``wiki`` table is a derived table exactly as ``event_vectors`` is —
nothing in ``sync.TABLES`` knows about it, PocketBase never sees it, and a store
without one works as before. What it holds is the maintainers' own notes: every
``*.md`` under one directory, split into sections, embedded in the same space as
the events so a question can retrieve both::

    func = pool(["http://gpu1:11434"], model="bge-m3")
    index_dir(replica, "ψ/wiki/jsonl-indexer", func)   # {files, sections, embedded, unchanged, removed}
    search(replica, "tail state byte offset", mode="hybrid")

One row is one section: the ``# Title`` preamble is section ``""``, every
``## heading`` starts a new one, and a section over ``SECTION_CAP`` characters
is cut at paragraph boundaries into parts (``…#slug~1``, ``…#slug~2``) that each
repeat the heading, so a part still knows what it is about. ``[[wikilinks]]``
and fenced code go in verbatim — a ``## `` line inside a fence does not split
anything.

Re-indexing is cheap by design. Every row carries the sha256 of its own text, so
a second run compares hashes and embeds only the sections that actually changed;
rows whose file (or whose heading) is gone are deleted. That means the table
mirrors **one** directory per replica: pointing ``index_dir`` at a different one
empties it of the first. Because only a changed row is written, the ``updated``
column is the file's mtime at its last text change — a touch alone never moves
it, and every place that reports the field says so.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lancedb.embeddings import EmbeddingFunction
from lancedb.index import FTS
from lancedb.pydantic import LanceModel, Vector

from .sync import Replica, table_names
from .vectors import pool

TABLE = "wiki"
SECTION_CAP = 1800        # characters per row before a section is cut at a paragraph boundary
MAX_FILE_BYTES = 2 << 20  # a 2 MB markdown file is a data dump, not a page
UPSERT_BATCH = 64         # sections per merge_insert; each one is embedded on the way in
DELETE_CHUNK = 200        # ids per `id IN (…)` predicate

FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*(.*)$")  # the whole run, not its first three characters
H2 = re.compile(r"^[ \t]{0,3}##[ \t]+(.*?)[ \t]*#*[ \t]*$")
H1 = re.compile(r"^[ \t]{0,3}#[ \t]+(.*?)[ \t]*#*[ \t]*$")
PARAGRAPH = re.compile(r"\r?\n[ \t]*\r?\n")


# ---------------------------------------------------------------- the model


def wiki_page_model(func: EmbeddingFunction) -> type[LanceModel]:
    """``wiki`` bound to one embedding function, the way ``event_vector_model`` binds ``event_vectors``."""

    class WikiPage(LanceModel):
        id: str  # "<relpath>#<section-slug>"
        path: str = ""
        title: str = ""
        section: str = ""
        tags: str = ""
        order: float = 0.0
        updated: str = ""  # file mtime at the last text change (a touch never rewrites the row)
        text: str = func.SourceField()
        vector: Vector(func.ndims()) = func.VectorField()  # type: ignore[valid-type]
        text_hash: str = ""

    return WikiPage


def page_hash(text: str) -> str:
    """The whole section, not a prefix: two long parts can share their first two thousand characters."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- markdown


def unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """A leading ``---`` block as a dict, plus the body after it.

    Enough YAML for a page header and no more: ``key: value``, an inline list
    (``tags: [a, b]``) and a dash list under a bare key. Anything else is left
    as the string it was written as, which is what the callers here want.
    """
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    key = ""
    for line in m.group(1).split("\n"):
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("- ") and key:
            if isinstance(meta.get(key), list):
                meta[key].append(unquote(item[2:]))
            continue
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            continue
        key = name.strip().lower()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [unquote(p) for p in value[1:-1].split(",") if p.strip()]
        else:
            meta[key] = unquote(value) if value else []
    return meta, text[m.end() :]


def tag_string(value: Any) -> str:
    """Frontmatter tags as one comma string, however they were written."""
    items = value if isinstance(value, list) else str(value).replace("|", ",").split(",")
    return ",".join(t for t in (str(x).strip() for x in items) if t)


def slug(heading: str) -> str:
    """A heading as an id fragment: lowercase, punctuation dropped, spaces to dashes. Thai survives (``\\w`` is Unicode)."""
    s = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"[\s_]+", "-", s).strip("-") or "section"


def first_h1(body: str) -> str:
    for line in body.split("\n"):
        m = H1.match(line)
        if m:
            return m.group(1).strip()
    return ""


def fence_state(line: str, char: str, size: int) -> tuple[str, int]:
    """The fence state after ``line``: ``(character, run length)`` while inside a fence, ``("", 0)`` outside.

    CommonMark, because markdown wikis really do nest fences: a block opens on a
    run of three or more backticks or tildes and closes only on the **same**
    character with a run at least as long and nothing after it. So a three-tick
    fence inside a four-tick one is content, and the outer block stays open —
    the old "first three characters, toggle on equality" tracker closed it there
    and let the next ``## `` line split a section in half.
    """
    m = FENCE.match(line)
    if not m:
        return char, size
    run, rest = m.group(1), m.group(2).strip()
    if char:
        return ("", 0) if run[0] == char and len(run) >= size and not rest else (char, size)
    if run[0] == "`" and "`" in rest:
        return char, size  # a backtick info string is not an opener at all (```` ```a`b ````)
    return run[0], len(run)


def sections(body: str) -> list[tuple[str, str]]:
    """``[(heading, text)]``: the preamble first with heading ``""``, then one entry per ``## ``.

    The heading line stays in its own text (so the words are searchable) and a
    ``## `` inside a fenced code block is code, not a heading.
    """
    out: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    fence, size = "", 0
    for line in body.split("\n"):
        state = fence_state(line, fence, size)
        if state != (fence, size):
            fence, size = state  # this line opened or closed a fence: never a heading
        elif not fence:
            m = H2.match(line)
            if m:
                out.append((heading, "\n".join(buf)))
                heading, buf = m.group(1).strip(), [line]
                continue
        buf.append(line)
    out.append((heading, "\n".join(buf)))
    return [(h, t.strip()) for h, t in out if t.strip()]


def bodyless(piece: str) -> bool:
    """True when a piece is headings and blank lines only — a title with nothing under it."""
    return not any(line.strip() and not line.lstrip().startswith("#") for line in piece.split("\n"))


def with_bodies(pieces: list[str]) -> list[str]:
    """No part is a heading alone: a bodyless piece joins the next one, or the last one before it.

    A section whose first paragraph is longer than the cap used to flush its
    ``## Heading`` line as part 1 — one embedded row that says nothing and
    matches nothing. The heading now leads the first real chunk instead.
    """
    kept: list[str] = []
    pending = ""
    for piece in pieces:
        if not piece.strip():
            continue
        piece = f"{pending}\n\n{piece}" if pending else piece
        pending = ""
        if bodyless(piece):
            pending = piece
            continue
        kept.append(piece)
    if pending:
        if kept:
            kept[-1] = f"{kept[-1]}\n\n{pending}"
        else:
            kept.append(pending)  # the whole section is a heading: one row is still better than none
    return kept


def parts(text: str, cap: int = SECTION_CAP) -> list[str]:
    """One section as pieces of at most ``cap`` characters, cut between paragraphs where it can be."""
    if len(text) <= cap:
        return [text]
    out: list[str] = []
    current = ""
    for para in PARAGRAPH.split(text):
        joined = f"{current}\n\n{para}" if current else para
        if len(joined) <= cap:
            current = joined
            continue
        if current:
            out.append(current)
            current = ""
        while len(para) > cap:  # a single paragraph past the cap has no boundary to use
            out.append(para[:cap])
            para = para[cap:]
        current = para
    if current:
        out.append(current)
    return with_bodies(out)


# ---------------------------------------------------------------- walking a directory


def markdown_files(root: Path) -> list[Path]:
    """Every ``*.md`` under ``root``, hidden directories and oversized files left out. ``_research`` is included."""
    out: list[Path] = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return out


def page_rows(root: Path, path: Path) -> list[dict[str, Any]]:
    """One markdown file as its section rows, in document order."""
    rel = path.relative_to(root).as_posix()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return []
    meta, body = frontmatter(raw)
    title = str(meta.get("title") or "") or first_h1(body) or path.stem
    tags = tag_string(meta.get("tags") or "")
    description = str(meta.get("description") or "")
    # The file's mtime as this text was read. It only ever reaches the table with
    # a row that is actually written, so what the column holds is the mtime at the
    # last *text change* — touching a file moves no row. See stats().
    updated = datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    rows: list[dict[str, Any]] = []
    taken: set[str] = set()
    for index, (heading, text) in enumerate(sections(body)):
        if index == 0 and not heading and description:
            text = f"{description}\n\n{text}"  # a description that is only in the header is still searchable
        base = slug(heading) if heading else ""
        # The same heading twice in one file must not collide on one id — and the
        # de-duplicated id must not collide either: "Notes" + "Notes" + "Notes 2"
        # used to yield "notes-2" twice, so one section vanished into the other's
        # row and the pair never stopped re-embedding. "~" is the one character
        # slug() always strips, so "~dupN" (like the "~N" part suffix) can never
        # be a real heading's slug; every id is reserved as it is handed out.
        candidate, dup = base, 1
        while candidate in taken:
            dup += 1
            candidate = f"{base}~dup{dup}"
        taken.add(candidate)
        base = candidate
        chunks = parts(text)
        for part_no, chunk in enumerate(chunks, 1):
            piece = chunk if part_no == 1 or not heading else f"## {heading}\n\n{chunk}"
            rows.append({
                "id": f"{rel}#{base if len(chunks) == 1 else f'{base}~{part_no}'}",
                "path": rel,
                "title": title,
                "section": heading,
                "tags": tags,
                "order": float(index) + (part_no - 1) / 1000.0,
                "updated": updated,
                "text": piece,
                "text_hash": page_hash(piece),
            })
    return rows


# ---------------------------------------------------------------- the table


def table(replica: Replica, func: EmbeddingFunction | None = None, create: bool = False) -> Any | None:
    """The replica's ``wiki`` table, or ``None`` when it has none and ``create`` is false."""
    db = replica.db()
    if TABLE in table_names(db):
        return db.open_table(TABLE)
    if not create:
        return None
    return db.create_table(TABLE, schema=wiki_page_model(func or pool()), exist_ok=True)


def exists(replica: Replica) -> bool:
    return TABLE in table_names(replica.db())


def hashes(tbl: Any) -> dict[str, str]:
    """``id`` → ``text_hash`` for everything in the table: what a re-index compares against."""
    if tbl.count_rows() == 0:
        return {}
    return {r["id"]: r["text_hash"] for r in tbl.search().select(["id", "text_hash"]).limit(10_000_000).to_list()}


def delete_ids(tbl: Any, ids: list[str]) -> int:
    """``DELETE … WHERE id IN (…)``, chunked so one predicate never grows past what DataFusion likes."""
    done = 0
    for i in range(0, len(ids), DELETE_CHUNK):
        chunk = ids[i : i + DELETE_CHUNK]
        listed = ", ".join("'" + x.replace("'", "''") + "'" for x in chunk)
        tbl.delete(f"id IN ({listed})")
        done += len(chunk)
    return done


def ensure_fts(tbl: Any) -> None:
    if tbl.count_rows() and not any(i.name == "text_idx" for i in tbl.list_indices()):
        tbl.create_index("text", config=FTS(), replace=True)


def index_dir(
    replica: Replica,
    directory: str | Path,
    func: EmbeddingFunction | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Walk ``directory``, upsert the sections that changed, drop the ones that are gone.

    Returns ``{files, sections, embedded, unchanged, removed}``. ``embedded`` is
    the only expensive number: a section whose text hash still matches is never
    sent to the pool, so a second run over an unchanged wiki costs one scan of
    the id column and nothing on the GPU.
    """
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"{root} is not a directory")

    fresh: list[dict[str, Any]] = []
    files = markdown_files(root)
    for md in files:
        fresh.extend(page_rows(root, md))

    tbl = table(replica, func, create=True)
    known = hashes(tbl)
    changed = [r for r in fresh if known.get(r["id"]) != r["text_hash"]]

    for i in range(0, len(changed), UPSERT_BATCH):
        batch = changed[i : i + UPSERT_BATCH]
        # LanceDB embeds `text` through the function the table was created with
        tbl.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(batch)
        log(f"embedded {min(i + len(batch), len(changed))}/{len(changed)} changed section(s)")

    live = {r["id"] for r in fresh}
    removed = delete_ids(tbl, [i for i in known if i not in live])
    if removed:
        log(f"removed {removed} row(s) whose file or heading is gone")
    if (changed or removed) and tbl.count_rows():
        tbl.create_index("text", config=FTS(), replace=True)  # refresh, not ensure: the new rows have to be in it
    counts = {"files": len(files), "sections": len(fresh), "embedded": len(changed),
              "unchanged": len(fresh) - len(changed), "removed": removed}
    log(" ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def search(replica: Replica, query: str, limit: int = 10, mode: str = "hybrid",
           func: EmbeddingFunction | None = None) -> list[dict[str, Any]]:
    """Sections nearest to ``query``. ``mode`` = vector | hybrid (vector + FTS, RRF-fused) | fts (BM25 alone).

    The vector column never comes back: it is 1024 floats a caller has no use for.
    """
    tbl = table(replica, func)
    if tbl is None or tbl.count_rows() == 0:
        return []
    if mode in ("hybrid", "fts"):
        ensure_fts(tbl)
    if mode == "fts":
        q = tbl.search(query, query_type="fts")
    elif mode == "hybrid":
        q = tbl.search(query, query_type="hybrid")
    else:
        q = tbl.search(query)
    return [{k: v for k, v in row.items() if k != "vector"} for row in q.limit(limit).to_list()]


def stats(replica: Replica) -> dict[str, Any]:
    """Rows, distinct files, and the newest ``updated`` — the last text change, not the last touch.

    A row is written only when its text hash moves, so ``updated`` is the file's
    mtime as of that write: ``touch page.md`` (or a re-save with no edit) leaves
    the stamp where it was, on purpose. Re-stamping every unchanged row would
    re-embed it, which is the one thing this table is built to avoid.
    """
    tbl = table(replica)
    if tbl is None:
        return {"table": TABLE, "rows": 0, "paths": 0, "updated": ""}
    rows = tbl.search().select(["path", "updated"]).limit(10_000_000).to_list()
    return {
        "table": TABLE,
        "rows": len(rows),
        "paths": len({r["path"] for r in rows}),
        "updated": max((r["updated"] for r in rows), default=""),
    }
