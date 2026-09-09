// facade.ts against a seeded replica: the console's API, answered from Lance.
//
// The fixture is small but shaped like the real store — two projects, four
// sessions (two of them sharing a prefix so resolution has something to get
// wrong), events on both sides of a Bangkok midnight, a week ledger and an
// import log. Credentials here are fixture values, not secrets.

import { test, expect, afterAll } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Index, makeArrowTable } from "@lancedb/lancedb";
import { Replica, TABLES, project, type TableSpec } from "../src/sync.ts";
import { facade } from "../src/facade.ts";

const EMAIL = "admin@unit.local";
const PASSWORD = "unit-fixture-password";

const root = mkdtempSync(join(tmpdir(), "structor-lance-facade-"));
const replica = new Replica({ name: "unit", url: "http://127.0.0.1:1", email: EMAIL, password: PASSWORD }, root);
const other = new Replica({ name: "other", url: "http://127.0.0.1:1", email: EMAIL, password: PASSWORD }, root);
afterAll(() => rmSync(root, { recursive: true, force: true }));

const spec = (n: TableSpec["name"]) => TABLES.find(t => t.name === n)!;
async function seed(name: TableSpec["name"], rows: Record<string, unknown>[]) {
  const s = spec(name);
  const t = await replica.table(s);
  await t.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll()
    .execute(makeArrowTable(rows.map(r => project(s, r as never)), { schema: s.schema }));
}

// ---- fixture timestamps: two days back, so every "last 14 days" window holds them
const ymd = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const stampAt = (ms: number) => new Date(ms).toISOString().replace("T", " ");
const T0 = Date.now() - 2 * 86_400_000;
const D = ymd(T0);                     // UTC day; 16:59Z on it is still this day in Bangkok
const D1 = ymd(T0 + 86_400_000);       // 17:00Z on D is already this day in Bangkok (+07:00)
const at = (day: string, hhmmss: string) => `${day} ${hhmmss}.000Z`;

await seed("projects", [
  { id: "p1", created: at(D, "00:00:00"), updated: at(D, "00:00:00"), path: "/opt/Code/alpha-guess", cwd: "/opt/Code/alpha", name: "alpha", encoded_dir: "-opt-Code-alpha", host: "m5" },
  { id: "p2", created: at(D, "00:00:00"), updated: at(D, "00:00:00"), path: "/opt/Code/beta", cwd: "", name: "beta", encoded_dir: "-opt-Code-beta", host: "kvmlab1" },
]);

await seed("sessions", [
  { id: "s1", created: at(D, "16:00:00"), updated: at(D1, "17:10:00"), session_id: "abc111", project: "p1", file_path: "/t/abc111.jsonl", tier: "projects", byte_offset: 400, file_size: 400, file_mtime: 1, lines_seen: 5, event_count: 5, first_ts: at(D, "16:59:00"), last_ts: at(D, "19:00:00"), first_prompt: "late night alpha", git_branch: "main", cwd: "/opt/Code/alpha" },
  { id: "s2", created: at(D, "05:00:00"), updated: at(D, "05:10:00"), session_id: "abc222", project: "p2", file_path: "/t/abc222.jsonl", tier: "subagents", byte_offset: 150, file_size: 200, file_mtime: 1, lines_seen: 2, event_count: 1, first_ts: at(D, "05:00:00"), last_ts: at(D, "05:00:00"), first_prompt: "beta project note", git_branch: "", cwd: "/opt/Code/beta" },
  { id: "s3", created: at(D, "04:00:00"), updated: at(D, "04:10:00"), session_id: "abc1110", project: "p1", file_path: "/t/abc1110.jsonl", tier: "projects", byte_offset: 10, file_size: 10, file_mtime: 1, lines_seen: 1, event_count: 1, first_ts: at(D, "04:00:00"), last_ts: at(D, "04:00:00"), first_prompt: "sibling that shares abc111", git_branch: "", cwd: "/opt/Code/alpha" },
  { id: "s4", created: at(D, "03:00:00"), updated: at(D, "03:10:00"), session_id: "zzz999", project: "p1", file_path: "/t/zzz999.jsonl", tier: "backup", byte_offset: 0, file_size: 0, file_mtime: 1, lines_seen: 0, event_count: 0, first_ts: "", last_ts: "", first_prompt: "", git_branch: "", cwd: "" },
]);

