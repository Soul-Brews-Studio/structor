'use strict';
// Structor live — transcripts arriving as cards, one column per project.
//
// Two modes. LIVE subscribes to the replica's relay (GET /api/<t>/live, an
// SSE stream of LiveMessage frames, one per PocketBase ingest) after a first
// fill from /api/<t>/live/recent. REPLAY reads the last `minutes` of the
// events table over the rows API and plays the rows back with their original
// gaps divided by `speed`. When the relay is missing (the Bun edition has
// none) live falls back to replay and says so.
//
// Fleet lesson baked in: only the inserted card animates, never the lane. No
// requestAnimationFrame, one setTimeout chain for replay, a 1 s ticker for the
// clock and meter, and nothing loaded from anywhere but this origin. Every
// string that reaches the DOM goes through esc().
//
// The file also loads under node, where there is no document: everything that
// touches the DOM is bound in main(), which only runs in a browser, and the
// pure helpers plus the state they read are exported for lance-py's
// tests/test_live_page.py (which reads a .cjs copy — ../package.json says
// "type": "module", and this is a classic script, not a module).

const doc = typeof document === 'undefined' ? null : document;
const $ = (sel, root) => (root || doc).querySelector(sel);

const CARD_CAP = 40;        // cards per lane; the oldest fall off the bottom
const ARRIVE_MS = 700;      // how long a new card keeps its `arrive` wash
const TEXT_CAP = 160;       // characters of text on a card
const MIN_TEXT = 20;        // shorter texts are noise ("ok", a nudge) unless a tool is named
const MAX_GAP_MS = 2500;    // replay never waits longer than this between cards
const MIN_GAP_MS = 40;      // ...and never less, so a burst still reads as a sequence
const METER_MIN = 10;       // minutes covered by the events-per-minute meter
const ROWS_LIMIT = 500;     // the rows API's ceiling
const TRIM_ROUNDS = 4;      // replay: how many times a window is narrowed to fit under ROWS_LIMIT
const TRIM_MARGIN = 0.8;    // ...and the slack left each time, because activity is never even
const IN_BATCH = 120;       // ids per `id IN (...)` lookup; keeps the URL well under 16 KB
const RECENT_LIMIT = 50;    // messages asked of /live/recent on open
const RERANK_MS = 60000;    // live: how often lanes are re-ranked by recent activity
const RETRY_MS = 10000;     // live: wait before reopening a stream the browser gave up on
const SEEN_CAP = 5000;      // dedup memory; trimmed back to SEEN_KEEP when exceeded
const SEEN_KEEP = 4000;
const SPEEDS = [1, 10, 30, 60, 300];
const N_TITLE = 'cards routed to this lane since the page opened; the newest ' + CARD_CAP + ' stay on screen';

// ---------- options from the query string ----------
const Q = new URLSearchParams(typeof location === 'undefined' ? '' : location.search);
function num(key, dflt, lo, hi) {
  const raw = Q.get(key);
  if (raw === null || raw === '') return dflt;
  const v = Number(raw);
  return isFinite(v) ? Math.min(hi, Math.max(lo, v)) : dflt;
}
const opts = {
  target: Q.get('target') || '',
  mode: Q.get('mode') === 'replay' ? 'replay' : 'live',
  minutes: num('minutes', 60, 1, 7 * 24 * 60),
  speed: num('speed', 30, 0.1, 10000),
  seconds: num('seconds', 0, 0, 86400),
  lanes: Math.round(num('lanes', 6, 1, 12)),
};

