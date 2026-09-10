"""The three commands over a seeded store, and the configuration rules they exit on."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from dream_fixtures import ScriptedChat, asker, seed_store, upsert
from structor_lance import wiki
from structor_lance.schema import SessionWeek
from typer.testing import CliRunner

from structor_dream import cli, config
from structor_dream.state import LOCK_FILE, RunLock

runner = CliRunner()


def last_json(result) -> dict:
    """The one JSON object a --json / nightly run prints on stdout, whatever progress went to stderr."""
    return json.loads(result.stdout.strip().split("\n")[-1])


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A seeded, embedded store behind the CLI's plumbing, a scripted chat, and a tmp dream directory."""
    r, e = seed_store(tmp_path / "store", embed=True)
    chat = ScriptedChat()
    monkeypatch.setattr(cli, "open_replica", lambda target: r)
    monkeypatch.setattr(cli, "open_embedder", lambda target: e)
    monkeypatch.setattr(cli, "asker_for", lambda replica, embedder, model: asker(r, e, chat))
    monkeypatch.setenv("STRUCTOR_DREAM_DIR", str(tmp_path / "dreams"))
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(tmp_path / "conf"))
    monkeypatch.delenv("STRUCTOR_WIKI_DIR", raising=False)
    return {"replica": r, "embedder": e, "chat": chat, "root": tmp_path / "dreams", "tmp": tmp_path}


def test_week_json_shape_and_second_run(wired: dict):
    out = runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "3", "--json"])
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout.strip().split("\n")[-1])
    assert result["week"] == "2026-W37" and result["sessions_in_week"] == 6 and result["conversational"] == 4
    assert result["chosen"] == 3 and result["digested"] == 3 and result["skipped"] == 0 and result["failed"] == []
    assert result["reduced"] == 3 and result["written"] is True and result["page"].endswith("/dreams/2026-W37.md")
    assert result["indexed"] is None                                            # no wiki_dir: not indexed, said so
    assert "outside the wiki directory" in out.output or "outside the wiki directory" in (out.stderr or "")
    assert (wired["root"] / "2026-W37.md").is_file()

    again = json.loads(runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "3", "--json", "--no-index"]).stdout.strip().split("\n")[-1])
    assert again["digested"] == 0 and again["skipped"] == 3
    plain = runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "3", "--no-index"])
    assert plain.exit_code == 0 and "digested 0, skipped 3" in plain.output


def test_week_defaults_to_the_current_week_and_validates_arguments(wired: dict, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 10, 6, 0, tzinfo=UTC))
    out = runner.invoke(cli.app, ["week", "--json", "--no-index"])
    assert out.exit_code == 0 and json.loads(out.stdout.strip().split("\n")[-1])["week"] == "2026-W37"
    assert runner.invoke(cli.app, ["week", "2026-13"]).exit_code == 64
    assert runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "0"]).exit_code == 64
    empty = runner.invoke(cli.app, ["week", "2025-W01", "--json", "--no-index"])
    assert empty.exit_code == 0 and json.loads(empty.stdout.strip().split("\n")[-1])["chosen"] == 0


def test_topic_json_shape_and_out_override(wired: dict):
    out = runner.invoke(cli.app, ["topic", "409 offset mismatch", "--k", "8", "--json"])
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout.strip().split("\n")[-1])
    assert result["query"] == "409 offset mismatch" and result["slug"] == "409-offset-mismatch"
    assert result["hits"] >= result["material"] >= 1 and result["read"] == result["material"]
    assert result["cited_events"] >= 1 and result["written"] is True and result["page"].endswith("/dreams/topic-409-offset-mismatch.md")
    assert result["indexed"] is None
    elsewhere = wired["tmp"] / "elsewhere" / "custom.md"
    custom = runner.invoke(cli.app, ["topic", "launchd tray", "--k", "4", "--out", str(elsewhere), "--json", "--no-index"])
    assert custom.exit_code == 0 and elsewhere.is_file()
    assert runner.invoke(cli.app, ["topic", "   "]).exit_code == 64 and runner.invoke(cli.app, ["topic", "x", "--k", "3"]).exit_code == 64


