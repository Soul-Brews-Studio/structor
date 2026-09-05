// Package schema owns the PocketBase collections that make Structor a
// week-stamped, incremental, per-session-per-week event store.
//
// Collections:
//
//	projects       one row per Claude Code project directory
//	sessions       one row per session jsonl, carrying its tail state
//	               (byte_offset / file_size / lines_seen — the pattern taken
//	               from session-viewer's session_tail_state table)
//	events         one row per jsonl line that has a uuid + timestamp
//	session_weeks  one row per (session, iso_week) — the ledger
//	oauth_clients  dynamically registered MCP clients (RFC 7591)
//	oauth_codes    short-lived authorization codes (PKCE S256)
//	oauth_tokens   hashed access / refresh tokens
//
// Ensure is idempotent: it creates what is missing and never alters what
// exists, so an operator can extend a collection in the admin UI without
// the next boot undoing it.
package schema

import (
	"errors"
	"fmt"

	"github.com/pocketbase/pocketbase/core"
)

const (
	Projects     = "projects"
	Sessions     = "sessions"
	Events       = "events"
	SessionWeeks = "session_weeks"
	OAuthClients = "oauth_clients"
	OAuthCodes   = "oauth_codes"
	OAuthTokens  = "oauth_tokens"
)

// Ensure creates every Structor collection that does not exist yet.
func Ensure(app core.App) error {
	projects, err := ensure(app, Projects, func(c *core.Collection) {
		c.Fields.Add(
			&core.TextField{Name: "path", Required: true, Max: 2000, Presentable: true},
			&core.TextField{Name: "name", Max: 500},
			&core.TextField{Name: "encoded_dir", Max: 2000},
			&core.TextField{Name: "cwd", Max: 2000}, // authoritative path, learned from transcripts
			&core.TextField{Name: "host", Max: 200},
			&core.AutodateField{Name: "created", OnCreate: true},
			&core.AutodateField{Name: "updated", OnCreate: true, OnUpdate: true},
		)
		c.AddIndex("idx_projects_path", true, "path", "")
	})
	if err != nil {
		return err
	}

	sessions, err := ensure(app, Sessions, func(c *core.Collection) {
		c.Fields.Add(
			&core.TextField{Name: "session_id", Required: true, Max: 200, Presentable: true},
			&core.RelationField{Name: "project", CollectionId: projects.Id, MaxSelect: 1, CascadeDelete: true},
			&core.TextField{Name: "file_path", Required: true, Max: 2000},
			&core.TextField{Name: "tier", Max: 50},
			&core.NumberField{Name: "byte_offset", OnlyInt: true},
			&core.NumberField{Name: "file_size", OnlyInt: true},
			&core.NumberField{Name: "file_mtime", OnlyInt: true},
			&core.NumberField{Name: "lines_seen", OnlyInt: true},
			&core.NumberField{Name: "event_count", OnlyInt: true},
			&core.DateField{Name: "first_ts"},
			&core.DateField{Name: "last_ts"},
			&core.TextField{Name: "first_prompt", Max: 4000},
			&core.TextField{Name: "git_branch", Max: 500},
			&core.TextField{Name: "cwd", Max: 2000},
			&core.TextField{Name: "model", Max: 200},
			&core.AutodateField{Name: "created", OnCreate: true},
			&core.AutodateField{Name: "updated", OnCreate: true, OnUpdate: true},
		)
		c.AddIndex("idx_sessions_session_id", true, "session_id", "")
		c.AddIndex("idx_sessions_file_path", true, "file_path", "")
		c.AddIndex("idx_sessions_last_ts", false, "last_ts", "")
	})
	if err != nil {
		return err
	}

	if _, err := ensure(app, Events, func(c *core.Collection) {
		c.Fields.Add(
			&core.RelationField{Name: "session", CollectionId: sessions.Id, MaxSelect: 1, CascadeDelete: true, Required: true},
			&core.TextField{Name: "uuid", Required: true, Max: 200},
			&core.TextField{Name: "parent_uuid", Max: 200},
			&core.TextField{Name: "type", Max: 100},
			&core.TextField{Name: "role", Max: 50},
			&core.DateField{Name: "ts", Required: true},
			&core.TextField{Name: "iso_week", Required: true, Max: 10},
			&core.TextField{Name: "text", Max: 16000},
			&core.JSONField{Name: "tools", MaxSize: 20000},
			&core.TextField{Name: "model", Max: 200},
			&core.BoolField{Name: "sidechain"},
			&core.NumberField{Name: "line_no", OnlyInt: true},
			&core.NumberField{Name: "raw_bytes", OnlyInt: true},
			&core.AutodateField{Name: "created", OnCreate: true},
		)
		c.AddIndex("idx_events_uuid", true, "uuid", "")
		c.AddIndex("idx_events_session_ts", false, "session, ts", "")
		c.AddIndex("idx_events_week", false, "iso_week", "")
	}); err != nil {
		return err
	}

	if _, err := ensure(app, SessionWeeks, func(c *core.Collection) {
		c.Fields.Add(
			&core.RelationField{Name: "session", CollectionId: sessions.Id, MaxSelect: 1, CascadeDelete: true, Required: true},
			&core.RelationField{Name: "project", CollectionId: projects.Id, MaxSelect: 1, CascadeDelete: true},
			&core.TextField{Name: "iso_week", Required: true, Max: 10, Presentable: true},
			&core.NumberField{Name: "event_count", OnlyInt: true},
			&core.NumberField{Name: "user_count", OnlyInt: true},
			&core.NumberField{Name: "assistant_count", OnlyInt: true},
			&core.NumberField{Name: "tool_count", OnlyInt: true},
			&core.DateField{Name: "first_ts"},
			&core.DateField{Name: "last_ts"},
			&core.AutodateField{Name: "created", OnCreate: true},
			&core.AutodateField{Name: "updated", OnCreate: true, OnUpdate: true},
		)
		c.AddIndex("idx_session_weeks_unique", true, "session, iso_week", "")
		c.AddIndex("idx_session_weeks_week", false, "iso_week", "")
	}); err != nil {
		return err
	}

	if _, err := ensure(app, OAuthClients, func(c *core.Collection) {
		c.Fields.Add(
			&core.TextField{Name: "client_id", Required: true, Max: 200, Presentable: true},
			&core.TextField{Name: "client_secret_hash", Max: 200},
			&core.TextField{Name: "client_name", Max: 500},
			&core.JSONField{Name: "redirect_uris", MaxSize: 20000},
			&core.TextField{Name: "token_endpoint_auth_method", Max: 50},
			&core.AutodateField{Name: "created", OnCreate: true},
		)
		c.AddIndex("idx_oauth_clients_id", true, "client_id", "")
	}); err != nil {
		return err
	}

	if _, err := ensure(app, OAuthCodes, func(c *core.Collection) {
		c.Fields.Add(
			&core.TextField{Name: "code_hash", Required: true, Max: 200},
			&core.TextField{Name: "client_id", Required: true, Max: 200},
			&core.TextField{Name: "redirect_uri", Max: 2000},
			&core.TextField{Name: "code_challenge", Max: 200},
			&core.TextField{Name: "scope", Max: 500},
			&core.TextField{Name: "subject", Max: 200},
			&core.DateField{Name: "expires"},
			&core.BoolField{Name: "used"},
			&core.AutodateField{Name: "created", OnCreate: true},
		)
		c.AddIndex("idx_oauth_codes_hash", true, "code_hash", "")
	}); err != nil {
		return err
	}

	if _, err := ensure(app, OAuthTokens, func(c *core.Collection) {
		c.Fields.Add(
			&core.TextField{Name: "token_hash", Required: true, Max: 200},
			&core.TextField{Name: "kind", Required: true, Max: 20}, // access | refresh
			&core.TextField{Name: "client_id", Required: true, Max: 200},
			&core.TextField{Name: "subject", Max: 200},
			&core.TextField{Name: "scope", Max: 500},
			&core.DateField{Name: "expires"},
			&core.BoolField{Name: "revoked"},
			&core.AutodateField{Name: "created", OnCreate: true},
		)
		c.AddIndex("idx_oauth_tokens_hash", true, "token_hash", "")
	}); err != nil {
		return err
	}

	return nil
}

