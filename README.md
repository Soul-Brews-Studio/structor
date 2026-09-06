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
├── tray/                 StructorTray (Swift, macOS menu bar): status + start/stop + target switch
├── haos/                 Home Assistant OS local add-on (kvmlab1)
└── scripts/deploy-haos.sh
```

## Run locally

```sh
make build            # bin/structor (Go) + bin/structor-cli (Rust)
make test             # go test, cargo test, swift build
make run              # http://127.0.0.1:8091  admin@structor.local / structor-dev-password
make scan             # one pass over ~/.claude/projects
make watch            # follow changes (fs events + 120s safety rescan)
make tray             # menu-bar app
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
shows live totals, starts/stops the local server and the watcher, opens the
dashboard/admin, and switches targets.

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
