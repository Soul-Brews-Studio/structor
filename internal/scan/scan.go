// Package scan walks a directory of Claude Code project transcripts and
// feeds new bytes into the store through ingest.Apply. It is the in-process
// twin of the Rust CLI: same tail-state contract, no HTTP hop, used for
// server-side imports (a mounted /share on kvmlab1) and for tests.
package scan

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/pocketbase/pocketbase/core"

	"structor/internal/ingest"
	"structor/internal/jsonl"
)

// Report summarises one scan pass.
type Report struct {
	Files     int      `json:"files"`
	Changed   int      `json:"changed"`
	Inserted  int      `json:"inserted"`
	Skipped   int      `json:"skipped"`
	Errors    []string `json:"errors,omitempty"`
	Elapsed   string   `json:"elapsed"`
}

// Dir scans root (a ~/.claude/projects-shaped tree) and applies every file
// that grew since the store last saw it.
func Dir(app core.App, root, host string, loc *time.Location) (Report, error) {
	start := time.Now()
	var rep Report
	state, err := ingest.AllState(app)
	if err != nil {
		return rep, err
	}
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			rep.Errors = append(rep.Errors, err.Error())
			return nil
		}
		if d.IsDir() {
			if d.Name() == "memory" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(d.Name(), ".jsonl") {
			return nil
		}
		rep.Files++
		info, err := d.Info()
		if err != nil {
			return nil
		}
		prev := state[path]
		if info.Size() == prev.FileSize && info.Size() == prev.ByteOffset {
			return nil
		}
		r, err := File(app, root, path, host, prev, info, loc)
		if err != nil {
			rep.Errors = append(rep.Errors, path+": "+err.Error())
			return nil
		}
		rep.Changed++
		rep.Inserted += r.Inserted
		rep.Skipped += r.Skipped
		return nil
	})
	rep.Elapsed = time.Since(start).String()
	return rep, err
}

// File ingests one transcript from its recorded offset.
func File(app core.App, root, path, host string, prev ingest.TailState, info fs.FileInfo, loc *time.Location) (ingest.Result, error) {
	offset := prev.ByteOffset
	if info.Size() < offset {
		// truncated or rewritten: start over, the unique uuid index dedups
		offset = 0
	}
	f, err := os.Open(path)
	if err != nil {
		return ingest.Result{}, err
	}
	defer f.Close()
	if _, err := f.Seek(offset, 0); err != nil {
		return ingest.Result{}, err
	}
	lineBase := prev.LinesSeen
	if offset == 0 {
		lineBase = 0
	}
	chunk, err := jsonl.ReadFrom(f, offset, lineBase)
	if err != nil {
		return ingest.Result{}, err
	}
	sessionID := SessionIDFor(path)
	projectPath, encoded, tier := Classify(root, path)
	req := ingest.Request{
		Project: ingest.Project{Path: projectPath, Name: filepath.Base(projectPath), EncodedDir: encoded, Host: host},
		Session: ingest.Session{
			SessionID:  sessionID,
			FilePath:   path,
			Tier:       tier,
			ByteOffset: prev.ByteOffset,
			FileSize:   info.Size(),
			FileMtime:  info.ModTime().Unix(),
		},
		Chunk:  ingest.ChunkState{NextOffset: chunk.NextOffset, LinesSeen: chunk.LinesSeen},
		Events: chunk.Events,
	}
	return ingest.Apply(app, req, loc)
}

// SessionIDFor derives a session id from a transcript path. Top-level
// transcripts are named <uuid>.jsonl and the stem is the id. Workflow
// journals are all named journal.jsonl, so the stem alone collides; those
// get "<stem>@<parent dir>" (e.g. journal@wf_c96a1543-c71), which is unique
// because workflow ids are.
func SessionIDFor(path string) string {
	stem := strings.TrimSuffix(filepath.Base(path), ".jsonl")
	if len(stem) >= 32 && strings.Count(stem, "-") >= 4 {
		return stem // uuid-shaped
	}
	parent := filepath.Base(filepath.Dir(path))
	if parent == "" || parent == "." || parent == "/" {
		return stem
	}
	return stem + "@" + parent
}

// Classify derives (project path, encoded dir, tier) from a transcript path
// under root. Tiers mirror the three-tier walk: "projects" for
// <root>/<encoded>/<id>.jsonl, "subagent" for anything nested deeper
// (workflow agents), "backup" when the root is not ~/.claude/projects.
func Classify(root, path string) (projectPath, encoded, tier string) {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return "unknown", "", "unknown"
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	if len(parts) == 0 {
		return "unknown", "", "unknown"
	}
	encoded = parts[0]
	projectPath = DecodeProjectDir(encoded)
	tier = "projects"
	if len(parts) > 2 {
		tier = "subagent"
	}
	if !strings.HasSuffix(filepath.ToSlash(root), "/.claude/projects") {
		tier = "backup"
	}
	return projectPath, encoded, tier
}

// DecodeProjectDir turns "-opt-Code-github-com-foo-bar" back into a best-effort
// path. Dots and dashes are both encoded as '-', so the result is a guess; the
// encoded form is kept alongside it as the stable key.
func DecodeProjectDir(encoded string) string {
	if !strings.HasPrefix(encoded, "-") {
		return encoded
	}
	return "/" + strings.ReplaceAll(strings.TrimPrefix(encoded, "-"), "-", "/")
}
