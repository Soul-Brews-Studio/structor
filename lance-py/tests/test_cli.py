"""The CLI over a throwaway Lance store: reads are direct, writes prefer the admin API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_admin import fake_embedding  # the same 8-dim stand-in for the Ollama pool
from typer.testing import CliRunner

from structor_lance import cli, server, vectors
from structor_lance import targets as targets_mod
from structor_lance.schema import Event, Session, from_record
from structor_lance.sync import Replica
from structor_lance.targets import Target

PASSWORD = "unit-store-secret"  # must never reach stdout
CLOSED = "http://127.0.0.1:1"  # nothing listens there: PocketBase unreachable, admin API absent

runner = CliRunner()

EVENTS = [
    {"id": "e1", "created": "2026-09-09 10:00:00.000Z", "session": "s1", "role": "user", "ts": "2026-09-09 10:00:00.000Z",
     "iso_week": "2026-W37", "text": "the beam holds the roof up"},
    {"id": "e2", "created": "2026-09-09 10:00:01.000Z", "session": "s1", "role": "assistant", "ts": "2026-09-09 10:00:01.000Z",
     "iso_week": "2026-W37", "text": "a house frame is joists and nothing else"},
    {"id": "e3", "created": "2026-09-09 10:00:02.000Z", "session": "s1", "role": "user", "ts": "2026-09-09 10:00:02.000Z",
     "iso_week": "2026-W37", "text": "roofing felt, no beam here"},
]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A 'unit' target pointing at a closed port, with three events and one session on disk."""
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "unit.json").write_text(json.dumps({"url": CLOSED, "admin_email": "e@unit", "admin_password": PASSWORD}))
    monkeypatch.setattr(targets_mod, "CONF_DIR", conf)

    data = tmp_path / "data"
    r = Replica(Target("unit", CLOSED, "e@unit", PASSWORD), data)
    r.table(Event).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
        [from_record(Event, e) for e in EVENTS]
    )
    r.table(Session).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
        [from_record(Session, {"id": "s1", "session_id": "abc", "updated": "u1", "event_count": 3})]
    )
    monkeypatch.setenv("STRUCTOR_LANCE_PY_DATA", str(data))
    monkeypatch.setenv("STRUCTOR_LANCE_PY_HTTP", "127.0.0.1:1")
    return data


def run(*args: str):
    return runner.invoke(cli.app, list(args))


def row(output: str, table: str) -> list[str]:
    """The columns of the printed line for one table."""
    return next(line for line in output.splitlines() if line.startswith(table)).split()


def test_targets_prints_name_and_url_but_never_the_password(store: Path):
    res = run("targets")
    assert res.exit_code == 0, res.output
    assert "unit" in res.output and CLOSED in res.output
    assert "local" in res.output  # the built-in dev target is always there
    assert PASSWORD not in res.output and "password" not in res.output.lower()


