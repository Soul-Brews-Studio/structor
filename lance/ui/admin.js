'use strict';
// Structor LanceDB admin — a table browser over the JSON API in src/admin.ts.
// No dependencies, no network beyond this origin. Every string that reaches the
// DOM goes through esc() first; the only markup this file builds itself is the
// <mark> wrapper around search terms.

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const KEY_THEME = 'structor.theme';
const KEY_PLACE = 'structor.lance.place';
const POLL_MS = 10000;
// The lag call counts rows on the PocketBase side, so it runs every third poll
// rather than every one; a sync, a target switch or a reload refreshes it too.
const LAG_EVERY = 3;
const TABS = ['rows', 'search', 'schema', 'stats'];

let statusDoc = null;     // last GET /api/status
let statusAt = 0;         // when that arrived (ms)
let targetName = '';
let tableName = '';
let tab = 'rows';
let syncState = null;     // state from GET /api/:t/sync
let lag = null;           // lag from the same call
let lagFor = '';          // target those two belong to
let busy = '';            // action in flight: sync | optimize | fts
let actionMsg = null;     // { err: boolean, text: string }
let fields = [];          // schema of the selected table
let cols = null;          // chosen columns, or null for all
let rowOffset = 0;
let lastRows = [];        // what the rows table currently shows
let lastHits = [];        // what the search panel currently shows
let loaded = {};          // which panels have data for this table
let pollTick = 0;
let drawerRecord = null;
let booted = false;       // the first target/table pick belongs to start(), not to a poll

// ---------- small helpers ----------
const esc = s => String(s === null || s === undefined ? '' : s)
  .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const enc = encodeURIComponent;
const fmtN = n => (typeof n === 'number' && isFinite(n) ? n.toLocaleString('en-US') : '–');

function fmtBytes(n) {
  if (typeof n !== 'number' || !isFinite(n)) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}

// PocketBase stamps look like "2026-09-09 15:38:06.483Z"; sync state is ISO.
function parseTs(s) {
  if (!s) return null;
  const d = new Date(String(s).replace(' ', 'T'));
  return isNaN(d.getTime()) ? null : d;
}

function rel(s) {
  const d = parseTs(s);
  if (!d) return 'never';
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return secs + 's ago';
  if (secs < 3600) return Math.round(secs / 60) + 'm ago';
  if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
  return Math.round(secs / 86400) + 'd ago';
}

async function api(path, init) {
  const res = await fetch(path, init);
  let body = null;
  try { body = await res.json(); } catch (e) { throw new Error('HTTP ' + res.status + ' (no JSON body)'); }
  if (!res.ok || (body && body.error)) throw new Error(body && body.error ? body.error : 'HTTP ' + res.status);
  return body;
}

function savePlace() {
  try { localStorage.setItem(KEY_PLACE, JSON.stringify({ targetName, tableName, tab })); } catch (e) { /* private mode */ }
}
function loadPlace() {
  try { return JSON.parse(localStorage.getItem(KEY_PLACE) || '{}') || {}; } catch (e) { return {}; }
}

const currentTarget = () => (statusDoc && statusDoc.targets || []).find(t => t.name === targetName) || null;
const currentTable = () => { const t = currentTarget(); return t && t.tables ? t.tables[tableName] : null; };
const ftsColumn = () => { const t = currentTable(); return t && t.fts ? t.fts : null; };
const base = () => '/api/' + enc(targetName) + '/tables/' + enc(tableName);

// ---------- theme ----------
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $$('[data-theme-pick]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.themePick === t)));
  try { localStorage.setItem(KEY_THEME, t); } catch (e) { /* private mode */ }
}
try { setTheme(localStorage.getItem(KEY_THEME) === 'paper' ? 'paper' : 'dark'); } catch (e) { setTheme('dark'); }
$$('[data-theme-pick]').forEach(b => b.addEventListener('click', () => setTheme(b.dataset.themePick)));

