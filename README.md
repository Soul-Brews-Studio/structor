# Structor

A week-stamped, incremental index of Claude Code session transcripts, built
as a thin layer on an embedded PocketBase, with a LanceDB replica beside it.
Not another jsonl indexer: the one thing a fleet of ten indexers never had —
one row per `(session, ISO week)`, kept current by byte-offset tail state.
MIT licensed; developed in the open at
[Soul-Brews-Studio/structor](https://github.com/Soul-Brews-Studio/structor).

```
app/
├── main.go               embedded PocketBase + routes + `import` command
├── internal/
│   ├── schema/           projects · sessions · events · session_weeks · oauth_*
│   ├── jsonl/            transcript line parser, ReadFrom(offset) with partial-line hold-back
│   ├── ingest/           Apply(): optimistic byte-offset concurrency, week ledger recompute, queries
│   ├── scan/             in-process directory scanner (server-side / tests)
│   ├── mcp/              Streamable-HTTP JSON-RPC endpoint, 6 read-only tools
│   └── oauth/            OAuth 2.1 AS: metadata, dynamic registration, PKCE, refresh
├── ui/index.html         dashboard (login = PocketBase superuser)
├── cli/                  structor-cli (Rust): scan / watch / status
├── lance/                structor-lance (Bun): LanceDB replica of the store + admin on :8092
│   └── ui/live.html      the live jsonl page, served by both replicas (see "Live animation")
├── lance-py/             structor-lance (Python): the same replica, ORM-style schema, admin on :8094
├── dream/                structor-dream (Python): model-generated dream pages over the replica, into the wiki
├── tray/                 StructorTray (Swift, macOS menu bar): status + start/stop + target switch
├── haos/                 Home Assistant OS local add-on (kvmlab1)
├── launchd/              LaunchAgent templates (@APP_DIR@ / @HOME@ filled in on install)
└── scripts/              deploy-haos.sh · agent.sh (launchd entry) · record-live.py (the live page → WebM + GIF)
```

## Run locally

```sh
make build            # bin/structor (Go) + bin/structor-cli (Rust)
make test             # go test, cargo test, swift build
make run              # http://127.0.0.1:8091  admin@structor.local / structor-dev-password
make scan             # one pass over ~/.claude/projects
make watch            # follow changes (fs events + 120s safety rescan)
make tray             # menu-bar app, run straight from tray/.build
make install-tray     # wrap it as /Applications/StructorTray.app and launch it
make lance-install    # bun install for the LanceDB replica (once, before make lance)
make lance            # LanceDB replica + admin on http://127.0.0.1:8092
make lance-once       # one sync pass into lance_data/, then exit
make lance-py-install # uv sync for the Python edition (once, before make lance-py)
make lance-py         # the same replica + admin on http://127.0.0.1:8094
make dream-install    # uv sync for structor-dream (once, before make dream-nightly)
make dream-nightly    # dream the weeks whose events changed, re-index the wiki, exit
make record-live      # record the live page as recordings/live-jsonl.{webm,gif} (URL= SECONDS= FPS= OUT= NAME=)
```

Override credentials with `STRUCTOR_ADMIN_EMAIL` / `STRUCTOR_ADMIN_PASSWORD`;
the server creates or resets that superuser on every boot.

- Control room (landing, human-sized actions): `/` — big state tiles, drag-and-drop
  or folder import of `.jsonl` files (stored under `<data>/uploads/<label>/`, indexed
  with the same tail-state rules, re-import resumes), writer health per host, copyable
  `structor-cli watch` / `claude mcp add` commands, reconcile, recent imports.
- Console (dense, lanceglass-style): `/console.html` — Intake ledger, Events stream,
  History calendar, Projects. PocketBase admin: `/_/` — health: `/api/health`
- Ingest API (superuser token): `GET /api/structor/state`, `POST /api/structor/ingest`,
  `POST /api/structor/upload` (multipart `files`, `label`), `POST /api/structor/scan`,
  `POST /api/structor/reconcile`
- Read API (superuser or any MCP bearer): `/api/structor/{status,search,sessions,projects,days,read,weeks,intake}`

## Live feed

The **Live** workspace is `tail -f` of the store. Events are inserted with
raw SQL (no per-record hooks), so the server publishes its own message on
the custom PocketBase realtime topic `structor/live` after every ingest that
inserted rows: session, project, host, writer, counts, and up to 40 trimmed
conversational rows (`ingest.LiveMessage`). Only superuser-authenticated
realtime clients receive it.

Browser protocol (no SDK): `GET api/realtime` opens the SSE stream and sends
`PB_CONNECT {clientId}`; `POST api/realtime {clientId, subscriptions:
["structor/live"]}` with the superuser token attaches auth and topics. The
tab shows connection state, events/ingests since open, events per minute,
project/role filters, Pause (rows buffer) and Clear; it disconnects in hidden
tabs. Latency is dominated by the watcher: transcript write → `structor-cli
watch` (~2s) → store → browser (<100ms).

## Live animation

`/live.html` on either replica admin (`lance/ui/live.html`, `live.js`,
`live.css` — static, no build step, no external resources) shows transcripts
arriving as a moving picture. The header carries the target, a badge, a
staleness clock ("last event 12 s ago"), an events-per-minute meter for the
last ten minutes and Pause; below it one **lane** per project — the most
active ones in the current buffer plus `other` — and each conversational row,
as it is indexed, becomes a card at the top of its lane: role dot (user blue,
assistant amber, tool violet), `HH:MM:SS`, the session's short id, the first
~160 characters of text. Only the new card animates (700 ms fade-and-slide,
off under `prefers-reduced-motion`); the list itself never re-lays out, which
is the lesson from session-viewer's animated list at 99 % CPU. Forty cards per
lane, the oldest dropped from the bottom. The badge names which of two things
you are watching, so a recording can never pass one off as the other:

- **LIVE · relay `<target>` · structor/live** — the page opens
  `EventSource('/api/<target>/live')`, a relay the
  Python edition adds over PocketBase's `structor/live` topic: one upstream
  subscription per target shared by every open tab, a 256-message ring
  buffer, `Last-Event-ID` replay on reconnect, a heartbeat every 15 s, at
  most 24 tabs (details in `lance-py/README.md`). The first fill comes from
  `/api/<target>/live/recent`. The Bun edition has no relay, so there the
  page falls back to replay and says so.
- **REPLAY · Lance replica `<target>` · events table · from `<ts>` · ×speed** —
  the last `minutes` of the replica's own `events` table (user and assistant
  rows of at least 20 characters), played back at `speed`× with the original
  gaps, each clamped to 2.5 s, under a progress bar with the replayed clock.
  The rows API reads 500 rows at a time in storage order, so a window holding
  more is narrowed to its newest part that fits: the badge's `from` is the
  first event actually played, and the progress line says "the newest 35 of
  180 min" when that happened.

