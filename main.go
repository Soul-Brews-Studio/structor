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
	"io/fs"
	"log"
	"net/http"
	"os"
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
		return schema.EnsureSuperuser(e.App, os.Getenv("STRUCTOR_ADMIN_EMAIL"), os.Getenv("STRUCTOR_ADMIN_PASSWORD"))
	})

	app.OnServe().BindFunc(func(se *core.ServeEvent) error {
		auth := oauth.New(se.App)
		auth.Register(se)
		m := &mcp.Server{App: se.App, Version: Version, Name: "structor"}

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
			st, err := ingest.GetStatus(e.App, Version)
			if err != nil {
				return e.InternalServerError("status", err)
			}
			return e.JSON(http.StatusOK, st)
		}))
		g.GET("/search", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			hits, err := ingest.Search(e.App, ingest.SearchOpts{
				Query: q.Get("q"), Project: q.Get("project"), Week: q.Get("week"),
				Session: q.Get("session"), Role: q.Get("role"), Limit: limit,
			})
			if err != nil {
				return e.InternalServerError("search", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"hits": hits})
		}))
		g.GET("/sessions", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, err := ingest.ListSessions(e.App, q.Get("project"), q.Get("week"), limit)
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
			if err != nil {
				return e.InternalServerError("read", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"events": rows, "offset": offset})
		}))
		g.GET("/days", requireBearer(func(e *core.RequestEvent) error {
			q := e.Request.URL.Query()
			limit, _ := strconv.Atoi(q.Get("limit"))
			rows, err := ingest.Days(e.App, q.Get("from"), q.Get("to"), q.Get("project"), loc, limit)
			if err != nil {
				return e.BadRequestError("days", err)
			}
			return e.JSON(http.StatusOK, map[string]any{"days": rows, "tz": loc.String()})
		}))
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

		// dashboard
		sub, err := fs.Sub(uiFS, "ui")
		if err != nil {
			return err
		}
		se.Router.GET("/{path...}", apis.Static(sub, false))

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
