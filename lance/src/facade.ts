// The old console on the new backend.
//
// app/ui/ (index.html = console, console.html, simple.html, navigation.css,
// fonts/) talks to PocketBase with RELATIVE urls: `api/structor/status`,
// `api/collections/_superusers/auth-with-password`, `api/realtime`. Served
// under /console/<target>/ those become /console/<target>/api/…, which this
// module answers from the LanceDB replica of <target>. No change to the
// console files; the same pages run against two backends.
//
// Contract (paths are relative to /console/<target>/api/):
//
//   POST collections/_superusers/auth-with-password  {identity, password}
//        → 200 {token} when they match the target's admin credentials
//          (targets.ts), else 400 {error}. Tokens are process-local.
//   every other path needs `Authorization: <token>` → else 401 {error}.
//
//   GET structor/status            ingest.Status: projects, sessions, events,
//        session_weeks (row counts), last_ingest = max(sessions.updated),
//        last_event_ts = max(sessions.last_ts), weeks = top 16 iso_week DESC
//        from session_weeks as {week, sessions, events, user_msgs}, version,
//        time (RFC3339 now), tz "Asia/Bangkok".
//   GET structor/projects?limit    {projects: ProjectRow[]} — id, path, cwd,
//        encoded_dir, name, host, sessions (count), events (sum of
//        sessions.event_count), last_ts (max sessions.last_ts); ordered by
//        last_ts DESC; limit default 200, max 1000.
//   GET structor/search?q&project&project_id&week&session&role&limit
//        {hits: SearchHit[], limit, truncated} — hits are events with
//        role <> '' and (text <> '' or tools not empty). With q: case-insensitive
//        substring match (LIKE %q%, the Go handler's semantics; the BM25 index
//        stays the admin's tool); without q: newest-first window.
//        Order ts DESC. SearchHit: session_id, project (cwd or path),
//        ts, iso_week, role, type, snippet (text[:600]), tools, line_no, uuid.
//        limit default 30, max 200.
//   GET structor/days?from&to&project&project_id&limit
//        {days: DayRow[], tz, truncated} — one row per (day in Asia/Bangkok,
//        session) over events with role <> '' between from and to (YYYY-MM-DD,
//        inclusive, at most 31 days): day, session_id, project, events,
//        user_msgs, first_ts, last_ts, preview (first user text[:200] that day),
//        git_branch. Order day DESC, last_ts DESC. limit default 500.
//   GET structor/read?session&offset&limit
//        {events: EventRow[], offset} — session is a session_id or unambiguous
//        prefix (404 {error} when none, 400 when ambiguous); rows with
//        role <> '' and (text or tools), ts ASC, text[:4000]; EventRow: ts,
//        role, type, text, tools, line_no. limit default 100, max 500.
//   GET structor/sessions?project&project_id&week&limit
//        {sessions: SessionRow[]} — session_id, project, tier, first_ts,
//        last_ts, event_count, first_prompt, git_branch, file_path; last_ts
//        DESC; limit default 50, max 500.
//   GET structor/weeks?week&session&limit
//        {weeks: LedgerRow[]} from session_weeks: week, session_id, project,
//        events, user_msgs, assistant_msgs, tool_calls, first_ts, last_ts.
//   GET structor/intake?limit&pending
//        {summary, runs, files, writers, connections, scan_dir, scan_interval}
//        summary (IntakeSummary): files = sessions count, bytes_tracked =
//        sum(file_size), bytes_indexed = sum(byte_offset), pending_files =
//        count(file_size > byte_offset), pending_bytes = sum of that gap,
//        last_ingest, runs_today, inserted_today (import_runs created today in
//        Asia/Bangkok), hosts (comma list of distinct projects.host).
//        runs: RunRow[] newest first (created, session_id, project, file_path,
//        from_offset, to_offset, lines, inserted, skipped, host, writer).
//        files: FileRow[] (session_id, project, file_path, tier, file_size,
//        byte_offset, lines_seen, event_count, file_mtime, updated), pending=1
//        keeps only file_size > byte_offset; updated DESC.
//        writers: WriterRow[] per (host, writer) over all import_runs:
//        host, writer, last_run, runs_24h, inserted_24h, files.
//        connections: {oauth_clients: 0, active_tokens: 0} (no OAuth tables here).
//        scan_dir "", scan_interval "".
//   GET structor/state?path      404 {error} (tail state is PocketBase's).
//   POST structor/scan | reconcile | upload | ingest
//        405 {error: "… not available on the LanceDB backend; use the
//        PocketBase console"}.
//   GET  realtime                 proxy: open ${target.url}/api/realtime and
//        stream the SSE body back unchanged (content-type text/event-stream,
//        no buffering, closed when the client goes away).
//   POST realtime                 proxy: forward the JSON body to
//        ${target.url}/api/realtime with Authorization = the replica's own
//        PocketBase token (pb.bearer()); return the upstream status.
//
// Everything is computed from the Lance tables (sync.ts TABLES: projects,
// sessions, events, session_weeks, import_runs). LanceDB has no ORDER BY,
// GROUP BY or JOIN: fetch the needed columns with `select`, aggregate in JS,
// and cache aggregates for 30s keyed by table version. A 288k-row events scan
// with three columns takes ~1s here; days and status must not scan events
// on every call.
//
// Three places where the contract above and the Go handlers it mirrors
// disagree, and what this file does:
//
//   * field names follow Go (internal/ingest), because the console reads them:
//     the weeks ledger row is `iso_week` (not `week`; `week` stays the name in
//     status.weeks, which is a different Go struct), and a writer row is
//     {host, writer, last_run, runs_24h, inserted_24h, files}.
//   * intake carries Go's `server_scan`, `host`, `tz`, `upload_dir` and
//     `superuser` keys as well as the contract's `scan_dir`/`scan_interval`.
//     index.html reads `d.server_scan.enabled` and simple.html reads
//     `it.upload_dir` unguarded; without them the Intake workspace — the
//     console's landing page — throws.
//   * GET realtime is the one authenticated-by-contract path served without a
//     token: EventSource cannot send an Authorization header, and PocketBase
//     itself opens the stream unauthenticated. The stream carries nothing
//     until a POST realtime (token required) subscribes it to a topic.