await seed("events", [
  // s1, around the Bangkok day boundary (17:00Z)
  { id: "e1", created: at(D, "16:59:01"), session: "s1", uuid: "u1", ts: at(D, "16:59:00"), iso_week: "2026-W37", role: "user", type: "user", text: "late night alpha", tools: "[]", line_no: 1 },
  { id: "e2", created: at(D, "17:00:01"), session: "s1", uuid: "u2", ts: at(D, "17:00:00"), iso_week: "2026-W37", role: "user", type: "user", text: "midnight crossing", tools: "[]", line_no: 2 },
  { id: "e3", created: at(D, "17:05:01"), session: "s1", uuid: "u3", ts: at(D, "17:05:00"), iso_week: "2026-W37", role: "assistant", type: "assistant", text: "answer about lancedb replicas", tools: "[]", line_no: 3 },
  { id: "e4", created: at(D, "18:00:01"), session: "s1", uuid: "u4", ts: at(D, "18:00:00"), iso_week: "2026-W37", role: "", type: "hook", text: "hook attachment", tools: "[]", line_no: 4 },
  { id: "e5", created: at(D, "19:00:01"), session: "s1", uuid: "u5", ts: at(D, "19:00:00"), iso_week: "2026-W37", role: "assistant", type: "assistant", text: "", tools: '["Bash","Read"]', line_no: 5 },
  // s2, earlier the same UTC day
  { id: "e6", created: at(D, "05:00:01"), session: "s2", uuid: "u6", ts: at(D, "05:00:00"), iso_week: "2026-W37", role: "user", type: "user", text: "beta project note", tools: "[]", line_no: 1 },
  // s3, the prefix sibling
  { id: "e7", created: at(D, "04:00:01"), session: "s3", uuid: "u7", ts: at(D, "04:00:00"), iso_week: "2026-W37", role: "user", type: "user", text: "sibling that shares abc111", tools: "[]", line_no: 1 },
]);

await seed("session_weeks", [
  { id: "w1", created: at(D, "20:00:00"), updated: at(D, "20:00:00"), session: "s1", project: "p1", iso_week: "2026-W37", event_count: 4, user_count: 2, assistant_count: 2, tool_count: 1, first_ts: at(D, "16:59:00"), last_ts: at(D, "19:00:00") },
  { id: "w2", created: at(D, "20:00:00"), updated: at(D, "20:00:00"), session: "s2", project: "p2", iso_week: "2026-W37", event_count: 1, user_count: 1, assistant_count: 0, tool_count: 0, first_ts: at(D, "05:00:00"), last_ts: at(D, "05:00:00") },
  { id: "w3", created: at(D, "20:00:00"), updated: at(D, "20:00:00"), session: "s3", project: "p1", iso_week: "2026-W36", event_count: 1, user_count: 1, assistant_count: 0, tool_count: 0, first_ts: at(D, "04:00:00"), last_ts: at(D, "04:00:00") },
]);

const hourAgo = stampAt(Date.now() - 3_600_000);
const twoHoursAgo = stampAt(Date.now() - 7_200_000);
await seed("import_runs", [
  { id: "r1", created: twoHoursAgo, session: "s1", project: "p1", from_offset: 0, to_offset: 200, lines: 3, inserted: 3, skipped: 0, host: "m5", writer: "cli" },
  { id: "r2", created: hourAgo, session: "s1", project: "p1", from_offset: 200, to_offset: 400, lines: 2, inserted: 2, skipped: 1, host: "m5", writer: "cli" },
  { id: "r3", created: hourAgo, session: "s2", project: "p2", from_offset: 0, to_offset: 150, lines: 1, inserted: 1, skipped: 0, host: "kvmlab1", writer: "server-scan" },
  { id: "r4", created: stampAt(Date.now() - 40 * 3_600_000), session: "s2", project: "p2", from_offset: 0, to_offset: 0, lines: 0, inserted: 9, skipped: 0, host: "old", writer: "cli" },
]);

