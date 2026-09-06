package scan

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/pocketbase/pocketbase/tests"

	"structor/internal/ingest"
	"structor/internal/schema"
)

const line1 = `{"uuid":"u1","parentUuid":null,"type":"user","timestamp":"2026-09-05T15:12:03Z","sessionId":"sess-1","message":{"role":"user","content":"hello"}}` + "\n"
const line2 = `{"uuid":"a1","parentUuid":"u1","type":"assistant","timestamp":"2026-09-05T15:12:05Z","sessionId":"sess-1","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}}` + "\n"

func TestDirIncremental(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatal(err)
	}
	defer app.Cleanup()
	if err := schema.Ensure(app); err != nil {
		t.Fatal(err)
	}

	root := filepath.Join(t.TempDir(), ".claude", "projects")
	proj := filepath.Join(root, "-opt-Code-repo")
	if err := os.MkdirAll(proj, 0o755); err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(proj, "sess-1.jsonl")
	if err := os.WriteFile(file, []byte(line1+`{"uuid":"partial","timestamp":"2026-09-05T15:12:06Z","type":"user"`), 0o644); err != nil {
		t.Fatal(err)
	}

	rep, err := Dir(app, root, "testhost", time.UTC)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Files != 1 || rep.Changed != 1 || rep.Inserted != 1 {
		t.Fatalf("first scan: %+v", rep)
	}

	// nothing new: the partial line is still partial, but size == offset? no —
	// size > offset because the partial tail is held back, so the file is
	// re-read and yields zero new events.
	rep, err = Dir(app, root, "testhost", time.UTC)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Inserted != 0 {
		t.Fatalf("second scan inserted %d", rep.Inserted)
	}

	// complete the partial line and append another
	f, _ := os.OpenFile(file, os.O_APPEND|os.O_WRONLY, 0o644)
	f.WriteString(`,"message":{"role":"user","content":"finished"}}` + "\n" + line2)
	f.Close()

	rep, err = Dir(app, root, "testhost", time.UTC)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Inserted != 2 {
		t.Fatalf("third scan inserted %d, want 2: %+v", rep.Inserted, rep)
	}

	st, _ := ingest.AllState(app)
	info, _ := os.Stat(file)
	if s := st[file]; s.ByteOffset != info.Size() || s.LinesSeen != 3 {
		t.Fatalf("tail state: %+v size=%d", s, info.Size())
	}

	// truncate + rewrite: offset resets, uuid index dedups
	if err := os.WriteFile(file, []byte(line1), 0o644); err != nil {
		t.Fatal(err)
	}
	rep, err = Dir(app, root, "testhost", time.UTC)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Inserted != 0 || rep.Skipped != 1 {
		t.Fatalf("after truncate: %+v", rep)
	}
	st, _ = ingest.AllState(app)
	if st[file].ByteOffset != int64(len(line1)) {
		t.Fatalf("offset after truncate = %d", st[file].ByteOffset)
	}

	sessions, _ := ingest.ListSessions(app, "", "", "", 10)
	if len(sessions) != 1 || sessions[0].Project != "/opt/Code/repo" || sessions[0].Tier != "projects" {
		t.Fatalf("session row: %+v", sessions)
	}
}

func TestSessionIDFor(t *testing.T) {
	cases := map[string]string{
		"/p/-x/f1e856a2-53bd-46b9-b26a-7dca05a201e6.jsonl":                     "f1e856a2-53bd-46b9-b26a-7dca05a201e6",
		"/p/-x/abc/subagents/workflows/wf_c96a1543-c71/journal.jsonl":          "journal@wf_c96a1543-c71",
		"/p/-x/abc/subagents/agent-1.jsonl":                                    "agent-1@subagents",
	}
	for in, want := range cases {
		if got := SessionIDFor(in); got != want {
			t.Errorf("%s → %s, want %s", in, got, want)
		}
	}
}

func TestSafeRelPath(t *testing.T) {
	ok := map[string]string{
		"abc.jsonl":                                  "_loose/abc.jsonl",
		"-opt-Code-repo/abc.jsonl":                   "-opt-Code-repo/abc.jsonl",
		"projects/-opt-Code-repo/x/sub/journal.jsonl": "projects/-opt-Code-repo/x/sub/journal.jsonl",
		"./a/./b.jsonl":                              "a/b.jsonl",
		"a\\b.jsonl":                                 "a/b.jsonl",
		"โฟลเดอร์/ไฟล์.jsonl":                        "โฟลเดอร์/ไฟล์.jsonl",
	}
	for in, want := range ok {
		got, valid := SafeRelPath(in)
		if !valid || got != want {
			t.Errorf("%q → %q (%v), want %q", in, got, valid, want)
		}
	}
	for _, bad := range []string{"", "/etc/passwd", "../x.jsonl", "a/../../x.jsonl", "a/..", "x\x00.jsonl", "a b/c.jsonl", "..", "."} {
		if got, valid := SafeRelPath(bad); valid {
			t.Errorf("%q accepted as %q", bad, got)
		}
	}
	if SafeSegment("hello world!") != "helloworld" || SafeSegment("..") != "" {
		t.Fatal("SafeSegment")
	}
	if _, _, tier := Classify("/data/uploads/browser", "/data/uploads/browser/-opt-x/abc.jsonl"); tier != "import" {
		t.Fatalf("import tier = %s", tier)
	}
}

func TestClassify(t *testing.T) {
	root := "/Users/x/.claude/projects"
	p, enc, tier := Classify(root, root+"/-opt-Code-repo/abc.jsonl")
	if p != "/opt/Code/repo" || enc != "-opt-Code-repo" || tier != "projects" {
		t.Fatalf("%s %s %s", p, enc, tier)
	}
	_, _, tier = Classify(root, root+"/-opt-Code-repo/abc/subagents/agent-1.jsonl")
	if tier != "subagent" {
		t.Fatalf("subagent tier = %s", tier)
	}
	_, _, tier = Classify("/backup/projects", "/backup/projects/-opt-Code-repo/abc.jsonl")
	if tier != "backup" {
		t.Fatalf("backup tier = %s", tier)
	}
}