import { TABLES, type Replica, type TableSpec } from "./sync.ts";

export interface Facade {
  /** Answer one console API request. `path` is relative to /console/<target>/api/ (no leading slash). */
  handle(r: Replica, path: string, req: Request, url: URL): Promise<Response>;
}

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status, headers: JSON_HEADERS });
const bad = (error: string, status = 400) => json({ error }, status);

/** Zone the Go server stamps ISO weeks and day buckets in. Fixed offset, so a day boundary is UTC + 7h. */
const TZ = "Asia/Bangkok";
const TZ_OFFSET_MS = 7 * 3_600_000;
const DAY_MS = 86_400_000;

/** Shown in the console's status strip; the replica has no PocketBase build stamp of its own. */
const VERSION = process.env.STRUCTOR_LANCE_VERSION ?? "lance";

// ---------------------------------------------------------------- tokens

const TOKEN_TTL_MS = 7 * 24 * 3_600_000;
const MAX_TOKENS = 200;
const tokens = new Map<string, { target: string; issued: number }>();

function issue(target: string): string {
  const now = Date.now();
  for (const [k, v] of tokens) if (now - v.issued > TOKEN_TTL_MS) tokens.delete(k);
  while (tokens.size >= MAX_TOKENS) tokens.delete(tokens.keys().next().value as string);
  const token = `lance.${crypto.randomUUID()}`;
  tokens.set(token, { target, issued: now });
  return token;
}

/** True when the request carries a token this process issued for this target. */
function authorized(r: Replica, req: Request): boolean {
  const raw = (req.headers.get("authorization") ?? "").trim();
  const token = raw.toLowerCase().startsWith("bearer ") ? raw.slice(7).trim() : raw;
  if (!token) return false;
  const rec = tokens.get(token);
  if (!rec) return false;
  if (Date.now() - rec.issued > TOKEN_TTL_MS) { tokens.delete(token); return false; }
  return rec.target === r.target.name;
}

/** Length-independent-ish comparison so a wrong password leaks nothing through timing. Never logs either side. */
function sameSecret(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

// ---------------------------------------------------------------- cache

type TName = TableSpec["name"];
const SPEC = Object.fromEntries(TABLES.map(t => [t.name, t])) as Record<TName, TableSpec>;
const tableOf = (r: Replica, n: TName) => r.table(SPEC[n]);

const CACHE_TTL_MS = 30_000;
const CACHE_MAX = 32;
interface Entry { version: string; at: number; value: unknown }
const caches = new WeakMap<Replica, Map<string, Entry>>();

/**
 * Memoise one derived value per replica. An entry is reused while every table
 * it was built from is still at the same version and it is younger than 30s —
 * the version keeps a stale aggregate from outliving an import, the age keeps
 * wall-clock windows (last 24h, "today") from drifting.
 */
async function cached<T>(r: Replica, tables: TName[], key: string, make: () => Promise<T>): Promise<T> {
  const version = (await Promise.all(tables.map(n => tableOf(r, n).then(t => t.version())))).join(".");
  let m = caches.get(r);
  if (!m) { m = new Map(); caches.set(r, m); }
  const hit = m.get(key);
  if (hit && hit.version === version && Date.now() - hit.at < CACHE_TTL_MS) return hit.value as T;
  const value = await make();
  m.delete(key);
  while (m.size >= CACHE_MAX) m.delete(m.keys().next().value as string);
  m.set(key, { version, at: Date.now(), value });
  return value;
}

// ---------------------------------------------------------------- helpers

/** Quote a literal for a LanceDB (DataFusion) predicate: single quotes, doubled inside. */
const lit = (s: string) => `'${String(s).replace(/'/g, "''")}'`;
/** PocketBase's timestamp shape, which is also what the ts/created columns hold: 2026-09-09 15:00:00.000Z. */
const stampAt = (ms: number) => new Date(ms).toISOString().replace("T", " ");
const cut = (s: unknown, n: number) => { const v = s == null ? "" : String(s); return v.length > n ? v.slice(0, n) : v; };
const str = (v: unknown) => (v == null ? "" : String(v));
const nnum = (v: unknown) => (typeof v === "bigint" ? Number(v) : typeof v === "number" ? v : Number(v ?? 0) || 0);
/** Descending by a timestamp-shaped string; "" (never seen) sorts last. */
const descTs = <T extends { ts: string }>(a: T, b: T) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0);
/** A real calendar date: the shape, and Date must not roll it over (2026-02-31 is refused, as Go's ParseInLocation refuses it). */
const isRealYMD = (s: string) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const ms = Date.parse(`${s}T00:00:00.000Z`);
  return !Number.isNaN(ms) && new Date(ms).toISOString().slice(0, 10) === s;
};

