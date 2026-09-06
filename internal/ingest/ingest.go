// Package ingest applies a chunk of parsed transcript lines to the store.
//
// The contract with clients (the Rust CLI, the Go scanner, anything else)
// is byte-offset optimistic concurrency: the client says "I read this file
// from offset A to offset B and found these events"; the server accepts
// only if its recorded offset for that file is still A. A mismatch means
// another client got there first and the caller should re-read state and
// try again — the same idea as session-viewer's session_tail_state, made
// multi-writer safe.
package ingest

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/subscriptions"
	"github.com/pocketbase/pocketbase/tools/types"

	"structor/internal/jsonl"
	"structor/internal/schema"
)

// Request is the JSON body of POST /api/structor/ingest.
type Request struct {
	Project Project       `json:"project"`
	Session Session       `json:"session"`
	Chunk   ChunkState    `json:"chunk"`
	Events  []jsonl.Event `json:"events"`
	Writer  string        `json:"writer,omitempty"` // cli | server-scan; recorded on the import run
}

type Project struct {
	Path       string `json:"path"`
	Name       string `json:"name"`
	EncodedDir string `json:"encoded_dir"`
	Host       string `json:"host"`
}

type Session struct {
	SessionID  string `json:"session_id"`
	FilePath   string `json:"file_path"`
	Tier       string `json:"tier"`
	ByteOffset int64  `json:"byte_offset"` // offset the client started reading from
	FileSize   int64  `json:"file_size"`
	FileMtime  int64  `json:"file_mtime"`
}

type ChunkState struct {
	NextOffset int64 `json:"next_offset"`
	LinesSeen  int64 `json:"lines_seen"`
}

// Result reports what the store did with the request.
type Result struct {
	SessionRecordID string   `json:"session_record_id"`
	Inserted        int      `json:"inserted"`
	Skipped         int      `json:"skipped"`
	ByteOffset      int64    `json:"byte_offset"`
	Weeks           []string `json:"weeks"`

	live *LiveMessage // filled during Apply, broadcast after commit
}

// LiveTopic is the custom PocketBase realtime topic the Live tab subscribes
// to. Events are inserted with raw SQL (no per-record hooks), so the store
// publishes its own compact message per ingest instead of relying on
// collection realtime; only superuser-authenticated clients receive it.
const LiveTopic = "structor/live"

// LiveEvent is one freshly indexed conversational row, trimmed for a feed.
type LiveEvent struct {
	UUID   string   `json:"uuid"`
	TS     string   `json:"ts"`
	Role   string   `json:"role"`
	Type   string   `json:"type"`
	Text   string   `json:"text"`
	Tools  []string `json:"tools,omitempty"`
	LineNo int64    `json:"line_no"`
}

// LiveMessage is what one ingest publishes: which session grew, by how
// much, and up to LiveEventCap of the new conversational rows.
type LiveMessage struct {
	At         string      `json:"at"`
	SessionID  string      `json:"session_id"`
	Project    string      `json:"project"`
	Host       string      `json:"host"`
	Writer     string      `json:"writer"`
	Inserted   int         `json:"inserted"`
	Skipped    int         `json:"skipped"`
	ByteOffset int64       `json:"byte_offset"`
	Events     []LiveEvent `json:"events"`
	Truncated  bool        `json:"truncated"` // more rows were inserted than Events carries
}

const (
	LiveEventCap = 40  // rows per message; a first full scan would otherwise flood the browser
	liveTextCap  = 280 // bytes of text per row in the feed
)

// broadcastLive delivers msg to every realtime client subscribed to LiveTopic
// whose auth is a superuser. Errors are impossible to act on here, so none
// are returned; a dropped feed message is recovered by the next status poll.
func broadcastLive(app core.App, msg *LiveMessage) {
	if msg == nil {
		return
	}
	broker := app.SubscriptionsBroker()
	if broker == nil {
		return
	}
	var data []byte
	for _, client := range broker.Clients() {
		if client.IsDiscarded() || !client.HasSubscription(LiveTopic) {
			continue
		}
		// "auth" is apis.RealtimeClientAuthKey; the literal avoids importing apis here.
		auth, _ := client.Get("auth").(*core.Record)
		if auth == nil || !auth.IsSuperuser() {
			continue
		}
		if data == nil {
			var err error
			if data, err = json.Marshal(msg); err != nil {
				return
			}
		}
		client.Send(subscriptions.Message{Name: LiveTopic, Data: data})
	}
}

// ErrOffsetConflict is returned when the stored byte_offset differs from
// the offset the client claims to have started at.
var ErrOffsetConflict = errors.New("byte_offset conflict")

// ConflictError carries the server's current state so the client can resume.
type ConflictError struct {
	Have int64 `json:"have"`
	Want int64 `json:"want"`
}

func (c *ConflictError) Error() string {
	return fmt.Sprintf("%v: server at %d, client started at %d", ErrOffsetConflict, c.Have, c.Want)
}

func (c *ConflictError) Unwrap() error { return ErrOffsetConflict }

