import { test, expect } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import * as lancedb from "@lancedb/lancedb";
import { makeArrowTable } from "@lancedb/lancedb";
import { pbQuote, PB } from "../src/pb.ts";
import { project, TABLES, Replica } from "../src/sync.ts";

const events = TABLES.find(t => t.name === "events")!;
const sessions = TABLES.find(t => t.name === "sessions")!;

test("pbQuote escapes quotes and backslashes for the PocketBase filter grammar", () => {
  expect(pbQuote("2026-09-09 15:00:00.100Z")).toBe("'2026-09-09 15:00:00.100Z'");
  expect(pbQuote("a'b")).toBe("'a\\'b'");
  expect(pbQuote("a\\b")).toBe("'a\\\\b'");
});

test("pageAfter asks PocketBase for rows strictly after the (stamp, id) cursor", async () => {
  const seen: string[] = [];
  const pb = new PB("http://pb.test", "e", "p");
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    seen.push(url);
    if (url.endsWith("/auth-with-password")) return new Response(JSON.stringify({ token: "tok" }), { status: 200 });
    expect((init?.headers as Record<string, string>).Authorization).toBe("tok");
    return new Response(JSON.stringify({ items: [], totalItems: 0 }), { status: 200 });
  }) as typeof fetch;
  try {
    await pb.pageAfter("events", "created", { stamp: "2026-09-09 15:00:00.100Z", id: "abc" });
  } finally {
    globalThis.fetch = realFetch;
  }
  const u = new URL(seen[seen.length - 1]);
  expect(u.pathname).toBe("/api/collections/events/records");
  expect(u.searchParams.get("sort")).toBe("created,id");
  expect(u.searchParams.get("perPage")).toBe("1000");
  expect(u.searchParams.get("filter")).toBe("(created > '2026-09-09 15:00:00.100Z') || (created = '2026-09-09 15:00:00.100Z' && id > 'abc')");
});

test("project() shapes a PocketBase record into the fixed Arrow schema", () => {
  const row = project(events, {
    id: "r1", created: "2026-09-09 15:00:00.000Z", session: "s1", uuid: "u1", ts: "2026-09-09 15:00:00.000Z",
    iso_week: "2026-W37", text: "hi", tools: [{ name: "Read" }], sidechain: 1, line_no: "7", raw_bytes: 120, extra: "dropped",
  });
  expect(row.tools).toBe('[{"name":"Read"}]');
  expect(row.sidechain).toBe(true);
  expect(row.line_no).toBe(7);
  expect(row.raw_bytes).toBe(120);
  expect(row.parent_uuid).toBe("");
  expect("extra" in row).toBe(false);
  expect(Object.keys(row)).toEqual(events.schema.fields.map(f => f.name));
});

test("mergeInsert by id updates a re-seen session row instead of duplicating it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "structor-lance-test-"));
  try {
    const db = await lancedb.connect(dir);
    const t = await db.createEmptyTable("sessions", sessions.schema);
    const a = project(sessions, { id: "s1", created: "c", updated: "u1", session_id: "abc", file_path: "/x", byte_offset: 10 });
    await t.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute(makeArrowTable([a], { schema: sessions.schema }));
    const b = project(sessions, { id: "s1", created: "c", updated: "u2", session_id: "abc", file_path: "/x", byte_offset: 20 });
    await t.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute(makeArrowTable([b], { schema: sessions.schema }));
    expect(await t.countRows()).toBe(1);
    const rows = await t.query().where("id = 's1'").toArray();
    expect(rows[0].byte_offset).toBe(20);
    expect(rows[0].updated).toBe("u2");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("Replica keeps one Lance directory per target and starts with an empty state", () => {
  const root = mkdtempSync(join(tmpdir(), "structor-lance-root-"));
  try {
    const r = new Replica({ name: "unit", url: "http://127.0.0.1:1", email: "e", password: "p" }, root);
    expect(r.dir).toBe(join(root, "unit"));
    expect(r.state.tables).toEqual({});
    expect(r.state.lastRun).toBe("");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