/** Go's rule for every limit: out of range falls back to the default, it is not clamped to the maximum. */
function limitOf(raw: string | null, def: number, max: number): number {
  const n = Math.floor(Number(raw));
  return Number.isFinite(n) && n > 0 && n <= max ? n : def;
}

/** RFC3339 without fractional seconds, the way Go's time.RFC3339 prints it. */
const nowRFC3339 = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

/** Rows come back as plain objects; only strings and float64 are in these schemas. */
type Row = Record<string, unknown>;

// ---------------------------------------------------------------- loaders

interface SessionRec {
  id: string; session_id: string; project: string; tier: string; file_path: string;
  first_ts: string; last_ts: string; event_count: number; first_prompt: string; git_branch: string;
  file_size: number; byte_offset: number; lines_seen: number; file_mtime: number; updated: string; cwd: string;
}
interface ProjectRec { id: string; path: string; cwd: string; encoded_dir: string; name: string; host: string; created: string; updated: string }
interface WeekRec { session: string; project: string; iso_week: string; event_count: number; user_count: number; assistant_count: number; tool_count: number; first_ts: string; last_ts: string }
interface RunRec { session: string; project: string; from_offset: number; to_offset: number; lines: number; inserted: number; skipped: number; host: string; writer: string; created: string }

const SESSION_COLS = ["id", "session_id", "project", "tier", "file_path", "first_ts", "last_ts", "event_count",
  "first_prompt", "git_branch", "file_size", "byte_offset", "lines_seen", "file_mtime", "updated", "cwd"];

interface Store {
  sessions: SessionRec[];
  byRecord: Map<string, SessionRec>;   // sessions.id (what events.session points at)
  projects: ProjectRec[];
  byProject: Map<string, ProjectRec>;
  /** display path of a session's project: the real cwd when known, else the decoded guess. */
  pathOf: Map<string, string>;         // sessions.id → project path
}

/** sessions + projects, the join the console needs on every page. 3.5k rows, ~10ms, cached per version. */
async function store(r: Replica): Promise<Store> {
  return cached(r, ["sessions", "projects"], "store", async () => {
    const st = await tableOf(r, "sessions");
    const pt = await tableOf(r, "projects");
    const srows = (await st.query().select(SESSION_COLS).limit(1_000_000).toArray()) as Row[];
    const prows = (await pt.query().limit(100_000).toArray()) as Row[];
    const projects: ProjectRec[] = prows.map(p => ({
      id: str(p.id), path: str(p.path), cwd: str(p.cwd), encoded_dir: str(p.encoded_dir),
      name: str(p.name), host: str(p.host), created: str(p.created), updated: str(p.updated),
    }));
    const byProject = new Map(projects.map(p => [p.id, p]));
    const sessions: SessionRec[] = srows.map(s => ({
      id: str(s.id), session_id: str(s.session_id), project: str(s.project), tier: str(s.tier),
      file_path: str(s.file_path), first_ts: str(s.first_ts), last_ts: str(s.last_ts),
      event_count: nnum(s.event_count), first_prompt: str(s.first_prompt), git_branch: str(s.git_branch),
      file_size: nnum(s.file_size), byte_offset: nnum(s.byte_offset), lines_seen: nnum(s.lines_seen),
      file_mtime: nnum(s.file_mtime), updated: str(s.updated), cwd: str(s.cwd),
    }));
    const byRecord = new Map(sessions.map(s => [s.id, s]));
    const pathOf = new Map<string, string>();
    for (const s of sessions) {
      const p = byProject.get(s.project);
      pathOf.set(s.id, p ? (p.cwd || p.path) : "");
    }
    return { sessions, byRecord, projects, byProject, pathOf };
  });
}

async function weekRows(r: Replica): Promise<WeekRec[]> {
  return cached(r, ["session_weeks"], "weeks", async () => {
    const t = await tableOf(r, "session_weeks");
    const rows = (await t.query().limit(1_000_000).toArray()) as Row[];
    return rows.map(w => ({
      session: str(w.session), project: str(w.project), iso_week: str(w.iso_week),
      event_count: nnum(w.event_count), user_count: nnum(w.user_count), assistant_count: nnum(w.assistant_count),
      tool_count: nnum(w.tool_count), first_ts: str(w.first_ts), last_ts: str(w.last_ts),
    }));
  });
}

