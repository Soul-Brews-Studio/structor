// The LanceDB admin: a JSON API over every replica plus the static UI.
// Loopback only; there is no auth because nothing here can reach a
// password (targets are resolved from ~/.config, the API never echoes them).
//
// GET  /api/status                                   all targets, table counts, sync state
// GET  /api/:t/tables                                 [{name, rows, version, indices}]
// GET  /api/:t/tables/:n/schema                       {fields:[{name,type,nullable}]}
// GET  /api/:t/tables/:n/rows?where&limit&offset&select   {rows, total, limit, offset} (no ORDER BY: Lance scans in storage order)
// GET  /api/:t/tables/:n/search?q&limit&where         {rows} with _score (FTS tables only)
// GET  /api/:t/tables/:n/stats                        {rows, version, versions, indices, stats}
// GET  /api/:t/sync                                   {state, lag}
// POST /api/:t/sync                                   pull now → {pulled, state}
// POST /api/:t/tables/:n/optimize                     compact + index new rows
// POST /api/:t/tables/:n/fts                          (re)build the FTS index

import { Index } from "@lancedb/lancedb";
import type * as lancedb from "@lancedb/lancedb";
import { Replica, TABLES, type TableSpec } from "./sync.ts";
import { join } from "node:path";

export interface AdminOpts {
  http: string;                // host:port
  uiDir: string;               // directory holding index.html
  version: string;
  replicas: Map<string, Replica>;
  dataRoot: string;
  readOnly?: boolean;          // --no-sync instances refuse every write (sync / optimize / fts)
}

// Hosts a browser may address this server as. Anything else (a DNS-rebound
// hostname, a LAN name) is refused, which is what makes "loopback only" an
// access control rather than just a bind address.
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

function hostOf(hostHeader: string): string {
  const h = hostHeader.trim().toLowerCase();
  if (h.startsWith("[")) return h.slice(0, h.indexOf("]") + 1);
  return h.split(":")[0];
}

/** null when the request may proceed, otherwise the refusal. */
function originCheck(req: Request): Response | null {
  const host = req.headers.get("host") ?? "";
  if (!LOOPBACK_HOSTS.has(hostOf(host))) return json({ error: "loopback only" }, 403);
  const origin = req.headers.get("origin");
  if (origin && origin !== "null") {
    let o: URL;
    try { o = new URL(origin); } catch { return json({ error: "bad origin" }, 403); }
    if (!LOOPBACK_HOSTS.has(o.hostname) || o.host.toLowerCase() !== host.trim().toLowerCase()) return json({ error: "cross-origin request refused" }, 403);
  }
  return null;
}

// Scalar functions a `where` may call. LanceDB hands the predicate to
// DataFusion, whose full library includes things like repeat() that can
// allocate without bound; the admin is unauthenticated on loopback, so keep the
// callable surface to what a table filter needs.
const ALLOWED_FUNCS = new Set([
  "lower", "upper", "length", "char_length", "character_length", "octet_length", "substr", "substring",
  "starts_with", "ends_with", "contains", "strpos", "position", "trim", "ltrim", "rtrim", "btrim",
  "regexp_like", "regexp_match", "coalesce", "nullif", "abs", "round", "floor", "ceil",
  "in", "not", "exists", "any", "all", "cast", "date_part", "date_trunc", "to_timestamp",
]);

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const json = (v: unknown, status = 200) => new Response(JSON.stringify(v, bigintSafe), { status, headers: JSON_HEADERS });
const bad = (msg: string, status = 400) => json({ error: msg }, status);
const bigintSafe = (_: string, v: unknown) => (typeof v === "bigint" ? Number(v) : v);

const MAX_LIMIT = 500;

function spec(name: string): TableSpec | undefined {
  return TABLES.find(t => t.name === name);
}

/**
 * Validate a `where` predicate. Returns the predicate, "" for none, or an error
 * message. String literals are blanked before the checks so text like
 * `'npm i --save'` is fine; comments, semicolons and functions outside
 * ALLOWED_FUNCS are refused.
 */
export function safeWhere(w: string): { where: string } | { error: string } {
  const s = w.trim();
  if (!s) return { where: "" };
  if (s.length > 2000) return { error: "where: too long" };
  const bare = s.replace(/'(?:[^']|'')*'/g, "''");
  if (/;|--|\/\*/.test(bare)) return { error: "where: comments and semicolons are not allowed" };
  for (const m of bare.matchAll(/\b([A-Za-z_][A-Za-z0-9_]*)\s*\(/g)) {
    if (!ALLOWED_FUNCS.has(m[1].toLowerCase())) return { error: `where: function ${m[1]}() is not allowed` };
  }
  return { where: s };
}

/** Column names only: letters, digits, underscore. */
function safeSelect(raw: string): string[] | null {
  const cols = raw.split(",").map(x => x.trim()).filter(Boolean);
  return cols.every(c => /^[A-Za-z_][A-Za-z0-9_]*$/.test(c)) ? cols : null;
}

