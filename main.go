// Structor: an embedded-PocketBase server that is a week-stamped index of
// Claude Code session transcripts, with an ingest API for the Rust CLI, an
// MCP endpoint (OAuth 2.1 + bearer) and a small dashboard on top of the
// PocketBase admin UI.
//
//	structor serve --http=127.0.0.1:8090     run the server
//	structor import ~/.claude/projects       index a directory in-process
//
// Environment:
//
//	STRUCTOR_ADMIN_EMAIL / STRUCTOR_ADMIN_PASSWORD  superuser created or reset at boot
//	STRUCTOR_TZ            IANA zone for ISO-week stamping (default Asia/Bangkok)
//	STRUCTOR_PUBLIC_URL    external origin for OAuth metadata (behind a tunnel)
//	STRUCTOR_MCP_TOKEN     static bearer accepted on /mcp
//	STRUCTOR_SCAN_DIR      if set, the server scans this tree itself every STRUCTOR_SCAN_INTERVAL (default 60s)
package main

import (
	"embed"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/pocketbase/pocketbase"
	"github.com/pocketbase/pocketbase/apis"
	"github.com/pocketbase/pocketbase/core"
	"github.com/spf13/cobra"

	"structor/internal/ingest"
	"structor/internal/mcp"
	"structor/internal/oauth"
	"structor/internal/scan"
	"structor/internal/schema"
)

// Version is stamped by -ldflags "-X main.Version=...".
var Version = "dev"

// logsMaxDays caps PocketBase's request-log retention (pb_data/auxiliary.db).
const logsMaxDays = 2

//go:embed ui
var uiFS embed.FS

func location() *time.Location {
	name := os.Getenv("STRUCTOR_TZ")
	if name == "" {
		name = "Asia/Bangkok"
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		log.Printf("STRUCTOR_TZ %q invalid, using UTC: %v", name, err)
		return time.UTC
	}
	return loc
}

func hostname() string {
	h, _ := os.Hostname()
	if i := strings.IndexByte(h, '.'); i > 0 {
		h = h[:i]
	}
	return h
}

