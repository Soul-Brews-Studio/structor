//! structor-cli — index Claude Code session transcripts into a Structor
//! (embedded PocketBase) server, incrementally, and follow changes.
//!
//! Tail-state contract (shared with the Go server): for every jsonl file the
//! server remembers `byte_offset` (always on a line boundary) and `lines_seen`.
//! The CLI reads from that offset, sends only complete lines, and tells the
//! server which offset it started from; a 409 means someone else advanced the
//! file first and the CLI re-reads state and retries.
//!
//!   structor-cli --url http://127.0.0.1:8090 --email a@b --password p scan
//!   structor-cli watch            # scan, then follow ~/.claude/projects
//!   structor-cli status

use anyhow::{anyhow, bail, Context, Result};
use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant, UNIX_EPOCH};
use walkdir::WalkDir;

const MAX_TEXT: usize = 8000;
const BATCH: usize = 500;

#[derive(Parser, Debug)]
#[command(name = "structor-cli", version, about)]
struct Cli {
    /// Server origin
    #[arg(long, env = "STRUCTOR_URL", default_value = "http://127.0.0.1:8090")]
    url: String,
    /// Superuser email (username)
    #[arg(long, env = "STRUCTOR_EMAIL")]
    email: Option<String>,
    /// Superuser password
    #[arg(long, env = "STRUCTOR_PASSWORD")]
    password: Option<String>,
    /// Pre-issued PocketBase token (skips login)
    #[arg(long, env = "STRUCTOR_TOKEN")]
    token: Option<String>,
    /// Host label stored on projects
    #[arg(long, env = "STRUCTOR_HOST")]
    host: Option<String>,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Check the server and credentials
    Login,
    /// Print store status
    Status,
    /// One incremental pass over a projects tree (default ~/.claude/projects)
    Scan {
        dir: Option<PathBuf>,
        /// Extra trees (backups, other tiers) scanned after the main one
        #[arg(long)]
        extra: Vec<PathBuf>,
    },
    /// Scan, then follow the tree for changes (filesystem events + periodic rescan)
    Watch {
        dir: Option<PathBuf>,
        #[arg(long)]
        extra: Vec<PathBuf>,
        /// Full rescan interval in seconds (safety net for missed events)
        #[arg(long, default_value_t = 120)]
        interval: u64,
        /// Debounce window for filesystem events, milliseconds
        #[arg(long, default_value_t = 1500)]
        debounce: u64,
    },
}

// ---------- wire types (must match Go: internal/ingest + internal/jsonl) ----------

#[derive(Serialize, Debug, Clone, Default)]
struct Event {
    uuid: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    parent_uuid: String,
    #[serde(rename = "type")]
    kind: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    role: String,
    ts: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    text: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tools: Vec<String>,
    #[serde(skip_serializing_if = "String::is_empty")]
    model: String,
    sidechain: bool,
    line_no: i64,
    raw_bytes: i64,
    #[serde(skip_serializing_if = "String::is_empty")]
    session_id: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    cwd: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    git_branch: String,
}

#[derive(Serialize)]
struct IngestRequest<'a> {
    project: ProjectInfo,
    session: SessionInfo,
    chunk: ChunkState,
    events: &'a [Event],
    /// recorded on the server's import log; "cli" for this tool
    writer: &'static str,
}

#[derive(Serialize, Clone)]
struct ProjectInfo {
    path: String,
    name: String,
    encoded_dir: String,
    host: String,
}

#[derive(Serialize, Clone)]
struct SessionInfo {
    session_id: String,
    file_path: String,
    tier: String,
    byte_offset: i64,
    file_size: i64,
    file_mtime: i64,
}

#[derive(Serialize, Clone, Copy)]
struct ChunkState {
    next_offset: i64,
    lines_seen: i64,
}

#[derive(Deserialize, Debug, Clone, Default)]
struct TailState {
    #[serde(default)]
    session_id: String,
    #[serde(default)]
    byte_offset: i64,
    #[serde(default)]
    file_size: i64,
    #[serde(default)]
    lines_seen: i64,
}

