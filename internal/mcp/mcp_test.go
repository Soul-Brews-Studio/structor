package mcp

import (
	"encoding/json"
	"testing"

	"github.com/pocketbase/pocketbase/tests"

	"structor/internal/schema"
)

func TestDispatch(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatal(err)
	}
	defer app.Cleanup()
	if err := schema.Ensure(app); err != nil {
		t.Fatal(err)
	}
	s := &Server{App: app, Version: "t", Name: "structor"}

	resp, ok := s.dispatch(rpcRequest{JSONRPC: "2.0", ID: json.RawMessage(`1`), Method: "initialize"})
	if !ok || resp.Error != nil {
		t.Fatalf("initialize: %+v", resp)
	}
	if _, ok := s.dispatch(rpcRequest{JSONRPC: "2.0", Method: "notifications/initialized"}); ok {
		t.Fatal("notification produced a response")
	}
	resp, _ = s.dispatch(rpcRequest{JSONRPC: "2.0", ID: json.RawMessage(`2`), Method: "tools/list"})
	tools := resp.Result.(map[string]any)["tools"].([]tool)
	if len(tools) != 6 {
		t.Fatalf("tools = %d", len(tools))
	}
	resp, _ = s.dispatch(rpcRequest{JSONRPC: "2.0", ID: json.RawMessage(`3`), Method: "tools/call", Params: json.RawMessage(`{"name":"status","arguments":{}}`)})
	if resp.Error != nil || resp.Result.(map[string]any)["isError"] != nil {
		t.Fatalf("status call: %+v", resp)
	}
	resp, _ = s.dispatch(rpcRequest{JSONRPC: "2.0", ID: json.RawMessage(`4`), Method: "tools/call", Params: json.RawMessage(`{"name":"search","arguments":{}}`)})
	if resp.Result.(map[string]any)["isError"] != true {
		t.Fatal("search without query should be a tool error")
	}
	resp, _ = s.dispatch(rpcRequest{JSONRPC: "2.0", ID: json.RawMessage(`5`), Method: "nope"})
	if resp.Error == nil || resp.Error.Code != -32601 {
		t.Fatalf("unknown method: %+v", resp)
	}
}