// ---------- status polling ----------
function setConn(ok, detail) {
  const dot = $('#dot');
  dot.className = 'connection-dot ' + (ok ? 'ready' : 'down');
  $('#connText').textContent = ok ? 'connected' : 'offline';
  $('#connPath').textContent = detail || '';
}

async function refreshStatus(withLag) {
  let doc;
  try {
    doc = await api('/api/status');
  } catch (e) {
    setConn(false, e.message);
    return;
  }
  statusDoc = doc;
  statusAt = Date.now();
  $('#ver').textContent = 'lance ' + (doc.version || 'dev');
  const targets = doc.targets || [];
  if (!targets.length) {
    setConn(true, 'no targets configured');
    $('#targets').innerHTML = '<p class="state">no targets: expected ~/.config/structor/&lt;name&gt;.json</p>';
    return;
  }
  const known = targets.some(t => t.name === targetName);
  if (!known) {
    targetName = targets[0].name;
    lag = null; syncState = null; lagFor = '';
  }
  const t = currentTarget();
  setConn(true, t ? t.url : '');
  if (booted && (!t.tables || !t.tables[tableName])) pickDefaultTable();
  renderRail();
  renderStatus();
  renderTabs();
  if (withLag) refreshLag();
}

function pickDefaultTable() {
  const t = currentTarget();
  const names = t && t.tables ? Object.keys(t.tables) : [];
  const next = names.indexOf('events') >= 0 ? 'events' : names[0] || '';
  if (next && next !== tableName) selectTable(next);
}

async function refreshLag() {
  const forTarget = targetName;
  try {
    const r = await api('/api/' + enc(forTarget) + '/sync');
    if (forTarget !== targetName) return;
    lag = r.lag; syncState = r.state; lagFor = forTarget;
    if (actionMsg && actionMsg.err && actionMsg.text.indexOf('lag check failed') === 0) actionMsg = null;
  } catch (e) {
    if (forTarget !== targetName) return;
    lag = null; lagFor = forTarget;
    actionMsg = { err: true, text: 'lag check failed: ' + e.message };
  }
  renderStatus();
}

// ---------- left rail ----------
function renderRail() {
  const targets = (statusDoc && statusDoc.targets) || [];
  $('#targets').innerHTML = targets.map(t => {
    const err = t.sync && t.sync.lastError;
    return '<button type="button" class="item" data-target="' + esc(t.name) + '" aria-pressed="' + (t.name === targetName) + '">' +
      '<b>' + esc(t.name) + '</b>' +
      '<span class="count">' + (err ? '<span class="chip bad">error</span>' : '') + '</span>' +
      '<small>' + esc(t.url) + '</small></button>';
  }).join('');

  const t = currentTarget();
  $('#railTarget').textContent = t ? t.name : '';
  const tables = t && t.tables ? t.tables : {};
  $('#tables').innerHTML = Object.keys(tables).map(n => {
    const info = tables[n] || {};
    if (info.error) {
      return '<button type="button" class="item" data-table="' + esc(n) + '" aria-pressed="' + (n === tableName) + '">' +
        '<b>' + esc(n) + '</b><span class="count">–</span><small class="err">' + esc(info.error) + '</small></button>';
    }
    const bits = ['v' + fmtN(info.version)];
    if (info.indices && info.indices.length) bits.push(info.indices.length + ' index' + (info.indices.length > 1 ? 'es' : ''));
    if (info.fts) bits.push('fts:' + info.fts);
    return '<button type="button" class="item" data-table="' + esc(n) + '" aria-pressed="' + (n === tableName) + '">' +
      '<b>' + esc(n) + '</b><span class="count">' + fmtN(info.rows) + '</span>' +
      '<small>' + esc(bits.join(' · ')) + '</small></button>';
  }).join('');

  const rows = [
    ['version', (statusDoc && statusDoc.version) || '–'],
    ['data dir', t ? t.dir : '–'],
    ['store', t ? t.url : '–'],
    ['polled', new Date(statusAt || Date.now()).toLocaleTimeString('en-GB')],
  ];
  $('#railMeta').innerHTML = rows.map(r =>
    '<dt>' + esc(r[0]) + '</dt><dd title="' + esc(r[1]) + '"><bdi>' + esc(r[1]) + '</bdi></dd>').join('');
  $('#pollAge').textContent = targets.length + ' target' + (targets.length === 1 ? '' : 's');
}

