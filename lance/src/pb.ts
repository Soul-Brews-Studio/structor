// Minimal PocketBase client for the replica: superuser login, paged record
// listing ordered by a (stamp, id) cursor, and the realtime feed on the
// custom `structor/live` topic. No SDK: three endpoints, fetch only.

export interface PBRecord {
  id: string;
  created: string;
  updated?: string;
  [k: string]: unknown;
}

export interface ListPage {
  items: PBRecord[];
  totalItems: number;
}

export interface Cursor {
  stamp: string; // value of the ordering column (created or updated) of the last row seen
  id: string;    // tiebreak
}

const RETRY_429 = [1000, 3000, 8000];

export class PB {
  private token = "";
  private auth?: Promise<string>;

  constructor(readonly url: string, private email: string, private password: string) {}

  /** POST /api/collections/_superusers/auth-with-password */
  async login(): Promise<string> {
    if (!this.auth) {
      this.auth = (async () => {
        const r = await fetch(`${this.url}/api/collections/_superusers/auth-with-password`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ identity: this.email, password: this.password }),
        });
        if (!r.ok) throw new Error(`login ${r.status} at ${this.url}`);
        const j = (await r.json()) as { token: string };
        this.token = j.token;
        return j.token;
      })().finally(() => { this.auth = undefined; });
    }
    return this.auth;
  }

  private async request(path: string, init: RequestInit = {}, attempt = 0): Promise<Response> {
    if (!this.token) await this.login();
    const r = await fetch(`${this.url}${path}`, {
      ...init,
      headers: { ...(init.headers || {}), Authorization: this.token },
    });
    if (r.status === 401 && attempt === 0) {
      this.token = "";
      return this.request(path, init, 1);
    }
    if (r.status === 429 && attempt < RETRY_429.length) {
      await Bun.sleep(RETRY_429[attempt]);
      return this.request(path, init, attempt + 1);
    }
    return r;
  }

  /** A valid superuser token for this store (logs in when needed). Used by the realtime proxy. */
  async bearer(): Promise<string> {
    if (!this.token) await this.login();
    return this.token;
  }

  /** Forget the cached token (after a 401 seen elsewhere). */
  invalidate() { this.token = ""; }

  async getJSON<T>(path: string): Promise<T> {
    const r = await this.request(path);
    if (!r.ok) throw new Error(`${path} → ${r.status} ${(await r.text()).slice(0, 200)}`);
    return (await r.json()) as T;
  }

  /**
   * One page of `collection` strictly after `cursor`, ordered by (stampField, id).
   * PocketBase caps perPage at 1000 (tools/search/provider.go MaxPerPage).
   */
  async pageAfter(collection: string, stampField: "created" | "updated", cursor: Cursor | null, perPage = 1000): Promise<ListPage> {
    const q = new URLSearchParams({
      perPage: String(Math.min(perPage, 1000)),
      sort: `${stampField},id`,
      skipTotal: "1",
    });
    if (cursor) {
      // PocketBase filter grammar: quoted strings, || and &&, = and >.
      const s = pbQuote(cursor.stamp);
      const i = pbQuote(cursor.id);
      q.set("filter", `(${stampField} > ${s}) || (${stampField} = ${s} && id > ${i})`);
    }
    const page = await this.getJSON<{ items: PBRecord[]; totalItems: number }>(
      `/api/collections/${collection}/records?${q.toString()}`,
    );
    return { items: page.items, totalItems: page.totalItems };
  }

  /** Total rows in a collection (one request, perPage=1). */
  async count(collection: string): Promise<number> {
    const page = await this.getJSON<{ totalItems: number }>(`/api/collections/${collection}/records?perPage=1&fields=id`);
    return page.totalItems;
  }

  async status(): Promise<Record<string, unknown>> {
    return this.getJSON(`/api/structor/status`);
  }

  /**
   * Subscribe to the realtime SSE stream and call `onMessage` for every
   * `structor/live` message. Resolves only when the connection closes; the
   * caller loops. PocketBase protocol: GET /api/realtime opens the stream and
   * sends PB_CONNECT {clientId}; POST /api/realtime {clientId, subscriptions}
   * (with Authorization) selects topics.
   */
  async live(topic: string, onMessage: (data: unknown) => void, signal?: AbortSignal): Promise<void> {
    if (!this.token) await this.login();
    const r = await fetch(`${this.url}/api/realtime`, { headers: { Accept: "text/event-stream" }, signal });
    if (!r.ok || !r.body) throw new Error(`realtime ${r.status}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let event = "";
    let data = "";
    const dispatch = async () => {
      if (event === "PB_CONNECT") {
        const { clientId } = JSON.parse(data || "{}") as { clientId: string };
        const sub = await this.request(`/api/realtime`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ clientId, subscriptions: [topic] }),
        });
        if (!sub.ok) throw new Error(`subscribe ${sub.status}`);
      } else if (event === topic && data) {
        try { onMessage(JSON.parse(data)); } catch { /* malformed frame: ignore */ }
      }
      event = ""; data = "";
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += dec.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).replace(/\r$/, "");
        buf = buf.slice(nl + 1);
        if (line === "") { await dispatch(); continue; }
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += (data ? "\n" : "") + line.slice(5).trim();
      }
    }
  }
}

/** Quote a value for the PocketBase filter grammar (single quotes, escaped). */
export function pbQuote(v: string): string {
  return `'${v.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}