// ---- calling the facade the way admin.ts does: path without query, url with it

interface Opts { method?: string; token?: string; query?: Record<string, string>; body?: unknown }
function call(path: string, o: Opts = {}) {
  const url = new URL(`http://127.0.0.1/console/unit/api/${path}`);
  for (const [k, v] of Object.entries(o.query ?? {})) url.searchParams.set(k, v);
  const headers: Record<string, string> = {};
  if (o.token) headers.Authorization = o.token;
  if (o.body !== undefined) headers["content-type"] = "application/json";
  const req = new Request(url, { method: o.method ?? "GET", headers, body: o.body === undefined ? undefined : JSON.stringify(o.body) });
  return facade.handle(replica, path, req, url);
}
async function get(path: string, query?: Record<string, string>) {
  const r = await call(path, { token, query });
  const body = await r.json();
  return { status: r.status, body: body as Record<string, unknown> };
}

const login = await call("collections/_superusers/auth-with-password", { method: "POST", body: { identity: EMAIL, password: PASSWORD } });
const token = ((await login.json()) as { token: string }).token;

test("login answers a token for the target's credentials and 400 for anything else", async () => {
  expect(login.status).toBe(200);
  expect(token.startsWith("lance.")).toBe(true);
  const wrongPw = await call("collections/_superusers/auth-with-password", { method: "POST", body: { identity: EMAIL, password: "nope" } });
  expect(wrongPw.status).toBe(400);
  expect((await wrongPw.json()).error).toBeTruthy();
  const wrongUser = await call("collections/_superusers/auth-with-password", { method: "POST", body: { identity: "someone@else", password: PASSWORD } });
  expect(wrongUser.status).toBe(400);
  const notJson = await call("collections/_superusers/auth-with-password", { method: "POST" });
  expect(notJson.status).toBe(400);
  expect((await call("collections/_superusers/auth-with-password")).status).toBe(405);
});

test("no token, a foreign token and another target's token are all 401", async () => {
  expect((await call("structor/status")).status).toBe(401);
  expect((await call("structor/status", { token: "lance.not-a-token" })).status).toBe(401);
  const url = new URL("http://127.0.0.1/console/other/api/structor/status");
  const r = await facade.handle(other, "structor/status", new Request(url, { headers: { Authorization: token } }), url);
  expect(r.status).toBe(401);
  expect((await r.json()).error).toBe("unauthorized");
});