Query parameters: `target` (default: the first one in `/api/status`),
`mode=live|replay` (default live), `minutes` (replay window, 60), `speed`
(replay factor, 30; presets 1 / 10 / 30 / 60 / 300), `seconds` (stop after N
seconds and show "done" — for recordings), `lanes` (6).

Two caveats belong on the page and in every clip of it. **Latency**: a line
written to a transcript reaches the page about 12 s later, measured; the
watcher's debounce is nearly all of it, the store, the relay and the browser
add milliseconds. **Corpus**: six indexers in this fleet index the same
transcripts and their counts disagree by up to ~9× (different row definitions,
different filters), so the badge names the store being read — one Lance
replica's `events` table, or the PocketBase live topic — and a count on the
page is that store's count, not "the corpus".

### Recording it

```sh
make record-live                                   # replay, up to the last 3 h at ×60 → recordings/live-jsonl.{webm,gif}
make record-live URL='http://127.0.0.1:8094/live.html?target=local&mode=live' SECONDS=30
make record-live URL='http://127.0.0.1:8780/' WAIT_FOR='input[placeholder^="filter"]' NAME=fleet   # another page on loopback
cd lance-py && just record                         # the same, from the Python edition's directory
uvx --with playwright python scripts/record-live.py \
    --url 'http://127.0.0.1:8094/live.html?target=local&mode=replay&minutes=180&speed=60&seconds=18' \
    --seconds 20 --fps 4 --width 1280 --height 720 --out recordings --name live-jsonl
```