/** Every import_runs row (12k rows, ~20ms, cached per version): writers and the run ledger are computed over all history, like the Go handlers. */
async function runRows(r: Replica): Promise<RunRec[]> {
  return cached(r, ["import_runs"], "runs", async () => {
    const t = await tableOf(r, "import_runs");
    const rows = (await t.query().limit(1_000_000).toArray()) as Row[];
    return rows.map(x => ({
      session: str(x.session), project: str(x.project), from_offset: nnum(x.from_offset), to_offset: nnum(x.to_offset),
      lines: nnum(x.lines), inserted: nnum(x.inserted), skipped: nnum(x.skipped),
      host: str(x.host), writer: str(x.writer), created: str(x.created),
    }));
  });
}

// ---------------------------------------------------------------- event scans

/** Rows the console calls conversational: a role, and something to show. */
const CONVERSATIONAL = "role <> '' AND (text <> '' OR (tools <> '[]' AND tools <> ''))";

/**
 * The set of session record ids a project/session filter allows, or null when
 * no such filter is set. Projects and session ids live in other tables, so
 * these are matched here and applied to event rows in JS.
 */
function sessionScope(s: Store, o: { project?: string; projectID?: string; session?: string }): Set<string> | null {
  const project = (o.project ?? "").toLowerCase();
  const pid = o.projectID ?? "";
  const sid = (o.session ?? "").toLowerCase();
  if (!project && !pid && !sid) return null;
  const out = new Set<string>();
  for (const s2 of s.sessions) {
    if (pid && s2.project !== pid) continue;
    if (project && !(s.pathOf.get(s2.id) ?? "").toLowerCase().includes(project)) continue;
    if (sid && !s2.session_id.toLowerCase().startsWith(sid)) continue;
    out.add(s2.id);
  }
  return out;
}

const EVENT_COLS = ["session", "uuid", "ts", "iso_week", "role", "type", "text", "tools", "line_no"];

/**
 * A session scope as a predicate, so Lance does the filtering instead of a JS
 * pass over rows it already read. Beyond SCOPE_PUSHDOWN_MAX ids the IN list is
 * not worth building and the caller keeps the JS filter.
 */
const SCOPE_PUSHDOWN_MAX = 2000;
function scopePred(scope: Set<string> | null): string | null {
  if (!scope) return null;
  if (scope.size === 0) return "session = ''";            // matches nothing
  if (scope.size > SCOPE_PUSHDOWN_MAX) return null;
  return `session IN (${[...scope].map(lit).join(", ")})`;
}

/** `%q%` for a case-insensitive substring match, the Go backend's `LIKE {:q} ESCAPE '\'` semantics. */
function likeContains(column: string, q: string): string {
  const esc = q.toLowerCase().replace(/[\\%_]/g, m => "\\" + m);
  return `lower(${column}) LIKE ${lit("%" + esc + "%")}`;
}
/** Windows tried, newest first, when looking for the most recent N events: 14 days, then 90, then everything. */
const WINDOWS_MS = [14 * DAY_MS, 90 * DAY_MS, 0];

/**
 * The newest `limit` event rows matching `base`, honouring a JS-side session
 * scope and an optional text matcher. Two passes: a narrow (ts, session[, text])
 * scan finds the timestamp of the limit-th eligible row, then only rows at or
 * after it are read with every column. Widening from 14 to 90 days to all time
 * stops as soon as a window holds enough rows.
 */
async function newestEvents(r: Replica, base: string[], scope: Set<string> | null, limit: number, matches?: (text: string) => boolean): Promise<Row[]> {
  const t = await tableOf(r, "events");
  const cols = matches ? ["ts", "session", "text"] : ["ts", "session"];
  let threshold = "";
  for (const w of WINDOWS_MS) {
    const from = w ? stampAt(Date.now() - w) : "";
    const pred = [...base, ...(from ? [`ts >= ${lit(from)}`] : [])].join(" AND ");
    const rows = (await t.query().where(pred).select(cols).limit(1_000_000).toArray()) as Row[];
    const eligible: string[] = [];
    for (const row of rows) {
      if (scope && !scope.has(str(row.session))) continue;
      if (matches && !matches(str(row.text))) continue;
      eligible.push(str(row.ts));
    }
    if (eligible.length === 0 && w) continue;       // nothing here: widen
    eligible.sort().reverse();
    threshold = eligible.length >= limit ? eligible[limit - 1] : from;
    if (eligible.length >= limit || !w) break;      // enough, or the last window
  }
  const pred = [...base, ...(threshold ? [`ts >= ${lit(threshold)}`] : [])].join(" AND ");
  const rows = (await t.query().where(pred).select(EVENT_COLS).limit(1_000_000).toArray()) as Row[];
  const keep = rows.filter(row => (!scope || scope.has(str(row.session))) && (!matches || matches(str(row.text))));
  keep.sort((a, b) => (str(a.ts) < str(b.ts) ? 1 : str(a.ts) > str(b.ts) ? -1 : 0));
  return keep.slice(0, limit);
}

