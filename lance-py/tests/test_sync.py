"""Schema, cursor and upsert behaviour of the replica (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from structor_lance.pb import Cursor, pb_quote
from structor_lance.schema import BY_NAME, TABLES, Event, Session, from_record, model_for
from structor_lance.sync import Replica, cmp_cursor, rewind
from structor_lance.targets import Target

UNIT = Target("unit", "http://127.0.0.1:1", "e", "p")


def test_models_match_the_bun_edition_column_for_column():
    # the Bun schema (app/lance/src/sync.ts) is TypeScript, so the check is structural: every table's Arrow column names
    expected = {
        "projects": {"id", "path", "name", "encoded_dir", "cwd", "host", "created", "updated"},
        "sessions": {"id", "session_id", "project", "file_path", "tier", "byte_offset", "file_size", "file_mtime", "lines_seen",
                     "event_count", "first_ts", "last_ts", "first_prompt", "git_branch", "cwd", "model", "created", "updated"},
        "events": {"id", "session", "uuid", "parent_uuid", "type", "role", "ts", "iso_week", "text", "tools", "model", "sidechain",
                   "line_no", "raw_bytes", "created"},
        "session_weeks": {"id", "session", "project", "iso_week", "event_count", "user_count", "assistant_count", "tool_count",
                          "first_ts", "last_ts", "created", "updated"},
        "import_runs": {"id", "session", "project", "from_offset", "to_offset", "lines", "inserted", "skipped", "host", "writer", "created"},
    }
    for model in TABLES:
        assert set(model.to_arrow_schema().names) == expected[model.__table__], model.__table__
    assert Event.__fts__ == "text" and Session.__stamp__ == "updated" and Event.__stamp__ == "created"
    assert model_for("events") is Event and set(BY_NAME) == set(expected)
    with pytest.raises(KeyError):
        model_for("nope")


def test_from_record_shapes_a_pocketbase_record():
    row = from_record(Event, {"id": "r1", "created": "c", "session": "s1", "tools": [{"name": "Read"}], "sidechain": 1,
                              "line_no": "7", "raw_bytes": 120, "extra": "dropped"})
    assert row.tools == '[{"name":"Read"}]' and row.sidechain is True and row.line_no == 7.0 and row.raw_bytes == 120.0
    assert row.parent_uuid == "" and not hasattr(row, "extra")


def test_pb_quote_and_cursor_helpers():
    assert pb_quote("a'b\\c") == "'a\\'b\\\\c'"
    assert rewind(Cursor("2026-09-09 15:00:01.500Z", "abc"), 2000) == Cursor("2026-09-09 14:59:59.500Z", "")
    assert rewind(Cursor("not a date", "x"), 2000) == Cursor("not a date", "x")
    assert cmp_cursor(Cursor("a", "z"), Cursor("b", "a")) == -1
    assert cmp_cursor(Cursor("a", "b"), Cursor("a", "a")) == 1
    assert cmp_cursor(Cursor("a", "a"), Cursor("a", "a")) == 0


def test_merge_insert_updates_a_re_seen_row(tmp_path: Path):
    r = Replica(UNIT, tmp_path)
    assert r.dir == tmp_path / "unit" and r.state["tables"] == {}
    t = r.table(Session)
    a = from_record(Session, {"id": "s1", "created": "c", "updated": "u1", "session_id": "abc", "file_path": "/x", "byte_offset": 10})
    t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute([a])
    b = from_record(Session, {"id": "s1", "created": "c", "updated": "u2", "session_id": "abc", "file_path": "/x", "byte_offset": 20})
    t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute([b])
    assert t.count_rows() == 1
    rows = t.search().where("id = 's1'").limit(5).to_pydantic(Session)
    assert rows[0].byte_offset == 20.0 and rows[0].updated == "u2"
    r.mark_fts_built("events")
    assert json.loads(r.state_path.read_text())["tables"]["events"]["ftsBuilt"] is True


def test_page_after_builds_the_keyset_filter(monkeypatch: pytest.MonkeyPatch):
    from structor_lance.pb import PB

    seen: list[dict] = []

    class FakeResp:
        def __init__(self, status: int, body: dict):
            self.status_code, self._body = status, body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    pb = PB("http://pb.test", "e", "p")

    def fake_post(path, json=None, **kw):
        assert path.endswith("/auth-with-password")
        return FakeResp(200, {"token": "tok"})

    def fake_request(method, path, headers=None, params=None, **kw):
        seen.append({"path": path, "params": params, "auth": (headers or {}).get("Authorization")})
        return FakeResp(200, {"items": [], "totalItems": 0})

    monkeypatch.setattr(pb._client, "post", fake_post)
    monkeypatch.setattr(pb._client, "request", fake_request)
    assert pb.page_after("events", "created", Cursor("2026-09-09 15:00:00.100Z", "abc")) == []
    call = seen[-1]
    assert call["path"] == "/api/collections/events/records" and call["auth"] == "tok"
    assert call["params"]["sort"] == "created,id" and call["params"]["perPage"] == 1000
    assert call["params"]["filter"] == "(created > '2026-09-09 15:00:00.100Z') || (created = '2026-09-09 15:00:00.100Z' && id > 'abc')"


def test_state_copy_and_stamps_without_milliseconds(tmp_path: Path):
    r = Replica(UNIT, tmp_path)
    r.mark_fts_built("events")
    snap = r.state_copy()
    snap["tables"]["events"]["ftsBuilt"] = False       # a reader's copy never reaches the live state
    assert r.state["tables"]["events"]["ftsBuilt"] is True
    assert rewind(Cursor("2026-09-09 15:00:00Z", "abc"), 2000) == Cursor("2026-09-09 14:59:58.000Z", "")
    assert rewind(Cursor("2026-09-09T15:00:00.250Z", "abc"), 250) == Cursor("2026-09-09 15:00:00.000Z", "")
