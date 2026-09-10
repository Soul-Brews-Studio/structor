// One replica per target: three Lance tables mirroring PocketBase's
// projects / sessions / events, pulled through the records API in
// (stamp, id) order and upserted by id. PocketBase stays the source of
// truth; this side only ever catches up.
//
// Cursors: events are immutable, so `created` orders them. sessions and
// projects are rewritten on every ingest (tail state, cwd reconcile), so
// they page by `updated` and a changed row comes around again.

import * as lancedb from "@lancedb/lancedb";
import { Index, makeArrowTable } from "@lancedb/lancedb";
import { Schema, Field, Utf8, Float64, Bool } from "apache-arrow";
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { join } from "node:path";
import { PB, type Cursor, type PBRecord } from "./pb.ts";
import type { Target } from "./targets.ts";

export interface TableSpec {
  name: "projects" | "sessions" | "events" | "session_weeks" | "import_runs";
  stamp: "created" | "updated";
  schema: Schema;
  fts?: string; // column that gets a full-text index
}

const utf8 = (n: string) => new Field(n, new Utf8(), true);
const num = (n: string) => new Field(n, new Float64(), true);
const bool = (n: string) => new Field(n, new Bool(), true);

export const TABLES: TableSpec[] = [
  {
    name: "projects",
    stamp: "updated",
    schema: new Schema([utf8("id"), utf8("path"), utf8("name"), utf8("encoded_dir"), utf8("cwd"), utf8("host"), utf8("created"), utf8("updated")]),
  },
  {
    name: "sessions",
    stamp: "updated",
    schema: new Schema([
      utf8("id"), utf8("session_id"), utf8("project"), utf8("file_path"), utf8("tier"),
      num("byte_offset"), num("file_size"), num("file_mtime"), num("lines_seen"), num("event_count"),
      utf8("first_ts"), utf8("last_ts"), utf8("first_prompt"), utf8("git_branch"), utf8("cwd"), utf8("model"),
      utf8("created"), utf8("updated"),
    ]),
  },
  {
    name: "events",
    stamp: "created",
    fts: "text",
    schema: new Schema([
      utf8("id"), utf8("session"), utf8("uuid"), utf8("parent_uuid"), utf8("type"), utf8("role"),
      utf8("ts"), utf8("iso_week"), utf8("text"), utf8("tools"), utf8("model"), bool("sidechain"),
      num("line_no"), num("raw_bytes"), utf8("created"),
    ]),
  },
  {
    // one row per (session, ISO week): the ledger the old console's status strip and Weeks view read
    name: "session_weeks",
    stamp: "updated",
    schema: new Schema([
      utf8("id"), utf8("session"), utf8("project"), utf8("iso_week"),
      num("event_count"), num("user_count"), num("assistant_count"), num("tool_count"),
      utf8("first_ts"), utf8("last_ts"), utf8("created"), utf8("updated"),
    ]),
  },
  {
    // one row per ingest call: the Intake ledger
    name: "import_runs",
    stamp: "created",
    schema: new Schema([
      utf8("id"), utf8("session"), utf8("project"), num("from_offset"), num("to_offset"),
      num("lines"), num("inserted"), num("skipped"), utf8("host"), utf8("writer"), utf8("created"),
    ]),
  },
];

export interface TableState {
  cursor: Cursor | null;
  rows: number;        // rows pulled since the replica was created
  lastPull: string;    // ISO time of the last page that returned rows
  ftsBuilt?: boolean;
}

export interface SyncState {
  target: string;
  url: string;
  tables: Record<string, TableState>;
  lastRun: string;
  lastError: string;
  lastDurationMs: number;
}

export class Replica {
  readonly dir: string;
  readonly pb: PB;
  state: SyncState;
  private db?: lancedb.Connection;
  private running: Promise<number> | null = null;
  private wake: (() => void) | null = null;
  private stopped = false;
  private optimizeDueFor = new Map<string, number>();

  constructor(readonly target: Target, dataRoot: string) {
    this.dir = join(dataRoot, target.name);
    mkdirSync(this.dir, { recursive: true });
    this.pb = new PB(target.url, target.email, target.password);
    this.state = this.loadState();
  }

  private get statePath() { return join(this.dir, "sync.json"); }