def test_nightly_dreams_the_current_week_and_the_weeks_whose_ledger_moved(wired: dict, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 10, 6, 0, tzinfo=UTC))
    first = runner.invoke(cli.app, ["nightly"])
    assert first.exit_code == 0, first.output
    summary = json.loads(first.stdout.strip().split("\n")[-1])
    assert summary["weeks"] == ["2026-W36", "2026-W37"]                          # no page yet: both weeks with rows
    assert summary["digested"] == 5 and summary["skipped"] == 0 and summary["failed"] == [] and summary["errors"] == []
    assert sorted(Path(p).name for p in summary["pages"]) == ["2026-W36.md", "2026-W37.md"]
    assert set(summary) >= {"weeks", "digested", "skipped", "pages", "elapsed_s"}

    # both pages are newer than every ledger row, so only the current week is due; its digests are all cached
    second = json.loads(runner.invoke(cli.app, ["nightly"]).stdout.strip().split("\n")[-1])
    assert second["weeks"] == ["2026-W37"] and second["digested"] == 0 and second["skipped"] == 4

    # W36's ledger moves after its page: it is due again, and one session is re-digested
    upsert(wired["replica"], SessionWeek, [{"id": "s3-2026-W36", "session": "s3", "project": "p2", "iso_week": "2026-W36",
                                            "event_count": 15, "user_count": 3, "assistant_count": 4,
                                            "first_ts": "2026-09-02 10:00:00.000Z", "last_ts": "2026-09-02 12:00:00.000Z",
                                            "created": "c", "updated": "2099-01-01 00:00:00.000Z"}])
    third = json.loads(runner.invoke(cli.app, ["nightly"]).stdout.strip().split("\n")[-1])
    assert third["weeks"] == ["2026-W36", "2026-W37"] and third["digested"] == 1 and third["skipped"] == 4


def test_dream_dir_precedence_and_missing_wiki_dir_exits_78(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conf = tmp_path / "conf"
    conf.mkdir()
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(conf))
    monkeypatch.delenv("STRUCTOR_DREAM_DIR", raising=False)
    monkeypatch.delenv("STRUCTOR_WIKI_DIR", raising=False)
    assert config.dream_dir() == ""                                             # nothing configured anywhere
    (conf / "lance.json").write_text(json.dumps({"wiki_dir": str(tmp_path / "wiki")}))
    assert config.dream_dir() == str(tmp_path / "wiki" / "dreams")             # <wiki_dir>/dreams by default
    (conf / "lance.json").write_text(json.dumps({"wiki_dir": str(tmp_path / "wiki"), "dream_dir": str(tmp_path / "d")}))
    assert config.dream_dir() == str(tmp_path / "d")                            # lance.json dream_dir beats the default
    monkeypatch.setenv("STRUCTOR_DREAM_DIR", str(tmp_path / "env"))
    assert config.dream_dir() == str(tmp_path / "env")                          # the environment wins
    monkeypatch.setenv("STRUCTOR_WIKI_DIR", str(tmp_path / "envwiki"))
    monkeypatch.delenv("STRUCTOR_DREAM_DIR")
    (conf / "lance.json").write_text("{}")
    assert config.dream_dir() == str(tmp_path / "envwiki" / "dreams")

    monkeypatch.delenv("STRUCTOR_WIKI_DIR")
    for args in (["week", "2026-W37"], ["topic", "x"], ["nightly"]):
        out = runner.invoke(cli.app, args)
        assert out.exit_code == 78, args
        assert "no dream directory" in out.output and "wiki_dir" in out.output