// ---------- status strip ----------
function renderStatus() {
  const t = currentTarget();
  const el = $('#statusbar');
  if (!t) { el.innerHTML = '<p class="state">waiting for /api/status…</p>'; return; }
  const s = (lagFor === targetName && syncState) ? syncState : (t.sync || {});
  const ranAt = parseTs(s.lastRun);
  const stale = ranAt && (Date.now() - ranAt.getTime()) > 120000;

  let html = '<div class="sb"><span class="lbl">sync</span>' +
    '<b class="' + (stale ? 'stale' : '') + '">' + esc(rel(s.lastRun)) + '</b>' +
    '<span>' + fmtN(s.lastDurationMs) + ' ms</span></div>';

  html += '<div class="sb"><span class="lbl">lag</span>' + lagChips() + '</div>';

  const fts = ftsColumn();
  const dis = busy ? ' disabled' : '';
  html += '<div class="sb-actions">' +
    '<button type="button" class="btn primary" data-act="sync"' + dis + '>' + (busy === 'sync' ? 'Syncing…' : 'Sync now') + '</button>' +
    '<button type="button" class="btn" data-act="optimize"' + (dis || !tableName ? ' disabled' : '') +
    ' title="Compact fragments and index new rows in ' + esc(tableName) + '">' +
    (busy === 'optimize' ? 'Optimizing…' : 'Optimize ' + esc(tableName || 'table')) + '</button>' +
    '<button type="button" class="btn" data-act="fts"' + (dis || !fts ? ' disabled' : '') +
    ' title="' + (fts ? 'Rebuild the full-text index on ' + esc(tableName) + '.' + esc(fts) + ' — this rereads every row' : 'This table has no full-text column') + '">' +
    (busy === 'fts' ? 'Rebuilding…' : 'Rebuild FTS') + '</button></div>';

  if (s.lastError) html += '<p class="sb-msg err">last sync error: ' + esc(s.lastError) + '</p>';
  if (actionMsg) html += '<p class="sb-msg' + (actionMsg.err ? ' err' : '') + '">' + esc(actionMsg.text) + '</p>';
  el.innerHTML = html;
}

function lagChips() {
  if (lagFor !== targetName || !lag) return '<span class="chip">checking…</span>';
  return Object.keys(lag).map(n => {
    const l = lag[n];
    if (l.remote < 0 || l.local < 0) return '<span class="chip bad">' + esc(n) + ' unreadable</span>';
    const d = l.remote - l.local;
    if (d === 0) return '<span class="chip ok">' + esc(n) + ' in sync</span>';
    return '<span class="chip behind">' + esc(n) + ' ' + (d > 0 ? '+' : '') + fmtN(d) +
      ' <span title="remote / local">(' + fmtN(l.remote) + '/' + fmtN(l.local) + ')</span></span>';
  }).join(' ');
}

$('#statusbar').addEventListener('click', ev => {
  const b = ev.target.closest('[data-act]');
  if (b && !b.disabled) runAction(b.dataset.act);
});