`scripts/record-live.py` opens the page in the system Chrome (Playwright,
`channel="chrome"`, headless, the viewport as given; the bundled Chromium is
the fallback, after `uvx --with playwright playwright install chromium`),
waits for `#lanes` (or whatever `--wait-for` names — pick an element that
appears with the page's data, not its empty shell), takes a screenshot every
`1/fps` seconds into a temp directory, and hands the frames to ffmpeg:
`<name>.webm` (libvpx-vp9, crf 32)
and `<name>.gif` (two-pass palette, 800 px wide, the same fps). The last line
on stdout is one JSON object with both paths and their byte sizes; progress
goes to stderr. `--dry-run` prints the plan — viewport, frame count, the exact
ffmpeg commands — without a browser (that is what the test runs); `--frames
DIR` re-encodes frames kept from a failed encode. Exit codes: 64 usage, 66 the
URL never showed the `--wait-for` element, 69 no browser could start, 70 ffmpeg missing or
failed. Playwright comes from `uvx` (the first run downloads it), so nothing is
added to any venv. Why stills and not Chromium's own screencast: its
`Page.captureScreenshot` hangs on the older console pages, so the live page is
built capture-safe (no `requestAnimationFrame` loops, no `backdrop-filter`, one
pulsing dot as the only endless animation) and captured one still at a time —
the pipeline that has worked here.

## Tail-state contract

Every session row carries `byte_offset` (always on a line boundary),
`file_size`, `lines_seen`. A client reads from `byte_offset`, sends only
complete lines, and states the offset it started from. The server accepts
only if its stored offset still matches, otherwise answers `409` with the
current offset. Truncated files restart at 0; the unique `events.uuid`
index dedups. Pattern lifted from session-viewer's `session_tail_state`,
made multi-writer safe.

Session identity: `<uuid>.jsonl` → the uuid. Workflow journals are all
named `journal.jsonl`, so they become `journal@<wf_dir>`.

## LanceDB replica + admin (structor-lance)

`lance/` is a Bun process that mirrors a Structor store into LanceDB and serves
an admin UI over it. It reads `projects`, `sessions`, `events`, `session_weeks`
and `import_runs` through the
PocketBase records API — no server change, no second jsonl walker — pages them
in `(stamp, id)` order, and upserts by `id` with `mergeInsert`. Every target in
`~/.config/structor/*.json` gets its own Lance directory under
`lance_data/<target>/`, with the cursor in that directory's `sync.json`. It
wakes on the `structor/live` realtime topic and otherwise polls (15s default).
`events.text` carries a full-text index. Vectors are optional and additive: the
Python edition fills a sibling `event_vectors` table (bge-m3 via an Ollama
pool) when asked, and lexical stays the default because it wins on the
known-item probes measured so far. **PocketBase
remains the source of truth** — byte offsets, the week ledger and import runs
never move, and Lance only ever catches up. (The reasoning is recorded in the
maintainers' notes: a replica, not a second indexer, because Go has no
LanceDB SDK and the PocketBase records API already pages every table.)

```sh
make lance-install    # bun install (once)
make lance            # replica + admin, foreground
make lance-once       # one sync pass, then exit
make lance-typecheck  # tsc --noEmit (also part of make test)
cd lance && bun src/main.ts --http 127.0.0.1:8097 --no-sync   # read-only second copy on any free port
```

Admin: <http://127.0.0.1:8092> — loopback only, no auth, because nothing it
serves can reach a password (targets are resolved from `~/.config`, the API
never echoes them). Flags: `--http`, `--data`, `--targets`, `--interval`,
`--no-sync`, `--once`; the same values come from `STRUCTOR_LANCE_HTTP`,
`STRUCTOR_LANCE_DATA`, `STRUCTOR_LANCE_TARGETS`, `STRUCTOR_LANCE_INTERVAL`.

```
GET  /api/status                                   mode: live, storage: lancedb; all targets, table counts, sync state
GET  /api/:t/tables                                [{name, rows, version, indices}]
GET  /api/:t/tables/:n/schema                      {fields:[{name,type,nullable}]}
GET  /api/:t/tables/:n/rows?where&limit&offset&select   {rows, total, limit, offset} (storage order; no ORDER BY)
GET  /api/:t/tables/:n/search?q&limit&where        {rows} with _score (FTS tables only)
GET  /api/:t/tables/:n/stats                       {rows, version, versions, indices, stats}
GET  /api/:t/sync                                  {state, lag}
POST /api/:t/sync                                  pull now → {pulled, state}
POST /api/:t/tables/:n/optimize                    compact + index new rows
POST /api/:t/tables/:n/fts                         (re)build the FTS index
```

`where` is a LanceDB SQL predicate; comments, semicolons and any function
outside a short allowlist (`lower`, `length`, `starts_with`, `regexp_like`,
… see `ALLOWED_FUNCS` in `src/admin.ts`) are refused. Requests whose `Host`
is not loopback, or whose `Origin` is another site, get 403; a `--no-sync`
instance answers every POST with 405.

### Two frontends, one backend

The PocketBase console pages in `ui/` (Intake / Live / Events / History /
Projects and the Import page) are also served by `structor-lance` at
`http://127.0.0.1:8092/console/<target>/`, unchanged: their relative
`api/…` calls are answered by `src/facade.ts` from that target's Lance tables
(status, projects, search over the FTS index, days, read, sessions, weeks,
intake; the realtime feed is proxied to the target's PocketBase). Sign in with
the target's admin credentials. Actions that write — scan, reconcile, import —
answer 405 there; use the PocketBase console (8091) for those. The tray's "Open
console on LanceDB", the admin's "Console" link and `just open-console` all
land on this copy.

### Python edition (lance-py)

`lance-py/` is the same replica written in Python: the `structor_lance`
package, uv-managed (3.13, lancedb 0.38, pydantic 2, FastAPI, typer). Same
PocketBase source, same five tables, same column names and Arrow types — what
differs is the schema style, which is ORM-like: each table is a
`lancedb.pydantic.LanceModel` that is at once the Arrow schema, the row you
upsert and the row you read back (`src/structor_lance/schema.py`), so
`Replica.table(Event)` hands back a typed table and `to_pydantic(Event)` hands
back typed rows. It listens on **8094** and keeps its own store in
`lance_data_py/<target>/`, leaving 8092 and `lance_data/` to the Bun edition;
both can run at the same time, and neither writes the other's directory. (It
listened on 8093 until 2026-09-10, when an unrelated lab turned out to hold
that port on this Mac.)

```sh
make lance-py-install   # uv sync (once)
make lance-py           # replica + admin, foreground, on 127.0.0.1:8094
make lance-py-once      # one sync pass into lance_data_py/, then exit
make lance-py-test      # ruff + pytest (also part of make test)
cd lance-py && uv run structor-lance serve --no-sync --http 127.0.0.1:8098   # read-only second copy
```

It answers the same `/api/…` routes as the Bun admin, serves the same
dependency-free admin UI out of `lance/ui/`, and mounts the same console pages
from `ui/` at `/console/<target>/` — one frontend, two backends. The CLI has
the same command surface, and `just --list` in `lance-py/` lists the recipes:

```sh
cd lance-py
uv run structor-lance targets | status | tables | schema <table> | rows <table> [--where …] \
                      | search <q> | lag | sync | optimize <table> | fts | serve | once
uv run structor-lance embed | vsearch <q> [--mode vector|hybrid] | vectors   # optional, see below
```

Reads go straight to the Lance directory, which is safe next to the running
replica; `sync`, `optimize` and `fts` go through the admin API at
`$STRUCTOR_LANCE_PY_HTTP` (default `127.0.0.1:8094`) when something answers
there, so only one process ever writes a table, and fall back to direct access
when nothing does. `$STRUCTOR_LANCE_PY_DATA` (default `lance_data_py/`) picks
the store. Passwords stay in the process: only a target's name and url are ever
printed.

**Vectors are optional.** Full-text search (BM25 over `events.text`) is the
default and needs nothing else. When an Ollama pool is configured, the Python
edition can also fill a sibling table, `event_vectors`, and search it by
meaning: `embed` fills it, `vsearch` queries it, `vectors` counts what is done
and what is pending, and the admin answers
`GET /api/<target>/tables/events/vsearch?q&limit&where&mode`. The pool is
`~/.config/structor/lance.json`:

```json
{ "ollama_urls": ["http://gpu1:11434", "http://gpu2:11434"], "embedding_model": "bge-m3" }
```

or `STRUCTOR_OLLAMA_URLS=http://gpu1:11434,http://gpu2:11434`. With none
configured, `embed` and `vsearch` exit 78 (EX_CONFIG) with that message and
everything else carries on. The space is bge-m3, 1024 dimensions, cosine, over
`text[:2000]`, named after lanceglass's convention but not lanceglass's space
(its id carries a model-revision segment and its write contract compares ids
exactly, so the two stores' vectors are not comparable until one adopts the
other's id). Measured throughput once the model is warm: 68 rows/s on one
RTX 4090 and roughly twice that on two (every batch is sharded across the
hosts, one thread each), so the ~94k conversational rows of this store take
about 12 minutes on the pair.

Ports on this Mac:

| port | serves |
|---|---|
| 8091 | PocketBase — dashboard, ingest/read API, `/mcp`; the source of truth |
| 8092 | `structor-lance` (Bun) — LanceDB admin at `/`, the console over the replica at `/console/<target>/` |
| 8094 | `structor-lance` (Python) — the same admin and console over `lance_data_py/` |

## Dreams (structor-dream)

`dream/` is a third Python program over the Python replica. It writes **dream
pages**: one markdown note per ISO week (or per topic), composed by the chat
model from a sample of that period's transcripts and saved into the
maintainers' wiki, so that `structor-lance ask` cites them beside raw events.
The transcripts are what happened; the wiki is what the maintainers decided it
meant; a dream page is what the model *inferred* it meant, and it says so. A
dream is inference, not measurement — every page carries that in its
frontmatter (`kind: dream`, `mode: week|topic`, `generated_by: <model>`,
`generated_at`, `status: inference`, `sources`) and in a one-paragraph "How this
was made" (model, counts, caps, date), and every bullet ends with the sessions
or events it rests on. Those citations are enforced in code, not asked for in
the prompt: a claim the model tied to no real id is dropped, a section left
with nothing valid says so, and transcript text is capped in code as well —
wherever the model read raw turns (a digest, a topic), every bullet and summary
it wrote is compared against them and any verbatim run longer than 120
characters is cut to that one phrase (a bullet is at most 300 characters; the
topic page's material table quotes one phrase of at most 120 characters per
hit). The name and the sampling rule come from the session-dream lab
this repo grew out of: stratify the material (projects for a week, time
horizons for a topic), sample, digest, then ask for recurring patterns,
contradictions, abandoned threads and a fixed number of cited insights.

```sh
cd dream
uv run structor-dream week [2026-W37] [--max-sessions 40] [--force] [--no-index]   # one week   → dreams/2026-W37.md
uv run structor-dream topic "409 offset mismatch" [--k 48] [--out path.md]         # one question → dreams/topic-409-offset-mismatch.md
uv run structor-dream nightly                                                      # current week + every week whose events moved since its page
uv run structor-dream draw 2026-W37                                                # the page's image_prompt → dreams/2026-W37.png via the Codex CLI's image tool (by hand; it costs)
```

Every reduce also asks the model for an `image_prompt` (one paragraph
describing a still illustration of the page, filtered and capped like every
other model output); `draw` turns it into a PNG beside the page and links it
under the title. It is not part of `nightly`. `--model codex` (or
`codex:<model>`) on `week` / `topic` / `nightly` uses the Codex CLI as the
chat model instead of the Ollama host — one sandboxed `codex exec` turn per
prompt — so both the page and its picture can come from the same account.

`week` takes up to `--max-sessions` conversational sessions of the week (at
least two human turns and ten events; round-robin over projects, most human
turns first, so no single project fills the sample), digests each one — one
chat call per session over a 6 k-char budget of fenced, filtered turns, the
same fences and instruction-line filters `ask` uses — then reduces the digests,
plus the previous week's insights for drift, into one page. Digests are cached
in `<data>/dreams/digest_state.json` beside the replica, keyed by session and
event count, so a second run of the same week digests nothing, a session that
grew is digested again, and `--force` redoes them all. Re-dreaming a week
overwrites its page (the page is derived; it is rewritten only when the content
changed, so an unchanged page keeps its mtime) but never a digest, and nothing
else in the dream directory is ever touched. `topic` retrieves hybrid hits for
the question, buckets them by age at run time (short ≤ 7 d, mid ≤ 30 d, long
≤ 90 d, archive) and by project, drops hits far below the best one, and asks one
question across the horizons; its page carries a per-horizon material table.
`nightly` is what launchd runs: the current ISO week (Asia/Bangkok) plus every
week whose `session_weeks` rows moved since that week's page was generated (a
week with events and no page is dreamed too), one wiki re-index at the end, one
JSON line on stdout — `{"weeks": [...], "digested": n, "skipped": n, "pages": [...]}`.

Where pages go is configuration, not source: `dream_dir` in
`~/.config/structor/lance.json`, or `STRUCTOR_DREAM_DIR` (the environment wins),
default `<wiki_dir>/dreams` — `wiki_dir` being the wiki directory from the same
file, see the Python edition above. With no wiki directory configured the
commands exit 78 (EX_CONFIG) and say what to set. When `dream_dir` sits under
`wiki_dir`, the default, every run finishes with the same hash-incremental
`wiki-index` the CLI offers, so a fresh page is searchable at once; a
`dream_dir` elsewhere gets the index command printed instead, and `--no-index`
skips it. The chat model and the embedding pool are the ones `ask` uses
(`chat_url`, `chat_model`, `ollama_urls` in that `lance.json`). Measured on
gemma3:27b a digest takes 10–50 s, so a 40-session week is on the order of half
an hour — which is why the job runs at night, why two digests are kept in flight,
and why the cache exists.

```sh
make dream-install    # uv sync (once)
make dream-test       # ruff + pytest, no GPU needed (also part of make test)
make dream-nightly    # the nightly pass in the foreground, the same command launchd runs
cd dream && just --list                                                          # week / topic / nightly / test
```

## MCP

`POST /mcp` (JSON-RPC 2.0, Streamable HTTP, JSON responses, no SSE stream).
Tools: `status`, `list_projects`, `list_sessions`, `search`, `read_session`,
`week_ledger`.

Auth, any of:

1. OAuth 2.1 access token from this server (claude.ai custom connector:
   add `https://<host>/mcp`, it discovers `/.well-known/oauth-protected-resource`,
   registers itself, opens the sign-in page — PocketBase superuser creds).
2. PocketBase superuser token (`POST /api/collections/_superusers/auth-with-password`).
3. `STRUCTOR_MCP_TOKEN` static bearer.

```sh
claude mcp add --transport http structor http://127.0.0.1:8091/mcp \
  --header "Authorization: Bearer $STRUCTOR_MCP_TOKEN"
```

Behind a tunnel set `STRUCTOR_PUBLIC_URL=https://structor.example.com` so
the metadata advertises the public origin.

## Deploy to kvmlab1 (HAOS local add-on)

```sh
make deploy           # cross-compile linux amd64+arm64, rsync to kvmlab1:/addons/structor, ha store reload, install/rebuild
```

Options come from `~/.config/structor/<guest>.json` on the deploying machine
(`admin_email`, `admin_password`, `mcp_token`, `public_url`, `scan_dir`); the
script POSTs them to the Supervisor API, since the `ha` CLI has no options
flag. Without that file, set `admin_password` in the HA add-on UI. Port 8090,
ingress panel in the HA sidebar. Point
`structor-cli --url http://<haos-host>:8090 …` at it, or pick the
target in the tray app.

### Public MCP through cloudflared (for claude.ai)

kvmlab1's cloudflared add-on runs in tunnel-token mode, so hostnames live in
the Cloudflare Zero Trust dashboard, not on the box. One-time step:

1. Zero Trust → Networks → Tunnels → the kvmlab1 tunnel → Public Hostname → Add:
   `structor.example.com` → service `http://local-structor:8090`
   (any other local add-on exposed through the same tunnel has the same shape).
2. Put `"public_url": "https://structor.example.com"` in
   `~/.config/structor/<guest>.json` and run `make deploy-files` so the OAuth
   metadata advertises the public origin.
3. claude.ai → Settings → Connectors → add `https://structor.example.com/mcp`.
   It registers itself, opens the Structor sign-in page, and gets a PKCE token.

## Tray

`~/.config/structor/tray.json` holds the tray's own settings (current target,
binaries, directories, replica URLs). Targets come from the same
`~/.config/structor/<name>.json` files every other process reads (`url`,
`admin_email`, `admin_password`); `local` exists without a file. An older
`tray.json` with an embedded `targets` array still works as a fallback. The menu
shows live totals (PocketBase, the Bun replica, the Python replica with its
vector count), starts/stops the local server, the watcher and both replicas,
opens the dashboard, the PocketBase admin, either LanceDB admin and either
console, switches targets, and has **Ask Structor…** (⌘K): a floating panel
that sends the question to the Python replica's `POST /api/<target>/ask` and
shows the answer with its cited sources. When launchd already runs a process the matching toggle is
shown as "running (launchd)" and disabled, so the menu cannot start a second
copy. Watcher and scan credentials are passed to `structor-cli` through the
environment, never on the command line.

