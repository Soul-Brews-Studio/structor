"""The admin API and its guards, mirroring the Bun edition's admin.test.ts and guards.test.ts.

No network: the replica is a temp directory seeded straight through
``Replica.table(Event)`` + merge_insert, and the PocketBase side is never called
(the target url points at a closed port).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lancedb.embeddings import EmbeddingFunction, get_registry, register

from structor_lance.admin import create_app, safe_select, safe_where
from structor_lance.schema import Event, from_record
from structor_lance.sync import Replica
from structor_lance.targets import Target
from structor_lance.vectors import Embedder

UNIT = Target("unit", "http://127.0.0.1:1", "e", "p")
BASE = "http://127.0.0.1:8094"  # the Host header has to be loopback or every request is a 403

EVENTS = [
    {"id": "e1", "created": "c1", "session": "s", "uuid": "u1", "ts": "2026-09-09 15:00:00.000Z",
     "iso_week": "2026-W37", "role": "user", "text": "<b>bold</b> hello", "line_no": 1},
    {"id": "e2", "created": "c2", "session": "s", "uuid": "u2", "ts": "2026-09-09 15:00:01.000Z",
     "iso_week": "2026-W37", "role": "assistant", "text": "hello back", "line_no": 2},
]


@register("fake-bag-admin")
class FakeBag(EmbeddingFunction):
    """Bag-of-characters vector in 8 dims: deterministic, no network, no GPU, similar strings land close."""

    dims: int = 8

    def ndims(self) -> int:
        return self.dims

    def compute_source_embeddings(self, texts, *a, **k):
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for ch in str(t).lower():
                v[ord(ch) % self.dims] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    def compute_query_embeddings(self, query, *a, **k):
        return self.compute_source_embeddings([query])


def fake_embedding() -> Any:
    # LanceDB re-hydrates a function from the registry, so it has to come from create()
    return get_registry().get("fake-bag-admin").create(dims=8)


def fake_embedder(r: Replica) -> Embedder:
    return Embedder(r, fake_embedding())


def build(tmp_path: Path, *, read_only: bool = False, seed: bool = True,
          embedder: Any = fake_embedder) -> tuple[TestClient, Replica]:
    root = tmp_path / "data"
    ui = tmp_path / "ui"
    console = tmp_path / "console"
    ui.mkdir(parents=True, exist_ok=True)
    console.mkdir(parents=True, exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>admin</title>")
    (ui / "admin.css").write_text(":root{}")
    (console / "index.html").write_text("<!doctype html><title>console</title>")
    r = Replica(UNIT, root)
    t = r.table(Event)
    if seed:
        t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
            [from_record(Event, e) for e in EVENTS]
        )
    app = create_app({"unit": r}, version="test", data_root=root, read_only=read_only, ui_dir=ui,
                     console_dir=console, embedder=embedder)
    return TestClient(app, base_url=BASE), r


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    c, _ = build(tmp_path)
    return c


# ---- guards, pure ----------------------------------------------------------


def test_safe_where_blanks_string_literals_before_looking_for_comments_and_semicolons():
    assert safe_where("text LIKE '%npm i --save%'") == ("text LIKE '%npm i --save%'", None)
    assert safe_where("text = 'it''s; fine'") == ("text = 'it''s; fine'", None)
    assert safe_where("role = 'x'; drop")[1]
    assert safe_where("role = 'x' -- c")[1]
    assert safe_where("role = 'x' /* c */")[1]
    assert safe_where("")[0] == "" and safe_where("  ")[1] is None
    assert safe_where("x = " + "'a'" * 900)[1] == "where: too long"


def test_safe_where_allows_table_filter_functions_and_refuses_the_rest_of_datafusion():
    assert safe_where("lower(role) = 'user' AND length(text) > 10")[0] == "lower(role) = 'user' AND length(text) > 10"
    assert safe_where("role IN ('user', 'assistant')")[0] == "role IN ('user', 'assistant')"
    assert "repeat()" in safe_where("length(repeat(text, 20000)) > 99999999999")[1]
    assert safe_where("random() > 0.5")[1]


def test_the_call_check_uses_an_ascii_word_boundary_like_javascript():
    # Python's \b is Unicode-aware: with it, the é glues to "repeat" and the call
    # sails past the allow-list that the Bun edition (ASCII \b) refuses it by
    assert "repeat()" in safe_where("length(érepeat(text, 20000)) > 1")[1]
    assert "repeat()" in safe_where("length(日repeat(text, 2)) > 1")[1]
    assert safe_where("lower(role) = 'user'")[0]  # a legal call is still legal


def test_safe_select_accepts_column_names_only():
    assert safe_select("id, ts") == ["id", "ts"]
    assert safe_select("") == []
    assert safe_select("id, length(text)") is None
    assert safe_select("id; drop") is None


# ---- the API ---------------------------------------------------------------


def test_status_lists_the_target_and_its_tables(client: TestClient):
    r = client.get("/api/status")
    assert r.status_code == 200
    j = r.json()
    assert j["targets"][0]["name"] == "unit"
    assert j["targets"][0]["tables"]["events"]["rows"] == 2
    assert j["targets"][0]["tables"]["events"]["fts"] == "text"
    assert j["version"] == "test" and j["dataRoot"] and j["time"].endswith("Z")
    assert "password" not in r.text and "p" != j["targets"][0].get("password", "")


def test_tables_lists_every_replicated_table(client: TestClient):
    j = client.get("/api/unit/tables").json()
    assert [t["name"] for t in j] == ["projects", "sessions", "events", "session_weeks", "import_runs"]
    assert next(t for t in j if t["name"] == "events")["rows"] == 2
    assert next(t for t in j if t["name"] == "sessions")["stamp"] == "updated"


def test_rows_honours_where_limit_and_offset_and_reports_the_filtered_total(client: TestClient):
    j = client.get("/api/unit/tables/events/rows", params={"where": "role = 'user'", "limit": 10}).json()
    assert j["total"] == 1
    assert j["rows"][0]["id"] == "e1"
    assert j["limit"] == 10 and j["offset"] == 0
    page2 = client.get("/api/unit/tables/events/rows", params={"limit": 1, "offset": 1}).json()
    assert len(page2["rows"]) == 1 and page2["total"] == 2
    only = client.get("/api/unit/tables/events/rows", params={"select": "id,role"}).json()
    assert set(only["rows"][0]) == {"id", "role"}


def test_limit_is_capped_and_a_bad_predicate_is_a_500_with_an_error_body_not_a_crash(client: TestClient):
    j = client.get("/api/unit/tables/events/rows", params={"limit": 100000}).json()
    assert j["limit"] == 500
    assert client.get("/api/unit/tables/events/rows", params={"limit": "abc"}).json()["limit"] == 50
    r = client.get("/api/unit/tables/events/rows", params={"where": "nosuchcol = 1"})
    assert r.status_code == 500
    assert r.json()["error"] and r.headers["content-type"].startswith("application/json")
    assert client.get("/api/unit/tables/events/rows", params={"where": "role = 'x'; drop"}).status_code == 400
    forbidden = client.get("/api/unit/tables/events/rows", params={"where": "random() > 0.5"})
    assert forbidden.status_code == 400 and "random()" in forbidden.json()["error"]


def test_an_infinite_limit_clamps_and_an_infinite_offset_is_no_offset(client: TestClient):
    # Number("Infinity") is a real number to JavaScript and Math.min clamps it;
    # int(float("inf")) raises OverflowError, which used to be a 500
    j = client.get("/api/unit/tables/events/rows", params={"limit": "Infinity"})
    assert j.status_code == 200 and j.json()["limit"] == 500
    o = client.get("/api/unit/tables/events/rows", params={"offset": "Infinity"})
    assert o.status_code == 200 and o.json()["offset"] == 0 and len(o.json()["rows"]) == 2
    assert client.get("/api/unit/tables/events/rows", params={"limit": "1e400"}).json()["limit"] == 500
    assert client.get("/api/unit/tables/events/rows", params={"limit": "-Infinity"}).json()["limit"] == 1
    assert client.get("/api/unit/tables/events/rows", params={"limit": "NaN"}).json()["limit"] == 50
    s = client.get("/api/unit/tables/events/search", params={"q": "hello", "limit": "Infinity"})
    assert s.status_code in (200, 500) and (s.json().get("limit") in (500, None))


def test_select_accepts_column_names_only_over_http(client: TestClient):
    assert client.get("/api/unit/tables/events/rows", params={"select": "id,ts"}).status_code == 200
    r = client.get("/api/unit/tables/events/rows", params={"select": "id, length(text)"})
    assert r.status_code == 400 and r.json()["error"] == "select: column names only"


def test_unknown_target_table_and_traversal_are_404(client: TestClient):
    assert client.get("/api/nope/tables").status_code == 404
    assert client.get("/api/nope/tables").json()["error"] == "unknown target"
    assert client.get("/api/unit/tables/nope/rows").status_code == 404
    assert client.get("/api/unit/sync/nope").status_code == 404
    assert client.get("/%2e%2e/%2e%2e/etc/passwd").status_code == 404
    assert client.get("/console/nope/").status_code == 404


def test_search_needs_the_fts_index_and_then_returns_scored_rows(client: TestClient):
    before = client.get("/api/unit/tables/events/search", params={"q": "hello"})
    # without an index LanceDB either errors (500) or flat-scans; both are acceptable, but the next call must work
    assert before.status_code in (200, 500)
    built = client.post("/api/unit/tables/events/fts")
    assert built.status_code == 200 and built.json() == {"ok": True, "column": "text"}
    j = client.get("/api/unit/tables/events/search", params={"q": "hello", "limit": 5}).json()
    assert len(j["rows"]) == 2 and j["q"] == "hello"
    assert isinstance(j["rows"][0]["_score"], float)
    assert client.get("/api/unit/tables/events/search").status_code == 400
    assert client.get("/api/unit/tables/events/search", params={"q": "x" * 501}).status_code == 400
    assert client.get("/api/unit/tables/projects/search", params={"q": "x"}).status_code == 400


def test_schema_and_stats_describe_the_table(client: TestClient):
    s = client.get("/api/unit/tables/events/schema").json()
    assert "iso_week" in [f["name"] for f in s["fields"]]
    st = client.get("/api/unit/tables/events/stats").json()
    assert st["rows"] == 2 and st["version"] > 0 and st["versions"] >= 1
    assert "totalBytes" in st["stats"] and "fragmentStats" in st["stats"]  # the UI reads the Node spelling


def test_schema_reports_arrow_js_type_names_in_the_tables_own_order(client: TestClient):
    """The shared UI is written against the Node bindings, which print Arrow-JS names."""
    fields = client.get("/api/unit/tables/events/schema").json()["fields"]
    by_name = {f["name"]: f["type"] for f in fields}
    assert by_name["line_no"] == "Float64"  # pyarrow calls it "double"
    assert by_name["text"] == "Utf8"  # pyarrow calls it "string"
    assert by_name["sidechain"] == "Bool"  # pyarrow calls it "bool"
    names = [f["name"] for f in fields]
    assert names[:3] == ["id", "session", "uuid"] and names[-1] == "created"  # the table's order, not sorted


def test_optimize_answers_ok_and_reports_the_table_it_left_behind(client: TestClient):
    """lancedb-python's optimize() returns None, so the UI is given numbers measured after it."""
    r = client.post("/api/unit/tables/events/optimize")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    # the Node binding's shape, which the shared admin UI prints (result.compaction.*, result.prune.*)
    assert set(j["result"]) == {"compaction", "prune"}
    assert set(j["result"]["compaction"]) == {"fragmentsRemoved", "fragmentsAdded", "filesRemoved", "filesAdded"}
    assert set(j["result"]["prune"]) == {"bytesRemoved", "oldVersionsRemoved"}
    assert j["result"]["compaction"]["fragmentsAdded"] >= 1