  private loadState(): SyncState {
    const empty: SyncState = { target: this.target.name, url: this.target.url, tables: {}, lastRun: "", lastError: "", lastDurationMs: 0 };
    if (!existsSync(this.statePath)) return empty;
    try { return { ...empty, ...(JSON.parse(readFileSync(this.statePath, "utf8")) as SyncState) }; } catch { return empty; }
  }

  /** Atomic write of sync.json. A failure is recorded in lastError, never thrown: the replica must outlive a full disk. */
  saveState() {
    try {
      const tmp = this.statePath + ".tmp";
      writeFileSync(tmp, JSON.stringify(this.state, null, 2));
      renameSync(tmp, this.statePath);
    } catch (e) {
      this.state.lastError = `state: ${(e as Error).message}`.slice(0, 500);
    }
  }

  markFtsBuilt(table: string) {
    const st = this.state.tables[table] ?? { cursor: null, rows: 0, lastPull: "" };
    st.ftsBuilt = true;
    this.state.tables[table] = st;
    this.saveState();
  }

  async connect(): Promise<lancedb.Connection> {
    if (!this.db) this.db = await lancedb.connect(this.dir);
    return this.db;
  }

  async table(spec: TableSpec): Promise<lancedb.Table> {
    const db = await this.connect();
    const names = await db.tableNames();
    if (names.includes(spec.name)) return db.openTable(spec.name);
    return db.createEmptyTable(spec.name, spec.schema);
  }

  /** Pull everything new for every table. Returns rows upserted. Serialised: a second call joins the running one. */
  syncNow(): Promise<number> {
    if (this.running) return this.running;
    this.running = this.doSync().finally(() => { this.running = null; });
    return this.running;
  }

  private async doSync(): Promise<number> {
    const t0 = Date.now();
    let total = 0;
    try {
      for (const spec of TABLES) total += await this.syncTable(spec);
      this.state.lastError = "";
    } catch (e) {
      this.state.lastError = `${(e as Error).message}`.slice(0, 500);
    }
    this.state.lastRun = new Date().toISOString();
    this.state.lastDurationMs = Date.now() - t0;
    this.saveState();
    return total;
  }

  private async syncTable(spec: TableSpec): Promise<number> {
    const tbl = await this.table(spec);
    const st: TableState = this.state.tables[spec.name] ?? { cursor: null, rows: 0, lastPull: "" };
    let pulled = 0;
    // PocketBase ids are random, so a row committed after a page was read, in
    // the same millisecond as the cursor and with a lower id, would sort before
    // the cursor and be missed forever. Each run therefore starts a little
    // before the saved cursor; the re-pulled rows are upserts, so the only
    // cost is a few extra rows per run. Paging within the run stays exact.
    let cursor: Cursor | null = st.cursor ? rewind(st.cursor, REWIND_MS) : null;
    for (;;) {
      const page = await this.pb.pageAfter(spec.name, spec.stamp, cursor);
      if (page.items.length === 0) break;
      const rows = page.items.map(r => project(spec, r));
      await tbl.mergeInsert("id").whenMatchedUpdateAll().whenNotMatchedInsertAll().execute(makeArrowTable(rows, { schema: spec.schema }));
      const last = page.items[page.items.length - 1];
      cursor = { stamp: String(last[spec.stamp] ?? last.created), id: last.id };
      // never move the saved cursor backwards (the rewind window re-reads old rows)
      if (!st.cursor || cmpCursor(cursor, st.cursor) > 0) st.cursor = cursor;
      st.rows += rows.length;
      st.lastPull = new Date().toISOString();
      pulled += rows.length;
      this.state.tables[spec.name] = st;
      this.saveState();
      if (page.items.length < 1000) break;
    }
    this.state.tables[spec.name] = st;
    if (spec.fts && pulled > 0) await this.ensureFts(spec, tbl, st);
    if (pulled > 0) await this.maybeOptimize(spec, tbl);
    return pulled;
  }

  /**
   * Every merge page is a new Lance version and a new fragment. Left alone,
   * a busy day is thousands of versions and tens of gigabytes (26GB for one
   * events table on 2026-09-10). optimize() compacts fragments, folds new rows
   * into the FTS index, and prunes versions older than PRUNE_AFTER_MS — far
   * shorter than the 7-day default, which is meant for readers that pin old
   * versions; nothing here does.
   */
  private async maybeOptimize(spec: TableSpec, tbl: lancedb.Table) {
    const due = this.optimizeDueFor.get(spec.name) ?? 0;
    if (Date.now() < due) return;
    this.optimizeDueFor.set(spec.name, Date.now() + OPTIMIZE_EVERY_MS);
    await tbl.optimize({ cleanupOlderThan: new Date(Date.now() - PRUNE_AFTER_MS) });
  }

