# structor-lance (Python edition)

A LanceDB replica of a Structor PocketBase store, its admin JSON API, the
admin UI, and the old console served over the replica — the same program as
`../lance` (Bun), written in Python with an ORM-style schema.

PocketBase stays the source of truth. This side reads `projects`, `sessions`,
`events`, `session_weeks` and `import_runs` through the records API in
`(stamp, id)` order and upserts by `id`; byte offsets, the week ledger and
import runs are never written back. Every target in `~/.config/structor/*.json`
gets its own directory under `lance_data_py/<target>/`, with the cursor in that
directory's `sync.json`.

It listens on **8094** with its own data directory, so it can run beside the
Bun edition (8092, `lance_data/`) without either touching the other's tables.
(It listened on 8093 until 2026-09-10, when an unrelated lab turned out to hold
that port on this Mac.)

## Layout

| file | what it owns |
|---|---|
| `schema.py` | the five tables as `LanceModel` classes — Arrow schema, insert row and result row in one class |
| `targets.py` | which stores to replicate; `Target.__repr__` hides the password |
| `pb.py` | the PocketBase client: login, keyset paging, the `structor/live` SSE stream |
| `sync.py` | `Replica`: the Lance directory, `sync.json`, the pull loop, `lag()` |
| `admin.py` | the JSON API over every replica plus the static UI |
| `facade.py` | the console API: `app/ui`'s relative `api/…` calls answered from Lance |
| `server.py` | the process: replicas + admin, `--once`, `--no-sync` |
| `cli.py` | this package's command line (`structor-lance`) |
| `vectors.py` | the optional vector side: an Ollama pool, `event_vectors`, embed and vector search |

The schema is the part that differs from the Bun edition. A table is a Pydantic
model that is also its own Arrow schema:

```python
class Event(Table):
    __table__ = "events"
    __stamp__ = "created"     # the column a pull pages by
    __fts__ = "text"          # the column with a full-text index

    session: str = ""
    role: str = ""
    text: str = ""
    sidechain: bool = False
```

`Replica.table(Event)` creates or opens that table, `merge_insert("id")` upserts
model instances, and `.to_pydantic(Event)` reads rows back as `Event` objects.
Column names and types match the Bun edition exactly, so a store written by one
opens in the other.

## Run

```sh
uv sync                      # or: make lance-py-install, just install
uv run structor-lance serve  # replica + admin on 127.0.0.1:8094
uv run structor-lance once   # one pull for every target, then exit
uv run structor-lance serve --no-sync --http 127.0.0.1:8098   # read-only copy of the same store
```

`just --list` has a recipe for each of these plus every CLI command;
`make lance-py`, `make lance-py-once` and `make lance-py-test` do the same from
`app/`. On this Mac launchd runs `serve` at login as
`studio.soulbrews.structor.lance-py` (`scripts/agent.sh lance-py`, log in
`~/Library/Logs/Structor/lance-py.log`).

## CLI

```sh
uv run structor-lance targets
uv run structor-lance status  [--target local]
uv run structor-lance tables  [--target local]
uv run structor-lance schema  <table>
uv run structor-lance rows    <table> [--where "role = 'user'"] [--limit 20] [--offset 0] [--select id,ts,text] [--json]
uv run structor-lance search  <query> [--table events] [--where …] [--limit 20] [--json]
uv run structor-lance lag                # rows on the PocketBase side vs rows here
uv run structor-lance sync               # pull now
uv run structor-lance optimize <table>   # compact fragments, index new rows
uv run structor-lance fts [--table events]
uv run structor-lance embed   [--limit N] [--batch 128] [--where "iso_week = '2026-W37'"]
uv run structor-lance vsearch <query> [--limit 20] [--where …] [--mode vector|hybrid] [--json]
uv run structor-lance vectors            # rows embedded, rows still pending
uv run structor-lance ask "<question>" [--k 10] [--mode hybrid|vector] [--where …] [--model gemma3:27b] [--no-plan] [--json]
```

Reads open the Lance directory directly, which is safe while the replica runs.
`sync`, `optimize` and `fts` are writes: they go through the admin API when
something answers on `$STRUCTOR_LANCE_PY_HTTP`, so only one process ever writes
a table, and fall back to direct access when nothing does. Output is a plain
text table; `--json` prints JSON. Predicates are DataFusion SQL with
single-quoted strings and no `ORDER BY` — Lance scans in storage order.
A `--limit` is a count, so it has to be 1 or more; `--offset` may be 0.