// ---------------------------------------------------------------- endpoints

async function status(r: Replica): Promise<Response> {
  const body = await cached(r, ["projects", "sessions", "events", "session_weeks"], "status", async () => {
    const [pt, st, et, wt] = await Promise.all([tableOf(r, "projects"), tableOf(r, "sessions"), tableOf(r, "events"), tableOf(r, "session_weeks")]);
    const [projects, sessions, events, session_weeks] = await Promise.all([pt.countRows(), st.countRows(), et.countRows(), wt.countRows()]);
    const s = await store(r);
    let last_ingest = "", last_event_ts = "";
    for (const x of s.sessions) {
      if (x.updated > last_ingest) last_ingest = x.updated;
      if (x.last_ts > last_event_ts) last_event_ts = x.last_ts;
    }
    const agg = new Map<string, { week: string; sessions: number; events: number; user_msgs: number }>();
    for (const w of await weekRows(r)) {
      const a = agg.get(w.iso_week) ?? { week: w.iso_week, sessions: 0, events: 0, user_msgs: 0 };
      a.sessions += 1; a.events += w.event_count; a.user_msgs += w.user_count;
      agg.set(w.iso_week, a);
    }
    const weeks = [...agg.values()].sort((a, b) => (a.week < b.week ? 1 : a.week > b.week ? -1 : 0)).slice(0, 16);
    return { projects, sessions, events, session_weeks, last_ingest, last_event_ts, weeks, version: VERSION, tz: TZ };
  });
  return json({ ...body, time: nowRFC3339() });
}

async function projects(r: Replica, url: URL): Promise<Response> {
  const limit = limitOf(url.searchParams.get("limit"), 200, 1000);
  const rows = await cached(r, ["sessions", "projects"], "projects", async () => {
    const s = await store(r);
    const agg = new Map<string, { sessions: number; events: number; last_ts: string }>();
    for (const x of s.sessions) {
      const a = agg.get(x.project) ?? { sessions: 0, events: 0, last_ts: "" };
      a.sessions += 1; a.events += x.event_count;
      if (x.last_ts > a.last_ts) a.last_ts = x.last_ts;
      agg.set(x.project, a);
    }
    return s.projects.map(p => {
      const a = agg.get(p.id) ?? { sessions: 0, events: 0, last_ts: "" };
      return { id: p.id, path: p.path, cwd: p.cwd, encoded_dir: p.encoded_dir, name: p.name, host: p.host, ...a };
    }).sort((a, b) => (a.last_ts < b.last_ts ? 1 : a.last_ts > b.last_ts ? -1 : 0));
  });
  return json({ projects: rows.slice(0, limit) });
}

async function search(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const q = (p.get("q") ?? "").trim();
  const limit = limitOf(p.get("limit"), 30, 200);
  const s = await store(r);
  const scope = sessionScope(s, { project: p.get("project") ?? "", projectID: p.get("project_id") ?? "", session: p.get("session") ?? "" });
  const base = [CONVERSATIONAL];
  if (p.get("role")) base.push(`role = ${lit(p.get("role")!)}`);
  if (p.get("week")) base.push(`iso_week = ${lit(p.get("week")!)}`);

  // Same semantics as the Go handler: a case-insensitive substring match
  // (LIKE %q%) over the scope, newest first. The BM25 index is the admin's
  // tool; the console's search box promises "text contains", and a mid-word
  // fragment that LIKE finds would be invisible to a tokeniser.
  if (q) base.push(likeContains("text", q));
  const pushed = scopePred(scope);
  if (pushed) base.push(pushed);
  const rows = await newestEvents(r, base, pushed ? null : scope, limit);
  const hits = rows.map(row => ({
    session_id: s.byRecord.get(str(row.session))?.session_id ?? "",
    project: s.pathOf.get(str(row.session)) ?? "",
    ts: str(row.ts), iso_week: str(row.iso_week), role: str(row.role), type: str(row.type),
    snippet: cut(row.text, 600), tools: str(row.tools), line_no: nnum(row.line_no), uuid: str(row.uuid),
  }));
  return json({ hits, limit, truncated: hits.length >= limit });
}

/** YYYY-MM-DD of a stored timestamp in Asia/Bangkok, memoised on the "YYYY-MM-DD HH" prefix (≤ 31×24 keys). */
function dayOf(ts: string, memo: Map<string, string>): string {
  const key = ts.slice(0, 13);
  const seen = memo.get(key);
  if (seen !== undefined) return seen;
  const ms = Date.parse(ts.replace(" ", "T"));
  const day = Number.isNaN(ms) ? ts.slice(0, 10) : new Date(ms + TZ_OFFSET_MS).toISOString().slice(0, 10);
  memo.set(key, day);
  return day;
}

