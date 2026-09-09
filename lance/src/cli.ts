// structor-lance CLI — the backend without the browser. Reads go straight to
// the Lance directory (safe alongside the running replica); writes (sync,
// optimize, fts) go through the admin API when it is up so only one process
// mutates a table, and fall back to direct access when it is not.
//
//   bun src/cli.ts targets
//   bun src/cli.ts status  [--target local]
//   bun src/cli.ts tables  [--target local]
//   bun src/cli.ts schema  <table> [--target local]
//   bun src/cli.ts rows    <table> [--where "role = 'user'"] [--limit 20] [--offset 0] [--select id,ts,text] [--json]
//   bun src/cli.ts search  <query> [--table events] [--where …] [--limit 20] [--json]
//   bun src/cli.ts lag     [--target local]
//   bun src/cli.ts sync    [--target local]
//   bun src/cli.ts optimize <table> [--target local]
//   bun src/cli.ts fts     [--table events] [--target local]
//
// Env: STRUCTOR_LANCE_DATA (default ../lance_data), STRUCTOR_LANCE_HTTP (default 127.0.0.1:8092)

import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Index } from "@lancedb/lancedb";
import { loadTargets } from "./targets.ts";
import { Replica, TABLES } from "./sync.ts";

const here = dirname(fileURLToPath(import.meta.url));
const argv = Bun.argv.slice(2);
const cmd = argv[0] ?? "help";
const positional = argv.slice(1).filter((a, i, arr) => !a.startsWith("--") && !(i > 0 && arr[i - 1].startsWith("--") && !isFlagOnly(arr[i - 1])));
const opt = (name: string, def = ""): string => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[i + 1] : def;
};
const flag = (name: string) => argv.includes(`--${name}`);
function isFlagOnly(f: string) { return f === "--json"; }
/** Integer option; a mistyped value is an error, not an empty result. */
const intOpt = (name: string, def: number): number => {
  const raw = opt(name, String(def));
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 0) fail(`--${name} must be a non-negative integer, got '${raw}'`);
  return n;
};

const dataRoot = resolve(process.env.STRUCTOR_LANCE_DATA ?? resolve(here, "..", "..", "lance_data"));
const api = `http://${process.env.STRUCTOR_LANCE_HTTP ?? "127.0.0.1:8092"}`;
const targetName = opt("target", "local");
const asJSON = flag("json");

function out(v: unknown) { console.log(typeof v === "string" ? v : JSON.stringify(v, (_, x) => (typeof x === "bigint" ? Number(x) : x), 2)); }
function fail(msg: string, code = 64): never { console.error(msg); process.exit(code); }

function replica(): Replica {
  const t = loadTargets([targetName])[0];
  if (!t) fail(`unknown target '${targetName}' (have: ${loadTargets().map(x => x.name).join(", ")})`);
  return new Replica(t, dataRoot);
}

async function viaApi(path: string, init?: RequestInit): Promise<unknown | null> {
  try {
    const r = await fetch(`${api}${path}`, { ...init, signal: AbortSignal.timeout(120_000) });
    if (!r.ok) fail(`${api}${path} → ${r.status} ${(await r.text()).slice(0, 300)}`, 70);
    return await r.json();
  } catch (e) {
    if ((e as Error).name === "TimeoutError") fail(`admin API timed out on ${path}`, 70);
    return null; // not running → direct
  }
}

function table(name: string) {
  const s = TABLES.find(t => t.name === name);
  if (!s) fail(`unknown table '${name}' (have: ${TABLES.map(t => t.name).join(", ")})`);
  return s;
}

function cell(v: unknown, width: number): string {
  let s = v == null ? "" : typeof v === "string" ? v : typeof v === "bigint" ? String(v) : JSON.stringify(v);
  s = s.replace(/\s+/g, " ");
  return s.length > width ? s.slice(0, width - 1) + "…" : s.padEnd(width);
}

function printRows(rows: Record<string, unknown>[]) {
  if (asJSON) return out(rows);
  if (rows.length === 0) return out("(no rows)");
  const cols = Object.keys(rows[0]);
  const widths = cols.map(c => Math.min(c === "text" ? 60 : 28, Math.max(c.length, ...rows.map(r => String(r[c] ?? "").length))));
  out(cols.map((c, i) => cell(c, widths[i])).join("  "));
  for (const r of rows) out(cols.map((c, i) => cell(r[c], widths[i])).join("  "));
}