#[derive(Deserialize, Debug)]
struct IngestResult {
    inserted: i64,
    skipped: i64,
    byte_offset: i64,
}

// ---------- parsing ----------

fn take_str(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn cap(mut s: String) -> String {
    if s.len() > MAX_TEXT {
        let mut end = MAX_TEXT;
        while !s.is_char_boundary(end) {
            end -= 1;
        }
        s.truncate(end);
    }
    s
}

fn flatten(content: Option<&Value>) -> (String, Vec<String>) {
    let mut text = String::new();
    let mut tools = Vec::new();
    match content {
        Some(Value::String(s)) => text.push_str(s),
        Some(Value::Array(blocks)) => {
            for b in blocks {
                match b.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        if !text.is_empty() {
                            text.push('\n');
                        }
                        text.push_str(b.get("text").and_then(Value::as_str).unwrap_or(""));
                    }
                    Some("tool_use") => {
                        let n = take_str(b, "name");
                        if !n.is_empty() {
                            tools.push(n);
                        }
                    }
                    Some("tool_result") => {
                        let (inner, _) = flatten(b.get("content"));
                        if !inner.is_empty() {
                            if !text.is_empty() {
                                text.push('\n');
                            }
                            text.push_str(&inner);
                        }
                    }
                    _ => {}
                }
                if text.len() > MAX_TEXT {
                    break;
                }
            }
        }
        _ => {}
    }
    (text, tools)
}

/// Parse one transcript line. None when it has no uuid/timestamp or is not JSON.
fn parse_line(line: &str, line_no: i64) -> Option<Event> {
    let t = line.trim();
    if !t.starts_with('{') {
        return None;
    }
    let v: Value = serde_json::from_str(t).ok()?;
    let uuid = take_str(&v, "uuid");
    let ts = take_str(&v, "timestamp");
    if uuid.is_empty() || ts.is_empty() {
        return None;
    }
    chrono::DateTime::parse_from_rfc3339(&ts).ok()?;
    let mut ev = Event {
        uuid,
        parent_uuid: take_str(&v, "parentUuid"),
        kind: take_str(&v, "type"),
        ts,
        sidechain: v.get("isSidechain").and_then(Value::as_bool).unwrap_or(false),
        line_no,
        raw_bytes: t.len() as i64,
        session_id: take_str(&v, "sessionId"),
        cwd: take_str(&v, "cwd"),
        git_branch: take_str(&v, "gitBranch"),
        ..Default::default()
    };
    if let Some(msg) = v.get("message").filter(|m| m.is_object()) {
        ev.role = take_str(msg, "role");
        ev.model = take_str(msg, "model");
        let (text, tools) = flatten(msg.get("content"));
        ev.text = text;
        ev.tools = tools;
    }
    if ev.text.is_empty() {
        ev.text = take_str(&v, "summary");
    }
    if ev.text.is_empty() {
        ev.text = flatten(v.get("content")).0;
    }
    ev.text = cap(ev.text);
    Some(ev)
}

struct Chunk {
    events: Vec<Event>,
    lines_seen: i64,
    next_offset: i64,
}

/// Read complete lines from `offset`; a trailing partial line is held back.
fn read_chunk(path: &Path, offset: i64, line_base: i64) -> Result<Chunk> {
    let mut f = File::open(path)?;
    f.seek(SeekFrom::Start(offset as u64))?;
    let mut r = BufReader::with_capacity(1 << 20, f);
    let mut buf = Vec::new();
    let mut c = Chunk { events: Vec::new(), lines_seen: 0, next_offset: offset };
    let mut line_no = line_base;
    loop {
        buf.clear();
        let n = r.read_until(b'\n', &mut buf)?;
        if n == 0 || buf.last() != Some(&b'\n') {
            break; // EOF or partial line
        }
        line_no += 1;
        c.lines_seen += 1;
        c.next_offset += n as i64;
        if let Ok(s) = std::str::from_utf8(&buf) {
            if let Some(ev) = parse_line(s, line_no) {
                c.events.push(ev);
            }
        }
    }
    Ok(c)
}