async function days(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const from = p.get("from") ?? "", to = p.get("to") ?? "";
  if (!isRealYMD(from)) return bad("days: from must be a real YYYY-MM-DD date");
  if (!isRealYMD(to)) return bad("days: to must be a real YYYY-MM-DD date");
  const limit = limitOf(p.get("limit"), 500, 5000);
  const project = p.get("project") ?? "", pid = p.get("project_id") ?? "";
  const s = await store(r);
  const scope = sessionScope(s, { project, projectID: pid });

  // Fixed +07:00: the local day starts 7h before the UTC day, so day = UTC ts + 7h.
  const start = Date.parse(`${from}T00:00:00.000Z`) - TZ_OFFSET_MS;
  let end = Date.parse(`${to}T00:00:00.000Z`) - TZ_OFFSET_MS + DAY_MS;
  if (end - start > 31 * DAY_MS) end = start + 31 * DAY_MS;
  if (end <= start) return json({ days: [], tz: TZ, truncated: false });

  const key = `days:${from}:${to}:${project}:${pid}`;
  const all = await cached(r, ["events", "sessions", "projects"], key, async () => {
    const t = await tableOf(r, "events");
    const pred = `ts >= ${lit(stampAt(start))} AND ts < ${lit(stampAt(end))} AND role <> ''`;
    const rows = (await t.query().where(pred).select(["session", "ts", "role", "text"]).limit(2_000_000).toArray()) as Row[];
    const memo = new Map<string, string>();
    interface Acc { day: string; session: string; events: number; user_msgs: number; first_ts: string; last_ts: string; preview: string; previewTs: string }
    const acc = new Map<string, Acc>();
    for (const row of rows) {
      const sid = str(row.session);
      if (scope && !scope.has(sid)) continue;
      const ts = str(row.ts);
      const day = dayOf(ts, memo);
      const k = `${day}|${sid}`;
      let a = acc.get(k);
      if (!a) { a = { day, session: sid, events: 0, user_msgs: 0, first_ts: ts, last_ts: ts, preview: "", previewTs: "" }; acc.set(k, a); }
      a.events += 1;
      if (ts < a.first_ts) a.first_ts = ts;
      if (ts > a.last_ts) a.last_ts = ts;
      if (str(row.role) === "user") {
        a.user_msgs += 1;
        const text = str(row.text);
        if (text !== "" && (a.previewTs === "" || ts < a.previewTs)) { a.previewTs = ts; a.preview = cut(text, 200); }
      }
    }
    return [...acc.values()]
      .map(a => ({
        day: a.day,
        session_id: s.byRecord.get(a.session)?.session_id ?? "",
        project: s.pathOf.get(a.session) ?? "",
        events: a.events, user_msgs: a.user_msgs, first_ts: a.first_ts, last_ts: a.last_ts,
        preview: a.preview, git_branch: s.byRecord.get(a.session)?.git_branch ?? "",
      }))
      // day DESC, then last_ts DESC, the Go handler's order (most recently active session first)
      .sort((x, y) => (x.day !== y.day ? (x.day < y.day ? 1 : -1) : x.last_ts < y.last_ts ? 1 : x.last_ts > y.last_ts ? -1 : 0));
  });
  return json({ days: all.slice(0, limit), tz: TZ, truncated: all.length > limit });
}

/** Exact session_id, else the one session it is a prefix of. */
function resolveSession(s: Store, prefix: string): { id: string } | { error: string; status: number } {
  // case-insensitive, like the SQLite LIKE the Go handler resolves with
  const want = prefix.trim().toLowerCase();
  if (!want) return { error: "no such session", status: 404 };
  const exact = s.sessions.find(x => x.session_id.toLowerCase() === want);
  if (exact) return { id: exact.id };
  const matches = s.sessions.filter(x => x.session_id.toLowerCase().startsWith(want));
  if (matches.length === 0) return { error: "no such session", status: 404 };
  if (matches.length > 1) return { error: "session id prefix is ambiguous, give more characters", status: 400 };
  return { id: matches[0].id };
}

async function read(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const want = p.get("session") ?? "";
  if (!want) return bad("session query param required");
  const limit = limitOf(p.get("limit"), 100, 500);
  const offset = Math.max(0, Math.floor(Number(p.get("offset")) || 0));
  const s = await store(r);
  const found = resolveSession(s, want);
  if ("error" in found) return bad(found.error, found.status);
  const rows = await cached(r, ["events"], `read:${found.id}`, async () => {
    const t = await tableOf(r, "events");
    const raw = (await t.query()
      .where(`session = ${lit(found.id)} AND ${CONVERSATIONAL}`)
      .select(["ts", "role", "type", "text", "tools", "line_no"])
      .limit(1_000_000).toArray()) as Row[];
    return raw
      .map(x => ({ ts: str(x.ts), role: str(x.role), type: str(x.type), text: cut(x.text, 4000), tools: str(x.tools), line_no: nnum(x.line_no) }))
      .sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.line_no - b.line_no));
  });
  return json({ events: rows.slice(offset, offset + limit), offset });
}