switch (cmd) {
  case "targets": {
    out(loadTargets().map(t => ({ name: t.name, url: t.url })));
    break;
  }
  case "status": {
    const r = replica();
    const rows: Record<string, unknown>[] = [];
    for (const s of TABLES) {
      const t = await r.table(s);
      rows.push({ table: s.name, rows: await t.countRows(), version: await t.version(), indices: (await t.listIndices().catch(() => [])).map(i => i.name).join(",") });
    }
    if (asJSON) out({ target: r.target.name, url: r.target.url, dir: r.dir, tables: rows, sync: r.state });
    else {
      out(`${r.target.name}  ${r.target.url}  →  ${r.dir}`);
      printRows(rows);
      out(`last run ${r.state.lastRun || "never"}  ${r.state.lastDurationMs}ms${r.state.lastError ? `  ERROR ${r.state.lastError}` : ""}`);
    }
    break;
  }
  case "tables": {
    const r = replica();
    const rows: Record<string, unknown>[] = [];
    for (const s of TABLES) rows.push({ table: s.name, rows: await (await r.table(s)).countRows(), stamp: s.stamp, fts: s.fts ?? "" });
    printRows(rows);
    break;
  }
  case "schema": {
    const s = table(positional[0] ?? fail("schema <table>"));
    const sc = await (await replica().table(s)).schema();
    printRows(sc.fields.map(f => ({ field: f.name, type: String(f.type), nullable: f.nullable })));
    break;
  }
  case "rows": {
    const s = table(positional[0] ?? fail("rows <table> [--where …]"));
    const t = await replica().table(s);
    let q = t.query();
    const where = opt("where");
    if (where) q = q.where(where);
    const select = opt("select").split(",").map(x => x.trim()).filter(Boolean);
    if (select.length) q = q.select(select);
    const rows = await q.limit(intOpt("limit", 20)).offset(intOpt("offset", 0)).toArray();
    printRows(rows as Record<string, unknown>[]);
    break;
  }
  case "search": {
    const query = positional[0] ?? fail("search <query>");
    const s = table(opt("table", "events"));
    if (!s.fts) fail(`${s.name} has no full-text column`);
    const t = await replica().table(s);
    let q = t.search(query, "fts", s.fts);
    const where = opt("where");
    if (where) q = q.where(where);
    const sel = opt("select", "_score,id,session,ts,role,text").split(",").map(x => x.trim()).filter(x => x && x !== "_score");
    // no .select(): lance warns when a projection omits _score; trim columns after the fact instead
    const rows = (await q.limit(intOpt("limit", 20)).toArray()) as Record<string, unknown>[];
    printRows(rows.map(r => ({ _score: Number(r._score).toFixed(3), ...Object.fromEntries(sel.map(c => [c, r[c]])) })));
    break;
  }
  case "lag": {
    const r = replica();
    const lag = await r.lag();
    printRows(Object.entries(lag).map(([table, v]) => ({ table, remote: v.remote, local: v.local, behind: v.remote < 0 ? "?" : v.remote - v.local })));
    break;
  }
  case "sync": {
    const via = await viaApi(`/api/${encodeURIComponent(targetName)}/sync`, { method: "POST" });
    if (via) { out(asJSON ? via : `pulled ${(via as { pulled: number }).pulled} rows (via admin)`); break; }
    const r = replica();
    const n = await r.syncNow();
    out(asJSON ? { pulled: n, state: r.state } : `pulled ${n} rows${r.state.lastError ? `  ERROR ${r.state.lastError}` : ""}`);
    break;
  }
  case "optimize": {
    const s = table(positional[0] ?? fail("optimize <table>"));
    const via = await viaApi(`/api/${encodeURIComponent(targetName)}/tables/${s.name}/optimize`, { method: "POST" });
    if (via) { out(via); break; }
    out(await (await replica().table(s)).optimize());
    break;
  }
  case "fts": {
    const s = table(opt("table", "events"));
    if (!s.fts) fail(`${s.name} has no full-text column`);
    const via = await viaApi(`/api/${encodeURIComponent(targetName)}/tables/${s.name}/fts`, { method: "POST" });
    if (via) { out(via); break; }
    const r = replica();
    await (await r.table(s)).createIndex(s.fts, { config: Index.fts(), replace: true });
    const st = r.state.tables[s.name]; if (st) st.ftsBuilt = true;
    out({ ok: true, column: s.fts });
    break;
  }
  default: {
    out(`structor-lance cli
  targets | status | tables | schema <table> | rows <table> [--where] [--limit] [--offset] [--select] | search <q> [--table] [--where] [--limit]
  lag | sync | optimize <table> | fts [--table]        options: --target <name> --json`);
    if (cmd !== "help") process.exit(64);
  }
}