async function runAction(kind) {
  if (busy) return;
  busy = kind;
  actionMsg = { err: false, text: kind === 'fts' ? 'rebuilding the full-text index — this can take a while…' : 'working…' };
  renderStatus();
  try {
    let res, text;
    if (kind === 'sync') {
      res = await api('/api/' + enc(targetName) + '/sync', { method: 'POST' });
      syncState = res.state; lagFor = targetName;
      text = 'sync: pulled ' + fmtN(res.pulled) + ' row' + (res.pulled === 1 ? '' : 's') +
        ' in ' + fmtN(res.state && res.state.lastDurationMs) + ' ms';
    } else if (kind === 'optimize') {
      res = await api(base() + '/optimize', { method: 'POST' });
      const c = (res.result && res.result.compaction) || {};
      const p = (res.result && res.result.prune) || {};
      text = 'optimize ' + tableName + ': fragments ' + fmtN(c.fragmentsRemoved) + ' removed / ' + fmtN(c.fragmentsAdded) +
        ' added, files ' + fmtN(c.filesRemoved) + ' / ' + fmtN(c.filesAdded) +
        ', pruned ' + fmtBytes(p.bytesRemoved) + ' over ' + fmtN(p.oldVersionsRemoved) + ' old versions';
    } else {
      res = await api(base() + '/fts', { method: 'POST' });
      text = 'full-text index rebuilt on ' + tableName + '.' + res.column;
    }
    actionMsg = { err: false, text: text };
    await refreshStatus(false);
    await refreshLag();
    if (tab === 'stats') loadStats();
    else if (tab === 'rows') loadRows();
  } catch (e) {
    actionMsg = { err: true, text: kind + ' failed: ' + e.message };
  } finally {
    busy = '';
    renderStatus();
  }
}

// ---------- rail interaction ----------
$('#targets').addEventListener('click', ev => {
  const b = ev.target.closest('[data-target]');
  if (!b || b.dataset.target === targetName) return;
  targetName = b.dataset.target;
  lag = null; syncState = null; lagFor = '';
  loaded = {};
  savePlace();
  renderRail(); renderStatus();
  if (!currentTable()) pickDefaultTable(); else selectTable(tableName);
  refreshLag();
});

$('#tables').addEventListener('click', ev => {
  const b = ev.target.closest('[data-table]');
  if (b) selectTable(b.dataset.table);
});

async function selectTable(n) {
  tableName = n;
  rowOffset = 0;
  cols = null;
  fields = [];
  loaded = {};
  lastRows = []; lastHits = [];
  savePlace();
  renderRail(); renderStatus(); renderTabs();
  $('#rowsResult').innerHTML = '';
  $('#rowsPager').innerHTML = '';
  $('#searchResult').innerHTML = '<p class="state">enter a query to search ' + esc(n) + '.</p>';
  await loadSchema();
  loadTab();
}

// ---------- tabs ----------
function renderTabs() {
  const known = !!currentTable();   // before the first /api/status the tabs stay as loaded
  const fts = ftsColumn();
  if (tab === 'search' && known && !fts) tab = 'rows';
  $$('#tabs button').forEach(b => {
    const isSearch = b.dataset.tab === 'search';
    b.disabled = isSearch && known && !fts;
    b.title = isSearch && known && !fts ? 'no full-text index on this table' : '';
    b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  });
  TABS.forEach(t => { $('#tab-' + t).hidden = t !== tab; });
}

$('#tabs').addEventListener('click', ev => {
  const b = ev.target.closest('[data-tab]');
  if (!b || b.disabled || b.dataset.tab === tab) return;
  tab = b.dataset.tab;
  savePlace();
  renderTabs();
  loadTab();
});

function loadTab() {
  if (tab === 'rows' && !loaded.rows) loadRows();
  else if (tab === 'stats') loadStats();
  else if (tab === 'schema' && !loaded.schema) loadSchema();
}