func main() {
	app := pocketbase.New()
	loc := location()

	app.RootCmd.AddCommand(importCmd(app, loc))

	app.OnBootstrap().BindFunc(func(e *core.BootstrapEvent) error {
		if err := e.Next(); err != nil {
			return err
		}
		if err := schema.Ensure(e.App); err != nil {
			return err
		}
		// PocketBase ships sane default rate-limit rules (*:auth 2 req/3s etc.)
		// but disabled. The superuser password guards ingest and MCP, so turn
		// them on unless the operator opted out.
		if os.Getenv("STRUCTOR_RATE_LIMITS") != "0" {
			settings := e.App.Settings()
			changed := false
			if !settings.RateLimits.Enabled {
				settings.RateLimits.Enabled = true
				changed = true
			}
			// The ingest API is called hundreds of times a minute by every watcher
			// (one request per grown file) and by browser scans; PocketBase's default
			// per-path rule throttled it to 429 within a day of running (2026-09-09).
			// Give the app routes their own generous budget; the auth rule stays.
			const ingestLabel = "/api/structor/"
			hasIngestRule := false
			for _, r := range settings.RateLimits.Rules {
				if r.Label == ingestLabel {
					hasIngestRule = true
					break
				}
			}
			if !hasIngestRule {
				settings.RateLimits.Rules = append(settings.RateLimits.Rules, core.RateLimitRule{Label: ingestLabel, Audience: "@auth", MaxRequests: 3000, Duration: 10})
				changed = true
			}
			// Request logs live in pb_data/auxiliary.db. A watcher retry storm
			// (429/401 on ingest, 2026-09-07..09) wrote 2.5M error rows there —
			// 1.4GB for three days of nothing. Keep two days instead of the default
			// five; the daily cleanup cron does the rest.
			if settings.Logs.MaxDays > logsMaxDays {
				settings.Logs.MaxDays = logsMaxDays
				changed = true
			}
			if changed {
				if err := e.App.Save(settings); err != nil {
					log.Printf("settings: %v", err)
				}
			}
		}
		return schema.EnsureSuperuser(e.App, os.Getenv("STRUCTOR_ADMIN_EMAIL"), os.Getenv("STRUCTOR_ADMIN_PASSWORD"))
	})

	app.OnServe().BindFunc(func(se *core.ServeEvent) error {
		auth := oauth.New(se.App)
		auth.Register(se)
		m := &mcp.Server{App: se.App, Version: Version, Name: "structor", Loc: loc}

		requireBearer := func(next func(e *core.RequestEvent) error) func(e *core.RequestEvent) error {
			return func(e *core.RequestEvent) error {
				if _, ok := auth.Authenticate(e); !ok {
					return auth.Challenge(e)
				}
				return next(e)
			}
		}

		// explicit methods: an Any("/mcp") pattern conflicts with the GET /{path...}
		// static fallback in Go's ServeMux precedence rules.
		mcpHandler := requireBearer(func(e *core.RequestEvent) error {
			e.Response.Header().Set("Access-Control-Allow-Origin", "*")
			return m.Handle(e)
		})
		for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodDelete} {
			se.Router.Route(method, "/mcp", mcpHandler)
		}
		se.Router.OPTIONS("/mcp", func(e *core.RequestEvent) error {
			h := e.Response.Header()
			h.Set("Access-Control-Allow-Origin", "*")
			h.Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
			h.Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id")
			return e.NoContent(http.StatusNoContent)
		})

		g := se.Router.Group("/api/structor")

		// read side: superuser session OR any accepted bearer
		g.GET("/status", requireBearer(func(e *core.RequestEvent) error {
			st, err := ingest.GetStatus(e.App, Version, loc)
			if err != nil {
				return e.InternalServerError("status", err)
			}
			return e.JSON(http.StatusOK, st)
		}))
		g.GET("/search", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			hits, err := ingest.Search(e.App, ingest.SearchOpts{
				Query: q.Get("q"), Project: q.Get("project"), ProjectID: q.Get("project_id"), Week: q.Get("week"),
				Session: q.Get("session"), Role: q.Get("role"), Limit: limit,
			})
			if err != nil {
				return e.InternalServerError("search", err)
			}
			if limit <= 0 || limit > 200 {
				limit = 30
			}
			return e.JSON(http.StatusOK, map[string]any{"hits": hits, "limit": limit, "truncated": len(hits) >= limit})
		}))
		g.GET("/sessions", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, err := ingest.ListSessions(e.App, q.Get("project"), q.Get("project_id"), q.Get("week"), limit)
			if err != nil {
				return e.InternalServerError("sessions", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"sessions": rows})
		}))
		g.GET("/projects", requireBearer(func(e *core.RequestEvent) error {
			limit, _ := strconv.Atoi(e.Request.URL.Query().Get("limit"))
			rows, err := ingest.ListProjects(e.App, limit)
			if err != nil {
				return e.InternalServerError("projects", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"projects": rows})
		}))
		g.GET("/read", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			if q.Get("session") == "" {
				return e.BadRequestError("session query param required", nil)
			}
			offset, _ := strconv.Atoi(q.Get("offset"))
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, err := ingest.ReadSession(e.App, q.Get("session"), offset, limit)
			switch {
			case errors.Is(err, ingest.ErrNoSession):
				return e.NotFoundError("no such session", nil)
			case errors.Is(err, ingest.ErrAmbiguousSession):
				return e.BadRequestError("session id prefix is ambiguous, give more characters", nil)
			case err != nil:
				return e.InternalServerError("read", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"events": rows, "offset": offset})
		}))
		g.GET("/days", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, truncated, err := ingest.Days(e.App, q.Get("from"), q.Get("to"), q.Get("project"), q.Get("project_id"), loc, limit)
			if err != nil {
				return e.BadRequestError("days", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"days": rows, "tz": loc.String(), "truncated": truncated})
		}))
		g.GET("/intake", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			summary, err := ingest.GetIntakeSummary(e.App)
			if err != nil {
				return e.InternalServerError("intake summary", err)
			}
			runs, err := ingest.ListRuns(e.App, limit)
			if err != nil {
				return e.InternalServerError("intake runs", err)
			}
			files, err := ingest.ListFiles(e.App, q.Get("pending") == "1", limit)
			if err != nil {
				return e.InternalServerError("intake files", err)
			}
			writers, err := ingest.ListWriters(e.App)
			if err != nil {
				return e.InternalServerError("intake writers", err)
			}
			conns, err := ingest.GetConnections(e.App)
			if err != nil {
				return e.InternalServerError("intake connections", err)
			}
			scanDir := os.Getenv("STRUCTOR_SCAN_DIR")
			interval := os.Getenv("STRUCTOR_SCAN_INTERVAL")
			if interval == "" {
				interval = "60s"
			}
			return e.JSON(http.StatusOK, map[string]any{
				"summary": summary, "runs": runs, "files": files, "writers": writers, "connections": conns,
				"server_scan": map[string]any{"enabled": scanDir != "", "dir": scanDir, "interval": interval},
				"upload_dir": filepath.Join(e.App.DataDir(), "uploads"),
				"host":       hostname(), "tz": loc.String(),
				"superuser":  e.HasSuperuserAuth(),
			})
		}))

		// Browser import: multipart files (a folder picked with webkitdirectory or
		// dropped .jsonl files) are stored under <data>/uploads/<host>/<relpath>
		// and ingested in-process with the same tail-state rules as the CLI.
		// Re-uploading a file resumes from its recorded offset.
		g.POST("/upload", func(e *core.RequestEvent) error {
			if err := e.Request.ParseMultipartForm(64 << 20); err != nil {
				return e.BadRequestError("multipart form expected", err)
			}
			label := scan.SafeSegment(e.Request.FormValue("label"))
			if label == "" {
				label = "browser"
			}
			root := filepath.Join(e.App.DataDir(), "uploads", label)
			if err := os.MkdirAll(root, 0o755); err != nil {
				return e.InternalServerError("uploads dir", err)
			}
			state, err := ingest.AllState(e.App)
			if err != nil {
				return e.InternalServerError("state", err)
			}
			type fileReport struct {
				Path     string `json:"path"`
				Inserted int    `json:"inserted"`
				Skipped  int    `json:"skipped"`
				Bytes    int64  `json:"bytes"`
				Error    string `json:"error,omitempty"`
			}
			var reports []fileReport
			totalIns := 0
			for _, fh := range e.Request.MultipartForm.File["files"] {
				// Go's multipart reduces fh.Filename to its base name; the folder
				// structure (webkitRelativePath) that tells us the project lives in
				// the raw Content-Disposition, which we validate ourselves.
				raw := fh.Filename
				if _, params, err := mime.ParseMediaType(fh.Header.Get("Content-Disposition")); err == nil && params["filename"] != "" {
					raw = params["filename"]
				}
				rel, ok := scan.SafeRelPath(raw)
				rep := fileReport{Path: rel, Bytes: fh.Size}
				if !ok || !strings.HasSuffix(rel, ".jsonl") {
					rep.Error = "rejected: only .jsonl files with a safe relative path"
					reports = append(reports, rep)
					continue
				}
				dst := filepath.Join(root, filepath.FromSlash(rel))
				if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
					rep.Error = err.Error()
					reports = append(reports, rep)
					continue
				}
				src, err := fh.Open()
				if err != nil {
					rep.Error = err.Error()
					reports = append(reports, rep)
					continue
				}
				out, err := os.Create(dst)
				if err == nil {
					_, err = io.Copy(out, src)
					out.Close()
				}
				src.Close()
				if err != nil {
					rep.Error = err.Error()
					reports = append(reports, rep)
					continue
				}
				info, err := os.Stat(dst)
				if err != nil {
					rep.Error = err.Error()
					reports = append(reports, rep)
					continue
				}
				res, err := scan.File(e.App, root, dst, "import:"+label, state[dst], info, loc)
				if err != nil {
					rep.Error = err.Error()
				} else {
					rep.Inserted, rep.Skipped = res.Inserted, res.Skipped
					totalIns += res.Inserted
				}
				reports = append(reports, rep)
			}
			if reports == nil {
				reports = []fileReport{}
			}
			return e.JSON(http.StatusOK, map[string]any{"files": reports, "inserted": totalIns, "root": root})
		}).Bind(apis.RequireSuperuserAuth())
		g.GET("/weeks", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, err := ingest.WeekLedger(e.App, q.Get("week"), q.Get("session"), limit)
			if err != nil {
				return e.InternalServerError("weeks", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"weeks": rows})
		}))

		// write side: superuser only (the CLI logs in with username/password)
		g.GET("/state", func(e *core.RequestEvent) error {
			st, err := ingest.AllState(e.App)
			if err != nil {
				return e.InternalServerError("state", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"sessions": st})
		}).Bind(apis.RequireSuperuserAuth())

		g.POST("/ingest", func(e *core.RequestEvent) error {
			var req ingest.Request
			if err := e.BindBody(&req); err != nil {
				return e.BadRequestError("invalid ingest body", err)
			}
			res, err := ingest.Apply(e.App, req, loc)
			if err != nil {
				var conflict *ingest.ConflictError
				if errors.As(err, &conflict) {
					return e.JSON(http.StatusConflict, map[string]any{"error": "byte_offset conflict", "have": conflict.Have, "want": conflict.Want})
				}
				return e.BadRequestError("ingest failed", err)
			}
			return e.JSON(http.StatusOK, res)
		}).Bind(apis.RequireSuperuserAuth())

		g.POST("/reconcile", func(e *core.RequestEvent) error {
			n, err := ingest.ReconcileProjects(e.App)
			if err != nil {
				return e.InternalServerError("reconcile", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"projects_updated": n})
		}).Bind(apis.RequireSuperuserAuth())

		g.POST("/scan", func(e *core.RequestEvent) error {
			dir := e.Request.URL.Query().Get("dir")
			if dir == "" {
				dir = os.Getenv("STRUCTOR_SCAN_DIR")
			}
			if dir == "" {
				return e.BadRequestError("dir query param or STRUCTOR_SCAN_DIR required", nil)
			}
			rep, err := scan.Dir(e.App, dir, hostname(), loc)
			if err != nil {
				return e.InternalServerError("scan", err)
			}
			return e.JSON(http.StatusOK, rep)
		}).Bind(apis.RequireSuperuserAuth())

		// dashboard: embedded static files with a CSP. Fonts are bundled, the
		// page has inline script/style, and it only ever talks to its own origin.
		sub, err := fs.Sub(uiFS, "ui")
		if err != nil {
			return err
		}
		const csp = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'"
		static := apis.Static(sub, false)
		serveUI := func(e *core.RequestEvent) error {
			e.Response.Header().Set("Content-Security-Policy", csp)
			e.Response.Header().Set("X-Content-Type-Options", "nosniff")
			// HTML must revalidate on every load so a redeploy shows up without a
			// hard refresh; fonts and CSS are content-stable and may be cached.
			if p := e.Request.URL.Path; p == "/" || strings.HasSuffix(p, ".html") {
				e.Response.Header().Set("Cache-Control", "no-cache")
			}
			return static(e)
		}
		// PocketBase's Static redirects /index.html to an absolute "/", which
		// escapes an ingress prefix; serve the file directly instead.
		se.Router.GET("/index.html", func(e *core.RequestEvent) error {
			e.Request.URL.Path = "/"
			e.Request.SetPathValue("path", "")
			return serveUI(e)
		})
		se.Router.GET("/{path...}", serveUI)

		// Repair project cwd/name from session evidence on every boot; stores
		// filled before cwd tracking existed otherwise keep the decoded guess.
		go func() {
			if n, err := ingest.ReconcileProjects(se.App); err != nil {
				log.Printf("reconcile projects: %v", err)
			} else if n > 0 {
				log.Printf("reconcile projects: %d updated from session cwd", n)
			}
			if n, err := ingest.PruneRuns(se.App, 30*24*time.Hour); err != nil {
				log.Printf("prune import runs: %v", err)
			} else if n > 0 {
				log.Printf("prune import runs: %d rows older than 30d removed", n)
			}
		}()
		if dir := os.Getenv("STRUCTOR_SCAN_DIR"); dir != "" {
			go serverScanLoop(se.App, dir, loc)
		}
		go func() {
			for {
				time.Sleep(6 * time.Hour)
				auth.Prune()
			}
		}()
		return se.Next()
	})

	if err := app.Start(); err != nil {
		log.Fatal(err)
	}
}