def test_static_ui_and_console_are_served(client: TestClient):
    root = client.get("/")
    assert root.status_code == 200 and "<title>admin</title>" in root.text
    assert root.headers["cache-control"] == "no-cache" and "default-src 'self'" in root.headers["content-security-policy"]
    css = client.get("/admin.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "public, max-age=3600"
    assert client.get("/nope.js").status_code == 404
    to_console = client.get("/console", follow_redirects=False)
    assert to_console.status_code == 302 and to_console.headers["location"] == "/console/unit/"
    assert client.get("/console/unit", follow_redirects=False).headers["location"] == "/console/unit/"
    page = client.get("/console/unit/")
    assert page.status_code == 200 and "<title>console</title>" in page.text
    # api/… under a console mount reaches facade.handle, not the static tree
    api = client.get("/console/unit/api/structor/status")
    assert api.status_code in (401, 501) and "error" in api.json()
    assert api.headers["content-type"].startswith("application/json")


def test_status_serialises_a_snapshot_of_the_sync_state_not_the_live_dict(tmp_path: Path):
    """The follow thread mutates state while a request thread serialises it; the route takes a copy."""
    c, r = build(tmp_path)
    r.state_copy = lambda: {"snapshot": True}  # type: ignore[method-assign]
    assert c.get("/api/status").json()["targets"][0]["sync"] == {"snapshot": True}
    assert c.get("/api/unit/sync").json()["state"] == {"snapshot": True}


def test_every_method_but_post_on_sync_is_the_read_path(tmp_path: Path):
    """Bun answers one route: POST pulls, anything else reads. A PUT was a 404 here."""
    c, _ = build(tmp_path)
    for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS"):
        res = c.request(method, "/api/unit/sync")
        assert res.status_code == 200, method
        assert set(res.json()) == {"state", "lag"}, method
    assert c.request("PUT", "/api/nope/sync").status_code == 404


# ---- vectors ---------------------------------------------------------------


def test_vsearch_ranks_the_nearest_events_and_counts_them_in_the_table_list(tmp_path: Path):
    c, r = build(tmp_path)
    assert next(t for t in c.get("/api/unit/tables").json() if t["name"] == "events")["vectors"] == 0
    assert fake_embedder(r).run(log=lambda _s: None) == 2  # both seeded events

    j = c.get("/api/unit/tables/events/vsearch", params={"q": "hello back", "limit": 5}).json()
    assert j["q"] == "hello back" and j["limit"] == 5 and j["mode"] == "vector"
    assert set(j["rows"][0]) == {"event_id", "session", "ts", "role", "text", "_distance"}
    assert j["rows"][0]["event_id"] == "e2"
    assert "vector" not in j["rows"][0]  # 8 floats here, 1024 in production: never in the body

    scoped = c.get("/api/unit/tables/events/vsearch", params={"q": "hello", "where": "role = 'user'"}).json()
    assert [x["event_id"] for x in scoped["rows"]] == ["e1"]
    hybrid = c.get("/api/unit/tables/events/vsearch", params={"q": "hello", "mode": "hybrid"}).json()
    assert hybrid["mode"] == "hybrid" and "_relevance_score" in hybrid["rows"][0]

    listed = next(t for t in c.get("/api/unit/tables").json() if t["name"] == "events")
    assert listed["vectors"] == 2
    assert c.get("/api/status").json()["targets"][0]["tables"]["events"]["vectors"] == 2


def test_vsearch_validates_its_query_and_says_what_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    c, r = build(tmp_path)
    assert c.get("/api/unit/tables/events/vsearch", params={"q": "x"}).status_code == 400
    assert "event_vectors" in c.get("/api/unit/tables/events/vsearch", params={"q": "x"}).json()["error"]

    fake_embedder(r).run(log=lambda _s: None)
    assert c.get("/api/unit/tables/events/vsearch").status_code == 400  # q required
    assert c.get("/api/unit/tables/events/vsearch", params={"q": "x" * 501}).status_code == 400
    bad_where = c.get("/api/unit/tables/events/vsearch", params={"q": "x", "where": "random() > 0.5"})
    assert bad_where.status_code == 400 and "random()" in bad_where.json()["error"]
    assert c.get("/api/nope/tables/events/vsearch", params={"q": "x"}).status_code == 404

    # the same store through an app with no fake function and no pool configured
    monkeypatch.setenv("STRUCTOR_OLLAMA_URLS", "")
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(tmp_path / "no-conf"))
    plain, _ = build(tmp_path, embedder=None)
    res = plain.get("/api/unit/tables/events/vsearch", params={"q": "x"})
    assert res.status_code == 400 and "STRUCTOR_OLLAMA_URLS" in res.json()["error"]