// ---------- state ----------
const S = {
  mode: opts.mode,
  target: opts.target,
  paused: false,
  done: false,
  es: null,              // the EventSource, live mode
  lastId: null,          // live: the relay id of the newest frame this page has seen
  pending: [],           // events held back while paused (live)
  seen: new Set(),       // event keys already on the page
  counts: new Map(),     // project -> events routed this run (lane counters)
  recent: [],            // {t, project} within the meter window; also drives re-ranking
  lanes: [],             // named lanes in column order
  other: null,           // the overflow lane, created when a project finds no free column
  laneBy: new Map(),     // project -> named lane
  notices: new Map(),    // key -> text; every notice stays until its key is cleared
  lastEventT: 0,         // ts (ms) of the newest event on the page — the store's stamp, not the card's arrival
  clock: 0,              // replay: ts (ms) of the last card played
  clockWall: 0,          // replay: wall-clock ms when `clock` was set; the replayed clock runs on from there
  sinceMs: 0,            // replay: where the window starts (wall clock ms), after any trimming
  fromMs: 0,             // replay: ts of the first event actually played, what the badge shows
  replay: null,          // {events, i, timer, t0, t1}
  meterDirty: true,
};

// the page's elements, bound by main() in a browser (a test stubs the few it needs)
const el = {};
const IDS = ['target', 'badge', 'conn', 'age', 'meter', 'speedWrap', 'speed', 'modeLink', 'pause', 'notice', 'progress', 'fill', 'clock', 'range', 'lanes'];
function bindDom() {
  for (const id of IDS) el[id] = $('#' + id);
}

// ---------- small helpers ----------
const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const enc = encodeURIComponent;
const safeId = s => /^[A-Za-z0-9_-]{1,64}$/.test(String(s || ''));
const uniq = a => Array.from(new Set(a));

// the store stamps "2026-09-11 13:19:38.926Z"; the relay's `ts` is ISO — both parse once the space is a T
function parseTs(s) {
  const d = new Date(String(s || '').replace(' ', 'T'));
  return isNaN(d.getTime()) ? null : d;
}
// local wall-clock HH:MM:SS
const hms = ms => new Date(ms).toLocaleTimeString('en-GB', { hourCycle: 'h23', hour: '2-digit', minute: '2-digit', second: '2-digit' });
// UTC "YYYY-MM-DD HH:MM:SS", the prefix the store's ts column compares against
const utcStamp = ms => new Date(ms).toISOString().slice(0, 19).replace('T', ' ');

function ago(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + ' s ago';
  if (s < 3600) return Math.round(s / 60) + ' m ago';
  return Math.round(s / 3600) + ' h ago';
}

function baseName(p) {
  const s = String(p === null || p === undefined ? '' : p).replace(/\/+$/, '');
  const i = s.lastIndexOf('/');
  return i >= 0 ? s.slice(i + 1) : s;
}

function oneLine(s, cap) {
  const t = String(s === null || s === undefined ? '' : s).replace(/\s+/g, ' ').trim();
  return t.length > cap ? t.slice(0, cap) + '…' : t;
}

// the eight characters that tell sessions apart: a UUID's first block, or —
// for a workflow subagent, "agent-<hex>@wf_<run>" — the hex after the shared
// prefix. Cutting the raw id gave "agent-a0" for every one of them.
function shortSid(sid) {
  const s = String(sid === null || sid === undefined ? '' : sid);
  const m = /^[a-z]+[-_]([0-9a-f]{8,})/i.exec(s);
  return (m ? m[1] : s).slice(0, 8) || '–';
}

// the relay sends tools as an array, the events table as a JSON string like '["Bash"]'
function toolsOf(v) {
  if (Array.isArray(v)) return v.map(String).filter(Boolean);
  if (typeof v === 'string' && v.charAt(0) === '[') {
    try { const a = JSON.parse(v); return Array.isArray(a) ? a.map(String).filter(Boolean) : []; } catch (e) { return []; }
  }
  return [];
}

