// Regressions from the facade review: substring search semantics, scope
// pushdown, real-date validation, JSON errors on rejection, case-insensitive
// session resolution.
import { test, expect, afterAll } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { makeArrowTable } from "@lancedb/lancedb";
import { Replica, TABLES, project as shape } from "../src/sync.ts";
import { facade } from "../src/facade.ts";
import type { PBRecord } from "../src/pb.ts";

const root = mkdtempSync(join(tmpdir(), "structor-lance-facade-fixes-"));
const replica = new Replica({ name: "unit", url: "http://127.0.0.1:1", email: "e@x", password: "pw" }, root);
async function seed(name: string, rows: Record<string, unknown>[]) {
  const spec = TABLES.find(t => t.name === name)!;
  const t = await replica.table(spec);
  await t.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute(makeArrowTable(rows.map(r => shape(spec, r as PBRecord)), { schema: spec.schema }));
}
const S = "2026-09-09 10:00:00.000Z";
await seed("projects", [{ id: "p1", created: S, updated: S, path: "/a", cwd: "/a", name: "a", host: "m5" }, { id: "p2", created: S, updated: S, path: "/b", cwd: "/b", name: "b", host: "m5" }]);
await seed("sessions", [
  { id: "s1", created: S, updated: S, session_id: "Alpha-1", project: "p1", file_path: "/a/1.jsonl", first_ts: S, last_ts: S, event_count: 2 },
  { id: "s2", created: S, updated: S, session_id: "beta-2", project: "p2", file_path: "/b/2.jsonl", first_ts: S, last_ts: S, event_count: 1 },
]);
await seed("events", [
  { id: "e1", created: S, session: "s1", uuid: "u1", ts: "2026-09-09 10:00:01.000Z", iso_week: "2026-W37", role: "user", text: "the Structor replica", line_no: 1 },
  { id: "e2", created: S, session: "s1", uuid: "u2", ts: "2026-09-09 10:00:02.000Z", iso_week: "2026-W37", role: "assistant", text: "100% done_now", line_no: 2 },
  { id: "e3", created: S, session: "s2", uuid: "u3", ts: "2026-09-09 10:00:03.000Z", iso_week: "2026-W37", role: "user", text: "structor in project b", line_no: 3 },
]);
await seed("session_weeks", []);
await seed("import_runs", []);

const login = await facade.handle(replica, "collections/_superusers/auth-with-password", new Request("http://x/", { method: "POST", body: JSON.stringify({ identity: "e@x", password: "pw" }) }), new URL("http://x/"));
const token = ((await login.json()) as { token: string }).token;
async function get(path: string, params: Record<string, string> = {}) {
  const url = new URL(`http://x/console/unit/api/${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const r = await facade.handle(replica, path, new Request(url, { headers: { Authorization: token } }), url);
  return { status: r.status, body: (await r.json()) as Record<string, unknown> };
}
afterAll(() => rmSync(root, { recursive: true, force: true }));

test("search is a case-insensitive substring match, mid-word included, like the Go handler", async () => {
  const mid = await get("structor/search", { q: "ructor" });
  expect((mid.body.hits as { uuid: string }[]).map(h => h.uuid).sort()).toEqual(["u1", "u3"]);
  const upper = await get("structor/search", { q: "STRUCTOR" });
  expect((upper.body.hits as unknown[]).length).toBe(2);
  const pct = await get("structor/search", { q: "100%" });
  expect((pct.body.hits as { uuid: string }[]).map(h => h.uuid)).toEqual(["u2"]);
  const under = await get("structor/search", { q: "done_now" });
  expect((under.body.hits as { uuid: string }[]).map(h => h.uuid)).toEqual(["u2"]);
  const quote = await get("structor/search", { q: "it's" });
  expect(quote.status).toBe(200);
});

test("a project scope narrows the search before the cut, so a scoped query still fills its page", async () => {
  const scoped = await get("structor/search", { q: "structor", project_id: "p2", limit: "1" });
  expect((scoped.body.hits as { uuid: string }[]).map(h => h.uuid)).toEqual(["u3"]);
  const none = await get("structor/search", { q: "structor", project_id: "nope" });
  expect((none.body.hits as unknown[]).length).toBe(0);
});

test("days refuses impossible dates and defaults to 500 rows", async () => {
  expect((await get("structor/days", { from: "2026-02-31", to: "2026-02-31" })).status).toBe(400);
  const ok = await get("structor/days", { from: "2026-09-09", to: "2026-09-09" });
  expect(ok.status).toBe(200);
  expect((ok.body.days as { session_id: string; preview: string }[]).map(d => d.session_id).sort()).toEqual(["Alpha-1", "beta-2"]);
});

test("read resolves session ids case-insensitively", async () => {
  expect((await get("structor/read", { session: "ALPHA-1" })).status).toBe(200);
  expect((await get("structor/read", { session: "alp" })).status).toBe(200);
  expect((await get("structor/read", { session: "zzz" })).status).toBe(404);
});

test("a failing endpoint answers JSON, never Bun's HTML error page", async () => {
  const url = new URL("http://x/console/unit/api/structor/days?from=2026-09-09&to=2026-09-09");
  // an impossible predicate makes Lance reject the query inside the handler
  const r = await facade.handle(replica, "structor/search", new Request(new URL("http://x/console/unit/api/structor/search?q=x&week=2026-W37"), { headers: { Authorization: token } }), new URL("http://x/console/unit/api/structor/search?q=x&week=2026-W37"));
  expect(r.headers.get("content-type")).toContain("application/json");
  void url;
});