# ---- host, origin and read-only --------------------------------------------


def test_a_non_loopback_host_or_a_foreign_origin_is_refused(client: TestClient):
    r = client.get("/api/status", headers={"Host": "evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "loopback only"
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": BASE}).status_code == 200
    assert client.get("/api/status", headers={"Origin": "null"}).status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_a_no_sync_instance_refuses_every_write(tmp_path: Path):
    c, replica = build(tmp_path, read_only=True)
    r = c.post("/api/unit/sync")
    assert r.status_code == 405 and "read-only" in r.json()["error"]
    assert c.post("/api/unit/tables/events/optimize").status_code == 405
    assert c.post("/api/unit/tables/events/fts").status_code == 405
    # a GET that would write: the first hybrid vsearch builds an index on event_vectors
    fake_embedder(replica).run(log=lambda _s: None)
    hybrid = c.get("/api/unit/tables/events/vsearch", params={"q": "hello", "mode": "hybrid"})
    assert hybrid.status_code == 405 and "read-only" in hybrid.json()["error"]
    assert c.get("/api/unit/tables/events/vsearch", params={"q": "hello"}).status_code == 200
    # reads still work, and the console's own POSTs are not the API's business
    assert c.get("/api/unit/tables").status_code == 200
    assert c.post("/console/unit/api/collections/_superusers/auth-with-password", json={}).status_code != 405


def test_optimize_result_has_the_node_shape_and_ipv6_host_parses():
    from structor_lance.admin import optimize_result
    from structor_lance.server import split_http

    r = optimize_result({"fragments": 9, "bytes": 1000, "versions": 12}, {"fragments": 1, "bytes": 700, "versions": 3})
    assert r == {"compaction": {"fragmentsRemoved": 9, "fragmentsAdded": 1, "filesRemoved": 9, "filesAdded": 1},
                 "prune": {"bytesRemoved": 300, "oldVersionsRemoved": 9}}
    assert split_http("::1") == ("::1", 8094)
    assert split_http("[::1]:8100") == ("[::1]", 8100)


def test_streaming_reply_runs_off_the_asgi_threadpool():
    import asyncio
    import threading

    from structor_lance.admin import stream_off_threadpool

    seen_threads: set[str] = set()

    def gen():
        for i in range(3):
            seen_threads.add(threading.current_thread().name)
            yield f"chunk{i}\n".encode()

    async def consume():
        out = []
        async for chunk in stream_off_threadpool(gen()):
            out.append(chunk)
        return out

    assert asyncio.run(consume()) == [b"chunk0\n", b"chunk1\n", b"chunk2\n"]
    assert seen_threads == {"sse-pump"}   # never the caller's thread, never an anyio worker