// Apply writes the request in one transaction.
func Apply(app core.App, req Request, loc *time.Location) (Result, error) {
	if req.Session.SessionID == "" || req.Session.FilePath == "" {
		return Result{}, errors.New("session_id and file_path are required")
	}
	if req.Project.Path == "" {
		return Result{}, errors.New("project.path is required")
	}
	var res Result
	var live *LiveMessage
	err := app.RunInTransaction(func(tx core.App) error {
		project, err := upsertProject(tx, req.Project)
		if err != nil {
			return err
		}
		defer func() { // project name is only final after the cwd update below
			if live != nil {
				live.Project = project.GetString("cwd")
				if live.Project == "" {
					live.Project = project.GetString("path")
				}
			}
		}()
		session, created, err := findOrCreateSession(tx, project, req.Session)
		if err != nil {
			return err
		}
		if !created && session.GetInt64("byte_offset") != req.Session.ByteOffset {
			return &ConflictError{Have: session.GetInt64("byte_offset"), Want: req.Session.ByteOffset}
		}

		weeks := map[string]bool{}
		live = &LiveMessage{
			At: types.NowDateTime().String(), SessionID: req.Session.SessionID, Host: req.Project.Host,
			Writer: req.Writer, Events: []LiveEvent{},
		}
		if live.Writer == "" {
			live.Writer = "cli"
		}
		var firstTS, lastTS types.DateTime
		if !created {
			firstTS = session.GetDateTime("first_ts")
			lastTS = session.GetDateTime("last_ts")
		}
		firstPrompt := session.GetString("first_prompt")
		gitBranch := session.GetString("git_branch")
		cwd := session.GetString("cwd") // latest seen, kept on the session
		shortestCwd := ""                // shortest in this chunk, offered to the project
		model := session.GetString("model")

		for _, ev := range req.Events {
			if ev.UUID == "" || ev.TS.IsZero() {
				res.Skipped++
				continue
			}
			week := jsonl.ISOWeek(ev.TS, loc)
			ts, _ := types.ParseDateTime(ev.TS.UTC())
			toolsJSON := "[]"
			if len(ev.Tools) > 0 {
				b, _ := json.Marshal(ev.Tools)
				toolsJSON = string(b)
			}
			text := jsonl.Truncate(ev.Text, jsonl.MaxText)
			now := types.NowDateTime()
			q := tx.DB().NewQuery(`INSERT OR IGNORE INTO ` + schema.Events + `
				(id, session, uuid, parent_uuid, type, role, ts, iso_week, text, tools, model, sidechain, line_no, raw_bytes, created)
				VALUES ({:id}, {:session}, {:uuid}, {:parent_uuid}, {:type}, {:role}, {:ts}, {:iso_week}, {:text}, {:tools}, {:model}, {:sidechain}, {:line_no}, {:raw_bytes}, {:created})`)
			r, err := q.Bind(dbx.Params{
				"id":          core.GenerateDefaultRandomId(),
				"session":     session.Id,
				"uuid":        ev.UUID,
				"parent_uuid": ev.ParentUUID,
				"type":        ev.Type,
				"role":        ev.Role,
				"ts":          ts.String(),
				"iso_week":    week,
				"text":        text,
				"tools":       toolsJSON,
				"model":       ev.Model,
				"sidechain":   ev.Sidechain,
				"line_no":     ev.LineNo,
				"raw_bytes":   ev.RawBytes,
				"created":     now.String(),
			}).Execute()
			if err != nil {
				return fmt.Errorf("insert event %s: %w", ev.UUID, err)
			}
			n, _ := r.RowsAffected()
			if n == 0 {
				res.Skipped++
				continue
			}
			res.Inserted++
			weeks[week] = true
			if ev.Role != "" && (text != "" || len(ev.Tools) > 0) {
				if len(live.Events) < LiveEventCap {
					live.Events = append(live.Events, LiveEvent{
						UUID: ev.UUID, TS: ts.String(), Role: ev.Role, Type: ev.Type,
						Text: jsonl.Truncate(text, liveTextCap), Tools: ev.Tools, LineNo: ev.LineNo,
					})
				} else {
					live.Truncated = true
				}
			}
			if firstTS.IsZero() || ts.Time().Before(firstTS.Time()) {
				firstTS = ts
			}
			if lastTS.IsZero() || ts.Time().After(lastTS.Time()) {
				lastTS = ts
			}
			if firstPrompt == "" && ev.Role == "user" && strings.TrimSpace(ev.Text) != "" {
				firstPrompt = truncate(ev.Text, 4000)
			}
			if ev.GitBranch != "" {
				gitBranch = ev.GitBranch
			}
			if ev.CWD != "" {
				cwd = ev.CWD
				if shortestCwd == "" || len(ev.CWD) < len(shortestCwd) {
					shortestCwd = ev.CWD
				}
			}
			if ev.Model != "" {
				model = ev.Model
			}
		}

		// The encoded dir name loses the dash/dot/slash distinction, so the
		// guessed path is ambiguous; a transcript's own cwd is authoritative.
		// Keep the shortest cwd seen: a session started in a subdirectory must
		// not rename the project to that subdirectory.
		if shortestCwd != "" {
			if old := project.GetString("cwd"); old == "" || len(shortestCwd) < len(old) {
				project.Set("cwd", shortestCwd)
				project.Set("name", filepath.Base(shortestCwd))
				if err := tx.Save(project); err != nil {
					return fmt.Errorf("update project from cwd: %w", err)
				}
			}
		}

		session.Set("byte_offset", req.Chunk.NextOffset)
		session.Set("file_size", req.Session.FileSize)
		session.Set("file_mtime", req.Session.FileMtime)
		session.Set("lines_seen", session.GetInt64("lines_seen")+req.Chunk.LinesSeen)
		session.Set("event_count", session.GetInt64("event_count")+int64(res.Inserted))
		session.Set("first_ts", firstTS)
		session.Set("last_ts", lastTS)
		session.Set("first_prompt", firstPrompt)
		session.Set("git_branch", gitBranch)
		session.Set("cwd", cwd)
		session.Set("model", model)
		if err := tx.Save(session); err != nil {
			return fmt.Errorf("save session: %w", err)
		}

		// The import log is the evidence the Intake page shows: one row per
		// request that moved the offset or inserted rows. No-op polls are not
		// recorded, so the log stays a history of writes, not of checks.
		if res.Inserted > 0 || req.Chunk.NextOffset != req.Session.ByteOffset || created {
			runs, err := tx.FindCollectionByNameOrId(schema.ImportRuns)
			if err != nil {
				return err
			}
			run := core.NewRecord(runs)
			run.Set("session", session.Id)
			run.Set("project", project.Id)
			run.Set("from_offset", req.Session.ByteOffset)
			run.Set("to_offset", req.Chunk.NextOffset)
			run.Set("lines", req.Chunk.LinesSeen)
			run.Set("inserted", res.Inserted)
			run.Set("skipped", res.Skipped)
			run.Set("host", req.Project.Host)
			writer := req.Writer
			if writer == "" {
				writer = "cli"
			}
			run.Set("writer", writer)
			if err := tx.Save(run); err != nil {
				return fmt.Errorf("save import run: %w", err)
			}
		}

		for w := range weeks {
			if err := recomputeWeek(tx, session, project, w); err != nil {
				return err
			}
			res.Weeks = append(res.Weeks, w)
		}
		res.SessionRecordID = session.Id
		res.ByteOffset = req.Chunk.NextOffset
		return nil
	})
	if err == nil && live != nil && res.Inserted > 0 {
		live.Inserted, live.Skipped, live.ByteOffset = res.Inserted, res.Skipped, res.ByteOffset
		res.live = live
		broadcastLive(app, live) // after commit, so a subscriber can read what it was told about
	}
	return res, err
}