fn decode_project_dir(encoded: &str) -> String {
    match encoded.strip_prefix('-') {
        Some(rest) => format!("/{}", rest.replace('-', "/")),
        None => encoded.to_string(),
    }
}

/// Session id from a transcript path. `<uuid>.jsonl` → the stem; anything else
/// (workflow `journal.jsonl` files all share a name) → `<stem>@<parent dir>`.
/// Must agree with Go `scan.SessionIDFor`.
fn session_id_for(path: &Path) -> String {
    let stem = path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    if stem.len() >= 32 && stem.matches('-').count() >= 4 {
        return stem;
    }
    match path.parent().and_then(Path::file_name) {
        Some(p) if !p.is_empty() => format!("{stem}@{}", p.to_string_lossy()),
        _ => stem,
    }
}

fn classify(root: &Path, path: &Path) -> (String, String, String) {
    let rel = path.strip_prefix(root).unwrap_or(path);
    let parts: Vec<_> = rel.components().map(|c| c.as_os_str().to_string_lossy().to_string()).collect();
    let encoded = parts.first().cloned().unwrap_or_default();
    let mut tier = if parts.len() > 2 { "subagent" } else { "projects" };
    if !root.to_string_lossy().ends_with("/.claude/projects") {
        tier = "backup";
    }
    (decode_project_dir(&encoded), encoded, tier.to_string())
}

// ---------- client ----------

/// Long-lived client. PocketBase superuser tokens expire (24h by default), so a
/// watcher that logged in once dies quietly with 401 a day later; every request
/// therefore re-logs in on 401 and retries once. 429 (rate limit) backs off and
/// retries instead of dropping the batch.
struct Client {
    url: String,
    token: std::cell::RefCell<String>,
    creds: Option<(String, String)>,
    host: String,
    agent: ureq::Agent,
}

const RETRY_429: [u64; 3] = [1, 3, 8]; // seconds between retries on Too Many Requests

impl Client {
    fn login(agent: &ureq::Agent, url: &str, email: &str, password: &str) -> Result<String> {
        let resp: Value = agent
            .post(&format!("{url}/api/collections/_superusers/auth-with-password"))
            .send_json(serde_json::json!({"identity": email, "password": password}))
            .map_err(|e| anyhow!("login failed: {e}"))?
            .into_json()?;
        resp.get("token").and_then(Value::as_str).map(str::to_string).ok_or_else(|| anyhow!("no token in login response"))
    }