// ---------- schema ----------
async function loadSchema() {
  const box = $('#schemaResult');
  box.innerHTML = '<div class="loading"></div>';
  try {
    const r = await api(base() + '/schema');
    fields = r.fields || [];
    loaded.schema = true;
    renderPicker();
    const info = currentTable() || {};
    const rows = fields.map(f => {
      const notes = [];
      if (info.fts === f.name) notes.push('full-text indexed');
      if (info.stamp === f.name) notes.push('sync cursor');
      if (f.name === 'id') notes.push('merge key');
      return '<tr><td>' + esc(f.name) + '</td><td>' + esc(f.type) + '</td><td>' + (f.nullable ? 'yes' : 'no') +
        '</td><td>' + esc(notes.join(', ')) + '</td></tr>';
    }).join('');
    box.innerHTML = '<div class="wrap"><table class="grid"><thead><tr><th>field</th><th>type</th><th>nullable</th><th>role</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      '<p class="hint">' + fields.length + ' fields · schema is fixed in src/sync.ts; PocketBase columns outside it are dropped on import.</p>';
  } catch (e) {
    box.innerHTML = '<p class="state err">' + esc(e.message) + '</p>';
  }
}

// ---------- column picker ----------
function renderPicker() {
  const chosen = cols || fields.map(f => f.name);
  $('#colsMenu').innerHTML =
    '<div class="row"><button type="button" data-cols="all">All</button>' +
    '<button type="button" data-cols="none">None</button></div>' +
    fields.map(f => '<label><input type="checkbox" data-col="' + esc(f.name) + '"' +
      (chosen.indexOf(f.name) >= 0 ? ' checked' : '') + '>' + esc(f.name) + '</label>').join('');
  $('#colsSummary').textContent = cols ? 'Columns (' + cols.length + '/' + fields.length + ')' : 'Columns (all)';
}

$('#colsMenu').addEventListener('click', ev => {
  const b = ev.target.closest('[data-cols]');
  if (!b) return;
  cols = b.dataset.cols === 'all' ? null : [];
  renderPicker();
  rowOffset = 0;
  loadRows();
});

$('#colsMenu').addEventListener('change', ev => {
  if (!ev.target.matches('[data-col]')) return;
  cols = $$('#colsMenu [data-col]').filter(c => c.checked).map(c => c.dataset.col);
  if (cols.length === fields.length) cols = null;
  $('#colsSummary').textContent = cols ? 'Columns (' + cols.length + '/' + fields.length + ')' : 'Columns (all)';
  rowOffset = 0;
  loadRows();
});

// ---------- rows ----------
$('#rowsForm').addEventListener('submit', ev => { ev.preventDefault(); rowOffset = 0; loadRows(); });
$('#rowsClear').addEventListener('click', () => { $('#whereIn').value = ''; rowOffset = 0; loadRows(); });
$('#limitIn').addEventListener('change', () => { rowOffset = 0; loadRows(); });

$('#rowsPager').addEventListener('click', ev => {
  const b = ev.target.closest('[data-page]');
  if (!b || b.disabled) return;
  const limit = Number($('#limitIn').value) || 50;
  rowOffset = b.dataset.page === 'prev' ? Math.max(0, rowOffset - limit) : rowOffset + limit;
  loadRows();
});

async function loadRows() {
  if (!tableName) return;
  const box = $('#rowsResult');
  box.innerHTML = '<div class="loading"></div><p class="state">loading rows…</p>';
  const p = new URLSearchParams();
  const where = $('#whereIn').value.trim();
  if (where) p.set('where', where);
  p.set('limit', String(Number($('#limitIn').value) || 50));
  p.set('offset', String(rowOffset));
  if (cols && cols.length) p.set('select', cols.join(','));
  if (cols && !cols.length) { box.innerHTML = '<p class="state">no columns selected.</p>'; $('#rowsPager').innerHTML = ''; return; }
  try {
    const r = await api(base() + '/rows?' + p.toString());
    lastRows = r.rows || [];
    loaded.rows = true;
    renderRows(r, where);
  } catch (e) {
    lastRows = [];
    $('#rowsPager').innerHTML = '';
    box.innerHTML = '<p class="state err">' + esc(e.message) + '</p>';
  }
}