func serverScanLoop(app core.App, dir string, loc *time.Location) {
	interval := 60 * time.Second
	if v := os.Getenv("STRUCTOR_SCAN_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			interval = d
		}
	}
	for {
		rep, err := scan.Dir(app, dir, hostname(), loc)
		if err != nil {
			log.Printf("scan %s: %v", dir, err)
		} else if rep.Changed > 0 {
			log.Printf("scan %s: %d files changed, %d events inserted (%s)", dir, rep.Changed, rep.Inserted, rep.Elapsed)
		}
		time.Sleep(interval)
	}
}

func importCmd(app *pocketbase.PocketBase, loc *time.Location) *cobra.Command {
	return &cobra.Command{
		Use:   "import [dir]",
		Short: "Index a ~/.claude/projects-shaped directory in-process (no HTTP)",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := app.Bootstrap(); err != nil {
				return err
			}
			rep, err := scan.Dir(app, args[0], hostname(), loc)
			if err != nil {
				return err
			}
			fmt.Printf("files=%d changed=%d inserted=%d skipped=%d elapsed=%s\n", rep.Files, rep.Changed, rep.Inserted, rep.Skipped, rep.Elapsed)
			for _, e := range rep.Errors {
				fmt.Println("error:", e)
			}
			return nil
		},
	}
}