// Live returns the message this ingest published, or nil when nothing was
// inserted. Exposed for tests and for callers that want to echo the feed.
func (r Result) Live() *LiveMessage { return r.live }

func truncate(s string, n int) string { return jsonl.Truncate(s, n) }

// likeEsc escapes LIKE metacharacters so user input matches literally.
// Every LIKE that takes user input pairs it with ESCAPE '\'.
func likeEsc(s string) string {
	s = strings.ReplaceAll(s, `\`, `\\`)
	s = strings.ReplaceAll(s, `%`, `\%`)
	return strings.ReplaceAll(s, `_`, `\_`)
}

// projectExpr is the one display/filter expression for a project's path:
// the real cwd learned from transcripts, else the dash-decoded guess. Every
// endpoint that filters by project substring uses this same expression so
// a value that works in one tab works in all of them.
const projectExpr = "COALESCE(NULLIF(p.cwd,''), p.path)"

// Sentinel errors for ReadSession.
var (
	ErrNoSession        = errors.New("no session matches that id")
	ErrAmbiguousSession = errors.New("session id prefix matches more than one session")
)

func upsertProject(app core.App, p Project) (*core.Record, error) {
	rec, err := app.FindFirstRecordByData(schema.Projects, "path", p.Path)
	if err == nil {
		changed := false
		// The client's name is a guess from the encoded folder; once a real cwd
		// is known the name derives from it and the guess must not overwrite it.
		if p.Name != "" && rec.GetString("name") == "" {
			rec.Set("name", p.Name)
			changed = true
		}
		if p.EncodedDir != "" && rec.GetString("encoded_dir") != p.EncodedDir {
			rec.Set("encoded_dir", p.EncodedDir)
			changed = true
		}
		if p.Host != "" && rec.GetString("host") != p.Host {
			rec.Set("host", p.Host)
			changed = true
		}
		if changed {
			if err := app.Save(rec); err != nil {
				return nil, err
			}
		}
		return rec, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	col, err := app.FindCollectionByNameOrId(schema.Projects)
	if err != nil {
		return nil, err
	}
	rec = core.NewRecord(col)
	rec.Set("path", p.Path)
	rec.Set("name", p.Name)
	rec.Set("encoded_dir", p.EncodedDir)
	rec.Set("host", p.Host)
	if err := app.Save(rec); err != nil {
		return nil, fmt.Errorf("create project: %w", err)
	}
	return rec, nil
}

func findOrCreateSession(app core.App, project *core.Record, s Session) (*core.Record, bool, error) {
	// Tail state is per file, so the file path is the lookup key. session_id is
	// unique too; a second file claiming an existing session_id (a backup copy
	// of the same transcript) is refused rather than silently merged.
	rec, err := app.FindFirstRecordByData(schema.Sessions, "file_path", s.FilePath)
	if err == nil {
		return rec, false, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, false, err
	}
	if other, err := app.FindFirstRecordByData(schema.Sessions, "session_id", s.SessionID); err == nil {
		return nil, false, fmt.Errorf("session_id %q already indexed from %s", s.SessionID, other.GetString("file_path"))
	}
	col, err := app.FindCollectionByNameOrId(schema.Sessions)
	if err != nil {
		return nil, false, err
	}
	rec = core.NewRecord(col)
	rec.Set("session_id", s.SessionID)
	rec.Set("project", project.Id)
	rec.Set("file_path", s.FilePath)
	rec.Set("tier", s.Tier)
	rec.Set("byte_offset", 0)
	rec.Set("file_size", s.FileSize)
	rec.Set("file_mtime", s.FileMtime)
	rec.Set("lines_seen", 0)
	rec.Set("event_count", 0)
	if err := app.Save(rec); err != nil {
		return nil, false, fmt.Errorf("create session: %w", err)
	}
	return rec, true, nil
}

type weekAgg struct {
	Count     int64  `db:"c"`
	Users     int64  `db:"u"`
	Assistant int64  `db:"a"`
	Tools     int64  `db:"t"`
	First     string `db:"f"`
	Last      string `db:"l"`
}

func recomputeWeek(app core.App, session, project *core.Record, week string) error {
	var agg weekAgg
	err := app.DB().NewQuery(`SELECT COUNT(*) AS c,
			SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS u,
			SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS a,
			SUM(CASE WHEN tools <> '[]' AND tools <> '' THEN 1 ELSE 0 END) AS t,
			MIN(ts) AS f, MAX(ts) AS l
		FROM ` + schema.Events + ` WHERE session={:s} AND iso_week={:w}`).
		Bind(dbx.Params{"s": session.Id, "w": week}).One(&agg)
	if err != nil {
		return fmt.Errorf("aggregate week %s: %w", week, err)
	}
	rec, err := app.FindFirstRecordByFilter(schema.SessionWeeks, "session={:s} && iso_week={:w}", dbx.Params{"s": session.Id, "w": week})
	if err != nil {
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		col, err := app.FindCollectionByNameOrId(schema.SessionWeeks)
		if err != nil {
			return err
		}
		rec = core.NewRecord(col)
		rec.Set("session", session.Id)
		rec.Set("project", project.Id)
		rec.Set("iso_week", week)
	}
	rec.Set("event_count", agg.Count)
	rec.Set("user_count", agg.Users)
	rec.Set("assistant_count", agg.Assistant)
	rec.Set("tool_count", agg.Tools)
	rec.Set("first_ts", agg.First)
	rec.Set("last_ts", agg.Last)
	if err := app.Save(rec); err != nil {
		return fmt.Errorf("save session_week %s: %w", week, err)
	}
	return nil
}

// TailState is what a client needs to resume a file.
type TailState struct {
	SessionID  string `json:"session_id" db:"session_id"`
	FilePath   string `json:"file_path" db:"file_path"`
	ByteOffset int64  `json:"byte_offset" db:"byte_offset"`
	FileSize   int64  `json:"file_size" db:"file_size"`
	FileMtime  int64  `json:"file_mtime" db:"file_mtime"`
	LinesSeen  int64  `json:"lines_seen" db:"lines_seen"`
}

// AllState returns tail state for every known session, keyed by file_path.
func AllState(app core.App) (map[string]TailState, error) {
	var rows []TailState
	err := app.DB().NewQuery(`SELECT session_id, file_path, byte_offset, file_size, file_mtime, lines_seen FROM ` + schema.Sessions).All(&rows)
	if err != nil {
		return nil, err
	}
	out := make(map[string]TailState, len(rows))
	for _, r := range rows {
		out[r.FilePath] = r
	}
	return out, nil
}

// Status is the dashboard / tray / MCP summary.
type Status struct {
	Projects     int64       `json:"projects"`
	Sessions     int64       `json:"sessions"`
	Events       int64       `json:"events"`
	SessionWeeks int64       `json:"session_weeks"`
	LastIngest   string      `json:"last_ingest"`
	LastEventTS  string      `json:"last_event_ts"`
	Weeks        []WeekRow   `json:"weeks"`
	Version      string      `json:"version"`
	Time         string      `json:"time"`
	TZ           string      `json:"tz"` // zone used for ISO weeks and day buckets
}

type WeekRow struct {
	Week     string `json:"week" db:"iso_week"`
	Sessions int64  `json:"sessions" db:"sessions"`
	Events   int64  `json:"events" db:"events"`
	Users    int64  `json:"user_msgs" db:"users"`
}

func GetStatus(app core.App, version string, loc *time.Location) (Status, error) {
	st := Status{Version: version, Time: time.Now().UTC().Format(time.RFC3339), TZ: loc.String()}
	count := func(table string) int64 {
		var n int64
		_ = app.DB().NewQuery("SELECT COUNT(*) FROM " + table).Row(&n)
		return n
	}
	st.Projects = count(schema.Projects)
	st.Sessions = count(schema.Sessions)
	st.Events = count(schema.Events)
	st.SessionWeeks = count(schema.SessionWeeks)
	_ = app.DB().NewQuery("SELECT COALESCE(MAX(updated),'') FROM " + schema.Sessions).Row(&st.LastIngest)
	_ = app.DB().NewQuery("SELECT COALESCE(MAX(last_ts),'') FROM " + schema.Sessions).Row(&st.LastEventTS)
	err := app.DB().NewQuery(`SELECT iso_week, COUNT(*) AS sessions, SUM(event_count) AS events, SUM(user_count) AS users
		FROM ` + schema.SessionWeeks + ` GROUP BY iso_week ORDER BY iso_week DESC LIMIT 16`).All(&st.Weeks)
	if err != nil {
		return st, err
	}
	if st.Weeks == nil {
		st.Weeks = []WeekRow{}
	}
	return st, nil
}

// SearchHit is one matching event with its session context.
type SearchHit struct {
	SessionID string `json:"session_id" db:"session_id"`
	Project   string `json:"project" db:"project_path"`
	TS        string `json:"ts" db:"ts"`
	Week      string `json:"iso_week" db:"iso_week"`
	Role      string `json:"role" db:"role"`
	Type      string `json:"type" db:"type"`
	Snippet   string `json:"snippet" db:"text"`
	Tools     string `json:"tools" db:"tools"`
	LineNo    int64  `json:"line_no" db:"line_no"`
	UUID      string `json:"uuid" db:"uuid"`
}

type SearchOpts struct {
	Query     string
	Project   string // substring of the project path (cwd or decoded guess)
	ProjectID string // exact project record id; preferred by the UI
	Week      string
	Session   string // session id prefix
	Role      string
	Limit     int
}

// Search does a case-insensitive substring match over event text. With an
// empty Query it is a plain newest-first window over the scope.
func Search(app core.App, o SearchOpts) ([]SearchHit, error) {
	if o.Limit <= 0 || o.Limit > 200 {
		o.Limit = 30
	}
	// Only conversational rows: hook attachments (role '') and assistant turns
	// that carry nothing but tool calls with no text are noise in a stream.
	where := []string{"e.role <> ''", "(e.text <> '' OR (e.tools <> '[]' AND e.tools <> ''))"}
	params := dbx.Params{"limit": o.Limit}
	if q := strings.TrimSpace(o.Query); q != "" {
		where = append(where, `e.text LIKE {:q} ESCAPE '\'`)
		params["q"] = "%" + likeEsc(q) + "%"
	}
	if o.ProjectID != "" {
		where = append(where, "s.project = {:pid}")
		params["pid"] = o.ProjectID
	}
	if o.Project != "" {
		where = append(where, projectExpr+` LIKE {:p} ESCAPE '\'`)
		params["p"] = "%" + likeEsc(o.Project) + "%"
	}
	if o.Week != "" {
		where = append(where, "e.iso_week = {:w}")
		params["w"] = o.Week
	}
	if o.Session != "" {
		where = append(where, `s.session_id LIKE {:sid} ESCAPE '\'`)
		params["sid"] = likeEsc(o.Session) + "%"
	}
	if o.Role != "" {
		where = append(where, "e.role = {:r}")
		params["r"] = o.Role
	}
	var hits []SearchHit
	err := app.DB().NewQuery(`SELECT s.session_id, COALESCE(NULLIF(p.cwd,''), p.path) AS project_path, e.ts, e.iso_week, e.role, e.type,
			substr(e.text, 1, 600) AS text, e.tools, e.line_no, e.uuid
		FROM ` + schema.Events + ` e
		JOIN ` + schema.Sessions + ` s ON s.id = e.session
		LEFT JOIN ` + schema.Projects + ` p ON p.id = s.project
		WHERE ` + strings.Join(where, " AND ") + `
		ORDER BY e.ts DESC LIMIT {:limit}`).Bind(params).All(&hits)
	if hits == nil {
		hits = []SearchHit{}
	}
	return hits, err
}