function cellOf(v) {
  if (v === null || v === undefined || v === '') return { cls: 'nil', text: '–' };
  if (typeof v === 'number') return { cls: 'num', text: fmtN(v) };
  if (typeof v === 'boolean') return { cls: '', text: String(v) };
  const s = String(v).replace(/\s+/g, ' ').trim();
  return { cls: '', text: s.length > 220 ? s.slice(0, 220) + '…' : s };
}

function renderRows(r, where) {
  const rows = r.rows || [];
  const to = r.offset + rows.length;
  const total = r.total >= 0 ? fmtN(r.total) : 'unknown';
  const atEnd = r.total >= 0 ? to >= r.total : rows.length < r.limit;
  $('#rowsPager').innerHTML =
    '<button type="button" class="btn" data-page="prev"' + (r.offset <= 0 ? ' disabled' : '') + '>Prev</button>' +
    '<button type="button" class="btn" data-page="next"' + (atEnd ? ' disabled' : '') + '>Next</button>' +
    '<span>' + (rows.length ? fmtN(r.offset + 1) + '–' + fmtN(to) : '0') + ' of ' + total + '</span>' +
    '<span class="grow reason">' + (where ? 'where ' + esc(where) : 'no filter') + ' · limit ' + r.limit + '</span>';

  if (!rows.length) {
    $('#rowsResult').innerHTML = '<p class="state">no rows' + (where ? ' match this predicate' : '') + '.</p>';
    return;
  }
  const keys = Object.keys(rows[0]);
  const head = keys.map(k => '<th>' + esc(k) + '</th>').join('');
  const body = rows.map((row, i) => '<tr tabindex="0" data-row="' + i + '">' + keys.map(k => {
    const c = cellOf(row[k]);
    return '<td class="' + c.cls + '">' + esc(c.text) + '</td>';
  }).join('') + '</tr>').join('');
  $('#rowsResult').innerHTML = '<div class="wrap"><table class="grid"><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>' +
    '<p class="hint">Click a row for the full record.</p>';
}

$('#rowsResult').addEventListener('click', ev => {
  const tr = ev.target.closest('[data-row]');
  if (tr) openRecord(lastRows[Number(tr.dataset.row)]);
});
$('#rowsResult').addEventListener('keydown', ev => {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const tr = ev.target.closest('[data-row]');
  if (tr) { ev.preventDefault(); openRecord(lastRows[Number(tr.dataset.row)]); }
});

// ---------- search ----------
$('#searchForm').addEventListener('submit', ev => { ev.preventDefault(); loadSearch(); });

function queryTerms(q) {
  return q.split(/[^\p{L}\p{N}_]+/u).filter(t => t.length > 1);
}

// Match on the raw text, escape each piece afterwards: marking already-escaped
// HTML would let a term like "amp" split an entity such as &amp; in two.
function highlight(text, terms) {
  const raw = String(text === null || text === undefined ? '' : text);
  if (!terms.length) return esc(raw);
  const alt = terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  let re;
  try { re = new RegExp('(' + alt + ')', 'gi'); } catch (e) { return esc(raw); }
  return raw.split(re).map((part, i) => (i % 2 ? '<mark>' + esc(part) + '</mark>' : esc(part))).join('');
}

function snippet(text, terms) {
  const s = String(text === null || text === undefined ? '' : text);
  const low = s.toLowerCase();
  let at = -1;
  terms.forEach(t => {
    const i = low.indexOf(t.toLowerCase());
    if (i >= 0 && (at < 0 || i < at)) at = i;
  });
  const start = at < 0 ? 0 : Math.max(0, at - 240);
  const end = Math.min(s.length, start + 900);
  return (start > 0 ? '…' : '') + s.slice(start, end) + (end < s.length ? '…' : '');
}

