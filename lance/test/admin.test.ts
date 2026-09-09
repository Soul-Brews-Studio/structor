import { test, expect, afterAll } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startAdmin } from "../src/admin.ts";
import { Replica, TABLES } from "../src/sync.ts";
import { makeArrowTable } from "@lancedb/lancedb";
import { project } from "../src/sync.ts";

const root = mkdtempSync(join(tmpdir(), "structor-lance-admin-"));
const replica = new Replica({ name: "unit", url: "http://127.0.0.1:1", email: "e", password: "p" }, root);
const events = TABLES.find(t => t.name === "events")!;
const t = await replica.table(events);
await t.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute(makeArrowTable([
  project(events, { id: "e1", created: "c1", session: "s", uuid: "u1", ts: "2026-09-09 15:00:00.000Z", iso_week: "2026-W37", role: "user", text: "<b>bold</b> hello", line_no: 1 }),
  project(events, { id: "e2", created: "c2", session: "s", uuid: "u2", ts: "2026-09-09 15:00:01.000Z", iso_week: "2026-W37", role: "assistant", text: "hello back", line_no: 2 }),
], { schema: events.schema }));

const server = startAdmin({ http: "127.0.0.1:0", uiDir: join(root, "no-ui"), version: "test", replicas: new Map([["unit", replica]]), dataRoot: root });
const base = `http://127.0.0.1:${server.port}`;

afterAll(() => { server.stop(true); rmSync(root, { recursive: true, force: true }); });

test("status lists the target and its tables", async () => {
  const j = await (await fetch(`${base}/api/status`)).json();
  expect(j.targets[0].name).toBe("unit");
  expect(j.targets[0].tables.events.rows).toBe(2);
  expect(JSON.stringify(j)).not.toContain("password");
});

test("rows honours where, limit and offset and reports the filtered total", async () => {
  const j = await (await fetch(`${base}/api/unit/tables/events/rows?where=${encodeURIComponent("role = 'user'")}&limit=10`)).json();
  expect(j.total).toBe(1);
  expect(j.rows[0].id).toBe("e1");
  const page2 = await (await fetch(`${base}/api/unit/tables/events/rows?limit=1&offset=1`)).json();
  expect(page2.rows.length).toBe(1);
});

test("limit is capped and a bad predicate is a 500 with an error body, not a crash", async () => {
  const j = await (await fetch(`${base}/api/unit/tables/events/rows?limit=100000`)).json();
  expect(j.limit).toBe(500);
  const r = await fetch(`${base}/api/unit/tables/events/rows?where=${encodeURIComponent("nosuchcol = 1")}`);
  expect(r.status).toBe(500);
  expect((await r.json()).error).toBeTruthy();
  const r2 = await fetch(`${base}/api/unit/tables/events/rows?where=${encodeURIComponent("role = 'x'; drop")}`);
  expect(r2.status).toBe(400);
});

test("unknown target/table and traversal are 404", async () => {
  expect((await fetch(`${base}/api/nope/tables`)).status).toBe(404);
  expect((await fetch(`${base}/api/unit/tables/nope/rows`)).status).toBe(404);
  expect((await fetch(`${base}/../../etc/passwd`)).status).toBe(404);
  expect((await fetch(`${base}/%2e%2e/%2e%2e/etc/passwd`)).status).toBe(404);
});

test("search needs the FTS index and then returns scored rows", async () => {
  const before = await fetch(`${base}/api/unit/tables/events/search?q=hello`);
  // without an index LanceDB either errors (500) or flat-scans; both are acceptable, but the next call must work
  expect([200, 500]).toContain(before.status);
  const built = await (await fetch(`${base}/api/unit/tables/events/fts`, { method: "POST" })).json();
  expect(built.ok).toBe(true);
  const j = await (await fetch(`${base}/api/unit/tables/events/search?q=hello&limit=5`)).json();
  expect(j.rows.length).toBe(2);
  expect(typeof j.rows[0]._score).toBe("number");
});

test("schema and stats describe the table", async () => {
  const s = await (await fetch(`${base}/api/unit/tables/events/schema`)).json();
  expect(s.fields.map((f: { name: string }) => f.name)).toContain("iso_week");
  const st = await (await fetch(`${base}/api/unit/tables/events/stats`)).json();
  expect(st.rows).toBe(2);
  expect(st.version).toBeGreaterThan(0);
});