func ensure(app core.App, name string, build func(c *core.Collection)) (*core.Collection, error) {
	existing, err := app.FindCollectionByNameOrId(name)
	if err == nil {
		// Additive migration: fields and indexes that this version knows about
		// but the stored collection lacks are added. Nothing is renamed or
		// removed, so operator edits in the admin UI survive.
		want := core.NewBaseCollection(name)
		build(want)
		changed := false
		for _, f := range want.Fields {
			if existing.Fields.GetByName(f.GetName()) == nil {
				existing.Fields.Add(f)
				changed = true
			}
		}
		for _, idx := range want.Indexes {
			found := false
			for _, have := range existing.Indexes {
				if have == idx {
					found = true
					break
				}
			}
			if !found {
				existing.Indexes = append(existing.Indexes, idx)
				changed = true
			}
		}
		if changed {
			if err := app.Save(existing); err != nil {
				return nil, fmt.Errorf("migrate collection %s: %w", name, err)
			}
			return app.FindCollectionByNameOrId(name)
		}
		return existing, nil
	}
	c := core.NewBaseCollection(name)
	build(c)
	if err := app.Save(c); err != nil {
		return nil, fmt.Errorf("create collection %s: %w", name, err)
	}
	created, err := app.FindCollectionByNameOrId(name)
	if err != nil {
		return nil, errors.Join(fmt.Errorf("reload collection %s", name), err)
	}
	return created, nil
}

// EnsureSuperuser creates the superuser or resets its password. It is the
// username/password the CLI, the dashboard and the OAuth login screen all use.
func EnsureSuperuser(app core.App, email, password string) error {
	if email == "" || password == "" {
		return nil
	}
	col, err := app.FindCollectionByNameOrId(core.CollectionNameSuperusers)
	if err != nil {
		return err
	}
	rec, err := app.FindAuthRecordByEmail(col, email)
	if err != nil {
		rec = core.NewRecord(col)
		rec.SetEmail(email)
	}
	if rec.ValidatePassword(password) {
		return nil
	}
	rec.SetPassword(password)
	return app.Save(rec)
}