export function startAdmin(o: AdminOpts) {
  const [host, portStr] = o.http.split(":");
  const port = Number(portStr || 8092);

  async function tableInfo(r: Replica, s: TableSpec) {
    const t = await r.table(s);
    const [rows, version, indices] = await Promise.all([t.countRows(), t.version(), t.listIndices().catch(() => [])]);
    return { name: s.name, rows, version, indices: indices.map(i => ({ name: i.name, columns: i.columns, type: i.indexType })), fts: s.fts ?? null, stamp: s.stamp };
  }

  const server = Bun.serve({
    hostname: host || "127.0.0.1",
    port,
    idleTimeout: 120,
    async fetch(req) {
      const url = new URL(req.url);
      const p = url.pathname;
      try {
        const refused = originCheck(req);
        if (refused) return refused;
        if (req.method === "POST" && o.readOnly) return json({ error: "read-only instance (started with --no-sync)" }, 405);
        if (p === "/api/status" && req.method === "GET") {
          const targets = [];
          for (const r of o.replicas.values()) {
            const tables: Record<string, unknown> = {};
            for (const s of TABLES) tables[s.name] = await tableInfo(r, s).catch(e => ({ name: s.name, error: String(e) }));
            targets.push({ name: r.target.name, url: r.target.url, dir: r.dir, tables, sync: r.state });
          }
          return json({ version: o.version, dataRoot: o.dataRoot, time: new Date().toISOString(), targets });
        }

        const m = p.match(/^\/api\/([^/]+)\/(sync|tables)(?:\/([^/]+)(?:\/(schema|rows|search|stats|optimize|fts))?)?$/);
        if (m) {
          const r = o.replicas.get(decodeURIComponent(m[1]));
          if (!r) return bad("unknown target", 404);
          const [, , kind, tname, op] = m;

          if (kind === "sync") {
            if (req.method === "POST") {
              const pulled = await r.syncNow();
              return json({ pulled, state: r.state });
            }
            const lag = await r.lag();
            return json({ state: r.state, lag });
          }

          if (!tname) {
            const out = [];
            for (const s of TABLES) out.push(await tableInfo(r, s));
            return json(out);
          }
          const s = spec(tname);
          if (!s) return bad("unknown table", 404);
          const t = await r.table(s);

          if (op === "schema") {
            const sc = await t.schema();
            return json({ fields: sc.fields.map(f => ({ name: f.name, type: String(f.type), nullable: f.nullable })) });
          }
          if (op === "rows") {
            const w = safeWhere(url.searchParams.get("where") ?? "");
            if ("error" in w) return bad(w.error);
            const limit = Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get("limit")) || 50));
            const offset = Math.max(0, Number(url.searchParams.get("offset")) || 0);
            const select = safeSelect(url.searchParams.get("select") ?? "");
            if (!select) return bad("select: column names only");
            let q = t.query();
            if (w.where) q = q.where(w.where);
            if (select.length) q = q.select(select);
            const rows = await q.limit(limit).offset(offset).toArray();
            let total = -1;
            try { total = await t.countRows(w.where || undefined); } catch { /* bad predicate already threw above */ }
            return json({ rows: rows.map(plain), total, limit, offset });
          }
          if (op === "search") {
            if (!s.fts) return bad("this table has no full-text index");
            const qs = (url.searchParams.get("q") ?? "").trim();
            if (!qs) return bad("q required");
            if (qs.length > 500) return bad("q: too long");
            const w = safeWhere(url.searchParams.get("where") ?? "");
            if ("error" in w) return bad(w.error);
            const limit = Math.min(MAX_LIMIT, Math.max(1, Number(url.searchParams.get("limit")) || 50));
            let q = t.search(qs, "fts", s.fts);
            if (w.where) q = q.where(w.where);
            const rows = await q.limit(limit).toArray();
            return json({ rows: rows.map(plain), q: qs, limit });
          }
          if (op === "stats") {
            const [rows, version, versions, indices, stats] = await Promise.all([
              t.countRows(), t.version(), t.listVersions().catch(() => []), t.listIndices().catch(() => []), t.stats().catch(() => null),
            ]);
            return json({ rows, version, versions: versions.length, indices, stats });
          }
          if (op === "optimize" && req.method === "POST") {
            const res = await t.optimize();
            return json({ ok: true, result: res });
          }
          if (op === "fts" && req.method === "POST") {
            if (!s.fts) return bad("this table has no full-text column");
            await t.createIndex(s.fts, { config: Index.fts(), replace: true });
            r.markFtsBuilt(s.name);
            return json({ ok: true, column: s.fts });
          }
          return bad("not found", 404);
        }

        if (p.startsWith("/api/")) return bad("not found", 404);

        // static UI
        const file = p === "/" ? "index.html" : p.replace(/^\/+/, "");
        if (file.includes("..")) return bad("not found", 404);
        const f = Bun.file(join(o.uiDir, file));
        if (!(await f.exists())) return new Response("not found", { status: 404 });
        const headers: Record<string, string> = { "cache-control": file.endsWith(".html") ? "no-cache" : "public, max-age=3600" };
        if (file.endsWith(".html")) headers["content-security-policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'";
        return new Response(f, { headers });
      } catch (e) {
        return json({ error: String((e as Error).message ?? e) }, 500);
      }
    },
  });
  return server;
}

/** Arrow rows come back as objects with possible bigint/Vector values; flatten for JSON. */
function plain(row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(row)) {
    if (typeof v === "bigint") out[k] = Number(v);
    else if (v && typeof v === "object" && "toArray" in (v as object)) out[k] = Array.from((v as { toArray(): unknown[] }).toArray());
    else out[k] = v;
  }
  return out;
}

export type { lancedb };