// SessionRow is a session summary for listings.
type SessionRow struct {
	SessionID   string `json:"session_id" db:"session_id"`
	Project     string `json:"project" db:"project_path"`
	Tier        string `json:"tier" db:"tier"`
	FirstTS     string `json:"first_ts" db:"first_ts"`
	LastTS      string `json:"last_ts" db:"last_ts"`
	EventCount  int64  `json:"event_count" db:"event_count"`
	FirstPrompt string `json:"first_prompt" db:"first_prompt"`
	GitBranch   string `json:"git_branch" db:"git_branch"`
	FilePath    string `json:"file_path" db:"file_path"`
}

func ListSessions(app core.App, project, projectID, week string, limit int) ([]SessionRow, error) {
	if limit <= 0 || limit > 500 {
		limit = 50
	}
	where := []string{"1=1"}
	params := dbx.Params{"limit": limit}
	if projectID != "" {
		where = append(where, "s.project = {:pid}")
		params["pid"] = projectID
	}
	if project != "" {
		where = append(where, projectExpr+` LIKE {:p} ESCAPE '\'`)
		params["p"] = "%" + likeEsc(project) + "%"
	}
	join := ""
	if week != "" {
		join = " JOIN " + schema.SessionWeeks + " w ON w.session = s.id AND w.iso_week = {:w} "
		params["w"] = week
	}
	var rows []SessionRow
	err := app.DB().NewQuery(`SELECT DISTINCT s.session_id, COALESCE(NULLIF(p.cwd,''), p.path) AS project_path, s.tier, s.first_ts, s.last_ts,
			s.event_count, substr(s.first_prompt,1,300) AS first_prompt, s.git_branch, s.file_path
		FROM ` + schema.Sessions + ` s LEFT JOIN ` + schema.Projects + ` p ON p.id = s.project ` + join + `
		WHERE ` + strings.Join(where, " AND ") + ` ORDER BY s.last_ts DESC LIMIT {:limit}`).Bind(params).All(&rows)
	if rows == nil {
		rows = []SessionRow{}
	}
	return rows, err
}