Optional keys, all with defaults: `lanceUrl` (`http://127.0.0.1:8092`, where
the admin is opened and polled), `lanceBind` (`127.0.0.1:8092`, the address a
tray-started replica listens on), `bunBinary` (first of `~/.bun/bin/bun`,
`/opt/homebrew/bin/bun`, `/usr/local/bin/bun`), `lanceDir` (`app/lance`, found
from `StructorAppDir` in the bundle's Info.plist, which `make install-tray`
stamps with the repo path); and for the Python edition `lancePyUrl`
(`http://127.0.0.1:8094`), `lancePyBind`, `uvBinary` (first of
`~/.local/bin/uv`, `/opt/homebrew/bin/uv`, `/usr/local/bin/uv`), `lancePyDir`
(`app/lance-py`). A `tray.json` that fails to decode is copied to
`tray.json.bad` and left in place; defaults are used for that run only.

`make install-tray` runs `scripts/bundle-tray.sh install`: it builds the
release binary, wraps it as an `LSUIElement` (menu bar only) bundle with
`CFBundleIdentifier` `studio.soulbrews.structor.tray`, ad-hoc signs it, copies
it to `/Applications/StructorTray.app`, and relaunches it. Rebuilding and
reinstalling is the same command again.

## Running at login (launchd)