// GET JSON; the error carries the status so a 404 can mean "no relay here"
async function api(path) {
  const res = await fetch(path, { cache: 'no-store' });
  let body = null;
  try { body = await res.json(); } catch (e) { /* not JSON, e.g. the static handler's "not found" */ }
  if (!res.ok || (body && body.error)) {
    const err = new Error(body && body.error ? body.error : 'HTTP ' + res.status);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---------- header ----------
function setConn(text, cls) {
  el.conn.textContent = text;
  el.conn.className = 'conn' + (cls ? ' ' + cls : '');
}

// notices stack under the header, one line per key, and a later one never
// erases an earlier one: "no relay on this edition" has to outlive whatever
// the replay it fell back to has to say. An empty text clears its key.
function notice(key, text) {
  if (text) S.notices.set(key, text); else S.notices.delete(key);
  const lines = Array.from(S.notices.values());
  el.notice.innerHTML = lines.map(t => '<span>' + esc(t) + '</span>').join('');
  el.notice.hidden = !lines.length;
}

function setBadgeLive() {
  el.badge.className = 'badge live';
  el.badge.innerHTML = '<i class="dot"></i>LIVE';
}

// the replay badge names the store it reads — six indexes cover this corpus
// and disagree — and dates itself from the first event it actually plays,
// which is later than the window asked for whenever that window was trimmed
// to fit the rows API (see fetchWindow). Until the rows are in, the window.
function setBadgeReplay() {
  const from = S.fromMs || S.sinceMs;
  const why = S.fromMs ? 'first event played: ' : 'window start: ';
  el.badge.className = 'badge replay';
  el.badge.innerHTML = '<b>REPLAY</b><span class="sep">·</span>Lance replica ' + esc(S.target) +
    '<span class="sep">·</span>events table<span class="sep">·</span>from <time title="' + esc(why + new Date(from).toISOString()) + '">' +
    esc(hms(from)) + '</time><span class="sep">·</span>×' + esc(String(opts.speed));
}

function setModeLink() {
  const p = new URLSearchParams();
  p.set('target', S.target);
  if (S.mode === 'live') {
    p.set('mode', 'replay');
    p.set('minutes', String(opts.minutes));
    el.modeLink.textContent = 'Replay';
    el.modeLink.title = 'Replay the last ' + opts.minutes + ' minutes from the events table';
  } else {
    p.set('mode', 'live');
    el.modeLink.textContent = 'Live';
    el.modeLink.title = 'Follow the relay as transcripts are ingested';
  }
  if (opts.lanes !== 6) p.set('lanes', String(opts.lanes));
  el.modeLink.href = 'live.html?' + p.toString();
}

function buildMeter() {
  el.meter.innerHTML = '<i></i>'.repeat(METER_MIN);
}

// "now" for the staleness clock, the meter and the progress line: the wall
// clock when live; during a replay the replayed clock, which runs on from the
// last card at `speed` up to the next card's time — so a long gap reads as a
// long gap even when the wait for it was clamped — and stands still on pause.
function nowMs() {
  if (S.mode !== 'replay' || !S.clock) return Date.now();
  const r = S.replay;
  if (S.paused || S.done || !r || r.i >= r.events.length) return S.clock;
  return Math.min(r.events[r.i].t, S.clock + (Date.now() - S.clockWall) * opts.speed);
}

// "last event N s ago" is measured from the event's own timestamp, never from
// when its card landed: a page opened on a quiet fleet says an hour, not a
// second, and a live card shows the ~12 s the watcher's debounce costs
function ageText() {
  return S.lastEventT ? 'last event ' + ago(nowMs() - S.lastEventT) : 'no events yet';
}

// bars are per minute, oldest on the left, ending at nowMs()
function renderMeter() {
  const now = nowMs();
  const from = now - METER_MIN * 60000;
  S.recent = S.recent.filter(r => r.t >= from);
  const b = new Array(METER_MIN).fill(0);
  for (const r of S.recent) {
    const k = Math.floor((now - r.t) / 60000);
    if (k >= 0 && k < METER_MIN) b[METER_MIN - 1 - k]++;
  }
  const max = Math.max(1, ...b);
  const bars = el.meter.children;
  for (let i = 0; i < METER_MIN; i++) {
    bars[i].style.height = (b[i] ? Math.max(12, Math.round(b[i] / max * 100)) : 6) + '%';
    bars[i].classList.toggle('hot', b[i] > 0);
  }
  el.meter.title = b.reduce((a, n) => a + n, 0) + ' events in the last ' + METER_MIN + ' minutes';
  S.meterDirty = false;
}

let tickN = 0;
function tick() {
  tickN++;
  el.age.textContent = ageText();
  if (S.replay && !S.paused && !S.done) progress();   // the replayed clock moves between cards too
  if (S.meterDirty || tickN % 5 === 0) renderMeter();
}

// ---------- lanes ----------
function makeLane(key, label) {
  const sec = document.createElement('section');
  sec.className = 'lane ' + (key === null ? 'other' : 'named');
  sec.innerHTML = '<div class="lane-head"><b title="' + esc(key === null ? 'projects without a column of their own' : key) + '">' +
    esc(label) + '</b><span class="n" title="' + esc(N_TITLE) + '">0</span></div><div class="lane-list"></div>';
  return { key, el: sec, list: $('.lane-list', sec), nEl: $('.n', sec), n: 0 };
}

function syncGrid() {
  el.lanes.style.setProperty('--lanes', String(S.lanes.length + (S.other ? 1 : 0)));
}

// the first lane replaces whatever placeholder text the lanes area was showing
function clearState() {
  if (!S.lanes.length && !S.other) el.lanes.innerHTML = '';
}

// the lane a project's cards go to: its own column while one is free, else "other"
function laneFor(project) {
  const named = S.laneBy.get(project);
  if (named) return named;
  if (S.lanes.length < opts.lanes - 1) {
    clearState();
    const lane = makeLane(project, baseName(project) || '(no project)');
    S.lanes.push(lane);
    S.laneBy.set(project, lane);
    el.lanes.insertBefore(lane.el, S.other ? S.other.el : null);
    syncGrid();
    return lane;
  }
  if (!S.other) {
    clearState();
    S.other = makeLane(null, 'other');
    el.lanes.appendChild(S.other.el);
    syncGrid();
  }
  return S.other;
}

// give the busiest projects the columns, in rank order, before any card lands
function planLanes(counts) {
  const ranked = Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
  ranked.slice(0, Math.max(0, opts.lanes - 1)).forEach(e => laneFor(e[0]));
}

function countBy(events) {
  const m = new Map();
  for (const ev of events) m.set(ev.project, (m.get(ev.project) || 0) + 1);
  return m;
}

// Live only: every RERANK_MS, hand the columns to the projects busiest in the
// meter window. Current holders win ties and keep free columns, so lanes only
// change when an outsider is clearly busier — a rare, one-shot DOM move.
function rerank() {
  if (S.mode !== 'live' || S.paused || S.done || opts.lanes < 2) return;
  const counts = countBy(S.recent);
  if (!counts.size) return;
  const cur = S.lanes.map(l => l.key);
  const ranked = Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1] || (cur.indexOf(a[0]) === -1) - (cur.indexOf(b[0]) === -1))
    .map(e => e[0]);
  const want = ranked.slice(0, opts.lanes - 1);
  for (const k of cur) if (want.length < opts.lanes - 1 && want.indexOf(k) === -1) want.push(k);
  const same = want.length === cur.length && want.every(k => cur.indexOf(k) !== -1);
  if (!same) rebuildLanes(want);
}

