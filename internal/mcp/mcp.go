// Package mcp serves the Model Context Protocol over Streamable HTTP from
// inside the PocketBase process: POST /mcp takes a JSON-RPC 2.0 message and
// answers with JSON. No SSE stream is offered (GET returns 405), which the
// spec allows for servers that never push. Tools are read-only views over
// the event store.
package mcp

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/pocketbase/pocketbase/core"

	"structor/internal/ingest"
)

const ProtocolVersion = "2025-06-18"

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type tool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// Server holds the app handle and version string.
type Server struct {
	App     core.App
	Version string
	Name    string
}

func obj(props map[string]any, required ...string) map[string]any {
	s := map[string]any{"type": "object", "properties": props}
	if len(required) > 0 {
		s["required"] = required
	}
	return s
}

func str(desc string) map[string]any { return map[string]any{"type": "string", "description": desc} }
func num(desc string) map[string]any { return map[string]any{"type": "integer", "description": desc} }

func (s *Server) tools() []tool {
	return []tool{
		{Name: "status", Description: "Store totals: projects, sessions, events, session_weeks, last ingest, recent ISO weeks.", InputSchema: obj(map[string]any{})},
		{Name: "list_projects", Description: "Projects with session and event counts, most recent first.", InputSchema: obj(map[string]any{"limit": num("max rows, default 200")})},
		{Name: "list_sessions", Description: "Sessions most recent first. Filter by project path substring and/or ISO week (2026-W36).", InputSchema: obj(map[string]any{"project": str("substring of project path"), "week": str("ISO week like 2026-W36"), "limit": num("max rows, default 50")})},
		{Name: "search", Description: "Substring search over transcript text (case-insensitive, finds Thai inside words). Returns snippets with session id, ISO week, role.", InputSchema: obj(map[string]any{"query": str("text to find"), "project": str("substring of project path"), "week": str("ISO week"), "session": str("session id prefix"), "role": str("user or assistant"), "limit": num("max hits, default 30")}, "query")},
		{Name: "read_session", Description: "Page through one session's messages in time order.", InputSchema: obj(map[string]any{"session": str("session id or prefix"), "offset": num("rows to skip"), "limit": num("rows, default 100")}, "session")},
		{Name: "week_ledger", Description: "The per-(session, ISO week) ledger: message and tool counts, first/last timestamp. Filter by week or session.", InputSchema: obj(map[string]any{"week": str("ISO week like 2026-W36"), "session": str("session id prefix"), "limit": num("max rows, default 100")})},
	}
}

// Handle is the PocketBase route action for /mcp.
func (s *Server) Handle(e *core.RequestEvent) error {
	switch e.Request.Method {
	case http.MethodGet:
		e.Response.Header().Set("Allow", "POST, DELETE")
		return e.String(http.StatusMethodNotAllowed, "SSE stream not offered; POST JSON-RPC to this endpoint")
	case http.MethodDelete:
		return e.NoContent(http.StatusOK)
	case http.MethodPost:
	default:
		return e.String(http.StatusMethodNotAllowed, "method not allowed")
	}

	body, err := readBody(e)
	if err != nil {
		return e.JSON(http.StatusBadRequest, rpcResponse{JSONRPC: "2.0", Error: &rpcError{-32700, "parse error: " + err.Error()}})
	}
	trimmed := strings.TrimSpace(string(body))
	if strings.HasPrefix(trimmed, "[") {
		var batch []rpcRequest
		if err := json.Unmarshal(body, &batch); err != nil {
			return e.JSON(http.StatusBadRequest, rpcResponse{JSONRPC: "2.0", Error: &rpcError{-32700, "parse error"}})
		}
		out := make([]rpcResponse, 0, len(batch))
		for _, r := range batch {
			if resp, ok := s.dispatch(r); ok {
				out = append(out, resp)
			}
		}
		if len(out) == 0 {
			return e.NoContent(http.StatusAccepted)
		}
		return e.JSON(http.StatusOK, out)
	}
	var req rpcRequest
	if err := json.Unmarshal(body, &req); err != nil {
		return e.JSON(http.StatusBadRequest, rpcResponse{JSONRPC: "2.0", Error: &rpcError{-32700, "parse error"}})
	}
	resp, ok := s.dispatch(req)
	if !ok {
		return e.NoContent(http.StatusAccepted)
	}
	return e.JSON(http.StatusOK, resp)
}

