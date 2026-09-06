package main

import (
	"regexp"
	"strings"
	"testing"
)

var commandBarPattern = regexp.MustCompile(`(?s)<header class="command-bar">.*?</header>`)

func readUI(t *testing.T, name string) string {
	t.Helper()
	b, err := uiFS.ReadFile("ui/" + name)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func commandBar(t *testing.T, name string) string {
	t.Helper()
	header := commandBarPattern.FindString(readUI(t, name))
	if header == "" {
		t.Fatalf("%s is missing <header class=\"command-bar\">", name)
	}
	return strings.TrimSpace(header)
}

func TestDefaultUIKeepsOriginalWorkspaces(t *testing.T) {
	s := readUI(t, "index.html")
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
	s := readUI(t, "simple.html")
	for _, required := range []string{`id="drop"`, `id="hRecent"`, `href="./"`, `href="./?ws=history"`} {
		if !strings.Contains(s, required) {
			t.Errorf("simple UI missing %s", required)
		}
	}
	if _, err := uiFS.ReadFile("ui/console.html"); err != nil {
		t.Fatal("existing console bookmarks must remain available:", err)
	}
}

func TestEveryPageUsesTheSameCommandBar(t *testing.T) {
	want := commandBar(t, "index.html")
	for _, name := range []string{"console.html", "simple.html"} {
		if got := commandBar(t, name); got != want {
			t.Errorf("%s command bar differs from index.html", name)
		}
	}
}

func TestSharedCommandBarContainsEveryAppDestination(t *testing.T) {
	header := commandBar(t, "index.html")
	for _, required := range []string{
		`href="simple.html"`,
		`id="wsSeg"`,
		`data-ws="intake"`,
		`data-ws="events"`,
		`data-ws="history"`,
		`data-ws="projects"`,
	} {
		if !strings.Contains(header, required) {
			t.Errorf("shared command bar missing %s", required)
		}
	}
}

func TestEveryPageLoadsSharedNavigationStyles(t *testing.T) {
	const stylesheet = `<link rel="stylesheet" href="navigation.css">`
	for _, name := range []string{"index.html", "console.html", "simple.html"} {
		if !strings.Contains(readUI(t, name), stylesheet) {
			t.Errorf("%s does not load %s", name, stylesheet)
		}
	}
}

func TestEveryPageUsesTheSameThemeAndTokenStorageKeys(t *testing.T) {
	keyPattern := regexp.MustCompile(`KEY_TOKEN\s*=\s*['"]([^'"]+)['"]\s*,\s*KEY_THEME\s*=\s*['"]([^'"]+)['"]`)
	for _, name := range []string{"index.html", "console.html", "simple.html"} {
		match := keyPattern.FindStringSubmatch(readUI(t, name))
		if len(match) != 3 {
			t.Fatalf("%s does not define both storage keys", name)
		}
		if match[1] != "structor.token" || match[2] != "structor.theme" {
			t.Errorf("%s storage keys = (%q, %q), want (%q, %q)", name, match[1], match[2], "structor.token", "structor.theme")
		}
	}
}
