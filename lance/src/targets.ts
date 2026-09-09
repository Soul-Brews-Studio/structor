// Which Structor stores to replicate. Same rule as scripts/agent.sh: every
// ~/.config/structor/<name>.json with url + admin_email + admin_password is a
// target, and `local` exists even without a file (dev defaults). Passwords
// never leave this process.

import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export interface Target {
  name: string;
  url: string;
  email: string;
  password: string;
}

const CONF_DIR = join(homedir(), ".config", "structor");
const RESERVED = new Set(["tray", "lance"]); // config files that are not targets

export function loadTargets(only?: string[]): Target[] {
  const out = new Map<string, Target>();
  out.set("local", { name: "local", url: "http://127.0.0.1:8091", email: "admin@structor.local", password: "structor-dev-password" });
  if (existsSync(CONF_DIR)) {
    for (const f of readdirSync(CONF_DIR)) {
      if (!f.endsWith(".json")) continue;
      const name = f.slice(0, -5);
      if (RESERVED.has(name)) continue;
      try {
        const j = JSON.parse(readFileSync(join(CONF_DIR, f), "utf8")) as Record<string, string>;
        const url = j.url || (name === "local" ? out.get("local")!.url : "");
        const email = j.admin_email || (name === "local" ? out.get("local")!.email : "");
        const password = j.admin_password || (name === "local" ? out.get("local")!.password : "");
        if (url && email && password) out.set(name, { name, url: url.replace(/\/+$/, ""), email, password });
      } catch {
        // unreadable file: not a target
      }
    }
  }
  const all = [...out.values()];
  return only && only.length ? all.filter(t => only.includes(t.name)) : all;
}
