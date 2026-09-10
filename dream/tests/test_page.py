"""A page says what made it, cites what it rests on, and is rewritten only when its content moved."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

from dream_fixtures import ScriptedChat, asker, seed_store
from structor_lance import wiki

from structor_dream import cli, material, page, reduce


def dreamed(tmp_path: Path) -> tuple[Path, str]:
    r, e = seed_store(tmp_path / "store")
    root = tmp_path / "dreams"
    a = asker(r, e, ScriptedChat())
    out = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=False, log=lambda _l: None)
    return Path(out["page"]), Path(out["page"]).read_text(encoding="utf-8")


def test_week_page_frontmatter_rule6_sections_and_sources(tmp_path: Path):
    _path, text = dreamed(tmp_path)
    meta, body = wiki.frontmatter(text)
    assert meta["kind"] == "dream" and meta["mode"] == "week" and meta["week"] == "2026-W37"
    assert meta["generated_by"] == "fake-model" and meta["status"] == "inference"
    assert meta["generated_at"].startswith("20") and "+07:00" in meta["generated_at"]
    assert meta["sessions_in_week"] == "6" and meta["sessions_digested"] == "4" and meta["projects"] == "3"
    assert meta["events_in_week"] == str(30 + 12 + 20 + 10 + 50 + 5)
    # the sources are the transcript uuids of every session the body cites — s1 (digest 1) and s3 (digest 2), once each
    assert meta["sources"] == ["11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "33333333-cccc-4ccc-8ccc-cccccccccccc"]

    assert body.startswith("\n# Dream — 2026-W37\n")
    how = body.split("\n")[3]
    assert "inference, not measurement" in how and "model-generated" in how and "`fake-model`" in how
    assert "6 sessions with events" in how and "4 sessions were chosen (cap 10" in how and "4 of them were digested" in how
    assert "an AI tool, not a person" in how
    # every section heading carries the week, so the wiki's per-section rows match a "Dream — <week>" query
    for title in ("## Patterns — 2026-W37", "## Decisions — 2026-W37", "## Lessons — 2026-W37",
                  "## Contradictions with last week — 2026-W37", "## Abandoned threads — 2026-W37",
                  "## Open questions — 2026-W37", "## Insights — 2026-W37", "## Sources"):
        assert f"\n{title}\n" in body, title
    assert "- Launchd and PATH problems came up in more than one project. [11111111, 33333333]" in body
    assert page.NO_CLAIM in body.split("## Contradictions with last week")[1].split("## Abandoned")[0]   # no previous week
    assert body.count("- Insight ") == 5 and "- Insight six" not in body
    assert "_Unsupported, in the model's words: Insight six rests on nothing in the digests._" in body

    sources = body.split("## Sources\n")[1]
    assert sources.count("`11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa`") == 1 and sources.count("`33333333-cccc") == 1
    assert "structor · 2026-09-08 10:00 → 2026-09-08 12:00 · events cited by its digest: `s1-37-0`, `s1-37-1`" in sources
    assert "2 more session(s) were digested and not cited: 44444444, 22222222." in sources   # stratified order
    assert "Bogus" not in body and "STRUCTOR-OWNED" not in text


def test_page_is_rewritten_only_when_the_content_changes(tmp_path: Path):
    path, text = dreamed(tmp_path)
    old = time.time() - 3600
    os.utime(path, (old, old))
    restamped = text.replace(wiki.frontmatter(text)[0]["generated_at"], "2030-01-01T00:00:00+07:00")
    assert not page.write_if_changed(path, restamped) and abs(path.stat().st_mtime - old) < 2
    assert "2030" not in path.read_text()
    assert page.write_if_changed(path, text.replace("Insight one", "Insight uno")) and path.stat().st_mtime > old + 100
    assert "Insight uno" in path.read_text() and not list(path.parent.glob("*.tmp"))
    assert page.write_if_changed(tmp_path / "new" / "x.md", "fresh") and (tmp_path / "new" / "x.md").read_text() == "fresh"


def test_previous_week_insights_feed_the_next_reduce(tmp_path: Path):
    r, e = seed_store(tmp_path / "store")
    root = tmp_path / "dreams"
    chat = ScriptedChat()
    a = asker(r, e, chat)
    cli.run_week(r, a, "2026-W36", root, max_sessions=10, force=False, log=lambda _l: None)
    assert reduce.previous_insights((root / "2026-W36.md").read_text()).startswith("- Insight one about launchd. [33333333]")
    out = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=False, log=lambda _l: None)
    assert "<previous_week n=0 2026-W36>" in chat.calls[-1][1]["content"]
    body = Path(out["page"]).read_text()
    assert "with the Insights of 2026-W36 fed for contradictions" in body
    assert "- Last week counted six indexers, this week ten. [33333333]" in body


def test_topic_page_has_the_material_table_per_horizon_and_event_sources(tmp_path: Path):
    r, e = seed_store(tmp_path / "store", embed=True)
    chat = ScriptedChat()
    a = asker(r, e, chat)
    hits = e.search("409 offset mismatch second writer", limit=20, where=cli.SEARCH_WHERE, mode="hybrid")
    items = material.topic_material(hits, 8, datetime(2026, 9, 10, tzinfo=UTC), a.names())
    assert items and all(i["horizon"] == "short" for i in items)               # everything seeded is days old
    reduced = reduce.dream_topic(a, "409 offset mismatch", items)
    assert "The theme is: 409 offset mismatch" in chat.calls[-1][1]["content"]
    assert "<event n=1 2026-09-08 10:00 user short · " in chat.calls[-1][1]["content"] or "assistant short · " in chat.calls[-1][1]["content"]

    made = {"model": "fake-model", "generated_at": "2026-09-10T13:00:00+07:00", "retrieved": len(hits), "share": 2}
    text = page.topic_page("409 offset mismatch", made, reduced, items)
    meta, body = wiki.frontmatter(text)
    assert meta["kind"] == "dream" and meta["mode"] == "topic" and meta["query"] == "409 offset mismatch"
    assert meta["status"] == "inference" and meta["hits"] == str(len(items)) and meta["sources"]
    assert body.startswith("\n# Dream — topic: 409 offset mismatch\n")
    assert "| n | horizon | when | project | phrase |" in body
    rows = [line for line in body.split("\n") if line.startswith("| ") and line[2].isdigit()]
    assert len(rows) == len(items) and all(" | short | 2026-09-08 " in row for row in rows)
    assert all(len(row.split(" | ")[-1]) <= page.PHRASE_CAP + 4 for row in rows)   # one quoted phrase, capped
    assert "## Patterns across horizons and projects" in body and "## Contradictions (short-term vs long-term)" in body
    assert "- The 409 offset mismatch recurs whenever two writers race. [1]" in body and "Bogus" not in body
    assert "## Sources\n" in body and "- [1] event `" in body and "· session `22222222-bbbb" in body
    assert page.topic_slug("409 offset mismatch") == "409-offset-mismatch" and len(page.topic_slug("x " * 100)) <= page.SLUG_CAP


def test_yaml_values_are_quoted_when_yaml_would_misread_them():
    assert page.yaml_value("gemma3:27b") == '"gemma3:27b"' and page.yaml_value("plain") == "plain"
    assert page.yaml_value("2026-W37") == '"2026-W37"' and page.yaml_value(12) == "12" and page.yaml_value(12.0) == "12"
    assert page.yaml_value(["a", "b:c"]) == '[a, "b:c"]' and page.yaml_value("") == '""'
    assert page.yaml_value('say "hi"') == '"say \\"hi\\""'
    meta, _ = wiki.frontmatter(page.frontmatter({"generated_by": "gemma3:27b", "week": "2026-W37", "sources": ["a", "b"]}))
    assert meta == {"generated_by": "gemma3:27b", "week": "2026-W37", "sources": ["a", "b"]}
