// Package oauth is a minimal OAuth 2.1 authorization server for the MCP
// endpoint: RFC 8414 metadata, RFC 9728 protected-resource metadata,
// RFC 7591 dynamic client registration, authorization-code grant with PKCE
// (S256 only), and refresh tokens. The "user" who logs in is a PocketBase
// superuser — the same username/password that runs the admin UI.
//
// Bearer tokens accepted on /mcp, in order:
//  1. an OAuth access token issued here
//  2. a PocketBase superuser auth token (Claude Code with a local login)
//  3. STRUCTOR_MCP_TOKEN, a static shared secret for scripts
package oauth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tools/types"

	"structor/internal/schema"
)

const (
	codeTTL    = 10 * time.Minute
	accessTTL  = 30 * 24 * time.Hour
	refreshTTL = 90 * 24 * time.Hour
)

// Server wires the OAuth routes.
type Server struct {
	App         core.App
	PublicURL   string // optional override, e.g. https://structor.example.com
	StaticToken string // optional STRUCTOR_MCP_TOKEN
	ResourcePath string // "/mcp"
}

func New(app core.App) *Server {
	return &Server{
		App:          app,
		PublicURL:    strings.TrimRight(os.Getenv("STRUCTOR_PUBLIC_URL"), "/"),
		StaticToken:  os.Getenv("STRUCTOR_MCP_TOKEN"),
		ResourcePath: "/mcp",
	}
}