    fn connect(cli: &Cli) -> Result<Self> {
        let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(120)).build();
        let url = cli.url.trim_end_matches('/').to_string();
        let (token, creds) = match (&cli.token, &cli.email, &cli.password) {
            (Some(t), _, _) => (t.clone(), None),
            (None, Some(e), Some(p)) => (Self::login(&agent, &url, e, p)?, Some((e.clone(), p.clone()))),
            _ => bail!("need --token or --email + --password (or STRUCTOR_EMAIL / STRUCTOR_PASSWORD)"),
        };
        let host = cli
            .host
            .clone()
            .or_else(|| hostname::get().ok().map(|h| h.to_string_lossy().split('.').next().unwrap_or("").to_string()))
            .unwrap_or_default();
        Ok(Self { url, token: std::cell::RefCell::new(token), creds, host, agent })
    }

    /// Re-authenticate after a 401. Returns false when there are no credentials
    /// to re-login with (a fixed --token), so the caller surfaces the 401.
    fn relogin(&self) -> Result<bool> {
        let Some((e, p)) = &self.creds else { return Ok(false) };
        let t = Self::login(&self.agent, &self.url, e, p)?;
        *self.token.borrow_mut() = t;
        eprintln!("{} re-authenticated (token had expired)", chrono::Local::now().format("%H:%M:%S"));
        Ok(true)
    }

    fn token(&self) -> String { self.token.borrow().clone() }

    fn get(&self, path: &str) -> Result<Value> {
        let mut relogged = false;
        let mut waits = RETRY_429.iter();
        loop {
            let r = self.agent.get(&format!("{}{}", self.url, path)).set("Authorization", &self.token()).call();
            match r {
                Ok(r) => return Ok(r.into_json()?),
                Err(ureq::Error::Status(401, _)) if !relogged && self.relogin()? => relogged = true,
                Err(ureq::Error::Status(429, _)) => match waits.next() {
                    Some(s) => std::thread::sleep(Duration::from_secs(*s)),
                    None => bail!("GET {path}: HTTP 429 after retries"),
                },
                Err(e) => bail!("GET {path}: {e}"),
            }
        }
    }

    fn state(&self) -> Result<HashMap<String, TailState>> {
        let v = self.get("/api/structor/state")?;
        let m: HashMap<String, TailState> = serde_json::from_value(v.get("sessions").cloned().unwrap_or(Value::Object(Default::default())))?;
        Ok(m)
    }

    fn ingest(&self, req: &IngestRequest) -> Result<Result<IngestResult, TailState>> {
        let body = serde_json::to_value(req)?;
        let mut relogged = false;
        let mut waits = RETRY_429.iter();
        loop {
            let resp = self
                .agent
                .post(&format!("{}/api/structor/ingest", self.url))
                .set("Authorization", &self.token())
                .send_json(body.clone());
            match resp {
                Ok(r) => return Ok(Ok(r.into_json()?)),
                Err(ureq::Error::Status(409, r)) => {
                    let v: Value = r.into_json()?;
                    return Ok(Err(TailState { byte_offset: v.get("have").and_then(Value::as_i64).unwrap_or(0), ..Default::default() }));
                }
                Err(ureq::Error::Status(401, r)) => {
                    if !relogged && self.relogin()? { relogged = true; continue; }
                    bail!("ingest HTTP 401: {}", r.into_string().unwrap_or_default());
                }
                Err(ureq::Error::Status(429, _)) => match waits.next() {
                    Some(s) => std::thread::sleep(Duration::from_secs(*s)),
                    None => bail!("ingest HTTP 429 after retries"),
                },
                Err(ureq::Error::Status(code, r)) => bail!("ingest HTTP {code}: {}", r.into_string().unwrap_or_default()),
                Err(e) => bail!("ingest: {e}"),
            }
        }
    }
}

// ---------- scanning ----------

struct Scanner<'a> {
    client: &'a Client,
    state: HashMap<String, TailState>,
}

#[derive(Default, Debug)]
struct Report {
    files: usize,
    changed: usize,
    inserted: i64,
    skipped: i64,
    errors: Vec<String>,
}

impl<'a> Scanner<'a> {
    fn new(client: &'a Client) -> Result<Self> {
        Ok(Self { client, state: client.state()? })
    }

    fn scan_tree(&mut self, root: &Path) -> Report {
        let mut rep = Report::default();
        for entry in WalkDir::new(root)
            .into_iter()
            .filter_entry(|e| !(e.file_type().is_dir() && e.file_name() == "memory"))
            .filter_map(Result::ok)
        {
            if !entry.file_type().is_file() || entry.path().extension().map(|e| e != "jsonl").unwrap_or(true) {
                continue;
            }
            rep.files += 1;
            match self.scan_file(root, entry.path()) {
                Ok(Some(r)) => {
                    rep.changed += 1;
                    rep.inserted += r.inserted;
                    rep.skipped += r.skipped;
                }
                Ok(None) => {}
                Err(e) => rep.errors.push(format!("{}: {e:#}", entry.path().display())),
            }
        }
        rep
    }