Environment: `STRUCTOR_LANCE_PY_HTTP` (default `127.0.0.1:8094`),
`STRUCTOR_LANCE_PY_DATA` (default `../lance_data_py`), plus
`STRUCTOR_LANCE_TARGETS` and `STRUCTOR_LANCE_INTERVAL`, which `serve` and
`once` read as their defaults (that is how the launchd agent, which passes no
flags, is pointed at a subset of targets). Passwords are read from
`~/.config/structor/*.json` and never printed: only a target's name and url
leave the process.

`--http` takes `host:port`, `:port` or a bare `port`; a port that is not a
number is a usage error (exit 64) rather than a hostname that quietly binds the
default port.

## Vectors (optional)

Full-text search is the default and needs nothing: BM25 over `events.text`,
built by `fts`, queried by `search`. Vector search is an addition, and it only
turns on when an Ollama pool is configured:

```json
// ~/.config/structor/lance.json
{ "ollama_urls": ["http://gpu1:11434", "http://gpu2:11434"], "embedding_model": "bge-m3" }
```

or `STRUCTOR_OLLAMA_URLS=http://gpu1:11434,http://gpu2:11434` in the
environment. With neither set, `embed` and `vsearch` exit 78 (EX_CONFIG) saying
so, and every other command is unaffected.

`embed` fills a sibling table, `event_vectors`, from the conversational events
(`role <> '' AND text <> ''`), newest first, and is resumable: rerun it and it
picks up the rows that have no vector yet. `vectors` prints how many are done
and how many are left. `vsearch` returns the nearest rows — `--mode hybrid`
fuses the vector hits with an FTS pass over the same table (RRF), which is
better when the query contains an exact identifier. The admin serves the same
thing at `GET /api/<target>/tables/events/vsearch?q&limit&where&mode`, and the
table list carries a `vectors` count for `events`. The first hybrid query builds
an FTS index on `event_vectors`, which is a write, so a `--no-sync` instance
answers 405 for it until some other process has built one.

`ask` is the generation half. First the chat model turns the question into
one to three keyword queries and, when the question points at a time ("last
night"), a `since` date; the question itself is always one of the queries.
Each query runs as a hybrid search over `event_vectors` (rows shorter than 80
characters skipped, rows before `since` skipped) and the hits are fused by
reciprocal rank. The top `--k` go into a numbered context block (≤ 7,000
characters, newest wins when events disagree) and the answer streams from the
chat model with `[n]` citations, then the sources and the queries that were
searched are printed. `--no-plan` searches with the question as typed;
`--min-text 0` includes the one-line events the default filter (80 characters)
skips — about 15% of the vector store, mostly prompts like "install on
kvmlab1". Event text goes into the prompt inside `<event>` fences and the
system prompt names it quoted data, so instruction-shaped transcript lines are
reported, not obeyed. On the admin the same call is `POST /api/<target>/ask`
(read-only instances included; at most 4 asks run at once, a wedged chat host
answers 504 after 240 s, bodies over 64 KB are refused). The chat host is `chat_url` in
`lance.json` (or `STRUCTOR_CHAT_URL`), defaulting to the last pool host; the
model is `chat_model` / `STRUCTOR_CHAT_MODEL`, default `gemma3:27b`
(`qwen3:*` runs with thinking off). Measured here: ~30 s per answer on a
27B model, everything on the mesh. The admin serves the same as
`POST /api/<target>/ask {question, k?, mode?, where?, model?}` → `{answer,
sources, model, chat_url, used, prompt_chars}`.

| | |
|---|---|
| space | `bge-m3`, 1024 dimensions, cosine, over `text[:2000]` — the same space lanceglass uses, so vectors from either tool are comparable |
| pool | every batch is sharded across the hosts, one thread each; a host that fails has its shard retried on the others |
| throughput | 68 rows/s on one RTX 4090, roughly 2× on two once the model is warm (the first batch pays for loading it) |
| this store | ~94k conversational rows ≈ 12 minutes on the pair |

## Test

```sh
uv run ruff check src tests
uv run pytest -q
```

The tests build a throwaway store in `tmp_path` and drive the CLI over it —
no network, no PocketBase, no writes to `lance_data_py/`.