// ProjectRow is a project summary. Path is the dash-decoded guess from the
// encoded directory name (ambiguous: '-' may be '/', '-' or '.'); Cwd is the
// real working directory learned from transcripts and wins when present.
type ProjectRow struct {
	ID         string `json:"id" db:"id"`
	Path       string `json:"path" db:"path"`
	Cwd        string `json:"cwd" db:"cwd"`
	EncodedDir string `json:"encoded_dir" db:"encoded_dir"`
	Name       string `json:"name" db:"name"`
	Host       string `json:"host" db:"host"`
	Sessions   int64  `json:"sessions" db:"sessions"`
	Events     int64  `json:"events" db:"events"`
	LastTS     string `json:"last_ts" db:"last_ts"`
}

func ListProjects(app core.App, limit int) ([]ProjectRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 200
	}
	var rows []ProjectRow
	err := app.DB().NewQuery(`SELECT p.id, p.path, p.cwd, p.encoded_dir, p.name, p.host, COUNT(s.id) AS sessions,
			COALESCE(SUM(s.event_count),0) AS events, COALESCE(MAX(s.last_ts),'') AS last_ts
		FROM ` + schema.Projects + ` p LEFT JOIN ` + schema.Sessions + ` s ON s.project = p.id
		GROUP BY p.id ORDER BY last_ts DESC LIMIT {:limit}`).Bind(dbx.Params{"limit": limit}).All(&rows)
	if rows == nil {
		rows = []ProjectRow{}
	}
	return rows, err
}

