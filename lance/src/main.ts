// structor-lance — LanceDB replica of Structor's PocketBase stores + admin UI.
//
//   bun src/main.ts [--http 127.0.0.1:8092] [--data ../lance_data] [--targets local,kvmlab1]
//                   [--interval 15] [--no-sync] [--once]
//
// Env: STRUCTOR_LANCE_HTTP, STRUCTOR_LANCE_DATA, STRUCTOR_LANCE_TARGETS, STRUCTOR_LANCE_INTERVAL

import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadTargets } from "./targets.ts";
import { Replica } from "./sync.ts";
import { startAdmin } from "./admin.ts";

const here = dirname(fileURLToPath(import.meta.url));
const args = Bun.argv.slice(2);
const flag = (name: string, env: string, def: string) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : (process.env[env] ?? def);
};
const has = (name: string) => args.includes(`--${name}`);

const http = flag("http", "STRUCTOR_LANCE_HTTP", "127.0.0.1:8092");
const dataRoot = resolve(flag("data", "STRUCTOR_LANCE_DATA", resolve(here, "..", "..", "lance_data")));
const only = flag("targets", "STRUCTOR_LANCE_TARGETS", "").split(",").map(s => s.trim()).filter(Boolean);
const intervalMs = Math.max(3, Number(flag("interval", "STRUCTOR_LANCE_INTERVAL", "15"))) * 1000;
const version = process.env.STRUCTOR_LANCE_VERSION ?? "dev";

const log = (s: string) => console.log(`${new Date().toLocaleTimeString("en-GB", { hour12: false })} ${s}`);

const targets = loadTargets(only);
if (targets.length === 0) {
  console.error("no targets: expected ~/.config/structor/<name>.json with url, admin_email, admin_password");
  process.exit(78);
}

// A failed promise in a sync loop or a realtime stream must not take the admin
// down with it; log and carry on. Both loops catch their own errors, this is
// the backstop.
process.on("unhandledRejection", e => log(`unhandled rejection: ${(e as Error)?.message ?? e}`));
process.on("uncaughtException", e => log(`uncaught exception: ${e.message}`));

const replicas = new Map<string, Replica>();
for (const t of targets) replicas.set(t.name, new Replica(t, dataRoot));
log(`targets: ${targets.map(t => `${t.name} (${t.url})`).join(", ")} → ${dataRoot}`);

if (has("once")) {
  // The admin port is the writer mutex. If a replica is already serving it,
  // ask that process to pull instead of opening the same tables from here.
  let delegated = false;
  try {
    const r = await fetch(`http://${http}/api/status`, { signal: AbortSignal.timeout(2000) });
    if (r.ok) {
      const st = (await r.json()) as { dataRoot?: string };
      if (st.dataRoot === dataRoot) delegated = true;
    }
  } catch { /* nothing listening: sync directly */ }
  for (const r of replicas.values()) {
    if (delegated) {
      const res = await fetch(`http://${http}/api/${encodeURIComponent(r.target.name)}/sync`, { method: "POST", signal: AbortSignal.timeout(600_000) });
      const j = (await res.json()) as { pulled?: number; error?: string };
      log(`${r.target.name}: +${j.pulled ?? 0} rows (via the running replica on ${http})${j.error ? ` (error: ${j.error})` : ""}`);
    } else {
      const n = await r.syncNow();
      log(`${r.target.name}: +${n} rows${r.state.lastError ? ` (error: ${r.state.lastError})` : ""}`);
    }
  }
  process.exit(0);
}

const readOnly = has("no-sync");
const server = startAdmin({ http, uiDir: resolve(here, "..", "ui"), version, replicas, dataRoot, readOnly });
log(`admin: http://${server.hostname}:${server.port}/${readOnly ? " (read-only: --no-sync)" : ""}`);

if (!readOnly) {
  for (const r of replicas.values()) r.follow(intervalMs, log).catch(e => log(`${r.target.name}: follow stopped: ${(e as Error).message}`));
}

const shutdown = () => {
  for (const r of replicas.values()) r.stop();
  server.stop(true);
  process.exit(0);
};
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