async function sessions(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const limit = limitOf(p.get("limit"), 50, 500);
  const week = p.get("week") ?? "";
  const s = await store(r);
  const scope = sessionScope(s, { project: p.get("project") ?? "", projectID: p.get("project_id") ?? "" });
  let inWeek: Set<string> | null = null;
  if (week) inWeek = new Set((await weekRows(r)).filter(w => w.iso_week === week).map(w => w.session));
  const rows = s.sessions
    .filter(x => (!scope || scope.has(x.id)) && (!inWeek || inWeek.has(x.id)))
    .sort((a, b) => (a.last_ts < b.last_ts ? 1 : a.last_ts > b.last_ts ? -1 : 0))
    .slice(0, limit)
    .map(x => ({
      session_id: x.session_id, project: s.pathOf.get(x.id) ?? "", tier: x.tier,
      first_ts: x.first_ts, last_ts: x.last_ts, event_count: x.event_count,
      first_prompt: cut(x.first_prompt, 300), git_branch: x.git_branch, file_path: x.file_path,
    }));
  return json({ sessions: rows });
}

async function weeks(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const limit = limitOf(p.get("limit"), 100, 1000);
  const week = p.get("week") ?? "";
  const prefix = (p.get("session") ?? "").toLowerCase();
  const s = await store(r);
  const rows = (await weekRows(r))
    .filter(w => (!week || w.iso_week === week) && s.byRecord.has(w.session))
    .filter(w => !prefix || (s.byRecord.get(w.session)?.session_id ?? "").toLowerCase().startsWith(prefix))
    .sort((a, b) => (a.iso_week !== b.iso_week ? (a.iso_week < b.iso_week ? 1 : -1) : a.last_ts < b.last_ts ? 1 : a.last_ts > b.last_ts ? -1 : 0))
    .slice(0, limit)
    .map(w => {
      const proj = s.byProject.get(w.project);
      return {
        iso_week: w.iso_week,
        session_id: s.byRecord.get(w.session)?.session_id ?? "",
        project: proj ? (proj.cwd || proj.path) : (s.pathOf.get(w.session) ?? ""),
        events: w.event_count, user_msgs: w.user_count, assistant_msgs: w.assistant_count, tool_calls: w.tool_count,
        first_ts: w.first_ts, last_ts: w.last_ts,
      };
    });
  return json({ weeks: rows });
}

async function intake(r: Replica, url: URL): Promise<Response> {
  const p = url.searchParams;
  const runsLimit = limitOf(p.get("limit"), 100, 500);
  const filesLimit = limitOf(p.get("limit"), 100, 1000);
  const pendingOnly = p.get("pending") === "1";
  const s = await store(r);
  const since = stampAt(Date.now() - DAY_MS);
  const allRuns = await runRows(r);
  const runs24 = allRuns.filter(x => x.created >= since);

  let bytes_tracked = 0, bytes_indexed = 0, pending_files = 0, pending_bytes = 0, last_ingest = "";
  for (const x of s.sessions) {
    bytes_tracked += x.file_size;
    bytes_indexed += x.byte_offset;
    if (x.file_size > x.byte_offset) { pending_files += 1; pending_bytes += x.file_size - x.byte_offset; }
    if (x.updated > last_ingest) last_ingest = x.updated;
  }
  let inserted_today = 0;
  const hosts = new Set<string>();
  for (const x of runs24) inserted_today += x.inserted;
  for (const p2 of s.projects) if (p2.host) hosts.add(p2.host);

  const runs = allRuns
    .filter(x => s.byRecord.has(x.session))
    .sort((a, b) => (a.created < b.created ? 1 : a.created > b.created ? -1 : 0))
    .slice(0, runsLimit)
    .map(x => {
      const sess = s.byRecord.get(x.session)!;
      return {
        created: x.created, session_id: sess.session_id, project: s.pathOf.get(x.session) ?? "", file_path: sess.file_path,
        from_offset: x.from_offset, to_offset: x.to_offset, lines: x.lines,
        inserted: x.inserted, skipped: x.skipped, host: x.host, writer: x.writer,
      };
    });

  const files = s.sessions
    .filter(x => !pendingOnly || x.file_size > x.byte_offset)
    .sort((a, b) => (a.updated < b.updated ? 1 : a.updated > b.updated ? -1 : 0))
    .slice(0, filesLimit)
    .map(x => ({
      session_id: x.session_id, project: s.pathOf.get(x.id) ?? "", file_path: x.file_path, tier: x.tier,
      file_size: x.file_size, byte_offset: x.byte_offset, lines_seen: x.lines_seen,
      event_count: x.event_count, file_mtime: x.file_mtime, updated: x.updated,
    }));

  const wagg = new Map<string, { host: string; writer: string; last_run: string; runs_24h: number; inserted_24h: number; files: Set<string> }>();
  // every (host, writer) that ever wrote, like Go's ListWriters; the 24h counters are a window on top
  for (const x of allRuns) {
    const k = `${x.host}|${x.writer}`;
    const a = wagg.get(k) ?? { host: x.host, writer: x.writer, last_run: "", runs_24h: 0, inserted_24h: 0, files: new Set<string>() };
    if (x.created >= since) { a.runs_24h += 1; a.inserted_24h += x.inserted; }
    a.files.add(x.session);
    if (x.created > a.last_run) a.last_run = x.created;
    wagg.set(k, a);
  }
  const writers = [...wagg.values()]
    .sort((a, b) => (a.last_run < b.last_run ? 1 : a.last_run > b.last_run ? -1 : 0))
    .map(a => ({ host: a.host, writer: a.writer, last_run: a.last_run, runs_24h: a.runs_24h, inserted_24h: a.inserted_24h, files: a.files.size }));

  return json({
    summary: {
      files: s.sessions.length, bytes_tracked, bytes_indexed, pending_files, pending_bytes,
      last_ingest, runs_today: runs24.length, inserted_today, hosts: [...hosts].sort().join(","),
    },
    runs, files, writers,
    // Go's keys; the replica has no OAuth tables, so both are 0
    connections: { oauth_clients: 0, active_tokens: 0 },
    scan_dir: "", scan_interval: "",
    // Go's keys, which the console reads without guarding.
    server_scan: { enabled: false, dir: "", interval: "" },
    upload_dir: "", host: r.target.name, tz: TZ, superuser: true,
  });
}

