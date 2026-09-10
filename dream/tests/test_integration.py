"""End to end without a GPU: dream a week, re-index the wiki, and find the page's own words through the wiki table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dream_fixtures import ScriptedChat, asker, seed_store
from structor_lance import rag, wiki
from test_rag import FakeChat, seeded  # lance-py's own seeded store and fake chat, imported by path
from typer.testing import CliRunner

from structor_dream import cli

runner = CliRunner()


def test_week_page_is_indexed_beside_the_wiki_and_found_by_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    r, e = seed_store(tmp_path / "store", embed=True)
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "tail-state.md").write_text("# Tail state\n\nThe importer keeps a byte offset per session file.\n")
    chat = ScriptedChat()
    monkeypatch.setattr(cli, "open_replica", lambda target: r)
    monkeypatch.setattr(cli, "open_embedder", lambda target: e)
    monkeypatch.setattr(cli, "asker_for", lambda replica, embedder, model: asker(r, e, chat))
    monkeypatch.setenv("STRUCTOR_WIKI_DIR", str(wiki_root))
    monkeypatch.delenv("STRUCTOR_DREAM_DIR", raising=False)
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(tmp_path / "no-conf"))

    out = runner.invoke(cli.app, ["week", "2026-W37", "--json"])
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout.strip().split("\n")[-1])
    assert result["page"] == str(wiki_root / "dreams" / "2026-W37.md")          # <wiki_dir>/dreams by default
    assert result["indexed"]["files"] == 2 and result["indexed"]["embedded"] >= 8   # the note plus every page section

    hit = wiki.search(r, "Launchd and PATH problems came up in more than one project", limit=3, mode="fts")[0]
    assert hit["path"] == "dreams/2026-W37.md" and hit["section"] == "Patterns — 2026-W37" and hit["title"] == "Dream — 2026-W37"
    assert "[11111111, 33333333]" in hit["text"]

    # an ask over the same store can now cite the page as a kind-wiki source
    a = rag.Asker(r, e, url="http://fake:11434", model="fake-model")
    monkeypatch.setattr(a, "chat_stream", FakeChat())
    answer = a.ask("what did we keep struggling with this week?", k=4, mode="vector", plan=False)
    docs = [s for s in answer["sources"] if s["kind"] == "wiki"]
    assert docs and any(s["path"].startswith("dreams/") for s in docs)

    # a second run changes nothing on disk, so nothing is re-embedded and the index is not touched
    again = json.loads(runner.invoke(cli.app, ["week", "2026-W37", "--json"]).stdout.strip().split("\n")[-1])
    assert again["digested"] == 0 and again["written"] is False and again["indexed"] is None


def test_the_lance_py_seeded_store_dreams_too(tmp_path: Path):
    """lance-py's ``seeded`` fixture has no session_weeks rows: a week over it chooses nothing and writes nothing."""
    r, e = seeded(tmp_path / "store")
    a = asker(r, e, ScriptedChat())
    out = cli.run_week(r, a, "2026-W37", tmp_path / "dreams", max_sessions=5, force=False, log=lambda _l: None)
    assert out["sessions_in_week"] == 0 and out["chosen"] == 0 and out["written"] is False
    assert not (tmp_path / "dreams" / "2026-W37.md").exists()