// re-home every card under a new set of named lanes; transitions are off
// while it happens so nothing on screen moves visibly
function rebuildLanes(keys) {
  const cards = [];
  for (const l of S.lanes.concat(S.other ? [S.other] : [])) cards.push(...l.list.children);
  cards.sort((a, b) => Number(b.dataset.t) - Number(a.dataset.t));
  el.lanes.classList.add('still');
  el.lanes.innerHTML = '';
  S.lanes = []; S.other = null; S.laneBy.clear();
  keys.forEach(k => laneFor(k));
  for (const c of cards) {
    const lane = laneFor(c.dataset.project);
    if (lane.list.children.length < CARD_CAP) lane.list.appendChild(c);
  }
  for (const l of S.lanes) { l.n = S.counts.get(l.key) || 0; l.nEl.textContent = String(l.n); }
  if (S.other) {
    let n = 0;
    for (const [p, c] of S.counts) if (!S.laneBy.has(p)) n += c;
    S.other.n = n; S.other.nEl.textContent = String(n);
  }
  setTimeout(() => el.lanes.classList.remove('still'), 80);
}

function placeholder(text) {
  if (!S.lanes.length && !S.other) el.lanes.innerHTML = '<p class="state">' + esc(text) + '</p>';
}

function errState(text) {
  el.lanes.innerHTML = '<p class="state err">' + esc(text) + '</p>';
  setConn('error', 'bad');
}