  /** Build the FTS index once the table has rows; afterwards fold new rows in with optimize(). */
  private async ensureFts(spec: TableSpec, tbl: lancedb.Table, st: TableState) {
    if (!spec.fts) return;
    if (!st.ftsBuilt) {
      if ((await tbl.countRows()) === 0) return;
      await tbl.createIndex(spec.fts, { config: Index.fts(), replace: true });
      st.ftsBuilt = true;
    }
    // new rows are folded into the index by maybeOptimize()
  }

  /** Follow the store: sync now, then again on every live message or every `intervalMs`. */
  async follow(intervalMs: number, log: (s: string) => void) {
    // realtime wake-up; the poll below is the safety net when the stream drops
    (async () => {
      while (!this.stopped) {
        try {
          await this.pb.live("structor/live", () => this.wake?.());
        } catch (e) {
          log(`live feed ${this.target.name}: ${(e as Error).message}`);
        }
        await Bun.sleep(5000);
      }
    })();
    let lastLogged = "";
    while (!this.stopped) {
      try {
        const n = await this.syncNow();
        if (n > 0) log(`${this.target.name}: +${n} rows`);
        else if (this.state.lastError && this.state.lastError !== lastLogged) log(`${this.target.name}: ${this.state.lastError}`);
        lastLogged = this.state.lastError;
      } catch (e) {
        // doSync already catches; this guards the loop itself so the process never dies here
        log(`${this.target.name}: ${(e as Error).message}`);
      }
      await new Promise<void>(res => {
        const t = setTimeout(res, intervalMs);
        this.wake = () => { clearTimeout(t); this.wake = null; res(); };
      });
      // let a burst of ingests coalesce into one pull
      await Bun.sleep(1500);
    }
  }

  stop() { this.stopped = true; this.wake?.(); }

  /** Rows on the PocketBase side minus rows here, per table — the lag the admin shows. */
  async lag(): Promise<Record<string, { remote: number; local: number }>> {
    const out: Record<string, { remote: number; local: number }> = {};
    for (const spec of TABLES) {
      const [remote, local] = await Promise.all([
        this.pb.count(spec.name).catch(() => -1),
        this.table(spec).then(t => t.countRows()).catch(() => -1),
      ]);
      out[spec.name] = { remote, local };
    }
    return out;
  }
}

const REWIND_MS = 2000;
export const OPTIMIZE_EVERY_MS = 5 * 60_000;
export const PRUNE_AFTER_MS = 60 * 60_000;

/** Cursor `ms` earlier than c, id cleared so every row at that stamp qualifies. PocketBase stamps look like "2026-09-09 15:00:00.100Z". */
export function rewind(c: Cursor, ms: number): Cursor {
  const t = Date.parse(c.stamp.replace(" ", "T"));
  if (Number.isNaN(t)) return c;
  const iso = new Date(t - ms).toISOString(); // 2026-09-09T15:00:00.100Z
  return { stamp: iso.replace("T", " "), id: "" };
}

/** Order two cursors the way PocketBase sorts them: by stamp, then id. */
export function cmpCursor(a: Cursor, b: Cursor): number {
  if (a.stamp !== b.stamp) return a.stamp < b.stamp ? -1 : 1;
  return a.id === b.id ? 0 : a.id < b.id ? -1 : 1;
}

/** Shape a PocketBase record into the table's fixed schema (unknown keys dropped, JSON stringified). */
export function project(spec: TableSpec, r: PBRecord): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of spec.schema.fields) {
    const v = r[f.name];
    if (f.type instanceof Utf8) out[f.name] = v == null ? "" : typeof v === "string" ? v : JSON.stringify(v);
    else if (f.type instanceof Float64) out[f.name] = typeof v === "number" ? v : Number(v ?? 0) || 0;
    else if (f.type instanceof Bool) out[f.name] = Boolean(v);
    else out[f.name] = v ?? null;
  }
  return out;
}
