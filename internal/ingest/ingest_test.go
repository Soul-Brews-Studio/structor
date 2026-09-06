package ingest

import (
	"errors"
	"testing"
	"time"

	"github.com/pocketbase/pocketbase/tests"

	"structor/internal/jsonl"
	"structor/internal/schema"
)

func newApp(t *testing.T) *tests.TestApp {
	t.Helper()
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(app.Cleanup)
	if err := schema.Ensure(app); err != nil {
		t.Fatal(err)
	}
	// second call must be a no-op
	if err := schema.Ensure(app); err != nil {
		t.Fatalf("Ensure not idempotent: %v", err)
	}
	return app
}

func ev(uuid, role, text string, ts time.Time, tools ...string) jsonl.Event {
	return jsonl.Event{UUID: uuid, Role: role, Type: role, Text: text, TS: ts, Tools: tools, LineNo: 1, RawBytes: int64(len(text))}
}

func TestApplyThenResumeThenConflict(t *testing.T) {
	app := newApp(t)
	bkk, _ := time.LoadLocation("Asia/Bangkok")
	base := time.Date(2026, 9, 5, 15, 0, 0, 0, time.UTC) // Sat, W36 in Bangkok

	req := Request{
		Project: Project{Path: "/opt/x/repo", Name: "repo", EncodedDir: "-opt-x-repo", Host: "m5"},
		Session: Session{SessionID: "s1", FilePath: "/tmp/s1.jsonl", Tier: "projects", ByteOffset: 0, FileSize: 1000, FileMtime: 1},
		Chunk:   ChunkState{NextOffset: 900, LinesSeen: 3},
		Events: []jsonl.Event{
			ev("u1", "user", "first prompt here", base),
			ev("a1", "assistant", "reply", base.Add(time.Second), "Bash"),
			// Sunday 20:00 UTC = Monday 03:00 Bangkok → W37
			ev("u2", "user", "next week already", base.Add(29*time.Hour)),
		},
	}
	res, err := Apply(app, req, bkk)
	if err != nil {
		t.Fatal(err)
	}
	if res.Inserted != 3 || res.ByteOffset != 900 || len(res.Weeks) != 2 {
		t.Fatalf("first apply: %+v", res)
	}

	st, err := AllState(app)
	if err != nil {
		t.Fatal(err)
	}
	if s := st["/tmp/s1.jsonl"]; s.ByteOffset != 900 || s.LinesSeen != 3 || s.SessionID != "s1" {
		t.Fatalf("state after first apply: %+v", s)
	}

	// resume from 900: one new event, one duplicate uuid
	req2 := req
	req2.Session.ByteOffset = 900
	req2.Session.FileSize = 1200
	req2.Chunk = ChunkState{NextOffset: 1200, LinesSeen: 2}
	req2.Events = []jsonl.Event{ev("a1", "assistant", "dup", base), ev("a2", "assistant", "new", base.Add(2*time.Second))}
	res2, err := Apply(app, req2, bkk)
	if err != nil {
		t.Fatal(err)
	}
	if res2.Inserted != 1 || res2.Skipped != 1 {
		t.Fatalf("resume apply: %+v", res2)
	}

	// stale client: claims it started at 900 again
	_, err = Apply(app, req2, bkk)
	var conflict *ConflictError
	if !errors.As(err, &conflict) || conflict.Have != 1200 || conflict.Want != 900 {
		t.Fatalf("expected offset conflict, got %v", err)
	}

	status, err := GetStatus(app, "test")
	if err != nil {
		t.Fatal(err)
	}
	if status.Projects != 1 || status.Sessions != 1 || status.Events != 4 || status.SessionWeeks != 2 {
		t.Fatalf("status: %+v", status)
	}
	if status.Weeks[0].Week != "2026-W37" || status.Weeks[1].Week != "2026-W36" || status.Weeks[1].Events != 3 {
		t.Fatalf("weeks: %+v", status.Weeks)
	}

	ledger, err := WeekLedger(app, "2026-W36", "", 10)
	if err != nil || len(ledger) != 1 {
		t.Fatalf("ledger: %v %+v", err, ledger)
	}
	if ledger[0].Users != 1 || ledger[0].Assistant != 2 || ledger[0].Tools != 1 {
		t.Fatalf("ledger counts: %+v", ledger[0])
	}

	hits, err := Search(app, SearchOpts{Query: "PROMPT"})
	if err != nil || len(hits) != 1 || hits[0].SessionID != "s1" {
		t.Fatalf("search: %v %+v", err, hits)
	}
	sessions, err := ListSessions(app, "", "2026-W37", 10)
	if err != nil || len(sessions) != 1 || sessions[0].FirstPrompt != "first prompt here" {
		t.Fatalf("sessions by week: %v %+v", err, sessions)
	}
	rows, err := ReadSession(app, "s", 0, 10)
	if err != nil || len(rows) != 4 || rows[0].Text != "first prompt here" {
		t.Fatalf("read session: %v %+v", err, rows)
	}
	projects, err := ListProjects(app, 10)
	if err != nil || len(projects) != 1 || projects[0].Events != 4 {
		t.Fatalf("projects: %v %+v", err, projects)
	}
}

func TestReconcileProjectsUsesShortestSessionCwd(t *testing.T) {
	app := newApp(t)
	base := time.Date(2026, 9, 5, 15, 0, 0, 0, time.UTC)
	mk := func(sid, file, cwd string) Request {
		e := ev("u-"+sid, "user", "hi", base)
		e.CWD = cwd
		return Request{
			Project: Project{Path: "/opt/Code/github/com/x/repo", Name: "repo", EncodedDir: "-opt-Code-github-com-x-repo"},
			Session: Session{SessionID: sid, FilePath: file, FileSize: 10},
			Chunk:   ChunkState{NextOffset: 10, LinesSeen: 1},
			Events:  []jsonl.Event{e},
		}
	}
	if _, err := Apply(app, mk("s1", "/f/s1.jsonl", "/opt/Code/github.com/x/repo/sub/dir"), time.UTC); err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(app, mk("s2", "/f/s2.jsonl", "/opt/Code/github.com/x/repo"), time.UTC); err != nil {
		t.Fatal(err)
	}
	// simulate a store filled before cwd tracking: blank the project's cwd/name
	p, _ := app.FindFirstRecordByData(schema.Projects, "path", "/opt/Code/github/com/x/repo")
	p.Set("cwd", "")
	p.Set("name", "repo-guess")
	if err := app.Save(p); err != nil {
		t.Fatal(err)
	}
	n, err := ReconcileProjects(app)
	if err != nil || n != 1 {
		t.Fatalf("reconcile: n=%d err=%v", n, err)
	}
	rows, _ := ListProjects(app, 10)
	if rows[0].Cwd != "/opt/Code/github.com/x/repo" || rows[0].Name != "repo" {
		t.Fatalf("project after reconcile: %+v", rows[0])
	}
	// idempotent
	if n, _ := ReconcileProjects(app); n != 0 {
		t.Fatalf("second reconcile updated %d", n)
	}
}

func TestApplyRejectsMissingKeys(t *testing.T) {
	app := newApp(t)
	if _, err := Apply(app, Request{}, time.UTC); err == nil {
		t.Fatal("expected error for empty request")
	}
}
