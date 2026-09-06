package main

import (
	"strings"
	"testing"
)

func TestDefaultUIKeepsOriginalWorkspaces(t *testing.T) {
	b, err := uiFS.ReadFile("ui/index.html")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	for _, required := range []string{`id="wsSeg"`, `data-ws="intake"`, `data-ws="events"`, `data-ws="history"`, `data-ws="projects"`, `href="simple.html"`} {
		if !strings.Contains(s, required) {
			t.Errorf("default UI missing %s", required)
		}
	}
	if strings.Contains(s, `id="drop"`) {
		t.Error("simple import landing replaced the original default UI")
	}
}

func TestSimplePageIsAdditiveAndLinksBack(t *testing.T) {
	b, err := uiFS.ReadFile("ui/simple.html")
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	for _, required := range []string{`id="drop"`, `id="hRecent"`, `href="./"`, `href="./?ws=history"`} {
		if !strings.Contains(s, required) {
			t.Errorf("simple UI missing %s", required)
		}
	}
	if _, err := uiFS.ReadFile("ui/console.html"); err != nil {
		t.Fatal("existing console bookmarks must remain available:", err)
	}
}