// ---------- cards ----------
// one card's data from either a relay LiveEvent or an events-table row
function normalize(raw, sid, project) {
  const d = parseTs(raw.ts);
  if (!d) return null;
  return {
    key: String(raw.uuid || raw.id || (sid + ':' + raw.ts)),
    t: d.getTime(),
    role: String(raw.role || ''),
    text: oneLine(raw.text, TEXT_CAP),
    tools: toolsOf(raw.tools),
    sid: String(sid || ''),
    project: String(project || ''),
  };
}

function cardEl(ev) {
  const a = document.createElement('article');
  const tool = !ev.text && ev.tools.length;
  a.className = 'card arrive ' + (tool || (ev.role !== 'user' && ev.role !== 'assistant') ? 'tool' : ev.role);
  a.dataset.project = ev.project;
  a.dataset.t = String(ev.t);
  a.innerHTML = '<div class="meta"><span class="dot"></span><time>' + esc(hms(ev.t)) + '</time>' +
    '<span class="sid" title="' + esc(ev.sid) + '">' + esc(shortSid(ev.sid)) + '</span>' +
    '<span class="proj" title="' + esc(ev.project) + '">' + esc(baseName(ev.project) || '–') + '</span></div>' +
    '<div class="text">' + esc(ev.text || (ev.tools.length ? '[tool: ' + ev.tools.join(', ') + ']' : '')) + '</div>';
  return a;
}

// prepend one card to its lane; returns false when it was already on the page
function addCard(ev) {
  if (S.seen.has(ev.key)) return false;
  S.seen.add(ev.key);
  if (S.seen.size > SEEN_CAP) for (const k of S.seen) { S.seen.delete(k); if (S.seen.size <= SEEN_KEEP) break; }
  S.counts.set(ev.project, (S.counts.get(ev.project) || 0) + 1);
  S.recent.push({ t: ev.t, project: ev.project });
  const lane = laneFor(ev.project);
  const card = cardEl(ev);
  lane.list.prepend(card);
  while (lane.list.children.length > CARD_CAP) lane.list.lastElementChild.remove();
  lane.n++;
  lane.nEl.textContent = String(lane.n);
  setTimeout(() => card.classList.remove('arrive'), ARRIVE_MS);
  if (ev.t > S.lastEventT) S.lastEventT = ev.t;
  S.meterDirty = true;
  return true;
}

// ---------- live ----------
// the relay's LiveMessage: {at, session_id, project, host, writer, inserted,
// skipped, byte_offset, events:[{uuid, ts, role, type, text, tools, line_no}], truncated}.
// /live/recent may hand the same shape wrapped as {id, message}.
function unwrap(msg) {
  if (msg && Array.isArray(msg.events)) return msg;
  if (msg && msg.message && Array.isArray(msg.message.events)) return msg.message;
  return null;
}

// the replay's server-side filter, applied here to relay rows for the same picture:
// conversational roles, and enough text to read — or a tool to name
function wanted(ev) {
  if (ev.role !== 'user' && ev.role !== 'assistant') return false;
  return ev.text.length >= MIN_TEXT || ev.tools.length > 0;
}

function eventsOf(msg) {
  const m = unwrap(msg);
  if (!m) return [];
  const sid = String(m.session_id || '');
  const project = String(m.project || '');
  return m.events.map(raw => normalize(raw, sid, project)).filter(ev => ev && wanted(ev));
}

function ingestMessage(msg) {
  let n = 0;
  for (const ev of eventsOf(msg)) {
    if (S.paused) {
      S.pending.push(ev);
      if (S.pending.length > ROWS_LIMIT) S.pending.shift();
      continue;
    }
    if (addCard(ev)) n++;
  }
  return n;
}