// BaseURL derives the externally visible origin for this request.
func (s *Server) BaseURL(r *http.Request) string {
	if s.PublicURL != "" {
		return s.PublicURL
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if p := r.Header.Get("X-Forwarded-Proto"); p != "" {
		scheme = strings.Split(p, ",")[0]
	}
	host := r.Host
	if h := r.Header.Get("X-Forwarded-Host"); h != "" {
		host = strings.Split(h, ",")[0]
	}
	return scheme + "://" + host
}

// Register mounts every route on the PocketBase router group.
func (s *Server) Register(se *core.ServeEvent) {
	r := se.Router
	for _, p := range []string{"/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp", "/.well-known/openid-configuration"} {
		r.GET(p, s.metadata)
	}
	for _, p := range []string{"/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"} {
		r.GET(p, s.protectedResource)
	}
	r.POST("/oauth/register", s.register)
	r.GET("/oauth/authorize", s.authorizeForm)
	r.POST("/oauth/authorize", s.authorizeSubmit)
	r.POST("/oauth/token", s.token)
	r.OPTIONS("/oauth/{path...}", cors)
	r.OPTIONS("/.well-known/{path...}", cors)
}

func cors(e *core.RequestEvent) error {
	h := e.Response.Header()
	h.Set("Access-Control-Allow-Origin", "*")
	h.Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
	h.Set("Access-Control-Allow-Headers", "Authorization, Content-Type, Mcp-Protocol-Version")
	return e.NoContent(http.StatusNoContent)
}

func (s *Server) metadata(e *core.RequestEvent) error {
	base := s.BaseURL(e.Request)
	e.Response.Header().Set("Access-Control-Allow-Origin", "*")
	return e.JSON(http.StatusOK, map[string]any{
		"issuer":                                base,
		"authorization_endpoint":                base + "/oauth/authorize",
		"token_endpoint":                        base + "/oauth/token",
		"registration_endpoint":                 base + "/oauth/register",
		"response_types_supported":              []string{"code"},
		"response_modes_supported":              []string{"query"},
		"grant_types_supported":                 []string{"authorization_code", "refresh_token"},
		"code_challenge_methods_supported":      []string{"S256"},
		"token_endpoint_auth_methods_supported": []string{"none", "client_secret_post", "client_secret_basic"},
		"scopes_supported":                      []string{"mcp"},
	})
}

func (s *Server) protectedResource(e *core.RequestEvent) error {
	base := s.BaseURL(e.Request)
	e.Response.Header().Set("Access-Control-Allow-Origin", "*")
	return e.JSON(http.StatusOK, map[string]any{
		"resource":                 base + s.ResourcePath,
		"authorization_servers":    []string{base},
		"bearer_methods_supported": []string{"header"},
		"scopes_supported":         []string{"mcp"},
		"resource_name":            "Structor MCP",
	})
}

type registerReq struct {
	ClientName              string   `json:"client_name"`
	RedirectURIs            []string `json:"redirect_uris"`
	TokenEndpointAuthMethod string   `json:"token_endpoint_auth_method"`
	GrantTypes              []string `json:"grant_types"`
	ResponseTypes           []string `json:"response_types"`
	Scope                   string   `json:"scope"`
}

func (s *Server) register(e *core.RequestEvent) error {
	e.Response.Header().Set("Access-Control-Allow-Origin", "*")
	var req registerReq
	if err := e.BindBody(&req); err != nil {
		return e.JSON(http.StatusBadRequest, map[string]string{"error": "invalid_client_metadata", "error_description": err.Error()})
	}
	if len(req.RedirectURIs) == 0 {
		return e.JSON(http.StatusBadRequest, map[string]string{"error": "invalid_redirect_uri", "error_description": "redirect_uris required"})
	}
	for _, u := range req.RedirectURIs {
		p, err := url.Parse(u)
		if err != nil || (p.Scheme != "https" && p.Hostname() != "localhost" && p.Hostname() != "127.0.0.1") {
			return e.JSON(http.StatusBadRequest, map[string]string{"error": "invalid_redirect_uri", "error_description": "redirect_uris must be https or localhost"})
		}
	}
	method := req.TokenEndpointAuthMethod
	if method == "" {
		method = "none"
	}
	clientID := randomToken(16)
	var secret string
	if method != "none" {
		secret = randomToken(32)
	}
	col, err := s.App.FindCollectionByNameOrId(schema.OAuthClients)
	if err != nil {
		return e.InternalServerError("oauth_clients missing", err)
	}
	rec := core.NewRecord(col)
	rec.Set("client_id", clientID)
	if secret != "" {
		rec.Set("client_secret_hash", hashToken(secret))
	}
	rec.Set("client_name", req.ClientName)
	uris, _ := json.Marshal(req.RedirectURIs)
	rec.Set("redirect_uris", string(uris))
	rec.Set("token_endpoint_auth_method", method)
	if err := s.App.Save(rec); err != nil {
		return e.InternalServerError("save client", err)
	}
	resp := map[string]any{
		"client_id":                  clientID,
		"client_name":                req.ClientName,
		"redirect_uris":              req.RedirectURIs,
		"token_endpoint_auth_method": method,
		"grant_types":                []string{"authorization_code", "refresh_token"},
		"response_types":             []string{"code"},
		"client_id_issued_at":        time.Now().Unix(),
	}
	if secret != "" {
		resp["client_secret"] = secret
		resp["client_secret_expires_at"] = 0
	}
	return e.JSON(http.StatusCreated, resp)
}

func (s *Server) findClient(clientID string) (*core.Record, error) {
	return s.App.FindFirstRecordByData(schema.OAuthClients, "client_id", clientID)
}

func redirectAllowed(rec *core.Record, uri string) bool {
	var uris []string
	_ = json.Unmarshal([]byte(rec.GetString("redirect_uris")), &uris)
	for _, u := range uris {
		if u == uri {
			return true
		}
	}
	return false
}

type authParams struct {
	ClientID, RedirectURI, State, Challenge, Method, Scope, Resource string
}

func readAuthParams(q url.Values) authParams {
	return authParams{
		ClientID:    q.Get("client_id"),
		RedirectURI: q.Get("redirect_uri"),
		State:       q.Get("state"),
		Challenge:   q.Get("code_challenge"),
		Method:      q.Get("code_challenge_method"),
		Scope:       q.Get("scope"),
		Resource:    q.Get("resource"),
	}
}

func (s *Server) validateAuth(p authParams) (*core.Record, string) {
	if p.ClientID == "" || p.RedirectURI == "" {
		return nil, "client_id and redirect_uri are required"
	}
	client, err := s.findClient(p.ClientID)
	if err != nil {
		return nil, "unknown client_id"
	}
	if !redirectAllowed(client, p.RedirectURI) {
		return nil, "redirect_uri not registered for this client"
	}
	if p.Challenge == "" || (p.Method != "S256" && p.Method != "") {
		return nil, "PKCE S256 code_challenge required"
	}
	return client, ""
}

func (s *Server) authorizeForm(e *core.RequestEvent) error {
	q := e.Request.URL.Query()
	if q.Get("response_type") != "code" {
		return e.String(http.StatusBadRequest, "response_type must be code")
	}
	p := readAuthParams(q)
	client, problem := s.validateAuth(p)
	if problem != "" {
		return e.String(http.StatusBadRequest, problem)
	}
	return e.HTML(http.StatusOK, loginPage(client.GetString("client_name"), p, ""))
}

func (s *Server) authorizeSubmit(e *core.RequestEvent) error {
	if err := e.Request.ParseForm(); err != nil {
		return e.String(http.StatusBadRequest, "bad form")
	}
	f := e.Request.PostForm
	p := readAuthParams(f)
	client, problem := s.validateAuth(p)
	if problem != "" {
		return e.String(http.StatusBadRequest, problem)
	}
	email := strings.TrimSpace(f.Get("email"))
	password := f.Get("password")
	user, err := s.App.FindAuthRecordByEmail(core.CollectionNameSuperusers, email)
	if err != nil || !user.ValidatePassword(password) {
		return e.HTML(http.StatusUnauthorized, loginPage(client.GetString("client_name"), p, "Wrong email or password."))
	}
	code := randomToken(32)
	col, err := s.App.FindCollectionByNameOrId(schema.OAuthCodes)
	if err != nil {
		return e.InternalServerError("oauth_codes missing", err)
	}
	rec := core.NewRecord(col)
	rec.Set("code_hash", hashToken(code))
	rec.Set("client_id", p.ClientID)
	rec.Set("redirect_uri", p.RedirectURI)
	rec.Set("code_challenge", p.Challenge)
	rec.Set("scope", p.Scope)
	rec.Set("subject", user.Email())
	exp, _ := types.ParseDateTime(time.Now().Add(codeTTL))
	rec.Set("expires", exp)
	if err := s.App.Save(rec); err != nil {
		return e.InternalServerError("save code", err)
	}
	u, _ := url.Parse(p.RedirectURI)
	qs := u.Query()
	qs.Set("code", code)
	if p.State != "" {
		qs.Set("state", p.State)
	}
	u.RawQuery = qs.Encode()
	return e.Redirect(http.StatusFound, u.String())
}

func (s *Server) token(e *core.RequestEvent) error {
	e.Response.Header().Set("Access-Control-Allow-Origin", "*")
	e.Response.Header().Set("Cache-Control", "no-store")
	if err := e.Request.ParseForm(); err != nil {
		return tokenErr(e, "invalid_request", "bad form")
	}
	f := e.Request.PostForm
	clientID := f.Get("client_id")
	clientSecret := f.Get("client_secret")
	if u, pw, ok := e.Request.BasicAuth(); ok {
		clientID, clientSecret = u, pw
	}
	client, err := s.findClient(clientID)
	if err != nil {
		return tokenErr(e, "invalid_client", "unknown client")
	}
	if h := client.GetString("client_secret_hash"); h != "" {
		if subtle.ConstantTimeCompare([]byte(h), []byte(hashToken(clientSecret))) != 1 {
			return tokenErr(e, "invalid_client", "bad client secret")
		}
	}

	switch f.Get("grant_type") {
	case "authorization_code":
		code := f.Get("code")
		verifier := f.Get("code_verifier")
		rec, err := s.App.FindFirstRecordByData(schema.OAuthCodes, "code_hash", hashToken(code))
		if err != nil || rec.GetBool("used") || rec.GetString("client_id") != clientID {
			return tokenErr(e, "invalid_grant", "code unknown, used, or for another client")
		}
		if rec.GetDateTime("expires").Time().Before(time.Now()) {
			return tokenErr(e, "invalid_grant", "code expired")
		}
		if ru := f.Get("redirect_uri"); ru != "" && ru != rec.GetString("redirect_uri") {
			return tokenErr(e, "invalid_grant", "redirect_uri mismatch")
		}
		if !pkceOK(rec.GetString("code_challenge"), verifier) {
			return tokenErr(e, "invalid_grant", "PKCE verification failed")
		}
		rec.Set("used", true)
		_ = s.App.Save(rec)
		return s.issue(e, clientID, rec.GetString("subject"), rec.GetString("scope"))
	case "refresh_token":
		rt := f.Get("refresh_token")
		rec, err := s.App.FindFirstRecordByData(schema.OAuthTokens, "token_hash", hashToken(rt))
		if err != nil || rec.GetString("kind") != "refresh" || rec.GetBool("revoked") || rec.GetString("client_id") != clientID {
			return tokenErr(e, "invalid_grant", "refresh token invalid")
		}
		if rec.GetDateTime("expires").Time().Before(time.Now()) {
			return tokenErr(e, "invalid_grant", "refresh token expired")
		}
		rec.Set("revoked", true)
		_ = s.App.Save(rec)
		return s.issue(e, clientID, rec.GetString("subject"), rec.GetString("scope"))
	}
	return tokenErr(e, "unsupported_grant_type", "use authorization_code or refresh_token")
}

func tokenErr(e *core.RequestEvent, code, desc string) error {
	return e.JSON(http.StatusBadRequest, map[string]string{"error": code, "error_description": desc})
}

func (s *Server) issue(e *core.RequestEvent, clientID, subject, scope string) error {
	access := randomToken(32)
	refresh := randomToken(32)
	col, err := s.App.FindCollectionByNameOrId(schema.OAuthTokens)
	if err != nil {
		return e.InternalServerError("oauth_tokens missing", err)
	}
	if scope == "" {
		scope = "mcp"
	}
	for _, t := range []struct {
		raw, kind string
		ttl       time.Duration
	}{{access, "access", accessTTL}, {refresh, "refresh", refreshTTL}} {
		rec := core.NewRecord(col)
		rec.Set("token_hash", hashToken(t.raw))
		rec.Set("kind", t.kind)
		rec.Set("client_id", clientID)
		rec.Set("subject", subject)
		rec.Set("scope", scope)
		exp, _ := types.ParseDateTime(time.Now().Add(t.ttl))
		rec.Set("expires", exp)
		if err := s.App.Save(rec); err != nil {
			return e.InternalServerError("save token", err)
		}
	}
	return e.JSON(http.StatusOK, map[string]any{
		"access_token":  access,
		"token_type":    "Bearer",
		"expires_in":    int(accessTTL.Seconds()),
		"refresh_token": refresh,
		"scope":         scope,
	})
}

// Principal describes who a bearer token belongs to.
type Principal struct {
	Subject string
	Via     string // oauth | pb-superuser | static
}

// Authenticate checks the Authorization header. ok=false means 401.
func (s *Server) Authenticate(e *core.RequestEvent) (Principal, bool) {
	if e.HasSuperuserAuth() {
		return Principal{Subject: e.Auth.Email(), Via: "pb-superuser"}, true
	}
	raw := strings.TrimSpace(e.Request.Header.Get("Authorization"))
	if raw == "" {
		return Principal{}, false
	}
	token := raw
	if len(raw) > 7 && strings.EqualFold(raw[:7], "bearer ") {
		token = strings.TrimSpace(raw[7:])
	}
	if s.StaticToken != "" && subtle.ConstantTimeCompare([]byte(token), []byte(s.StaticToken)) == 1 {
		return Principal{Subject: "static", Via: "static"}, true
	}
	rec, err := s.App.FindFirstRecordByData(schema.OAuthTokens, "token_hash", hashToken(token))
	if err == nil {
		if rec.GetString("kind") == "access" && !rec.GetBool("revoked") && rec.GetDateTime("expires").Time().After(time.Now()) {
			return Principal{Subject: rec.GetString("subject"), Via: "oauth"}, true
		}
		return Principal{}, false
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return Principal{}, false
	}
	if user, err := s.App.FindAuthRecordByToken(token, core.TokenTypeAuth); err == nil && user.IsSuperuser() {
		return Principal{Subject: user.Email(), Via: "pb-superuser"}, true
	}
	return Principal{}, false
}

// Challenge writes the 401 that tells an MCP client where to start OAuth.
func (s *Server) Challenge(e *core.RequestEvent) error {
	base := s.BaseURL(e.Request)
	e.Response.Header().Set("WWW-Authenticate",
		fmt.Sprintf(`Bearer resource_metadata="%s/.well-known/oauth-protected-resource"`, base))
	return e.JSON(http.StatusUnauthorized, map[string]string{"error": "unauthorized", "error_description": "bearer token required"})
}

// Prune deletes expired codes and tokens. Safe to call on a timer.
func (s *Server) Prune() {
	now := types.NowDateTime().String()
	_, _ = s.App.DB().NewQuery("DELETE FROM " + schema.OAuthCodes + " WHERE expires < {:now} OR used = 1").Bind(dbx.Params{"now": now}).Execute()
	_, _ = s.App.DB().NewQuery("DELETE FROM " + schema.OAuthTokens + " WHERE expires < {:now}").Bind(dbx.Params{"now": now}).Execute()
}

func pkceOK(challenge, verifier string) bool {
	if challenge == "" || verifier == "" {
		return false
	}
	sum := sha256.Sum256([]byte(verifier))
	calc := base64.RawURLEncoding.EncodeToString(sum[:])
	return subtle.ConstantTimeCompare([]byte(calc), []byte(challenge)) == 1
}

func hashToken(t string) string {
	sum := sha256.Sum256([]byte(t))
	return hex.EncodeToString(sum[:])
}

func randomToken(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

func loginPage(clientName string, p authParams, errMsg string) string {
	hidden := ""
	for k, v := range map[string]string{
		"client_id": p.ClientID, "redirect_uri": p.RedirectURI, "state": p.State,
		"code_challenge": p.Challenge, "code_challenge_method": "S256", "scope": p.Scope, "resource": p.Resource,
	} {
		hidden += fmt.Sprintf(`<input type="hidden" name="%s" value="%s">`, k, html.EscapeString(v))
	}
	if clientName == "" {
		clientName = "an MCP client"
	}
	errHTML := ""
	if errMsg != "" {
		errHTML = `<p class="err">` + html.EscapeString(errMsg) + `</p>`
	}
	return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Structor — sign in</title>
<style>
body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:28px;width:min(360px,90vw)}
h1{font-size:18px;margin:0 0 4px}p{margin:0 0 16px;color:#9aa3b2}label{display:block;margin:10px 0 4px;color:#c7cdd8}
input[type=email],input[type=password]{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:#fff}
button{margin-top:18px;width:100%;padding:11px;border:0;border-radius:8px;background:#4f7cff;color:#fff;font-weight:600;cursor:pointer}
.err{color:#ff7a7a}.small{font-size:12px;margin-top:14px}
</style></head><body>
<form method="post" action="/oauth/authorize">
<h1>Structor</h1>
<p>Allow <strong>` + html.EscapeString(clientName) + `</strong> to read your session index over MCP?</p>
` + errHTML + hidden + `
<label>Email</label><input type="email" name="email" autocomplete="username" required autofocus>
<label>Password</label><input type="password" name="password" autocomplete="current-password" required>
<button type="submit">Sign in &amp; allow</button>
<p class="small">Signs in with the PocketBase superuser account. Tokens can be revoked in the admin UI under oauth_tokens.</p>
</form></body></html>`
}