// ReconcileProjects sets each project's cwd and name from the shortest
// non-empty cwd among its sessions. It repairs stores that were filled before
// cwd tracking existed and is cheap enough to run at every boot.
func ReconcileProjects(app core.App) (int, error) {
	type row struct {
		ID   string `db:"id"`
		Cwd  string `db:"cwd"`
		Cur  string `db:"cur"`
		Name string `db:"name"`
	}
	var rows []row
	err := app.DB().NewQuery(`SELECT p.id, COALESCE(p.cwd,'') AS cur, COALESCE(p.name,'') AS name,
			COALESCE((SELECT s.cwd FROM ` + schema.Sessions + ` s WHERE s.project = p.id AND s.cwd <> ''
			 ORDER BY LENGTH(s.cwd) ASC, s.cwd ASC LIMIT 1), '') AS cwd
		FROM ` + schema.Projects + ` p`).All(&rows)
	if err != nil {
		return 0, err
	}
	updated := 0
	for _, r := range rows {
		if r.Cwd == "" || (r.Cwd == r.Cur && r.Name == filepath.Base(r.Cwd)) {
			continue
		}
		rec, err := app.FindRecordById(schema.Projects, r.ID)
		if err != nil {
			return updated, err
		}
		rec.Set("cwd", r.Cwd)
		rec.Set("name", filepath.Base(r.Cwd))
		if err := app.Save(rec); err != nil {
			return updated, fmt.Errorf("reconcile project %s: %w", r.ID, err)
		}
		updated++
	}
	return updated, nil
}

// EventRow is a transcript line for read_session.
type EventRow struct {
	TS     string `json:"ts" db:"ts"`
	Role   string `json:"role" db:"role"`
	Type   string `json:"type" db:"type"`
	Text   string `json:"text" db:"text"`
	Tools  string `json:"tools" db:"tools"`
	LineNo int64  `json:"line_no" db:"line_no"`
}

// ResolveSession turns an exact session id or unambiguous prefix into the
// session record id. Wildcards in the input are matched literally.
func ResolveSession(app core.App, sessionIDPrefix string) (string, error) {
	if strings.TrimSpace(sessionIDPrefix) == "" {
		return "", ErrNoSession
	}
	var ids []string
	err := app.DB().NewQuery(`SELECT id FROM ` + schema.Sessions + ` WHERE session_id LIKE {:sid} ESCAPE '\' ORDER BY session_id LIMIT 2`).
		Bind(dbx.Params{"sid": likeEsc(sessionIDPrefix) + "%"}).Column(&ids)
	if err != nil {
		return "", err
	}
	switch len(ids) {
	case 0:
		return "", ErrNoSession
	case 1:
		return ids[0], nil
	default:
		// an exact match wins over a longer sibling that shares the prefix
		var exact string
		_ = app.DB().NewQuery(`SELECT id FROM ` + schema.Sessions + ` WHERE session_id = {:sid}`).Bind(dbx.Params{"sid": sessionIDPrefix}).Row(&exact)
		if exact != "" {
			return exact, nil
		}
		return "", ErrAmbiguousSession
	}
}

// ReadSession pages one session's conversational rows in time order. The id
// may be a prefix, but it must resolve to exactly one session.
func ReadSession(app core.App, sessionIDPrefix string, offset, limit int) ([]EventRow, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	id, err := ResolveSession(app, sessionIDPrefix)
	if err != nil {
		return nil, err
	}
	var rows []EventRow
	err = app.DB().NewQuery(`SELECT e.ts, e.role, e.type, substr(e.text,1,4000) AS text, e.tools, e.line_no
		FROM ` + schema.Events + ` e
		WHERE e.session = {:id} AND e.role <> '' AND (e.text <> '' OR (e.tools <> '[]' AND e.tools <> ''))
		ORDER BY e.ts ASC LIMIT {:limit} OFFSET {:offset}`).
		Bind(dbx.Params{"id": id, "limit": limit, "offset": offset}).All(&rows)
	if rows == nil {
		rows = []EventRow{}
	}
	return rows, err
}

// DayRow is one (calendar day, session) record for the History ledger.
// Days are computed in the configured zone, so a Bangkok evening is not
// filed under the UTC date of the next morning.
type DayRow struct {
	Day       string `json:"day" db:"day"` // YYYY-MM-DD in loc
	SessionID string `json:"session_id" db:"session_id"`
	Project   string `json:"project" db:"project_path"`
	Events    int64  `json:"events" db:"events"`
	Users     int64  `json:"user_msgs" db:"users"`
	FirstTS   string `json:"first_ts" db:"first_ts"`
	LastTS    string `json:"last_ts" db:"last_ts"`
	Preview   string `json:"preview" db:"preview"`
	Branch    string `json:"git_branch" db:"git_branch"`
}