def test_a_second_run_on_the_same_replica_exits_75_instead_of_erasing_the_first(wired: dict):
    """launchd at 03:30 meets a hand run started at 03:10: the second must not load the cache the first is still
    writing. Every command takes the replica's flock first; nightly still prints its JSON line so the log says why."""
    r, root, chat = wired["replica"], wired["root"], wired["chat"]
    other = RunLock(config.cache_dir(r) / LOCK_FILE)                                # what the first run holds for its life
    assert other.acquire()
    try:
        week = runner.invoke(cli.app, ["week", "2026-W37", "--json", "--no-index"])
        assert week.exit_code == 75 and "another structor-dream run holds" in week.output
        assert f"pid {os.getpid()}" in week.output and "run this again" in week.output
        assert chat.calls == [] and not (root / "2026-W37.md").exists()             # nothing digested, nothing written
        topic = runner.invoke(cli.app, ["topic", "409 offset mismatch", "--k", "4", "--json"])
        assert topic.exit_code == 75 and chat.calls == []
        night = runner.invoke(cli.app, ["nightly"])
        assert night.exit_code == 75
        summary = last_json(night)
        assert summary["weeks"] == [] and summary["pages"] == [] and summary["digested"] == 0
        assert len(summary["errors"]) == 1 and summary["errors"][0].startswith("locked: another structor-dream run holds")
    finally:
        other.release()
    done = runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "2", "--json", "--no-index"])
    assert done.exit_code == 0 and (root / "2026-W37.md").is_file()                 # the lock went with the other run
    probe = RunLock(config.cache_dir(r) / LOCK_FILE)
    assert probe.acquire()                                                          # and the command released its own
    probe.release()


def test_week_and_nightly_still_report_when_the_index_pass_cannot_run(wired: dict, monkeypatch: pytest.MonkeyPatch):
    """A written page is the run's product: a pool that is not configured (exit 78 from the embedder) or a wiki
    table that will not index is reported as ``index_error`` beside it — never a lost summary, never a traceback."""
    wiki_root = wired["tmp"] / "wiki"
    monkeypatch.setenv("STRUCTOR_WIKI_DIR", str(wiki_root))
    monkeypatch.setenv("STRUCTOR_DREAM_DIR", str(wiki_root / "dreams"))              # under the wiki: an index pass is wanted
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 10, 6, 0, tzinfo=UTC))

    def no_pool(target):
        typer.echo("no ollama pool configured", err=True)
        raise typer.Exit(78)                                                        # what lance-py's cli.embedder does

    monkeypatch.setattr(cli, "open_embedder", no_pool)
    out = runner.invoke(cli.app, ["week", "2026-W37", "--max-sessions", "2", "--json"])
    assert out.exit_code == 0, out.output
    result = last_json(out)
    assert result["written"] is True and result["indexed"] is None
    assert result["index_error"].startswith("not indexed: the embedding pool is not configured (exit 78)")
    assert result["index_error"].endswith("the page is written") and (wiki_root / "dreams" / "2026-W37.md").is_file()

    # W36 has no page yet, and once it does W37's reduce sees its Insights (a contradictions bullet appears), so both
    # pages are written; the index pass fails the same way and the summary still comes out, with the error beside it
    night = runner.invoke(cli.app, ["nightly", "--max-sessions", "2"])
    assert night.exit_code == 0, night.output
    summary = last_json(night)
    assert sorted(Path(p).name for p in summary["pages"]) == ["2026-W36.md", "2026-W37.md"] and summary["indexed"] is None
    assert summary["errors"] == [f"index: {result['index_error']}"]

    # the pool is back but the wiki table raises mid-index: the same shape, with the exception named
    monkeypatch.setattr(cli, "open_embedder", lambda target: wired["embedder"])

    def broken(*_args, **_kwargs):
        raise NotADirectoryError("wiki gone")

    monkeypatch.setattr(cli.wiki_table, "index_dir", broken)
    out = runner.invoke(cli.app, ["topic", "409 offset mismatch", "--k", "4", "--json"])
    assert out.exit_code == 0, out.output
    result = last_json(out)
    assert result["written"] is True and result["indexed"] is None
    assert result["index_error"] == "not indexed: NotADirectoryError: wiki gone; the page is written"
    assert cli.index_after(wired["replica"], "local", wiki_root / "dreams", lambda _l: None, wanted=False) == (None, "")