    /// Returns Ok(None) when the file has nothing new.
    fn scan_file(&mut self, root: &Path, path: &Path) -> Result<Option<IngestResult>> {
        let key = path.to_string_lossy().to_string();
        let meta = std::fs::metadata(path)?;
        let size = meta.len() as i64;
        let prev = self.state.get(&key).cloned().unwrap_or_default();
        if size == prev.file_size && size == prev.byte_offset {
            return Ok(None);
        }
        let mtime = meta.modified().ok().and_then(|m| m.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64).unwrap_or(0);
        let session_id = session_id_for(path);
        let (project_path, encoded, tier) = classify(root, path);

        let mut start = prev.byte_offset;
        let mut line_base = prev.lines_seen;
        if size < start {
            start = 0; // truncated / rewritten
            line_base = 0;
        }
        let chunk = read_chunk(path, start, line_base).context("read")?;

        let project = ProjectInfo {
            path: project_path.clone(),
            name: project_path.rsplit('/').next().unwrap_or("").to_string(),
            encoded_dir: encoded,
            host: self.client.host.clone(),
        };
        let mut total = IngestResult { inserted: 0, skipped: 0, byte_offset: prev.byte_offset };
        let mut server_offset = prev.byte_offset; // what the server believes
        let mut prev_line = line_base; // last line number the server has been told about

        // Batches advance the server's offset stepwise; each batch's next_offset is
        // the end of its last line, so a crash between batches loses nothing.
        let batches: Vec<&[Event]> = if chunk.events.is_empty() { vec![&[][..]] } else { chunk.events.chunks(BATCH).collect() };
        let n = batches.len();
        for (i, batch) in batches.into_iter().enumerate() {
            let last = i + 1 == n;
            let (end_line, next_offset) = if last {
                (line_base + chunk.lines_seen, chunk.next_offset)
            } else {
                let l = batch.last().unwrap().line_no;
                (l, line_end_offset(path, l, line_base, start)?)
            };
            let lines = end_line - prev_line;
            prev_line = end_line;
            let req = IngestRequest {
                project: project.clone(),
                session: SessionInfo {
                    session_id: session_id.clone(),
                    file_path: key.clone(),
                    tier: tier.clone(),
                    byte_offset: server_offset,
                    file_size: size,
                    file_mtime: mtime,
                },
                chunk: ChunkState { next_offset, lines_seen: lines },
                events: batch,
                writer: "cli",
            };
            match self.client.ingest(&req)? {
                Ok(r) => {
                    total.inserted += r.inserted;
                    total.skipped += r.skipped;
                    total.byte_offset = r.byte_offset;
                    server_offset = r.byte_offset;
                }
                Err(conflict) => {
                    // someone else advanced this file: adopt their offset, retry next pass
                    self.state.insert(key.clone(), TailState { session_id, byte_offset: conflict.byte_offset, file_size: 0, lines_seen: prev.lines_seen });
                    bail!("offset conflict (server at {}), will retry next pass", conflict.byte_offset);
                }
            }
        }
        self.state.insert(
            key,
            TailState { session_id, byte_offset: total.byte_offset, file_size: size, lines_seen: line_base + chunk.lines_seen },
        );
        Ok(Some(total))
    }
}

/// Byte offset just past line `line_no` (1-based, counted from line_base at byte `start`).
fn line_end_offset(path: &Path, line_no: i64, line_base: i64, start: i64) -> Result<i64> {
    let mut f = File::open(path)?;
    f.seek(SeekFrom::Start(start as u64))?;
    let mut r = BufReader::with_capacity(1 << 20, f);
    let mut buf = Vec::new();
    let mut off = start;
    let mut n = line_base;
    while n < line_no {
        buf.clear();
        let k = r.read_until(b'\n', &mut buf)?;
        if k == 0 {
            break;
        }
        off += k as i64;
        n += 1;
    }
    Ok(off)
}

fn default_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")).join(".claude").join("projects")
}