// Days returns per-(day, session) activity between from and to (inclusive,
// YYYY-MM-DD in loc). At most 31 days are served per call. truncated is true
// when more rows matched than limit allowed, so a caller never mistakes a
// cut-off ledger for empty days.
//
// The day boundary uses one UTC offset, taken at the range start. That is
// exact for fixed-offset zones (Asia/Bangkok, the default) and off by one
// hour on the transition day for DST zones — documented, not hidden.
func Days(app core.App, from, to string, project, projectID string, loc *time.Location, limit int) ([]DayRow, bool, error) {
	if limit <= 0 || limit > 5000 {
		limit = 500
	}
	start, err := time.ParseInLocation("2006-01-02", from, loc)
	if err != nil {
		return nil, false, fmt.Errorf("from: %w", err)
	}
	end, err := time.ParseInLocation("2006-01-02", to, loc)
	if err != nil {
		return nil, false, fmt.Errorf("to: %w", err)
	}
	end = end.AddDate(0, 0, 1)
	if end.Sub(start) > 31*24*time.Hour {
		end = start.AddDate(0, 0, 31)
	}
	_, offsetSec := start.In(loc).Zone()
	params := dbx.Params{
		"from":   start.UTC().Format("2006-01-02 15:04:05.000Z"),
		"to":     end.UTC().Format("2006-01-02 15:04:05.000Z"),
		"offset": fmt.Sprintf("%+d seconds", offsetSec),
		"limit":  limit + 1, // one extra row tells us whether we were cut off
	}
	where := "e.ts >= {:from} AND e.ts < {:to} AND e.role <> ''"
	if projectID != "" {
		where += " AND s.project = {:pid}"
		params["pid"] = projectID
	}
	if project != "" {
		where += " AND " + projectExpr + ` LIKE {:p} ESCAPE '\'`
		params["p"] = "%" + likeEsc(project) + "%"
	}
	var rows []DayRow
	err = app.DB().NewQuery(`SELECT date(e.ts, {:offset}) AS day, s.session_id,
			COALESCE(NULLIF(p.cwd,''), p.path) AS project_path,
			COUNT(*) AS events,
			SUM(CASE WHEN e.role='user' THEN 1 ELSE 0 END) AS users,
			MIN(e.ts) AS first_ts, MAX(e.ts) AS last_ts,
			substr(COALESCE((SELECT u.text FROM ` + schema.Events + ` u WHERE u.session = s.id AND u.role='user'
				AND date(u.ts, {:offset}) = date(e.ts, {:offset}) AND u.text <> '' ORDER BY u.ts LIMIT 1), ''), 1, 200) AS preview,
			s.git_branch
		FROM ` + schema.Events + ` e
		JOIN ` + schema.Sessions + ` s ON s.id = e.session
		LEFT JOIN ` + schema.Projects + ` p ON p.id = s.project
		WHERE ` + where + `
		GROUP BY day, s.id
		ORDER BY day DESC, last_ts DESC LIMIT {:limit}`).Bind(params).All(&rows)
	if rows == nil {
		rows = []DayRow{}
	}
	truncated := false
	if len(rows) > limit {
		rows = rows[:limit]
		truncated = true
	}
	return rows, truncated, err
}

// ---------- intake: detection + import evidence ----------

// IntakeSummary is the top strip of the Intake workspace.
type IntakeSummary struct {
	Files         int64  `json:"files" db:"files"`
	BytesTracked  int64  `json:"bytes_tracked" db:"bytes_tracked"`
	BytesIndexed  int64  `json:"bytes_indexed" db:"bytes_indexed"`
	PendingFiles  int64  `json:"pending_files" db:"pending_files"` // file_size > byte_offset: partial tail held back
	PendingBytes  int64  `json:"pending_bytes" db:"pending_bytes"`
	LastIngest    string `json:"last_ingest" db:"last_ingest"`
	RunsToday     int64  `json:"runs_today" db:"runs_today"`
	InsertedToday int64  `json:"inserted_today" db:"inserted_today"`
	Hosts         string `json:"hosts" db:"hosts"`
}

func GetIntakeSummary(app core.App) (IntakeSummary, error) {
	var s IntakeSummary
	err := app.DB().NewQuery(`SELECT COUNT(*) AS files,
			COALESCE(SUM(file_size),0) AS bytes_tracked,
			COALESCE(SUM(byte_offset),0) AS bytes_indexed,
			COALESCE(SUM(CASE WHEN file_size > byte_offset THEN 1 ELSE 0 END),0) AS pending_files,
			COALESCE(SUM(CASE WHEN file_size > byte_offset THEN file_size - byte_offset ELSE 0 END),0) AS pending_bytes,
			COALESCE(MAX(updated),'') AS last_ingest
		FROM ` + schema.Sessions).One(&s)
	if err != nil {
		return s, err
	}
	dayAgo := types.NowDateTime().Time().Add(-24 * time.Hour).UTC().Format("2006-01-02 15:04:05.000Z")
	err = app.DB().NewQuery(`SELECT COUNT(*) AS runs_today, COALESCE(SUM(inserted),0) AS inserted_today,
			COALESCE(GROUP_CONCAT(DISTINCT host),'') AS hosts
		FROM ` + schema.ImportRuns + ` WHERE created >= {:d}`).Bind(dbx.Params{"d": dayAgo}).One(&s)
	return s, err
}

// RunRow is one import-log line.
type RunRow struct {
	Created    string `json:"created" db:"created"`
	SessionID  string `json:"session_id" db:"session_id"`
	Project    string `json:"project" db:"project_path"`
	FilePath   string `json:"file_path" db:"file_path"`
	FromOffset int64  `json:"from_offset" db:"from_offset"`
	ToOffset   int64  `json:"to_offset" db:"to_offset"`
	Lines      int64  `json:"lines" db:"lines"`
	Inserted   int64  `json:"inserted" db:"inserted"`
	Skipped    int64  `json:"skipped" db:"skipped"`
	Host       string `json:"host" db:"host"`
	Writer     string `json:"writer" db:"writer"`
}

func ListRuns(app core.App, limit int) ([]RunRow, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var rows []RunRow
	err := app.DB().NewQuery(`SELECT r.created, s.session_id, ` + projectExpr + ` AS project_path, s.file_path,
			r.from_offset, r.to_offset, r.lines, r.inserted, r.skipped, r.host, r.writer
		FROM ` + schema.ImportRuns + ` r
		JOIN ` + schema.Sessions + ` s ON s.id = r.session
		LEFT JOIN ` + schema.Projects + ` p ON p.id = r.project
		ORDER BY r.created DESC LIMIT {:limit}`).Bind(dbx.Params{"limit": limit}).All(&rows)
	if rows == nil {
		rows = []RunRow{}
	}
	return rows, err
}

// FileRow is one tracked transcript with its tail state.
type FileRow struct {
	SessionID  string `json:"session_id" db:"session_id"`
	Project    string `json:"project" db:"project_path"`
	FilePath   string `json:"file_path" db:"file_path"`
	Tier       string `json:"tier" db:"tier"`
	FileSize   int64  `json:"file_size" db:"file_size"`
	ByteOffset int64  `json:"byte_offset" db:"byte_offset"`
	LinesSeen  int64  `json:"lines_seen" db:"lines_seen"`
	EventCount int64  `json:"event_count" db:"event_count"`
	FileMtime  int64  `json:"file_mtime" db:"file_mtime"`
	Updated    string `json:"updated" db:"updated"`
}