def test_a_week_that_yields_no_digest_gets_an_empty_page_and_is_not_dreamed_again(wired: dict, monkeypatch: pytest.MonkeyPatch):
    """Rows in the ledger but nothing to dream: the night after must not scan (or pay for) the same week again —
    unless its ledger moved, or the reason was one that may pass (the chat host down)."""
    r, root, chat = wired["replica"], wired["root"], wired["chat"]
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 10, 6, 0, tzinfo=UTC))
    row = {"project": "p2", "assistant_count": 20, "created": "c",
           "first_ts": "2026-08-26 10:00:00.000Z", "last_ts": "2026-08-26 12:00:00.000Z"}

    # W35: one session with one human turn — in the ledger, not conversational; no model call, an empty page
    upsert(r, SessionWeek, [{**row, "id": "s5-2026-W35", "session": "s5", "iso_week": "2026-W35", "event_count": 40,
                             "user_count": 1, "updated": "2026-08-26 12:00:05.000Z"}])
    assert "2026-W35" in cli.stale_weeks(r, root, config.now())
    out = runner.invoke(cli.app, ["week", "2026-W35", "--json", "--no-index"])
    assert out.exit_code == 0, out.output
    result = last_json(out)
    assert result["chosen"] == 0 and result["written"] is True and chat.calls == []
    assert result["reason"].startswith("no session in the week was conversational")
    meta, body = wiki.frontmatter((root / "2026-W35.md").read_text())
    assert meta["kind"] == "dream" and meta["mode"] == "week" and meta["status"] == "empty" and meta["sources"] == []
    assert meta["ledger_at"] == "2026-08-26 12:00:05.000Z" and meta["sessions_digested"] == "0"
    assert "Nothing was dreamed for 2026-W35" in body and "an AI tool, not a person" in body
    assert "2026-W35" not in cli.stale_weeks(r, root, config.now())                 # made from this ledger: not due
    assert last_json(runner.invoke(cli.app, ["week", "2026-W35", "--json", "--no-index"]))["written"] is False

    # the ledger moves: due once more, and the empty page is remade from the new stamp
    upsert(r, SessionWeek, [{**row, "id": "s5-2026-W35", "session": "s5", "iso_week": "2026-W35", "event_count": 41,
                             "user_count": 1, "updated": "2026-08-27 09:00:00.000Z"}])
    assert "2026-W35" in cli.stale_weeks(r, root, config.now())
    moved = last_json(runner.invoke(cli.app, ["week", "2026-W35", "--json", "--no-index"]))
    assert moved["written"] is True and chat.calls == []
    assert wiki.frontmatter((root / "2026-W35.md").read_text())[0]["ledger_at"] == "2026-08-27 09:00:00.000Z"
    assert "2026-W35" not in cli.stale_weeks(r, root, config.now())

    # W34: a conversational row whose session has no turn of MIN_TEXT characters in that week — the other reason
    upsert(r, SessionWeek, [{**row, "id": "s1-2026-W34", "session": "s1", "project": "p1", "iso_week": "2026-W34",
                             "event_count": 10, "user_count": 2, "updated": "2026-08-19 12:00:05.000Z"}])
    result = last_json(runner.invoke(cli.app, ["week", "2026-W34", "--json", "--no-index"]))
    assert result["empty"] == ["s1"] and result["written"] is True and chat.calls == []
    assert result["reason"].startswith("the 1 chosen session(s) had no user or assistant turn")
    assert wiki.frontmatter((root / "2026-W34.md").read_text())[0]["status"] == "empty"
    assert "2026-W34" not in cli.stale_weeks(r, root, config.now())
    assert "given up on after" in cli.nothing_reason([{}], {"empty": [], "gave_up": ["s3"]})
    assert "nothing to read (1)" in cli.nothing_reason([{}], {"empty": ["s1"], "gave_up": ["s3"]})

    # a week whose session failed for a reason that may pass (the host down) gets no page and stays due
    def down(messages):
        raise RuntimeError("gpu box fell over")

    monkeypatch.setattr(cli, "asker_for", lambda replica, embedder, model: asker(r, wired["embedder"], down))
    result = last_json(runner.invoke(cli.app, ["week", "2026-W36", "--json", "--no-index"]))
    assert result["failed"] == ["s3"] and result["written"] is False and "reason" not in result
    assert not (root / "2026-W36.md").exists() and "2026-W36" in cli.stale_weeks(r, root, config.now())


