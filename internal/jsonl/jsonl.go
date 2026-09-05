// Package jsonl parses Claude Code session transcript lines into the flat
// event shape the store keeps. It is deliberately forgiving: a line that is
// not JSON, or has no uuid/timestamp, is counted but not turned into an event.
package jsonl

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"
)

// Event is one transcript line reduced to what the ledger needs.
type Event struct {
	UUID       string    `json:"uuid"`
	ParentUUID string    `json:"parent_uuid,omitempty"`
	Type       string    `json:"type"`
	Role       string    `json:"role,omitempty"`
	TS         time.Time `json:"ts"`
	Text       string    `json:"text,omitempty"`
	Tools      []string  `json:"tools,omitempty"`
	Model      string    `json:"model,omitempty"`
	Sidechain  bool      `json:"sidechain,omitempty"`
	LineNo     int64     `json:"line_no"`
	RawBytes   int64     `json:"raw_bytes"`
	SessionID  string    `json:"session_id,omitempty"`
	CWD        string    `json:"cwd,omitempty"`
	GitBranch  string    `json:"git_branch,omitempty"`
}

// MaxText caps the excerpt kept per event. Full transcripts stay on disk;
// the store is an index, not a copy.
const MaxText = 8000

type rawLine struct {
	UUID       string          `json:"uuid"`
	ParentUUID string          `json:"parentUuid"`
	Type       string          `json:"type"`
	Timestamp  string          `json:"timestamp"`
	SessionID  string          `json:"sessionId"`
	CWD        string          `json:"cwd"`
	GitBranch  string          `json:"gitBranch"`
	Sidechain  bool            `json:"isSidechain"`
	Message    json.RawMessage `json:"message"`
	Summary    string          `json:"summary"`
	Content    json.RawMessage `json:"content"`
}

type rawMessage struct {
	Role    string          `json:"role"`
	Model   string          `json:"model"`
	Content json.RawMessage `json:"content"`
}

type contentBlock struct {
	Type    string          `json:"type"`
	Text    string          `json:"text"`
	Name    string          `json:"name"`
	Content json.RawMessage `json:"content"`
}

// ParseLine turns one transcript line into an Event. ok is false when the
// line carries no uuid or timestamp (mode markers, hook attachments, etc).
func ParseLine(line []byte, lineNo int64) (ev Event, ok bool) {
	line = bytes.TrimSpace(line)
	if len(line) == 0 || line[0] != '{' {
		return Event{}, false
	}
	var raw rawLine
	if err := json.Unmarshal(line, &raw); err != nil {
		return Event{}, false
	}
	if raw.UUID == "" || raw.Timestamp == "" {
		return Event{}, false
	}
	ts, err := time.Parse(time.RFC3339Nano, raw.Timestamp)
	if err != nil {
		ts, err = time.Parse(time.RFC3339, raw.Timestamp)
		if err != nil {
			return Event{}, false
		}
	}
	ev = Event{
		UUID:       raw.UUID,
		ParentUUID: raw.ParentUUID,
		Type:       raw.Type,
		TS:         ts.UTC(),
		Sidechain:  raw.Sidechain,
		LineNo:     lineNo,
		RawBytes:   int64(len(line)),
		SessionID:  raw.SessionID,
		CWD:        raw.CWD,
		GitBranch:  raw.GitBranch,
	}
	if len(raw.Message) > 0 && raw.Message[0] == '{' {
		var msg rawMessage
		if err := json.Unmarshal(raw.Message, &msg); err == nil {
			ev.Role = msg.Role
			ev.Model = msg.Model
			ev.Text, ev.Tools = flattenContent(msg.Content)
		}
	}
	if ev.Text == "" && raw.Summary != "" {
		ev.Text = raw.Summary
	}
	if ev.Text == "" && len(raw.Content) > 0 {
		ev.Text, _ = flattenContent(raw.Content)
	}
	if len(ev.Text) > MaxText {
		ev.Text = ev.Text[:MaxText]
	}
	return ev, true
}

func flattenContent(content json.RawMessage) (string, []string) {
	if len(content) == 0 {
		return "", nil
	}
	if content[0] == '"' {
		var s string
		if err := json.Unmarshal(content, &s); err == nil {
			return s, nil
		}
		return "", nil
	}
	if content[0] != '[' {
		return "", nil
	}
	var blocks []contentBlock
	if err := json.Unmarshal(content, &blocks); err != nil {
		return "", nil
	}
	var sb strings.Builder
	var tools []string
	for _, b := range blocks {
		switch b.Type {
		case "text":
			if sb.Len() > 0 {
				sb.WriteString("\n")
			}
			sb.WriteString(b.Text)
		case "tool_use":
			if b.Name != "" {
				tools = append(tools, b.Name)
			}
		case "tool_result":
			inner, _ := flattenContent(b.Content)
			if inner != "" {
				if sb.Len() > 0 {
					sb.WriteString("\n")
				}
				sb.WriteString(inner)
			}
		}
		if sb.Len() > MaxText {
			break
		}
	}
	return sb.String(), tools
}

// Chunk is the result of reading a file from a byte offset: the complete
// lines found, and the offset just past the last newline. A partial trailing
// line is held back so the next read starts exactly where this one stopped.
type Chunk struct {
	Events     []Event
	LinesSeen  int64
	NextOffset int64
}

// ReadFrom reads complete lines from r (already positioned at offset) and
// stops at the last newline. lineBase is the line number of the first line.
func ReadFrom(r io.Reader, offset int64, lineBase int64) (Chunk, error) {
	br := bufio.NewReaderSize(r, 1<<20)
	c := Chunk{NextOffset: offset}
	lineNo := lineBase
	for {
		line, err := br.ReadBytes('\n')
		if err != nil {
			if err == io.EOF {
				// partial line without newline: hold back
				return c, nil
			}
			return c, err
		}
		lineNo++
		c.LinesSeen++
		c.NextOffset += int64(len(line))
		if ev, ok := ParseLine(line, lineNo); ok {
			c.Events = append(c.Events, ev)
		}
	}
}

// ISOWeek renders t in loc as "2026-W36".
func ISOWeek(t time.Time, loc *time.Location) string {
	y, w := t.In(loc).ISOWeek()
	return fmt.Sprintf("%d-W%02d", y, w)
}