async function loadSearch() {
  const q = $('#qIn').value.trim();
  const box = $('#searchResult');
  if (!q) { box.innerHTML = '<p class="state">enter a query.</p>'; return; }
  box.innerHTML = '<div class="loading"></div><p class="state">searching…</p>';
  const p = new URLSearchParams();
  p.set('q', q);
  p.set('limit', String(Number($('#sLimitIn').value) || 50));
  const where = $('#sWhereIn').value.trim();
  if (where) p.set('where', where);
  try {
    const r = await api(base() + '/search?' + p.toString());
    lastHits = r.rows || [];
    renderHits(lastHits, q, where);
  } catch (e) {
    lastHits = [];
    box.innerHTML = '<p class="state err">' + esc(e.message) + '</p>';
  }
}

function renderHits(rows, q, where) {
  const box = $('#searchResult');
  if (!rows.length) {
    box.innerHTML = '<p class="state">no hits for ' + esc(q) + (where ? ' with ' + esc(where) : '') + '.</p>';
    return;
  }
  const terms = queryTerms(q);
  const col = ftsColumn() || 'text';
  const head = '<p class="hint">' + rows.length + ' hits on ' + esc(tableName) + '.' + esc(col) +
    ' · BM25 score, best first' + (where ? ' · where ' + esc(where) : '') + '</p>';
  box.innerHTML = head + rows.map((row, i) => {
    const meta = ['ts', 'role', 'type', 'iso_week', 'session'].filter(k => row[k])
      .map(k => '<span class="' + (k === 'role' ? 'who ' + esc(String(row[k])) : '') + '">' + esc(String(row[k])) + '</span>').join('');
    return '<article class="hit" tabindex="0" data-hit="' + i + '">' +
      '<header><span class="score">' + (typeof row._score === 'number' ? row._score.toFixed(3) : '–') + '</span>' +
      meta + '<span class="reason">' + esc(row.id || '') + '</span></header>' +
      '<div class="text">' + highlight(snippet(row[col], terms), terms) + '</div></article>';
  }).join('');
}

$('#searchResult').addEventListener('click', ev => {
  const h = ev.target.closest('[data-hit]');
  if (h) openRecord(lastHits[Number(h.dataset.hit)]);
});
$('#searchResult').addEventListener('keydown', ev => {
  if (ev.key !== 'Enter' && ev.key !== ' ') return;
  const h = ev.target.closest('[data-hit]');
  if (h) { ev.preventDefault(); openRecord(lastHits[Number(h.dataset.hit)]); }
});

// ---------- stats ----------
async function loadStats() {
  const box = $('#statsResult');
  box.innerHTML = '<div class="loading"></div><p class="state">reading stats…</p>';
  try {
    const r = await api(base() + '/stats');
    const st = r.stats || {};
    const fr = st.fragmentStats || {};
    const cards = [
      ['rows', fmtN(r.rows)],
      ['version', fmtN(r.version)],
      ['versions kept', fmtN(r.versions)],
      ['indices', fmtN((r.indices || []).length)],
      ['fragments', fmtN(fr.numFragments)],
      ['small fragments', fmtN(fr.numSmallFragments)],
      ['total bytes', fmtBytes(st.totalBytes)],
    ];
    let html = '<div class="stats">' + cards.map(c =>
      '<div><span class="lbl">' + esc(c[0]) + '</span><b>' + esc(c[1]) + '</b></div>').join('') + '</div>';

    const idx = r.indices || [];
    html += '<p class="section-head">Indices</p>';
    html += idx.length
      ? '<div class="wrap"><table class="grid"><thead><tr><th>name</th><th>type</th><th>columns</th></tr></thead><tbody>' +
        idx.map(i => '<tr><td>' + esc(i.name) + '</td><td>' + esc(i.indexType || i.type) + '</td><td>' +
          esc((i.columns || []).join(', ')) + '</td></tr>').join('') + '</tbody></table></div>'
      : '<p class="state">no indices on this table.</p>';

    const lens = fr.lengths;
    if (lens) {
      html += '<p class="section-head">Fragment lengths</p><div class="stats">' +
        ['min', 'p25', 'p50', 'p75', 'p99', 'max', 'mean'].filter(k => k in lens).map(k =>
          '<div><span class="lbl">' + esc(k) + '</span><b>' + fmtN(lens[k]) + '</b></div>').join('') + '</div>';
    }
    html += '<p class="section-head">Raw stats</p><pre class="code">' + esc(JSON.stringify(st, null, 2)) + '</pre>';
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = '<p class="state err">' + esc(e.message) + '</p>';
  }
}