def test_a_moved_ledger_with_an_unchanged_dream_is_restamped_not_repaid(wired: dict, monkeypatch: pytest.MonkeyPatch):
    """A re-sync bumps ``updated`` on a week whose digests did not change: the reduce comes back identical, the
    page still moves (``ledger_at``), and the next night neither lists the week nor pays its reduce again."""
    r, root, chat = wired["replica"], wired["root"], wired["chat"]
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 10, 6, 0, tzinfo=UTC))
    assert last_json(runner.invoke(cli.app, ["nightly"]))["weeks"] == ["2026-W36", "2026-W37"]
    page = root / "2026-W36.md"
    before = page.read_text()
    assert wiki.frontmatter(before)[0]["ledger_at"] == "2026-09-02 12:00:05.000Z"

    upsert(r, SessionWeek, [{"id": "s3-2026-W36", "session": "s3", "project": "p2", "iso_week": "2026-W36", "event_count": 14,
                             "user_count": 3, "assistant_count": 3, "first_ts": "2026-09-02 10:00:00.000Z",
                             "last_ts": "2026-09-02 12:00:00.000Z", "created": "c", "updated": "2099-01-01 00:00:00.000Z"}])
    calls = len(chat.calls)
    second = last_json(runner.invoke(cli.app, ["nightly"]))
    assert second["weeks"] == ["2026-W36", "2026-W37"] and second["digested"] == 0    # same event_count: cached
    assert len(chat.calls) == calls + 2                                               # one reduce per week, no digest
    assert second["pages"] == [str(page)]                                             # rewritten for its stamp alone
    after = page.read_text()
    assert wiki.frontmatter(after)[0]["ledger_at"] == "2099-01-01 00:00:00.000Z"
    assert after.replace("2099-01-01 00:00:00.000Z", "2026-09-02 12:00:05.000Z") == before   # nothing else moved

    calls = len(chat.calls)
    third = last_json(runner.invoke(cli.app, ["nightly"]))
    assert third["weeks"] == ["2026-W37"] and third["pages"] == [] and len(chat.calls) == calls + 1   # W36 not repaid

    # a page from before ledger_at existed is judged by generated_at: older than the ledger is due, newer is not
    text = after.replace('ledger_at: "2099-01-01 00:00:00.000Z"\n', "")
    assert "ledger_at" not in text
    page.write_text(text)
    assert "2026-W36" in cli.stale_weeks(r, root, config.now())
    page.write_text(text.replace(wiki.frontmatter(text)[0]["generated_at"], "2100-01-01T00:00:00+07:00"))
    assert "2026-W36" not in cli.stale_weeks(r, root, config.now())


def test_week_labels_and_helpers():
    assert config.current_week(datetime(2026, 9, 10, 6, 0, tzinfo=UTC)) == "2026-W37"
    assert config.current_week(datetime(2026, 9, 13, 17, 30, tzinfo=UTC)) == "2026-W38"   # Monday 00:30 in Bangkok
    assert config.previous_week("2026-W01") == "2025-W52" and config.previous_week("2026-W37") == "2026-W36"
    assert config.parse_week(" 2026-W37 ") == (2026, 37)
    for bad in ("2026-W54", "2026-W00", "26-W37", "2026-w37"):
        with pytest.raises(ValueError):
            config.parse_week(bad)
    assert config.under(Path("/a/b/c"), Path("/a/b")) and not config.under(Path("/a/bc"), Path("/a/b"))