`make install-agents` installs seven LaunchAgents and starts them, stopping any
hand-started copy of the same process first — all but `dream`, which is loaded
and then waits for its clock:

| label (`studio.soulbrews.structor.…`) | runs | log (`~/Library/Logs/Structor/`) |
|---|---|---|
| `serve` | `scripts/agent.sh serve` → `bin/structor serve` on 127.0.0.1:8091 | `serve.log` |
| `watch-local` | `scripts/agent.sh watch local` → `structor-cli watch` (120s rescan) | `watch-local.log` |
| `watch-kvmlab1` | `scripts/agent.sh watch kvmlab1` → `structor-cli watch` (`watch_interval`, 300s) | `watch-kvmlab1.log` |
| `lance` | `scripts/agent.sh lance` → `bun lance/src/main.ts`, admin on 127.0.0.1:8092 | `lance.log` |
| `lance-py` | `scripts/agent.sh lance-py` → `uv run structor-lance serve`, admin on 127.0.0.1:8094 | `lance-py.log` |
| `dream` | `scripts/agent.sh dream` → `uv run structor-dream nightly`, once a day at 03:30 (calendar job, no port) | `dream.log` |
| `tray` | `/Applications/StructorTray.app` (needs `make install-tray` first) | `tray.log` |

Templates are in `launchd/`; `@APP_DIR@` / `@HOME@` are substituted on install.
The `dream` agent is the odd one out: `StartCalendarInterval` (Hour 3, Minute
30) instead of `KeepAlive`, `RunAtLoad` false, so `scripts/install-agents.sh
dream` loads it without running it and touches none of the other six;
`launchctl print gui/$(id -u)/studio.soulbrews.structor.dream` shows it
waiting. A run missed while the Mac slept fires at the next wake; one missed
while it was powered off does not. To run it now instead of at 03:30, use
`make dream-nightly` (or `launchctl kickstart` the label). It has no port, so
the installer stops nothing for it — a hand-run `structor-dream` is a one-shot
that ends on its own — and it is skipped when uv or `dream/.venv` is missing
(run `make dream-install`). `scripts/install-agents.sh --uninstall dream`
removes just that one; `--uninstall` alone removes all seven.
Credentials never appear on a command line: `scripts/agent.sh` reads
`~/.config/structor/<target>.json` (`url`, `admin_email`, `admin_password`,
optional `watch_interval`, and for `local.json` optional `http` / `data_dir`)
and passes them through the environment; a missing `local.json` means the dev
defaults. The `lance` agent is handed no credentials at all — the Bun process
reads the same config files itself — only `STRUCTOR_LANCE_HTTP` (8092) and
`STRUCTOR_LANCE_DATA` (`lance_data/`); an optional
`~/.config/structor/lance.json` with `{"targets": ["local", "kvmlab1"]}`
narrows which stores it replicates. The `lance-py` agent works the same way on
`STRUCTOR_LANCE_PY_HTTP` (8094) and `STRUCTOR_LANCE_PY_DATA` (`lance_data_py/`),
reading the same `lance.json`. launchd starts them with a bare PATH, so the
script looks for `bun` in `~/.bun/bin`, `/opt/homebrew/bin`, `/usr/local/bin`,
and for `uv` in `~/.local/bin`, `/opt/homebrew/bin`, then PATH; the installer
skips `lance` when bun or `lance/node_modules` is missing (run
`make lance-install`) and `lance-py` when uv or `lance-py/.venv` is missing (run
`make lance-py-install`). A hand-started replica is stopped by
whoever listens on the admin port (`lsof -ti tcp:8092`, and `tcp:8094` for the
Python one), since `make lance` shows up in `ps` only as `bun src/main.ts` and
`make lance-py` only as `uv run structor-lance serve` — for `lance-py` the
listener's command line is read first (`ps -o command=`) and only a
`structor_lance` / `lance-py` process is killed, so a stranger that happens to
hold the port keeps running; each port doubles as that
edition's writer mutex (`make lance-once` / `make lance-py-once` delegate to a
running replica instead of opening the same tables twice). `make agents-status` prints state and pid per agent
(`dream` shows a state and no pid between runs),
`make uninstall-agents` boots them out and deletes the plists. Do not also add
the tray as a Login Item, or two copies start; and with the agents installed,
leave the tray's own Start server / Start watcher toggles alone, they would
start a second copy.