async function startLive() {
  setBadgeLive();
  setConn('connecting…', '');
  let recent;
  try {
    recent = await api('/api/' + enc(S.target) + '/live/recent?limit=' + RECENT_LIMIT);
  } catch (e) {
    if (e.status === 404) {
      // no relay on this edition: say so (the notice stays for the whole
      // visit), take away the Live link there is nothing behind, and replay
      S.mode = 'replay';
      el.modeLink.hidden = true;
      notice('relay', 'no live relay on this edition (/api/' + S.target + '/live is 404) — replaying the last ' + opts.minutes + ' minutes from the events table instead');
      return startReplay();
    }
    setConn('relay unreachable', 'bad');
    placeholder('live/recent failed: ' + e.message + ' — retrying in ' + RETRY_MS / 1000 + ' s');
    setTimeout(() => { if (!S.done) startLive(); }, RETRY_MS);
    return;
  }
  if (typeof recent.last_id === 'number') S.lastId = recent.last_id;
  const msgs = Array.isArray(recent && recent.messages) ? recent.messages : [];
  const buffered = [];
  msgs.forEach(m => buffered.push(...eventsOf(m)));
  planLanes(countBy(buffered));
  msgs.forEach(ingestMessage);
  if (!buffered.length) placeholder('waiting for the first ingest — a transcript write reaches this page about 12 s later');
  openStream();
  armStop();
  setInterval(rerank, RERANK_MS);
}

// the stream URL carries the newest relay id this page has seen, so the first
// open resumes right after the /live/recent fill and a reopen after a CLOSED
// stream picks up what the retry wait cost; the seen-set only removes the
// overlap, it cannot fill a gap. (On its own reconnects the browser sends
// Last-Event-ID, which the relay prefers over the query.)
function streamUrl(target, lastId) {
  const q = lastId === null || lastId === undefined ? '' : '?last_id=' + enc(String(lastId));
  return '/api/' + enc(target) + '/live' + q;
}

function openStream() {
  const es = new EventSource(streamUrl(S.target, S.lastId));
  S.es = es;
  es.onopen = () => { if (!S.paused) setConn('live', 'live'); };
  es.addEventListener('live', e => {
    const id = Number(e.lastEventId);
    if (isFinite(id) && id > 0) S.lastId = id;
    let msg;
    try { msg = JSON.parse(e.data); } catch (err) { return; }
    ingestMessage(msg);
  });
  es.onerror = () => {
    if (S.done) return;
    if (es.readyState === EventSource.CLOSED) {
      // the browser stops retrying after a hard failure (a 503 from the client
      // cap, a wrong content type); the fresh EventSource resumes from S.lastId
      setConn('closed — retrying in ' + RETRY_MS / 1000 + ' s', 'bad');
      setTimeout(() => { if (!S.done) openStream(); }, RETRY_MS);
    } else if (!S.paused) {
      setConn('reconnecting…', 'warn');   // the browser retries and sends Last-Event-ID
    }
  };
}

// ---------- replay ----------
// session record id -> {sid: the session's UUID, project: cwd or path}, the
// same strings the relay puts on a LiveMessage so lanes match across modes.
// One sessions lookup, then one projects lookup, each batched by IN_BATCH ids.
async function resolveProjects(rows) {
  const out = new Map();
  const sids = uniq(rows.map(r => String(r.session || ''))).filter(safeId);
  const sessions = await lookup('sessions', sids, 'id,session_id,project,cwd');
  const pids = uniq(sessions.map(s => String(s.project || ''))).filter(safeId);
  const projects = new Map();
  for (const p of await lookup('projects', pids, 'id,path,cwd')) projects.set(p.id, String(p.cwd || p.path || ''));
  for (const s of sessions) {
    out.set(s.id, { sid: String(s.session_id || s.id), project: projects.get(s.project) || String(s.cwd || '') });
  }
  return out;
}

