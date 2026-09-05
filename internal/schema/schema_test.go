package schema

import (
	"testing"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
)

func TestEnsureAddsMissingFields(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatal(err)
	}
	defer app.Cleanup()

	if err := Ensure(app); err != nil {
		t.Fatal(err)
	}
	// simulate an older deployment: drop a field and an index from projects
	projects, _ := app.FindCollectionByNameOrId(Projects)
	projects.Fields.RemoveByName("cwd")
	projects.Indexes = nil
	if err := app.Save(projects); err != nil {
		t.Fatal(err)
	}
	projects, _ = app.FindCollectionByNameOrId(Projects)
	if projects.Fields.GetByName("cwd") != nil {
		t.Fatal("setup: cwd should be gone")
	}

	if err := Ensure(app); err != nil {
		t.Fatal(err)
	}
	projects, _ = app.FindCollectionByNameOrId(Projects)
	if projects.Fields.GetByName("cwd") == nil {
		t.Fatal("cwd not re-added")
	}
	if len(projects.Indexes) != 1 {
		t.Fatalf("index not restored: %v", projects.Indexes)
	}

	// superuser upsert is idempotent
	if err := EnsureSuperuser(app, "x@y.z", "password123"); err != nil {
		t.Fatal(err)
	}
	if err := EnsureSuperuser(app, "x@y.z", "password123"); err != nil {
		t.Fatal(err)
	}
	u, err := app.FindAuthRecordByEmail(core.CollectionNameSuperusers, "x@y.z")
	if err != nil || !u.ValidatePassword("password123") {
		t.Fatal("superuser missing or wrong password")
	}
	if err := EnsureSuperuser(app, "x@y.z", "newpassword456"); err != nil {
		t.Fatal(err)
	}
	u, _ = app.FindAuthRecordByEmail(core.CollectionNameSuperusers, "x@y.z")
	if !u.ValidatePassword("newpassword456") {
		t.Fatal("password not reset")
	}
}
