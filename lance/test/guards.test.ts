import { test, expect, afterAll } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { safeWhere, startAdmin } from "../src/admin.ts";
import { Replica, rewind, cmpCursor } from "../src/sync.ts";

test("safeWhere blanks string literals before looking for comments and semicolons", () => {
  expect(safeWhere("text LIKE '%npm i --save%'")).toEqual({ where: "text LIKE '%npm i --save%'" });
  expect(safeWhere("text = 'it''s; fine'")).toEqual({ where: "text = 'it''s; fine'" });
  expect("error" in safeWhere("role = 'x'; drop")).toBe(true);
  expect("error" in safeWhere("role = 'x' -- c")).toBe(true);
  expect("error" in safeWhere("role = 'x' /* c */")).toBe(true);
});

test("safeWhere allows table-filter functions and refuses the rest of DataFusion", () => {
  expect(safeWhere("lower(role) = 'user' AND length(text) > 10")).toEqual({ where: "lower(role) = 'user' AND length(text) > 10" });
  expect(safeWhere("role IN ('user', 'assistant')")).toEqual({ where: "role IN ('user', 'assistant')" });
  const r = safeWhere("length(repeat(text, 20000)) > 99999999999");
  expect("error" in r && r.error).toContain("repeat()");
  expect("error" in safeWhere("random() > 0.5")).toBe(true);
});

test("rewind steps a PocketBase stamp back and clears the id; cmpCursor orders by stamp then id", () => {
  expect(rewind({ stamp: "2026-09-09 15:00:01.500Z", id: "abc" }, 2000)).toEqual({ stamp: "2026-09-09 14:59:59.500Z", id: "" });
  expect(rewind({ stamp: "not a date", id: "x" }, 2000)).toEqual({ stamp: "not a date", id: "x" });
  expect(cmpCursor({ stamp: "a", id: "z" }, { stamp: "b", id: "a" })).toBe(-1);
  expect(cmpCursor({ stamp: "a", id: "b" }, { stamp: "a", id: "a" })).toBe(1);
  expect(cmpCursor({ stamp: "a", id: "a" }, { stamp: "a", id: "a" })).toBe(0);
});

const root = mkdtempSync(join(tmpdir(), "structor-lance-guards-"));
const replica = new Replica({ name: "unit", url: "http://127.0.0.1:1", email: "e", password: "p" }, root);
const server = startAdmin({ http: "127.0.0.1:0", uiDir: root, version: "test", replicas: new Map([["unit", replica]]), dataRoot: root, readOnly: true });
const base = `http://127.0.0.1:${server.port}`;
afterAll(() => { server.stop(true); rmSync(root, { recursive: true, force: true }); });

test("requests for a non-loopback Host or from a foreign Origin are refused", async () => {
  expect((await fetch(`${base}/api/status`, { headers: { Host: "evil.example" } })).status).toBe(403);
  expect((await fetch(`${base}/api/status`, { headers: { Origin: "https://evil.example" } })).status).toBe(403);
  expect((await fetch(`${base}/api/status`, { headers: { Origin: `http://127.0.0.1:${server.port}` } })).status).toBe(200);
  expect((await fetch(`${base}/api/status`)).status).toBe(200);
});

test("a --no-sync instance refuses every write", async () => {
  const r = await fetch(`${base}/api/unit/sync`, { method: "POST" });
  expect(r.status).toBe(405);
  expect((await fetch(`${base}/api/unit/tables/events/optimize`, { method: "POST" })).status).toBe(405);
  expect((await fetch(`${base}/api/unit/sync`)).status).toBe(200);
});

test("select accepts column names only", async () => {
  expect((await fetch(`${base}/api/unit/tables/events/rows?select=id,ts`)).status).toBe(200);
  expect((await fetch(`${base}/api/unit/tables/events/rows?select=${encodeURIComponent("id, length(text)")}`)).status).toBe(400);
});