async function lookup(table, ids, select) {
  const rows = [];
  for (let i = 0; i < ids.length; i += IN_BATCH) {
    const batch = ids.slice(i, i + IN_BATCH);
    const p = new URLSearchParams({ where: 'id IN (' + batch.map(x => "'" + x + "'").join(',') + ')', limit: String(ROWS_LIMIT), select });
    try {
      const r = await api('/api/' + enc(S.target) + '/tables/' + table + '/rows?' + p.toString());
      rows.push(...(r.rows || []));
    } catch (e) {
      notice('lookup-' + table, table + ' lookup failed: ' + e.message + ' — lanes fall back to session ids');
    }
  }
  return rows;
}

function showReplayControls() {
  el.progress.hidden = false;
  el.speedWrap.hidden = false;
  const speeds = SPEEDS.indexOf(opts.speed) === -1 ? SPEEDS.concat([opts.speed]).sort((a, b) => a - b) : SPEEDS;
  el.speed.innerHTML = speeds.map(s => '<option value="' + s + '"' + (s === opts.speed ? ' selected' : '') + '>' + s + '</option>').join('');
}

// the rows API reads `limit` rows at most, in storage order, so a window that
// holds more would come back as an arbitrary slice of itself. Narrow the
// window instead, assuming even activity and leaving slack: the minutes that
// should hold about limit * TRIM_MARGIN rows. Unchanged when the window fits.
function trimMinutes(minutes, total, limit) {
  if (!(total > limit) || minutes <= 1) return minutes;
  return Math.max(1, Math.floor(minutes * limit / total * TRIM_MARGIN));
}

// the replay's rows: the last `opts.minutes`, narrowed (a few rounds at most)
// until the whole window fits in one read. Returns {rows, total, minutes,
// sinceMs, trimmed, overflow}; `overflow` means it never fit — activity so
// bursty that the narrowed window came back empty, or too many rounds — and
// the rows are an arbitrary slice, which the caller says out loud.
async function fetchWindow() {
  let minutes = opts.minutes;
  let over = null;   // the last over-full response, the fallback when a narrower window is empty
  for (let round = 0; ; round++) {
    const sinceMs = Date.now() - minutes * 60000;
    const p = new URLSearchParams({
      where: "ts >= '" + utcStamp(sinceMs) + "' AND role IN ('user','assistant') AND length(text) >= " + MIN_TEXT,
      limit: String(ROWS_LIMIT),
      select: 'id,session,ts,role,text,tools,uuid',
    });
    const res = await api('/api/' + enc(S.target) + '/tables/events/rows?' + p.toString());
    const rows = res.rows || [];
    const total = typeof res.total === 'number' && res.total >= rows.length ? res.total : rows.length;
    const got = { rows, total, minutes, sinceMs, trimmed: minutes < opts.minutes, overflow: total > rows.length };
    if (over && !rows.length) return over;   // the burst is older than this slice: play the slice we have
    if (!got.overflow || round >= TRIM_ROUNDS || minutes <= 1) return got;
    over = got;
    minutes = trimMinutes(minutes, total, ROWS_LIMIT);
  }
}

async function startReplay() {
  showReplayControls();
  S.sinceMs = Date.now() - opts.minutes * 60000;
  setBadgeReplay();
  setConn('loading…', '');
  let win;
  try {
    win = await fetchWindow();
  } catch (e) {
    errState('events query failed: ' + e.message);
    return;
  }
  const rows = win.rows;
  if (!rows.length) {
    placeholder('no events in the last ' + opts.minutes + ' minutes on this replica — widen ?minutes=');
    setConn('done', '');
    el.pause.disabled = true;
    return;
  }
  if (win.overflow) {
    notice('window', 'the window holds ' + win.total + ' events; the rows API returns ' + rows.length + ' in storage order — shorten ?minutes= for the full picture');
  }
  const names = await resolveProjects(rows);
  const events = rows
    .map(r => { const s = names.get(r.session) || {}; return normalize(r, s.sid || r.session, s.project || ''); })
    .filter(Boolean)
    .sort((a, b) => a.t - b.t);
  planLanes(countBy(events));
  S.replay = { events, i: 0, timer: 0, t0: events[0].t, t1: events[events.length - 1].t };
  S.sinceMs = win.sinceMs;
  S.fromMs = S.replay.t0;
  setBadgeReplay();   // now dated from the first event played, not the window asked for
  el.range.textContent = hms(S.replay.t0) + ' → ' + hms(S.replay.t1) + ' · ' + events.length + ' events' +
    (win.trimmed ? ' · the newest ' + win.minutes + ' of ' + opts.minutes + ' min (the rows API reads ' + ROWS_LIMIT + ' at a time)' : '');
  setConn('replaying', 'live');
  armStop();
  step();
}