// ---------------------------------------------------------------- realtime proxy

const SSE_HEADERS = { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive", "x-accel-buffering": "no" };

/** Open the target's SSE stream and hand its body straight back; abort upstream when the browser goes away. */
async function realtimeGet(r: Replica, req: Request): Promise<Response> {
  const ctrl = new AbortController();
  const stop = () => ctrl.abort();
  req.signal.addEventListener("abort", stop, { once: true });
  try {
    const upstream = await fetch(`${r.target.url}/api/realtime`, { headers: { Accept: "text/event-stream" }, signal: ctrl.signal });
    if (!upstream.ok || !upstream.body) { ctrl.abort(); return bad(`realtime upstream ${upstream.status}`, 502); }
    return new Response(upstream.body, { status: 200, headers: SSE_HEADERS });
  } catch (e) {
    ctrl.abort();
    return bad(`realtime upstream: ${(e as Error).message}`, 502);
  }
}

/** Forward a subscribe body with the replica's own PocketBase credentials, once retried on a stale token. */
async function realtimePost(r: Replica, req: Request): Promise<Response> {
  const body = await req.text();
  const send = async (token: string) => fetch(`${r.target.url}/api/realtime`, {
    method: "POST",
    headers: { "content-type": "application/json", Authorization: token },
    body,
  });
  try {
    let upstream = await send(await r.pb.bearer());
    if (upstream.status === 401) {
      r.pb.invalidate();
      upstream = await send(await r.pb.bearer());
    }
    const text = await upstream.text();
    if (upstream.status === 204 || upstream.status === 205 || upstream.status === 304) return new Response(null, { status: upstream.status });
    return new Response(text, { status: upstream.status, headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" } });
  } catch (e) {
    return bad(`realtime upstream: ${(e as Error).message}`, 502);
  }
}

// ---------------------------------------------------------------- dispatch

const WRITE_PATHS = new Set(["structor/scan", "structor/reconcile", "structor/upload", "structor/ingest"]);

export const facade: Facade = {
  async handle(r, path, req, url) {
    const p = path.replace(/^\/+/, "");
    try {
      if (p === "collections/_superusers/auth-with-password") {
        if (req.method !== "POST") return bad("method not allowed", 405);
        let identity = "", password = "";
        try {
          const body = (await req.json()) as { identity?: unknown; password?: unknown };
          identity = typeof body.identity === "string" ? body.identity : "";
          password = typeof body.password === "string" ? body.password : "";
        } catch {
          return bad("invalid login body");
        }
        // Compared against the target's own admin credentials; neither side is ever logged or echoed.
        if (!sameSecret(identity, r.target.email) || !sameSecret(password, r.target.password)) {
          return bad("Failed to authenticate.");
        }
        return json({ token: issue(r.target.name) });
      }

      // EventSource cannot send Authorization, and the stream is inert until a
      // POST (token required) subscribes it; see the note in the header.
      // `return await`, not `return`: a rejected promise must land in the
      // catch below and become {error} JSON, not Bun's HTML error page
      if (p === "realtime" && req.method === "GET") return await realtimeGet(r, req);

      if (!authorized(r, req)) return bad("unauthorized", 401);

      if (p === "realtime") {
        if (req.method !== "POST") return bad("method not allowed", 405);
        return await realtimePost(r, req);
      }
      if (WRITE_PATHS.has(p)) {
        return bad(`${p} is not available on the LanceDB backend; use the PocketBase console`, 405);
      }
      if (req.method !== "GET") return bad("method not allowed", 405);

      switch (p) {
        case "structor/status": return await status(r);
        case "structor/projects": return await projects(r, url);
        case "structor/search": return await search(r, url);
        case "structor/days": return await days(r, url);
        case "structor/read": return await read(r, url);
        case "structor/sessions": return await sessions(r, url);
        case "structor/weeks": return await weeks(r, url);
        case "structor/intake": return await intake(r, url);
        case "structor/state": return bad("tail state lives in PocketBase; the LanceDB replica does not serve it", 404);
        default: return bad(`not found: ${p}`, 404);
      }
    } catch (e) {
      return bad(String((e as Error)?.message ?? e), 500);
    }
  },
};
