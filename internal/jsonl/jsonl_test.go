package jsonl

import (
	"strings"
	"testing"
	"time"
)

const sample = `{"type":"mode","mode":"normal","sessionId":"abc"}
{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user","content":"ok col can you dig deep find all josnl"},"uuid":"u1","timestamp":"2026-09-05T15:12:03.123Z","sessionId":"abc","cwd":"/opt/x","gitBranch":"main"}
{"parentUuid":"u1","isSidechain":false,"type":"assistant","message":{"role":"assistant","model":"claude-fable-5-1","content":[{"type":"text","text":"On it."},{"type":"tool_use","id":"t1","name":"Bash","input":{}}]},"uuid":"a1","timestamp":"2026-09-05T15:12:05Z","sessionId":"abc"}
not json at all
{"parentUuid":"a1","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"hello from tool"}]},"uuid":"u2","timestamp":"2026-09-05T15:12:06Z","sessionId":"abc"}
{"type":"summary","summary":"a summary line","leafUuid":"a1"}
{"uuid":"partial","timestamp":"2026-09-05T15:12:07Z","type":"user","message":{"role":"user","content":"cut off`

func TestReadFromHoldsBackPartialLine(t *testing.T) {
	c, err := ReadFrom(strings.NewReader(sample), 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if c.LinesSeen != 6 {
		t.Fatalf("lines seen = %d, want 6 (partial last line held back)", c.LinesSeen)
	}
	if len(c.Events) != 3 {
		t.Fatalf("events = %d, want 3", len(c.Events))
	}
	wantOffset := int64(strings.LastIndex(sample, "\n") + 1)
	if c.NextOffset != wantOffset {
		t.Fatalf("next offset = %d, want %d", c.NextOffset, wantOffset)
	}
	u1 := c.Events[0]
	if u1.UUID != "u1" || u1.Role != "user" || u1.Text != "ok col can you dig deep find all josnl" || u1.CWD != "/opt/x" || u1.GitBranch != "main" {
		t.Fatalf("u1 parsed wrong: %+v", u1)
	}
	a1 := c.Events[1]
	if a1.Model != "claude-fable-5-1" || a1.Text != "On it." || len(a1.Tools) != 1 || a1.Tools[0] != "Bash" || a1.ParentUUID != "u1" {
		t.Fatalf("a1 parsed wrong: %+v", a1)
	}
	if c.Events[2].Text != "hello from tool" {
		t.Fatalf("tool_result text not flattened: %q", c.Events[2].Text)
	}
	if u1.LineNo != 2 || a1.LineNo != 3 {
		t.Fatalf("line numbers wrong: %d %d", u1.LineNo, a1.LineNo)
	}
}

func TestReadFromResumesAtOffset(t *testing.T) {
	full := sample + "\n"
	first, _ := ReadFrom(strings.NewReader(full[:100]), 0, 0)
	rest, _ := ReadFrom(strings.NewReader(full[first.NextOffset:]), first.NextOffset, first.LinesSeen)
	if first.LinesSeen+rest.LinesSeen != 7 {
		t.Fatalf("resumed line count = %d, want 7", first.LinesSeen+rest.LinesSeen)
	}
	if rest.NextOffset != int64(len(full)) {
		t.Fatalf("resumed offset = %d, want %d", rest.NextOffset, len(full))
	}
	// the 7th line is unterminated JSON even with a newline, so still 3 events
	if len(first.Events)+len(rest.Events) != 3 {
		t.Fatalf("resumed events = %d, want 3", len(first.Events)+len(rest.Events))
	}
}

func TestISOWeekUsesZone(t *testing.T) {
	bkk, _ := time.LoadLocation("Asia/Bangkok")
	// Sunday 2026-09-06 20:00 UTC is Monday 03:00 in Bangkok: next ISO week there.
	ts := time.Date(2026, 9, 6, 20, 0, 0, 0, time.UTC)
	if got := ISOWeek(ts, time.UTC); got != "2026-W36" {
		t.Fatalf("utc week = %s", got)
	}
	if got := ISOWeek(ts, bkk); got != "2026-W37" {
		t.Fatalf("bangkok week = %s", got)
	}
}

func TestTextCap(t *testing.T) {
	long := strings.Repeat("x", MaxText+500)
	ev, ok := ParseLine([]byte(`{"uuid":"z","timestamp":"2026-01-01T00:00:00Z","type":"user","message":{"role":"user","content":"`+long+`"}}`), 1)
	if !ok || len(ev.Text) != MaxText {
		t.Fatalf("text cap: ok=%v len=%d", ok, len(ev.Text))
	}
}