### UI entry points

- `/` retains the original Intake / Events / History / Projects interface.
- `/simple.html` is the human-sized **Import** page (drop/pick transcripts, writer
  health, connect an AI client, housekeeping, recent imports), reached through the
  **Import** button in the shared command bar. Its spacing follows a 4pt scale with
  8px radii, after the P2P Dropbox add-on's calmer composition; the brand link returns
  to the workspace.
- `/console.html` remains available for existing bookmarks.
- HTML is served with `Cache-Control: no-cache`, so a redeploy shows without a hard refresh.

Restoration checks: `go vet ./...`, `go test ./...`, both Linux architectures,
inline JavaScript syntax, and authenticated browser navigation on desktop/mobile.
The default stays Intake; `/?ws=history` still selects History. No API, data,
credential, or ingestion behavior changes in this UI routing correction.

All UI entry points share the same command bar: Dark / Paper / Simple view /
Intake / Events / History / Projects. Navigation styling lives in
`ui/navigation.css`; `ui_test.go` prevents the three header copies from drifting.
Simple view marks its own menu entry active and routes the workspace buttons to
`/?ws=...`, preserving the existing default and shared login/theme storage.

Simple-view actions use an explicit 2×2 desktop grid, collapsing to one column
at 720px. Local authenticated browser checks verified all four workspace links,
theme persistence across views, active states, equal row edges/heights at 1280px,
and no horizontal overflow at 390px (Paper theme). Go vet, all Go tests and
inline JavaScript syntax passed. The optional UI detector lacked HTML/CSS parser
modules, so visual validation used browser geometry and screenshots instead.
