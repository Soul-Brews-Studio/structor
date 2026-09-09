# Structor app

A week-stamped, incremental index of Claude Code session transcripts, built
as a thin layer on an embedded PocketBase. This is the implementation of the
decision in `ψ/writing/decision-thin-layer-not-fork.md`: not another
jsonl indexer, but the one thing none of the fleet's ten indexers had — one
row per `(session, ISO week)`, kept current by byte-offset tail state.

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
├── tray/                 StructorTray (Swift, macOS menu bar): status + start/stop + target switch
├── haos/                 Home Assistant OS local add-on (kvmlab1)
├── launchd/              LaunchAgent templates (@APP_DIR@ / @HOME@ filled in on install)
└── scripts/deploy-haos.sh
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
`events.text` carries a full-text index; there are no vectors yet, on the
measured evidence that lexical wins on these known-item queries. **PocketBase
remains the source of truth** — byte offsets, the week ledger and import runs
never move, and Lance only ever catches up. Reasoning:
`ψ/writing/decision-lancedb-replica-not-second-indexer.md`.

```sh
make lance-install    # bun install (once)
make lance            # replica + admin, foreground
make lance-once       # one sync pass, then exit
make lance-typecheck  # tsc --noEmit (also part of make test)
cd lance && bun src/main.ts --http 127.0.0.1:8094 --no-sync   # read-only second copy on any free port
```

Admin: <http://127.0.0.1:8092> — loopback only, no auth, because nothing it
serves can reach a password (targets are resolved from `~/.config`, the API
never echoes them). Flags: `--http`, `--data`, `--targets`, `--interval`,
`--no-sync`, `--once`; the same values come from `STRUCTOR_LANCE_HTTP`,
`STRUCTOR_LANCE_DATA`, `STRUCTOR_LANCE_TARGETS`, `STRUCTOR_LANCE_INTERVAL`.

```
GET  /api/status                                   all targets, table counts, sync state
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

Ports on this Mac:

| port | serves |
|---|---|
| 8091 | PocketBase — dashboard, ingest/read API, `/mcp`; the source of truth |
| 8092 | `structor-lance` — LanceDB admin at `/`, the console over the replica at `/console/<target>/` |

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
`structor-cli --url http://kvmlab1.oracle.netbird:8090 …` at it, or pick the
target in the tray app.

### Public MCP through cloudflared (for claude.ai)

kvmlab1's cloudflared add-on runs in tunnel-token mode, so hostnames live in
the Cloudflare Zero Trust dashboard, not on the box. One-time step:

1. Zero Trust → Networks → Tunnels → the kvmlab1 tunnel → Public Hostname → Add:
   `structor.buildwithoracle.com` → service `http://local-structor:8090`
   (same shape as `digger-wiki.buildwithoracle.com` → `local-digger-wiki:8104`).
2. Put `"public_url": "https://structor.buildwithoracle.com"` in
   `~/.config/structor/kvmlab1.json` and run `make deploy-files` so the OAuth
   metadata advertises the public origin.
3. claude.ai → Settings → Connectors → add `https://structor.buildwithoracle.com/mcp`.
   It registers itself, opens the Structor sign-in page, and gets a PKCE token.

## Tray

`~/.config/structor/tray.json` lists targets (local, kvmlab1, …). The menu
shows live totals, starts/stops the local server, the watcher and the LanceDB
replica, opens the dashboard, the PocketBase admin and the LanceDB admin, and
switches targets. When launchd already runs a process the matching toggle is
shown as "running (launchd)" and disabled, so the menu cannot start a second
copy. Watcher and scan credentials are passed to `structor-cli` through the
environment, never on the command line.

Optional keys, all with defaults: `lanceUrl` (`http://127.0.0.1:8092`, where
the admin is opened and polled), `lanceBind` (`127.0.0.1:8092`, the address a
tray-started replica listens on), `bunBinary` (first of `~/.bun/bin/bun`,
`/opt/homebrew/bin/bun`, `/usr/local/bin/bun`), `lanceDir` (`app/lance`, found
from `StructorAppDir` in the bundle's Info.plist, which `make install-tray`
stamps with the repo path). A `tray.json` that fails to decode is copied to
`tray.json.bad` and left in place; defaults are used for that run only.

`make install-tray` runs `scripts/bundle-tray.sh install`: it builds the
release binary, wraps it as an `LSUIElement` (menu bar only) bundle with
`CFBundleIdentifier` `studio.soulbrews.structor.tray`, ad-hoc signs it, copies
it to `/Applications/StructorTray.app`, and relaunches it. Rebuilding and
reinstalling is the same command again.

## Running at login (launchd)

`make install-agents` installs five LaunchAgents and starts them, stopping any
hand-started copy of the same process first:

| label (`studio.soulbrews.structor.…`) | runs | log (`~/Library/Logs/Structor/`) |
|---|---|---|
| `serve` | `scripts/agent.sh serve` → `bin/structor serve` on 127.0.0.1:8091 | `serve.log` |
| `watch-local` | `scripts/agent.sh watch local` → `structor-cli watch` (120s rescan) | `watch-local.log` |
| `watch-kvmlab1` | `scripts/agent.sh watch kvmlab1` → `structor-cli watch` (`watch_interval`, 300s) | `watch-kvmlab1.log` |
| `lance` | `scripts/agent.sh lance` → `bun lance/src/main.ts`, admin on 127.0.0.1:8092 | `lance.log` |
| `tray` | `/Applications/StructorTray.app` (needs `make install-tray` first) | `tray.log` |

Templates are in `launchd/`; `@APP_DIR@` / `@HOME@` are substituted on install.
Credentials never appear on a command line: `scripts/agent.sh` reads
`~/.config/structor/<target>.json` (`url`, `admin_email`, `admin_password`,
optional `watch_interval`, and for `local.json` optional `http` / `data_dir`)
and passes them through the environment; a missing `local.json` means the dev
defaults. The `lance` agent is handed no credentials at all — the Bun process
reads the same config files itself — only `STRUCTOR_LANCE_HTTP` (8092) and
`STRUCTOR_LANCE_DATA` (`lance_data/`); an optional
`~/.config/structor/lance.json` with `{"targets": ["local", "kvmlab1"]}`
narrows which stores it replicates. launchd starts it with a bare PATH, so the
script looks for `bun` in `~/.bun/bin`, `/opt/homebrew/bin`, `/usr/local/bin`,
then PATH, and the installer skips `lance` when bun or `lance/node_modules` is
missing (run `make lance-install`). A hand-started replica is stopped by
whoever listens on the admin port (`lsof -ti tcp:8092`), since `make lance`
shows up in `ps` only as `bun src/main.ts`; the port doubles as the writer
mutex (`make lance-once` delegates to a running replica instead of opening the
same tables twice). `make agents-status` prints state and pid per agent,
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