func readBody(e *core.RequestEvent) ([]byte, error) {
	defer e.Request.Body.Close()
	var buf strings.Builder
	b := make([]byte, 32*1024)
	for {
		n, err := e.Request.Body.Read(b)
		buf.Write(b[:n])
		if err != nil {
			break
		}
		if buf.Len() > 4<<20 {
			return nil, fmt.Errorf("body too large")
		}
	}
	return []byte(buf.String()), nil
}

// dispatch returns (response, true) or (_, false) for notifications.
func (s *Server) dispatch(req rpcRequest) (rpcResponse, bool) {
	isNotification := len(req.ID) == 0 || string(req.ID) == "null"
	resp := rpcResponse{JSONRPC: "2.0", ID: req.ID}
	switch req.Method {
	case "initialize":
		resp.Result = map[string]any{
			"protocolVersion": ProtocolVersion,
			"capabilities":    map[string]any{"tools": map[string]any{"listChanged": false}},
			"serverInfo":      map[string]any{"name": s.Name, "version": s.Version},
			"instructions":    "Structor: week-stamped index of Claude Code session transcripts. Use search for text, week_ledger for what happened in an ISO week, read_session to page a transcript.",
		}
	case "notifications/initialized", "notifications/cancelled", "notifications/progress":
		return resp, false
	case "ping":
		resp.Result = map[string]any{}
	case "tools/list":
		resp.Result = map[string]any{"tools": s.tools()}
	case "tools/call":
		var p struct {
			Name      string         `json:"name"`
			Arguments map[string]any `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &p); err != nil {
			resp.Error = &rpcError{-32602, "invalid params"}
			break
		}
		out, err := s.call(p.Name, p.Arguments)
		if err != nil {
			resp.Result = map[string]any{"content": []map[string]any{{"type": "text", "text": err.Error()}}, "isError": true}
			break
		}
		text, _ := json.MarshalIndent(out, "", " ")
		resp.Result = map[string]any{"content": []map[string]any{{"type": "text", "text": string(text)}}, "structuredContent": out}
	case "resources/list", "prompts/list":
		resp.Result = map[string]any{strings.Split(req.Method, "/")[0]: []any{}}
	default:
		resp.Error = &rpcError{-32601, "method not found: " + req.Method}
	}
	if isNotification {
		return resp, false
	}
	return resp, true
}

func argS(a map[string]any, k string) string {
	if v, ok := a[k]; ok && v != nil {
		return fmt.Sprint(v)
	}
	return ""
}

func argI(a map[string]any, k string) int {
	if v, ok := a[k]; ok && v != nil {
		switch n := v.(type) {
		case float64:
			return int(n)
		case int:
			return n
		case string:
			var i int
			fmt.Sscanf(n, "%d", &i)
			return i
		}
	}
	return 0
}

func (s *Server) call(name string, a map[string]any) (any, error) {
	if a == nil {
		a = map[string]any{}
	}
	switch name {
	case "status":
		return ingest.GetStatus(s.App, s.Version)
	case "list_projects":
		return ingest.ListProjects(s.App, argI(a, "limit"))
	case "list_sessions":
		return ingest.ListSessions(s.App, argS(a, "project"), argS(a, "week"), argI(a, "limit"))
	case "search":
		if argS(a, "query") == "" {
			return nil, fmt.Errorf("query is required")
		}
		return ingest.Search(s.App, ingest.SearchOpts{
			Query: argS(a, "query"), Project: argS(a, "project"), Week: argS(a, "week"),
			Session: argS(a, "session"), Role: argS(a, "role"), Limit: argI(a, "limit"),
		})
	case "read_session":
		if argS(a, "session") == "" {
			return nil, fmt.Errorf("session is required")
		}
		return ingest.ReadSession(s.App, argS(a, "session"), argI(a, "offset"), argI(a, "limit"))
	case "week_ledger":
		return ingest.WeekLedger(s.App, argS(a, "week"), argS(a, "session"), argI(a, "limit"))
	}
	return nil, fmt.Errorf("unknown tool: %s", name)
}