// ---------- record drawer ----------
const drawer = $('#drawer');
$('#drawerClose').addEventListener('click', () => drawer.close());
$('#drawerCopy').addEventListener('click', async () => {
  const note = $('#drawerNote');
  try {
    await navigator.clipboard.writeText(JSON.stringify(drawerRecord, null, 2));
    note.textContent = 'copied to clipboard';
  } catch (e) {
    note.textContent = 'copy failed: ' + e.message;
  }
});

async function openRecord(row) {
  if (!row) return;
  drawerRecord = row;
  $('#drawerTitle').innerHTML = esc(tableName) + ' · <code>' + esc(row.id || '(no id)') + '</code>';
  $('#drawerNote').textContent = '';
  $('#drawerBody').innerHTML = renderRecord(row);
  if (!drawer.open) drawer.showModal();
  // The grid may be showing a subset of columns; fetch the whole record by id.
  if (row.id && cols) {
    try {
      const q = new URLSearchParams({ limit: '1', where: "id = '" + String(row.id).replace(/'/g, "''") + "'" });
      const r = await api(base() + '/rows?' + q.toString());
      if (r.rows && r.rows[0] && drawerRecord === row) {
        drawerRecord = r.rows[0];
        $('#drawerBody').innerHTML = renderRecord(r.rows[0]);
        $('#drawerNote').textContent = 'full record reloaded by id';
      }
    } catch (e) {
      $('#drawerNote').textContent = 'could not reload the full record: ' + e.message;
    }
  }
}

function renderRecord(row) {
  return Object.keys(row).map(k => {
    const v = row[k];
    let body;
    if (v === null || v === undefined || v === '') {
      body = '<p class="v nil">–</p>';
    } else if (k === 'tools' || (typeof v === 'string' && /^[[{]/.test(v.trim()))) {
      let pretty = String(v);
      try { pretty = JSON.stringify(JSON.parse(String(v)), null, 2); } catch (e) { /* not JSON after all */ }
      body = '<pre>' + esc(pretty) + '</pre>';
    } else if (typeof v === 'string' && (v.length > 120 || v.indexOf('\n') >= 0)) {
      body = '<pre>' + esc(v) + '</pre>';
    } else if (typeof v === 'number') {
      body = '<p class="v">' + fmtN(v) + '</p>';
    } else {
      body = '<p class="v">' + esc(String(v)) + '</p>';
    }
    return '<div class="rec"><span class="k">' + esc(k) + '</span>' + body + '</div>';
  }).join('');
}

// ---------- boot ----------
(async function start() {
  const place = loadPlace();
  if (place.targetName) targetName = place.targetName;
  if (place.tableName) tableName = place.tableName;
  if (TABS.indexOf(place.tab) >= 0) tab = place.tab;
  renderTabs();
  await refreshStatus(false);
  const t = currentTarget();
  if (t && t.tables) {
    const names = Object.keys(t.tables);
    const want = names.indexOf(tableName) >= 0 ? tableName
      : (names.indexOf('events') >= 0 ? 'events' : names[0] || '');
    if (want) await selectTable(want);
  }
  booted = true;
  refreshLag();
  setInterval(() => {
    pollTick++;
    refreshStatus(pollTick % LAG_EVERY === 0);
  }, POLL_MS);
})();