def test_tables_and_schema_describe_the_store(store: Path):
    res = run("tables", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert "events" in res.output and "text" in res.output
    assert row(res.output, "events")[1] == "3"

    res = run("schema", "events", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert "iso_week" in res.output and "sidechain" in res.output and "bool" in res.output

    assert run("schema", "nope", "-t", "unit").exit_code == 64
    assert run("tables", "-t", "ghost").exit_code == 64


def test_status_names_the_target_and_its_directory(store: Path):
    res = run("status", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert str(store / "unit") in res.output and "last run never" in res.output
    assert PASSWORD not in res.output


def test_rows_filters_with_where_and_json_parses(store: Path):
    res = run("rows", "events", "--where", "role = 'user'", "--select", "id,role", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert "e1" in res.output and "e3" in res.output and "e2" not in res.output

    res = run("rows", "events", "--where", "id = 'e2'", "--select", "id,role,text", "-t", "unit", "--json")
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert rows == [{"id": "e2", "role": "assistant", "text": "a house frame is joists and nothing else"}]

    assert json.loads(run("rows", "events", "--limit", "1", "--offset", "5", "-t", "unit", "--json").output) == []


def test_search_after_the_index_is_built(store: Path):
    built = run("fts", "--table", "events", "-t", "unit")
    assert built.exit_code == 0, built.output
    assert json.loads(built.output) == {"ok": True, "table": "events", "column": "text"}
    assert json.loads((store / "unit" / "sync.json").read_text())["tables"]["events"]["ftsBuilt"] is True

    res = run("search", "beam", "--limit", "5", "-t", "unit", "--json")
    assert res.exit_code == 0, res.output
    hits = json.loads(res.output)
    assert {h["id"] for h in hits} == {"e1", "e3"}
    assert float(hits[0]["_score"]) > 0

    res = run("search", "beam", "--where", "role = 'assistant'", "-t", "unit")
    assert res.exit_code == 0 and "(no rows)" in res.output

    assert run("search", "beam", "--table", "sessions", "-t", "unit").exit_code == 64


def test_lag_reports_an_unreachable_store_as_minus_one(store: Path):
    res = run("lag", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert row(res.output, "events")[1:] == ["-1", "3", "?"]  # remote unknown, 3 rows here, lag unknowable


def test_a_limit_that_is_not_a_count_is_an_error(store: Path):
    assert run("rows", "events", "--limit", "-1", "-t", "unit").exit_code != 0
    assert run("rows", "events", "--offset", "-1", "-t", "unit").exit_code != 0
    assert run("rows", "events", "--limit", "abc", "-t", "unit").exit_code != 0
    assert run("search", "beam", "--limit", "-3", "-t", "unit").exit_code != 0


def test_a_limit_of_zero_is_refused_because_it_asks_for_nothing(store: Path):
    """--limit 0 read as "no limit" and quietly printed an empty page instead."""
    for args in (("rows", "events", "--limit", "0"), ("search", "beam", "--limit", "0")):
        res = run(*args, "-t", "unit")
        assert res.exit_code != 0, res.output
        assert "1 or more" in res.output
    assert run("rows", "events", "--offset", "0", "--limit", "1", "-t", "unit").exit_code == 0  # offset 0 is fine
    assert run("vsearch", "beam", "--limit", "0", "-t", "unit").exit_code != 0


def test_sync_prefers_the_admin_api_when_one_answers(store: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, str]] = []

    def fake_request(method: str, url: str, **kw):
        seen.append((method, url))
        return httpx.Response(200, json={"pulled": 7, "state": {}})

    monkeypatch.setattr(httpx, "request", fake_request)
    res = run("sync", "-t", "unit")
    assert res.exit_code == 0, res.output
    assert seen == [("POST", "http://127.0.0.1:1/api/unit/sync")]
    assert "pulled 7 rows (via admin)" in res.output


def test_sync_falls_back_to_a_direct_pull_when_the_admin_is_down(store: Path):
    res = run("sync", "-t", "unit")  # port 1 refuses; the PocketBase behind it is unreachable too
    assert res.exit_code == 0, res.output
    assert "pulled 0 rows" in res.output and "ERROR" in res.output
    assert PASSWORD not in res.output


# ---- serve: the flag, and what launchd puts in the environment ------------


def test_a_bare_port_is_a_port_and_a_junk_port_is_a_usage_error(monkeypatch: pytest.MonkeyPatch):
    """--http 8102 was parsed as a HOST called "8102" and the process bound the default port."""
    assert server.split_http("8101") == ("127.0.0.1", 8101)
    assert server.split_http(":8101") == ("127.0.0.1", 8101)
    assert server.split_http("127.0.0.1:8101") == ("127.0.0.1", 8101)
    assert server.split_http("localhost:8101") == ("localhost", 8101)
    assert server.split_http("[::1]:8101") == ("[::1]", 8101)
    assert server.split_http("") == ("127.0.0.1", server.DEFAULT_PORT)
    assert server.split_http("localhost") == ("localhost", server.DEFAULT_PORT)
    assert server.DEFAULT_HTTP == "127.0.0.1:8094"  # 8093 belongs to an unrelated lab on this Mac

    for junk in ("127.0.0.1:abc", ":0", "127.0.0.1:99999", ":-1"):
        with pytest.raises(SystemExit) as e:
            server.split_http(junk)
        assert e.value.code == 64  # EX_USAGE

    # and the CLI parses its --http with that same function, not its own guess
    monkeypatch.delenv("STRUCTOR_LANCE_PY_HTTP", raising=False)
    assert cli.bind("8102") == "127.0.0.1:8102"
    assert cli.bind("") == server.DEFAULT_HTTP


def test_serve_takes_its_targets_and_interval_from_the_environment(store: Path, monkeypatch: pytest.MonkeyPatch):
    """scripts/agent.sh sets STRUCTOR_LANCE_TARGETS; the launchd copy has no flags to read."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(server, "serve", lambda **kw: seen.update(kw))
    monkeypatch.setenv("STRUCTOR_LANCE_TARGETS", "unit, local")
    monkeypatch.setenv("STRUCTOR_LANCE_INTERVAL", "45")

    assert run("serve").exit_code == 0
    assert seen["targets"] == ["unit", "local"] and seen["interval"] == 45.0
    assert seen["http"] == "127.0.0.1:1"  # from STRUCTOR_LANCE_PY_HTTP, through split_http

    seen.clear()
    assert run("serve", "--targets", "unit", "--interval", "7").exit_code == 0
    assert seen["targets"] == ["unit"] and seen["interval"] == 7.0  # a flag still wins

    seen.clear()
    monkeypatch.setenv("STRUCTOR_LANCE_INTERVAL", "not-a-number")
    assert run("once").exit_code == 0
    assert seen["targets"] == ["unit", "local"] and seen["once"] is True


# ---- vectors ---------------------------------------------------------------


def test_embed_vsearch_and_vectors_walk_the_event_vector_table(store: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRUCTOR_OLLAMA_URLS", "http://fake:11434")
    monkeypatch.setattr(vectors, "pool", lambda *a, **k: fake_embedding())

    counted = run("vectors", "-t", "unit", "--json")
    assert counted.exit_code == 0, counted.output
    assert json.loads(counted.output) == {"target": "unit", "table": "event_vectors", "embedded": 0, "pending": 3}

    first = run("embed", "--limit", "2", "-t", "unit")
    assert first.exit_code == 0, first.output
    assert "embedded 2/2" in first.output and "2 rows embedded" in first.output
    assert json.loads(run("vectors", "-t", "unit", "--json").output)["embedded"] == 2

    rest = run("embed", "-t", "unit")  # resumable: only what is left
    assert rest.exit_code == 0 and "1 rows embedded" in rest.output
    assert json.loads(run("vectors", "-t", "unit", "--json").output) == {
        "target": "unit", "table": "event_vectors", "embedded": 3, "pending": 0}

    hits = run("vsearch", "the beam holds the roof", "--limit", "2", "-t", "unit", "--json")
    assert hits.exit_code == 0, hits.output
    rows = json.loads(hits.output)
    assert len(rows) == 2 and set(rows[0]) == {"_distance", "ts", "role", "text"}
    assert rows[0]["text"] == "the beam holds the roof up" and float(rows[0]["_distance"]) >= 0

    scoped = run("vsearch", "beam", "--where", "role = 'assistant'", "-t", "unit", "--json")
    assert [r["role"] for r in json.loads(scoped.output)] == ["assistant"]
    hybrid = run("vsearch", "beam", "--mode", "hybrid", "--limit", "2", "-t", "unit", "--json")
    assert hybrid.exit_code == 0 and "_relevance_score" in json.loads(hybrid.output)[0]
    assert run("vsearch", "beam", "--mode", "sideways", "-t", "unit").exit_code == 64


def test_the_vector_commands_say_so_when_no_pool_is_configured(store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRUCTOR_OLLAMA_URLS", "")
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(tmp_path / "no-conf"))
    for args in (("embed",), ("vsearch", "beam")):
        res = run(*args, "-t", "unit")
        assert res.exit_code == 78, res.output  # EX_CONFIG: nothing to retry until it is configured
        assert "STRUCTOR_OLLAMA_URLS" in res.output and "ollama_urls" in res.output
    # counting needs no host at all
    assert run("vectors", "-t", "unit", "--json").exit_code == 0