fn print_report(label: &str, rep: &Report, elapsed: Duration) {
    println!(
        "{label}: files={} changed={} inserted={} skipped={} errors={} in {:.1}s",
        rep.files,
        rep.changed,
        rep.inserted,
        rep.skipped,
        rep.errors.len(),
        elapsed.as_secs_f64()
    );
    for e in &rep.errors {
        eprintln!("  ! {e}");
    }
}

fn run_scan(scanner: &mut Scanner, dirs: &[PathBuf]) {
    for d in dirs {
        let t = Instant::now();
        let rep = scanner.scan_tree(d);
        print_report(&d.display().to_string(), &rep, t.elapsed());
    }
}

fn watch(client: &Client, dirs: Vec<PathBuf>, interval: u64, debounce: u64) -> Result<()> {
    let mut scanner = Scanner::new(client)?;
    run_scan(&mut scanner, &dirs);

    let (tx, rx) = mpsc::channel();
    let mut debouncer = notify_debouncer_mini::new_debouncer(Duration::from_millis(debounce), move |res| {
        let _ = tx.send(res);
    })?;
    for d in &dirs {
        debouncer.watcher().watch(d, notify::RecursiveMode::Recursive)?;
        println!("watching {}", d.display());
    }
    let mut last_full = Instant::now();
    loop {
        match rx.recv_timeout(Duration::from_secs(5)) {
            Ok(Ok(events)) => {
                let mut paths: Vec<PathBuf> = events
                    .into_iter()
                    .map(|e| e.path)
                    .filter(|p| p.extension().map(|e| e == "jsonl").unwrap_or(false))
                    .collect();
                paths.sort();
                paths.dedup();
                for p in paths {
                    if !p.is_file() {
                        continue;
                    }
                    let root = dirs.iter().find(|d| p.starts_with(d)).cloned().unwrap_or_else(|| dirs[0].clone());
                    match scanner.scan_file(&root, &p) {
                        Ok(Some(r)) if r.inserted > 0 => println!(
                            "{} +{} events (offset {})",
                            chrono::Local::now().format("%H:%M:%S"),
                            r.inserted,
                            r.byte_offset
                        ),
                        Ok(_) => {}
                        Err(e) => eprintln!("  ! {}: {e:#}", p.display()),
                    }
                }
            }
            Ok(Err(e)) => eprintln!("watch error: {e:?}"),
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => bail!("watcher stopped"),
        }
        if last_full.elapsed() >= Duration::from_secs(interval) {
            // refresh server state too, in case another writer (server-side scan, second CLI) moved offsets
            if let Ok(s) = client.state() {
                scanner.state = s;
            }
            run_scan(&mut scanner, &dirs);
            last_full = Instant::now();
        }
    }
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match &cli.cmd {
        Cmd::Login => {
            let c = Client::connect(&cli)?;
            let st = c.get("/api/structor/status")?;
            println!("ok: {} — v{} — {} sessions, {} events", c.url, st["version"], st["sessions"], st["events"]);
        }
        Cmd::Status => {
            let c = Client::connect(&cli)?;
            println!("{}", serde_json::to_string_pretty(&c.get("/api/structor/status")?)?);
        }
        Cmd::Scan { dir, extra } => {
            let c = Client::connect(&cli)?;
            let mut dirs = vec![dir.clone().unwrap_or_else(default_dir)];
            dirs.extend(extra.iter().cloned());
            let mut scanner = Scanner::new(&c)?;
            run_scan(&mut scanner, &dirs);
        }
        Cmd::Watch { dir, extra, interval, debounce } => {
            let c = Client::connect(&cli)?;
            let mut dirs = vec![dir.clone().unwrap_or_else(default_dir)];
            dirs.extend(extra.iter().cloned());
            watch(&c, dirs, *interval, *debounce)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    const L1: &str = r#"{"uuid":"u1","parentUuid":null,"type":"user","timestamp":"2026-09-05T15:12:03Z","sessionId":"s","cwd":"/x","gitBranch":"main","message":{"role":"user","content":"hello"}}"#;
    const L2: &str = r#"{"uuid":"a1","parentUuid":"u1","type":"assistant","timestamp":"2026-09-05T15:12:05Z","message":{"role":"assistant","model":"m","content":[{"type":"text","text":"hi"},{"type":"tool_use","name":"Bash"}]}}"#;

    #[test]
    fn parses_user_and_assistant() {
        let u = parse_line(L1, 1).unwrap();
        assert_eq!((u.role.as_str(), u.text.as_str(), u.cwd.as_str(), u.git_branch.as_str()), ("user", "hello", "/x", "main"));
        let a = parse_line(L2, 2).unwrap();
        assert_eq!(a.tools, vec!["Bash"]);
        assert_eq!(a.model, "m");
        assert_eq!(a.parent_uuid, "u1");
        assert!(parse_line(r#"{"type":"mode"}"#, 3).is_none());
        assert!(parse_line("garbage", 4).is_none());
    }

    #[test]
    fn chunk_holds_back_partial_and_resumes() {
        let dir = std::env::temp_dir().join(format!("structor-cli-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("s.jsonl");
        std::fs::write(&p, format!("{L1}\n{{\"uuid\":\"part")).unwrap();
        let c = read_chunk(&p, 0, 0).unwrap();
        assert_eq!(c.events.len(), 1);
        assert_eq!(c.lines_seen, 1);
        assert_eq!(c.next_offset as usize, L1.len() + 1);

        let mut f = std::fs::OpenOptions::new().append(true).open(&p).unwrap();
        write!(f, "ial\",\"timestamp\":\"2026-09-05T15:12:04Z\",\"type\":\"user\"}}\n{L2}\n").unwrap();
        let c2 = read_chunk(&p, c.next_offset, c.lines_seen).unwrap();
        assert_eq!(c2.events.len(), 2);
        assert_eq!(c2.events[0].uuid, "partial");
        assert_eq!(c2.events[1].line_no, 3);
        assert_eq!(c2.next_offset as usize, std::fs::metadata(&p).unwrap().len() as usize);
        let line2 = r#"{"uuid":"partial","timestamp":"2026-09-05T15:12:04Z","type":"user"}"#;
        assert_eq!(line_end_offset(&p, 2, 1, c.next_offset).unwrap(), c.next_offset + line2.len() as i64 + 1);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn caps_text_on_char_boundary() {
        let s: String = "ก".repeat(MAX_TEXT);
        let out = cap(s);
        assert!(out.len() <= MAX_TEXT);
        assert!(std::str::from_utf8(out.as_bytes()).is_ok());
    }

    #[test]
    fn session_ids() {
        assert_eq!(session_id_for(Path::new("/p/-x/f1e856a2-53bd-46b9-b26a-7dca05a201e6.jsonl")), "f1e856a2-53bd-46b9-b26a-7dca05a201e6");
        assert_eq!(session_id_for(Path::new("/p/-x/abc/subagents/workflows/wf_c96a1543-c71/journal.jsonl")), "journal@wf_c96a1543-c71");
        assert_eq!(session_id_for(Path::new("/p/-x/abc/subagents/agent-1.jsonl")), "agent-1@subagents");
    }

    #[test]
    fn classify_tiers() {
        let root = Path::new("/Users/x/.claude/projects");
        let (p, e, t) = classify(root, Path::new("/Users/x/.claude/projects/-opt-Code-repo/abc.jsonl"));
        assert_eq!((p.as_str(), e.as_str(), t.as_str()), ("/opt/Code/repo", "-opt-Code-repo", "projects"));
        let (_, _, t) = classify(root, Path::new("/Users/x/.claude/projects/-opt-Code-repo/abc/sub/x.jsonl"));
        assert_eq!(t, "subagent");
        let (_, _, t) = classify(Path::new("/bk"), Path::new("/bk/-opt-Code-repo/abc.jsonl"));
        assert_eq!(t, "backup");
    }
}