// ListFiles returns tracked files, most recently written first. pendingOnly
// keeps files whose size exceeds the indexed offset.
func ListFiles(app core.App, pendingOnly bool, limit int) ([]FileRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	where := "1=1"
	if pendingOnly {
		where = "s.file_size > s.byte_offset"
	}
	var rows []FileRow
	err := app.DB().NewQuery(`SELECT s.session_id, ` + projectExpr + ` AS project_path, s.file_path, s.tier, s.file_size,
			s.byte_offset, s.lines_seen, s.event_count, s.file_mtime, s.updated
		FROM ` + schema.Sessions + ` s LEFT JOIN ` + schema.Projects + ` p ON p.id = s.project
		WHERE ` + where + ` ORDER BY s.updated DESC LIMIT {:limit}`).Bind(dbx.Params{"limit": limit}).All(&rows)
	if rows == nil {
		rows = []FileRow{}
	}
	return rows, err
}

// WriterRow is one (host, writer) pair with its recent activity — the
// landing page's "who is feeding this store" panel.
type WriterRow struct {
	Host     string `json:"host" db:"host"`
	Writer   string `json:"writer" db:"writer"`
	LastRun  string `json:"last_run" db:"last_run"`
	Runs24h  int64  `json:"runs_24h" db:"runs_24h"`
	Added24h int64  `json:"inserted_24h" db:"inserted_24h"`
	Files    int64  `json:"files" db:"files"`
}

func ListWriters(app core.App) ([]WriterRow, error) {
	dayAgo := types.NowDateTime().Time().Add(-24 * time.Hour).UTC().Format("2006-01-02 15:04:05.000Z")
	var rows []WriterRow
	err := app.DB().NewQuery(`SELECT host, writer, MAX(created) AS last_run,
			SUM(CASE WHEN created >= {:d} THEN 1 ELSE 0 END) AS runs_24h,
			COALESCE(SUM(CASE WHEN created >= {:d} THEN inserted ELSE 0 END),0) AS inserted_24h,
			COUNT(DISTINCT session) AS files
		FROM ` + schema.ImportRuns + ` GROUP BY host, writer ORDER BY last_run DESC`).Bind(dbx.Params{"d": dayAgo}).All(&rows)
	if rows == nil {
		rows = []WriterRow{}
	}
	return rows, err
}

// Connections counts MCP clients and live OAuth tokens.
type Connections struct {
	OAuthClients int64 `json:"oauth_clients" db:"oauth_clients"`
	ActiveTokens int64 `json:"active_tokens" db:"active_tokens"`
}

func GetConnections(app core.App) (Connections, error) {
	var c Connections
	now := types.NowDateTime().String()
	err := app.DB().NewQuery(`SELECT (SELECT COUNT(*) FROM ` + schema.OAuthClients + `) AS oauth_clients,
		(SELECT COUNT(*) FROM ` + schema.OAuthTokens + ` WHERE kind='access' AND revoked=0 AND expires > {:now}) AS active_tokens`).
		Bind(dbx.Params{"now": now}).One(&c)
	return c, err
}

// PruneRuns deletes import-log rows older than keep. Returns rows removed.
func PruneRuns(app core.App, keep time.Duration) (int64, error) {
	cutoff := types.NowDateTime().Time().Add(-keep).UTC().Format("2006-01-02 15:04:05.000Z")
	res, err := app.DB().NewQuery("DELETE FROM " + schema.ImportRuns + " WHERE created < {:c}").Bind(dbx.Params{"c": cutoff}).Execute()
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// WeekLedger lists (session, week) rows for a week or a session.
type LedgerRow struct {
	Week      string `json:"iso_week" db:"iso_week"`
	SessionID string `json:"session_id" db:"session_id"`
	Project   string `json:"project" db:"project_path"`
	Events    int64  `json:"events" db:"event_count"`
	Users     int64  `json:"user_msgs" db:"user_count"`
	Assistant int64  `json:"assistant_msgs" db:"assistant_count"`
	Tools     int64  `json:"tool_calls" db:"tool_count"`
	FirstTS   string `json:"first_ts" db:"first_ts"`
	LastTS    string `json:"last_ts" db:"last_ts"`
}

func WeekLedger(app core.App, week, sessionPrefix string, limit int) ([]LedgerRow, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	where := []string{"1=1"}
	params := dbx.Params{"limit": limit}
	if week != "" {
		where = append(where, "w.iso_week = {:w}")
		params["w"] = week
	}
	if sessionPrefix != "" {
		where = append(where, `s.session_id LIKE {:sid} ESCAPE '\'`)
		params["sid"] = likeEsc(sessionPrefix) + "%"
	}
	var rows []LedgerRow
	err := app.DB().NewQuery(`SELECT w.iso_week, s.session_id, COALESCE(NULLIF(p.cwd,''), p.path) AS project_path, w.event_count, w.user_count,
			w.assistant_count, w.tool_count, w.first_ts, w.last_ts
		FROM ` + schema.SessionWeeks + ` w JOIN ` + schema.Sessions + ` s ON s.id = w.session
		LEFT JOIN ` + schema.Projects + ` p ON p.id = w.project
		WHERE ` + strings.Join(where, " AND ") + ` ORDER BY w.iso_week DESC, w.last_ts DESC LIMIT {:limit}`).
		Bind(params).All(&rows)
	if rows == nil {
		rows = []LedgerRow{}
	}
	return rows, err
}