// one setTimeout chain: play a card, wait the (scaled, clamped) gap, repeat
function step() {
  const r = S.replay;
  if (!r || S.paused || S.done) return;
  if (r.i >= r.events.length) { finish('done'); return; }
  const ev = r.events[r.i++];
  addCard(ev);
  S.clock = ev.t;
  S.clockWall = Date.now();
  progress();
  if (r.i >= r.events.length) { finish('done'); return; }
  const gap = (r.events[r.i].t - ev.t) / opts.speed;
  r.timer = setTimeout(step, Math.min(MAX_GAP_MS, Math.max(MIN_GAP_MS, gap)));
}

function progress() {
  const r = S.replay;
  const now = nowMs();
  const span = Math.max(1, r.t1 - r.t0);
  el.fill.style.width = Math.round((now - r.t0) / span * 100) + '%';
  el.clock.textContent = hms(now);
}

// ---------- run control ----------
function armStop() {
  if (opts.seconds > 0) setTimeout(() => finish('done'), opts.seconds * 1000);
}

function finish(label) {
  if (S.done) return;
  S.done = true;
  if (S.es) S.es.close();
  if (S.replay) {
    clearTimeout(S.replay.timer);
    if (S.replay.i >= S.replay.events.length) el.fill.style.width = '100%';   // played to the end
  }
  setConn(label, '');
  el.pause.disabled = true;
}

function onPause() {
  if (S.done) return;
  S.paused = !S.paused;
  el.pause.textContent = S.paused ? 'Resume' : 'Pause';
  if (S.paused) {
    if (S.replay) clearTimeout(S.replay.timer);
    setConn('paused', 'warn');
    return;
  }
  if (S.mode === 'replay') {
    setConn('replaying', 'live');
    step();
  } else {
    setConn(S.es && S.es.readyState === EventSource.OPEN ? 'live' : 'reconnecting…', S.es && S.es.readyState === EventSource.OPEN ? 'live' : 'warn');
    S.pending.splice(0).forEach(addCard);   // what arrived meanwhile, in order
  }
}

function onSpeed() {
  const v = Number(el.speed.value);
  if (!isFinite(v) || v <= 0) return;
  opts.speed = v;   // the running setTimeout chain picks it up at its next gap
  setBadgeReplay();
}

// ---------- boot ----------
async function boot() {
  buildMeter();
  let status = null;
  try { status = await api('/api/status'); } catch (e) { notice('status', '/api/status unreachable: ' + e.message); }
  const targets = (status && status.targets) || [];
  if (!S.target) {
    S.target = targets.length ? String(targets[0].name) : 'local';
  } else if (targets.length && !targets.some(t => t.name === S.target)) {
    notice('target', 'target "' + S.target + '" is not on this replica — using ' + targets[0].name);
    S.target = String(targets[0].name);
  }
  el.target.textContent = S.target;
  document.title = 'Structor · Live · ' + S.target;
  setModeLink();
  setInterval(tick, 1000);
  if (S.mode === 'live') await startLive();
  else await startReplay();
}

function main() {
  bindDom();
  el.pause.addEventListener('click', onPause);
  el.speed.addEventListener('change', onSpeed);
  boot();
}

if (doc) {
  main();
} else if (typeof module !== 'undefined' && module.exports) {
  // node: the pure parts, for tests. Nothing here touches the DOM unless a test stubs `el`.
  module.exports = { S, opts, el, notice, setBadgeReplay, nowMs, ageText, streamUrl, trimMinutes, shortSid,
    parseTs, hms, ago, baseName, oneLine, toolsOf, normalize, wanted, eventsOf, N_TITLE };
}