test("status counts every table and folds the week ledger", async () => {
  const { status, body } = await get("structor/status");
  expect(status).toBe(200);
  expect(body).toMatchObject({ projects: 2, sessions: 4, events: 7, session_weeks: 3, tz: "Asia/Bangkok" });
  expect(body.last_ingest).toBe(at(D1, "17:10:00"));      // max(sessions.updated)
  expect(body.last_event_ts).toBe(at(D, "19:00:00"));     // max(sessions.last_ts)
  expect(typeof body.version).toBe("string");
  expect(String(body.time)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  const weeks = body.weeks as unknown as { week: string; sessions: number; events: number; user_msgs: number }[];
  expect(weeks.map(w => w.week)).toEqual(["2026-W37", "2026-W36"]);   // newest first
  expect(weeks[0]).toEqual({ week: "2026-W37", sessions: 2, events: 5, user_msgs: 3 });
});

test("projects carry their session and event totals, newest activity first", async () => {
  const { body } = await get("structor/projects");
  const rows = body.projects as unknown as { id: string; path: string; cwd: string; encoded_dir: string; name: string; host: string; sessions: number; events: number; last_ts: string }[];
  expect(rows.map(p => p.id)).toEqual(["p1", "p2"]);
  expect(rows[0]).toEqual({ id: "p1", path: "/opt/Code/alpha-guess", cwd: "/opt/Code/alpha", encoded_dir: "-opt-Code-alpha", name: "alpha", host: "m5", sessions: 3, events: 6, last_ts: at(D, "19:00:00") });
  expect(rows[1].sessions).toBe(1);
  const one = await get("structor/projects", { limit: "1" });
  expect((one.body.projects as unknown as unknown[]).length).toBe(1);
});

test("search without q is a newest-first window over conversational rows only", async () => {
  const { body } = await get("structor/search");
  const hits = body.hits as unknown as { ts: string; role: string; session_id: string; project: string; snippet: string; tools: string; uuid: string; line_no: number }[];
  expect(hits.map(h => h.uuid)).toEqual(["u5", "u3", "u2", "u1", "u6", "u7"]);   // ts DESC, hook row (role '') dropped
  expect(hits[0].tools).toBe('["Bash","Read"]');                                  // kept: no text but tools
  expect(hits[0].project).toBe("/opt/Code/alpha");                                // cwd wins over the decoded guess
  expect(hits.find(h => h.session_id === "abc222")!.project).toBe("/opt/Code/beta"); // no cwd: the guess
  expect(body.limit).toBe(30);
  expect(body.truncated).toBe(false);
  const capped = await get("structor/search", { limit: "2" });
  expect((capped.body.hits as unknown as unknown[]).length).toBe(2);
  expect(capped.body.truncated).toBe(true);
  const byRole = await get("structor/search", { role: "user" });
  expect((byRole.body.hits as unknown as { role: string }[]).every(h => h.role === "user")).toBe(true);
  const byProject = await get("structor/search", { project_id: "p2" });
  expect((byProject.body.hits as unknown as { uuid: string }[]).map(h => h.uuid)).toEqual(["u6"]);
  const byWeek = await get("structor/search", { week: "2026-W99" });
  expect(body.hits).toBeTruthy();
  expect((byWeek.body.hits as unknown as unknown[]).length).toBe(0);
});

test("search with q uses the full-text index and still honours the filters", async () => {
  const t = await replica.table(spec("events"));
  await t.createIndex("text", { config: Index.fts(), replace: true });
  const { body } = await get("structor/search", { q: "lancedb" });
  const hits = body.hits as unknown as { uuid: string; snippet: string }[];
  expect(hits.map(h => h.uuid)).toEqual(["u3"]);
  expect(hits[0].snippet).toBe("answer about lancedb replicas");
  const scoped = await get("structor/search", { q: "lancedb", project_id: "p2" });
  expect((scoped.body.hits as unknown as unknown[]).length).toBe(0);
  const many = await get("structor/search", { q: "abc111" });   // matches the sibling session's text
  expect((many.body.hits as unknown as { uuid: string }[]).map(h => h.uuid)).toEqual(["u7"]);
});

test("days groups by Bangkok day, so 17:00Z belongs to the next day", async () => {
  const { body } = await get("structor/days", { from: D, to: D1 });
  expect(body.tz).toBe("Asia/Bangkok");
  expect(body.truncated).toBe(false);
  const rows = body.days as unknown as { day: string; session_id: string; events: number; user_msgs: number; first_ts: string; last_ts: string; preview: string; git_branch: string; project: string }[];
  expect(rows.map(r => `${r.day}/${r.session_id}`)).toEqual([`${D1}/abc111`, `${D}/abc111`, `${D}/abc222`, `${D}/abc1110`]);
  const crossed = rows[0];
  expect(crossed.events).toBe(3);              // 17:00, 17:05 and the tools-only 19:00 row
  expect(crossed.user_msgs).toBe(1);
  expect(crossed.first_ts).toBe(at(D, "17:00:00"));
  expect(crossed.last_ts).toBe(at(D, "19:00:00"));
  expect(crossed.preview).toBe("midnight crossing");
  expect(crossed.git_branch).toBe("main");
  expect(crossed.project).toBe("/opt/Code/alpha");
  const before = rows.find(r => r.day === D && r.session_id === "abc111")!;
  expect(before.events).toBe(1);               // only 16:59Z; the hook row has no role
  expect(before.preview).toBe("late night alpha");
  const oneProject = await get("structor/days", { from: D, to: D1, project_id: "p2" });
  expect((oneProject.body.days as unknown as { session_id: string }[]).map(r => r.session_id)).toEqual(["abc222"]);
  const byPath = await get("structor/days", { from: D, to: D1, project: "alpha" });
  expect((byPath.body.days as unknown as { project: string }[]).every(r => r.project === "/opt/Code/alpha")).toBe(true);
  const cut = await get("structor/days", { from: D, to: D1, limit: "1" });
  expect((cut.body.days as unknown as unknown[]).length).toBe(1);
  expect(cut.body.truncated).toBe(true);
  expect((await get("structor/days", { from: "nope", to: D1 })).status).toBe(400);
  expect((await get("structor/days", { to: D1 })).status).toBe(400);
});

test("read resolves a prefix, pages in time order and reports the bad cases", async () => {
  const { body } = await get("structor/read", { session: "abc1110" });
  const rows = body.events as unknown as { ts: string; role: string; type: string; text: string; tools: string; line_no: number }[];
  expect(rows.length).toBe(1);
  expect(rows[0]).toEqual({ ts: at(D, "04:00:00"), role: "user", type: "user", text: "sibling that shares abc111", tools: "[]", line_no: 1 });

  const exact = await get("structor/read", { session: "abc111" });   // also a prefix of abc1110: the exact id wins
  const s1rows = exact.body.events as unknown as { line_no: number }[];
  expect(s1rows.map(r => r.line_no)).toEqual([1, 2, 3, 5]);          // ts ASC, the role-less hook row dropped
  expect(exact.body.offset).toBe(0);

  const page = await get("structor/read", { session: "abc111", offset: "2", limit: "1" });
  expect((page.body.events as unknown as { line_no: number }[]).map(r => r.line_no)).toEqual([3]);
  expect(page.body.offset).toBe(2);

  const ambiguous = await get("structor/read", { session: "abc" });
  expect(ambiguous.status).toBe(400);
  expect(String(ambiguous.body.error)).toContain("ambiguous");
  expect((await get("structor/read", { session: "nothing-like-this" })).status).toBe(404);
  expect((await get("structor/read")).status).toBe(400);
});

test("sessions lists newest-last_ts first and filters by project and week", async () => {
  const { body } = await get("structor/sessions");
  const rows = body.sessions as unknown as { session_id: string; project: string; tier: string; first_prompt: string; file_path: string; event_count: number }[];
  expect(rows.map(r => r.session_id)).toEqual(["abc111", "abc222", "abc1110", "zzz999"]);
  expect(rows[0]).toMatchObject({ project: "/opt/Code/alpha", tier: "projects", first_prompt: "late night alpha", file_path: "/t/abc111.jsonl", event_count: 5 });
  const p2 = await get("structor/sessions", { project_id: "p2" });
  expect((p2.body.sessions as unknown as { session_id: string }[]).map(r => r.session_id)).toEqual(["abc222"]);
  const w36 = await get("structor/sessions", { week: "2026-W36" });
  expect((w36.body.sessions as unknown as { session_id: string }[]).map(r => r.session_id)).toEqual(["abc1110"]);
  const one = await get("structor/sessions", { limit: "1" });
  expect((one.body.sessions as unknown as unknown[]).length).toBe(1);
});

test("weeks serves the ledger rows with Go's field names", async () => {
  const { body } = await get("structor/weeks");
  const rows = body.weeks as unknown as { iso_week: string; session_id: string; project: string; events: number; user_msgs: number; assistant_msgs: number; tool_calls: number; first_ts: string; last_ts: string }[];
  expect(rows.map(w => `${w.iso_week}/${w.session_id}`)).toEqual(["2026-W37/abc111", "2026-W37/abc222", "2026-W36/abc1110"]);
  expect(rows[0]).toEqual({ iso_week: "2026-W37", session_id: "abc111", project: "/opt/Code/alpha", events: 4, user_msgs: 2, assistant_msgs: 2, tool_calls: 1, first_ts: at(D, "16:59:00"), last_ts: at(D, "19:00:00") });
  const filtered = await get("structor/weeks", { week: "2026-W36" });
  expect((filtered.body.weeks as unknown as unknown[]).length).toBe(1);
  const bySession = await get("structor/weeks", { session: "abc222" });
  expect((bySession.body.weeks as unknown as { session_id: string }[]).map(w => w.session_id)).toEqual(["abc222"]);
});

test("intake summarises tail state, the import log, the tracked files and the writers", async () => {
  const { body } = await get("structor/intake");
  expect(body.summary).toEqual({
    files: 4, bytes_tracked: 610, bytes_indexed: 560, pending_files: 1, pending_bytes: 50,
    last_ingest: at(D1, "17:10:00"), runs_today: 3, inserted_today: 6, hosts: "kvmlab1,m5",
  });
  const runs = body.runs as unknown as { created: string; session_id: string; project: string; file_path: string; from_offset: number; to_offset: number; lines: number; inserted: number; skipped: number; host: string; writer: string }[];
  expect(runs.length).toBe(4);                                  // all history, like Go's ListRuns (the 40h-old row included)
  expect(runs[0].created >= runs[1].created).toBe(true);        // newest first
  expect(runs.find(r => r.session_id === "abc222")).toMatchObject({ project: "/opt/Code/beta", file_path: "/t/abc222.jsonl", writer: "server-scan", host: "kvmlab1" });
  const files = body.files as unknown as { session_id: string; file_size: number; byte_offset: number; updated: string }[];
  expect(files.map(f => f.session_id)).toEqual(["abc111", "abc222", "abc1110", "zzz999"]);   // updated DESC
  const pending = await get("structor/intake", { pending: "1" });
  expect((pending.body.files as unknown as { session_id: string }[]).map(f => f.session_id)).toEqual(["abc222"]);
  const writers = body.writers as unknown as { host: string; writer: string; runs_24h: number; inserted_24h: number; files: number; last_run: string }[];
  // every writer that ever wrote (Go's ListWriters), the quiet "old" host included, with 24h counters at 0
  expect(writers.map(w => `${w.host}/${w.writer}`).sort()).toEqual(["kvmlab1/server-scan", "m5/cli", "old/cli"]);
  expect(writers.find(w => w.host === "old")).toMatchObject({ runs_24h: 0, inserted_24h: 0, files: 1 });
  const cli = writers.find(w => w.writer === "cli")!;
  expect(cli).toMatchObject({ host: "m5", runs_24h: 2, inserted_24h: 5, files: 1, last_run: hourAgo });
  expect(body.connections).toEqual({ oauth_clients: 0, active_tokens: 0 });   // Go's keys
  // keys the console reads without guarding
  expect(body.server_scan).toEqual({ enabled: false, dir: "", interval: "" });
  expect(body.tz).toBe("Asia/Bangkok");
  expect(typeof body.host).toBe("string");
  expect(typeof body.upload_dir).toBe("string");
});

test("writes, tail state and unknown paths are refused with an error body", async () => {
  for (const p of ["structor/scan", "structor/reconcile", "structor/upload", "structor/ingest"]) {
    const r = await call(p, { method: "POST", token });
    expect(r.status).toBe(405);
    expect(String((await r.json()).error)).toContain("PocketBase console");
    expect((await call(p, { method: "POST" })).status).toBe(401);   // auth is checked first
  }
  const state = await get("structor/state", { path: "/t/abc111.jsonl" });
  expect(state.status).toBe(404);
  expect(state.body.error).toBeTruthy();
  const unknown = await get("structor/nope");
  expect(unknown.status).toBe(404);
  const post = await call("structor/status", { method: "POST", token });
  expect(post.status).toBe(405);
});

test("realtime GET is open (EventSource cannot authenticate) and POST is not", async () => {
  const post = await call("realtime", { method: "POST", body: { clientId: "x", subscriptions: [] } });
  expect(post.status).toBe(401);
  // the fixture target is a closed port: the proxy reports the upstream failure rather than 401
  const stream = await call("realtime");
  expect(stream.status).toBe(502);
  expect(String((await stream.json()).error)).toContain("realtime upstream");
});
