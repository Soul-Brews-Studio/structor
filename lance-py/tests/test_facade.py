"""facade.py against a seeded replica: the console's API, answered from Lance.

The fixture is small but shaped like the real store — two projects, four
sessions (two of them sharing a prefix so resolution has something to get
wrong), events on both sides of a Bangkok midnight, a week ledger and an import
log. A second, smaller fixture carries the regressions from the Bun edition's
facade review: substring semantics, scope pushdown, real-date validation,
case-insensitive session resolution. Credentials here are fixture values, not
secrets.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from structor_lance import facade
from structor_lance.schema import BY_NAME, from_record
from structor_lance.sync import Replica
from structor_lance.targets import Target

EMAIL = "admin@unit.local"
PASSWORD = "unit-fixture-password"

DAY_MS = 86_400_000


def seed(r: Replica, name: str, rows: list[dict]) -> None:
    model = BY_NAME[name]
    t = r.table(model)
    if rows:
        t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
            [from_record(model, row) for row in rows])


def ymd(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def at(day: str, hhmmss: str) -> str:
    return f"{day} {hhmmss}.000Z"


class Client:
    """Calls handle() the way admin.py does: path without query, query as a mapping."""

    def __init__(self, replica: Replica, token: str = ""):
        self.replica = replica
        self.token = token

    def call(self, path: str, *, method: str = "GET", token: str | None = None,
             query: dict[str, str] | None = None, body: Any = None) -> tuple[int, Any]:
        headers = {}
        tok = self.token if token is None else token
        if tok:
            headers["Authorization"] = tok
        raw = b"" if body is None else json.dumps(body).encode()
        reply = facade.handle(self.replica, path, method, query or {}, headers, raw)
        assert isinstance(reply.body, bytes)
        return reply.status, (json.loads(reply.body) if reply.body else None)

    def get(self, path: str, query: dict[str, str] | None = None) -> tuple[int, Any]:
        return self.call(path, query=query)


# ---------------------------------------------------------------- the main fixture

T0 = time.time() * 1000 - 2 * DAY_MS
D = ymd(T0)  # UTC day; 16:59Z on it is still this day in Bangkok
D1 = ymd(T0 + DAY_MS)  # 17:00Z on D is already this day in Bangkok (+07:00)
HOUR_AGO = facade.stamp_at(time.time() * 1000 - 3_600_000)
TWO_HOURS_AGO = facade.stamp_at(time.time() * 1000 - 7_200_000)


@pytest.fixture(scope="module")
def replica(tmp_path_factory: pytest.TempPathFactory) -> Replica:
    root = tmp_path_factory.mktemp("facade")
    r = Replica(Target("unit", "http://127.0.0.1:1", EMAIL, PASSWORD), root)
    seed(r, "projects", [
        {"id": "p1", "created": at(D, "00:00:00"), "updated": at(D, "00:00:00"), "path": "/opt/Code/alpha-guess",
         "cwd": "/opt/Code/alpha", "name": "alpha", "encoded_dir": "-opt-Code-alpha", "host": "m5"},
        {"id": "p2", "created": at(D, "00:00:00"), "updated": at(D, "00:00:00"), "path": "/opt/Code/beta",
         "cwd": "", "name": "beta", "encoded_dir": "-opt-Code-beta", "host": "kvmlab1"},
    ])
    seed(r, "sessions", [
        {"id": "s1", "created": at(D, "16:00:00"), "updated": at(D1, "17:10:00"), "session_id": "abc111",
         "project": "p1", "file_path": "/t/abc111.jsonl", "tier": "projects", "byte_offset": 400, "file_size": 400,
         "file_mtime": 1, "lines_seen": 5, "event_count": 5, "first_ts": at(D, "16:59:00"), "last_ts": at(D, "19:00:00"),
         "first_prompt": "late night alpha", "git_branch": "main", "cwd": "/opt/Code/alpha"},
        {"id": "s2", "created": at(D, "05:00:00"), "updated": at(D, "05:10:00"), "session_id": "abc222",
         "project": "p2", "file_path": "/t/abc222.jsonl", "tier": "subagents", "byte_offset": 150, "file_size": 200,
         "file_mtime": 1, "lines_seen": 2, "event_count": 1, "first_ts": at(D, "05:00:00"), "last_ts": at(D, "05:00:00"),
         "first_prompt": "beta project note", "git_branch": "", "cwd": "/opt/Code/beta"},
        {"id": "s3", "created": at(D, "04:00:00"), "updated": at(D, "04:10:00"), "session_id": "abc1110",
         "project": "p1", "file_path": "/t/abc1110.jsonl", "tier": "projects", "byte_offset": 10, "file_size": 10,
         "file_mtime": 1, "lines_seen": 1, "event_count": 1, "first_ts": at(D, "04:00:00"), "last_ts": at(D, "04:00:00"),
         "first_prompt": "sibling that shares abc111", "git_branch": "", "cwd": "/opt/Code/alpha"},
        {"id": "s4", "created": at(D, "03:00:00"), "updated": at(D, "03:10:00"), "session_id": "zzz999",
         "project": "p1", "file_path": "/t/zzz999.jsonl", "tier": "backup", "byte_offset": 0, "file_size": 0,
         "file_mtime": 1, "lines_seen": 0, "event_count": 0, "first_ts": "", "last_ts": "", "first_prompt": "",
         "git_branch": "", "cwd": ""},
    ])
    seed(r, "events", [
        # s1, around the Bangkok day boundary (17:00Z)
        {"id": "e1", "created": at(D, "16:59:01"), "session": "s1", "uuid": "u1", "ts": at(D, "16:59:00"),
         "iso_week": "2026-W37", "role": "user", "type": "user", "text": "late night alpha", "tools": "[]", "line_no": 1},
        {"id": "e2", "created": at(D, "17:00:01"), "session": "s1", "uuid": "u2", "ts": at(D, "17:00:00"),
         "iso_week": "2026-W37", "role": "user", "type": "user", "text": "midnight crossing", "tools": "[]", "line_no": 2},
        {"id": "e3", "created": at(D, "17:05:01"), "session": "s1", "uuid": "u3", "ts": at(D, "17:05:00"),
         "iso_week": "2026-W37", "role": "assistant", "type": "assistant", "text": "answer about lancedb replicas",
         "tools": "[]", "line_no": 3},
        {"id": "e4", "created": at(D, "18:00:01"), "session": "s1", "uuid": "u4", "ts": at(D, "18:00:00"),
         "iso_week": "2026-W37", "role": "", "type": "hook", "text": "hook attachment", "tools": "[]", "line_no": 4},
        {"id": "e5", "created": at(D, "19:00:01"), "session": "s1", "uuid": "u5", "ts": at(D, "19:00:00"),
         "iso_week": "2026-W37", "role": "assistant", "type": "assistant", "text": "",
         "tools": '["Bash","Read"]', "line_no": 5},
        # s2, earlier the same UTC day
        {"id": "e6", "created": at(D, "05:00:01"), "session": "s2", "uuid": "u6", "ts": at(D, "05:00:00"),
         "iso_week": "2026-W37", "role": "user", "type": "user", "text": "beta project note", "tools": "[]", "line_no": 1},
        # s3, the prefix sibling
        {"id": "e7", "created": at(D, "04:00:01"), "session": "s3", "uuid": "u7", "ts": at(D, "04:00:00"),
         "iso_week": "2026-W37", "role": "user", "type": "user", "text": "sibling that shares abc111",
         "tools": "[]", "line_no": 1},
    ])
    seed(r, "session_weeks", [
        {"id": "w1", "created": at(D, "20:00:00"), "updated": at(D, "20:00:00"), "session": "s1", "project": "p1",
         "iso_week": "2026-W37", "event_count": 4, "user_count": 2, "assistant_count": 2, "tool_count": 1,
         "first_ts": at(D, "16:59:00"), "last_ts": at(D, "19:00:00")},
        {"id": "w2", "created": at(D, "20:00:00"), "updated": at(D, "20:00:00"), "session": "s2", "project": "p2",
         "iso_week": "2026-W37", "event_count": 1, "user_count": 1, "assistant_count": 0, "tool_count": 0,
         "first_ts": at(D, "05:00:00"), "last_ts": at(D, "05:00:00")},
        {"id": "w3", "created": at(D, "20:00:00"), "updated": at(D, "20:00:00"), "session": "s3", "project": "p1",
         "iso_week": "2026-W36", "event_count": 1, "user_count": 1, "assistant_count": 0, "tool_count": 0,
         "first_ts": at(D, "04:00:00"), "last_ts": at(D, "04:00:00")},
    ])
    seed(r, "import_runs", [
        {"id": "r1", "created": TWO_HOURS_AGO, "session": "s1", "project": "p1", "from_offset": 0, "to_offset": 200,
         "lines": 3, "inserted": 3, "skipped": 0, "host": "m5", "writer": "cli"},
        {"id": "r2", "created": HOUR_AGO, "session": "s1", "project": "p1", "from_offset": 200, "to_offset": 400,
         "lines": 2, "inserted": 2, "skipped": 1, "host": "m5", "writer": "cli"},
        {"id": "r3", "created": HOUR_AGO, "session": "s2", "project": "p2", "from_offset": 0, "to_offset": 150,
         "lines": 1, "inserted": 1, "skipped": 0, "host": "kvmlab1", "writer": "server-scan"},
        {"id": "r4", "created": facade.stamp_at(time.time() * 1000 - 40 * 3_600_000), "session": "s2", "project": "p2",
         "from_offset": 0, "to_offset": 0, "lines": 0, "inserted": 9, "skipped": 0, "host": "old", "writer": "cli"},
    ])
    return r


@pytest.fixture(scope="module")
def token(replica: Replica) -> str:
    status, body = Client(replica).call(
        "collections/_superusers/auth-with-password", method="POST",
        body={"identity": EMAIL, "password": PASSWORD})
    assert status == 200
    return body["token"]


@pytest.fixture
def c(replica: Replica, token: str) -> Client:
    return Client(replica, token)


# ---------------------------------------------------------------- auth


def test_login_answers_a_token_for_the_targets_credentials_and_400_for_anything_else(replica: Replica, token: str):
    anon = Client(replica)
    assert token.startswith("lance.")
    status, body = anon.call("collections/_superusers/auth-with-password", method="POST",
                             body={"identity": EMAIL, "password": "nope"})
    assert status == 400 and body["error"]
    assert anon.call("collections/_superusers/auth-with-password", method="POST",
                     body={"identity": "someone@else", "password": PASSWORD})[0] == 400
    assert anon.call("collections/_superusers/auth-with-password", method="POST")[0] == 400  # not JSON
    assert anon.call("collections/_superusers/auth-with-password", method="POST", body=["a"])[0] == 400
    assert anon.call("collections/_superusers/auth-with-password")[0] == 405


def test_no_token_a_foreign_token_and_another_targets_token_are_all_401(replica: Replica, token: str, tmp_path: Path):
    anon = Client(replica)
    assert anon.get("structor/status")[0] == 401
    assert anon.call("structor/status", token="lance.not-a-token")[0] == 401
    other = Replica(Target("other", "http://127.0.0.1:1", EMAIL, PASSWORD), tmp_path)
    status, body = Client(other, token).get("structor/status")
    assert status == 401 and body["error"] == "unauthorized"
    # a bearer-prefixed header is the same token
    assert Client(replica, f"Bearer {token}").get("structor/status")[0] == 200


# ---------------------------------------------------------------- reads


def test_status_counts_every_table_and_folds_the_week_ledger(c: Client):
    status, body = c.get("structor/status")
    assert status == 200
    assert {k: body[k] for k in ("projects", "sessions", "events", "session_weeks", "tz")} == {
        "projects": 2, "sessions": 4, "events": 7, "session_weeks": 3, "tz": "Asia/Bangkok"}
    assert body["last_ingest"] == at(D1, "17:10:00")  # max(sessions.updated)
    assert body["last_event_ts"] == at(D, "19:00:00")  # max(sessions.last_ts)
    assert isinstance(body["version"], str)
    assert datetime.strptime(body["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert [w["week"] for w in body["weeks"]] == ["2026-W37", "2026-W36"]  # newest first
    assert body["weeks"][0] == {"week": "2026-W37", "sessions": 2, "events": 5, "user_msgs": 3}


def test_projects_carry_their_session_and_event_totals_newest_activity_first(c: Client):
    rows = c.get("structor/projects")[1]["projects"]
    assert [p["id"] for p in rows] == ["p1", "p2"]
    assert rows[0] == {"id": "p1", "path": "/opt/Code/alpha-guess", "cwd": "/opt/Code/alpha",
                       "encoded_dir": "-opt-Code-alpha", "name": "alpha", "host": "m5",
                       "sessions": 3, "events": 6, "last_ts": at(D, "19:00:00")}
    assert rows[1]["sessions"] == 1
    assert len(c.get("structor/projects", {"limit": "1"})[1]["projects"]) == 1


def test_search_without_q_is_a_newest_first_window_over_conversational_rows_only(c: Client):
    body = c.get("structor/search")[1]
    hits = body["hits"]
    assert [h["uuid"] for h in hits] == ["u5", "u3", "u2", "u1", "u6", "u7"]  # ts DESC, hook row (role '') dropped
    assert hits[0]["tools"] == '["Bash","Read"]'  # kept: no text but tools
    assert hits[0]["project"] == "/opt/Code/alpha"  # cwd wins over the decoded guess
    assert next(h for h in hits if h["session_id"] == "abc222")["project"] == "/opt/Code/beta"  # no cwd: the guess
    assert body["limit"] == 30 and body["truncated"] is False
    capped = c.get("structor/search", {"limit": "2"})[1]
    assert len(capped["hits"]) == 2 and capped["truncated"] is True
    assert all(h["role"] == "user" for h in c.get("structor/search", {"role": "user"})[1]["hits"])
    assert [h["uuid"] for h in c.get("structor/search", {"project_id": "p2"})[1]["hits"]] == ["u6"]
    assert c.get("structor/search", {"week": "2026-W99"})[1]["hits"] == []


def test_search_with_q_is_a_substring_match_that_still_honours_the_filters(c: Client):
    hits = c.get("structor/search", {"q": "lancedb"})[1]["hits"]
    assert [h["uuid"] for h in hits] == ["u3"]
    assert hits[0]["snippet"] == "answer about lancedb replicas"
    assert c.get("structor/search", {"q": "lancedb", "project_id": "p2"})[1]["hits"] == []
    many = c.get("structor/search", {"q": "abc111"})[1]["hits"]  # matches the sibling session's text
    assert [h["uuid"] for h in many] == ["u7"]
    assert c.get("structor/search", {"session": "abc1"})[1]["hits"]  # session-id prefix scope


def test_days_groups_by_bangkok_day_so_1700z_belongs_to_the_next_day(c: Client):
    body = c.get("structor/days", {"from": D, "to": D1})[1]
    assert body["tz"] == "Asia/Bangkok" and body["truncated"] is False
    rows = body["days"]
    assert [f"{x['day']}/{x['session_id']}" for x in rows] == [
        f"{D1}/abc111", f"{D}/abc111", f"{D}/abc222", f"{D}/abc1110"]
    crossed = rows[0]
    assert crossed["events"] == 3  # 17:00, 17:05 and the tools-only 19:00 row
    assert crossed["user_msgs"] == 1
    assert crossed["first_ts"] == at(D, "17:00:00") and crossed["last_ts"] == at(D, "19:00:00")
    assert crossed["preview"] == "midnight crossing"
    assert crossed["git_branch"] == "main" and crossed["project"] == "/opt/Code/alpha"
    before = next(x for x in rows if x["day"] == D and x["session_id"] == "abc111")
    assert before["events"] == 1  # only 16:59Z; the hook row has no role
    assert before["preview"] == "late night alpha"
    one = c.get("structor/days", {"from": D, "to": D1, "project_id": "p2"})[1]["days"]
    assert [x["session_id"] for x in one] == ["abc222"]
    by_path = c.get("structor/days", {"from": D, "to": D1, "project": "alpha"})[1]["days"]
    assert all(x["project"] == "/opt/Code/alpha" for x in by_path)
    cut = c.get("structor/days", {"from": D, "to": D1, "limit": "1"})[1]
    assert len(cut["days"]) == 1 and cut["truncated"] is True
    assert c.get("structor/days", {"from": "nope", "to": D1})[0] == 400
    assert c.get("structor/days", {"to": D1})[0] == 400


def test_read_resolves_a_prefix_pages_in_time_order_and_reports_the_bad_cases(c: Client):
    rows = c.get("structor/read", {"session": "abc1110"})[1]["events"]
    assert rows == [{"ts": at(D, "04:00:00"), "role": "user", "type": "user",
                     "text": "sibling that shares abc111", "tools": "[]", "line_no": 1}]

    exact = c.get("structor/read", {"session": "abc111"})[1]  # also a prefix of abc1110: the exact id wins
    assert [x["line_no"] for x in exact["events"]] == [1, 2, 3, 5]  # ts ASC, the role-less hook row dropped
    assert exact["offset"] == 0

    page = c.get("structor/read", {"session": "abc111", "offset": "2", "limit": "1"})[1]
    assert [x["line_no"] for x in page["events"]] == [3] and page["offset"] == 2

    status, body = c.get("structor/read", {"session": "abc"})
    assert status == 400 and "ambiguous" in body["error"]
    assert c.get("structor/read", {"session": "nothing-like-this"})[0] == 404
    assert c.get("structor/read")[0] == 400


def test_sessions_lists_newest_last_ts_first_and_filters_by_project_and_week(c: Client):
    rows = c.get("structor/sessions")[1]["sessions"]
    assert [x["session_id"] for x in rows] == ["abc111", "abc222", "abc1110", "zzz999"]
    assert rows[0] == {"session_id": "abc111", "project": "/opt/Code/alpha", "tier": "projects",
                       "first_ts": at(D, "16:59:00"), "last_ts": at(D, "19:00:00"), "event_count": 5,
                       "first_prompt": "late night alpha", "git_branch": "main", "file_path": "/t/abc111.jsonl"}
    assert [x["session_id"] for x in c.get("structor/sessions", {"project_id": "p2"})[1]["sessions"]] == ["abc222"]
    assert [x["session_id"] for x in c.get("structor/sessions", {"week": "2026-W36"})[1]["sessions"]] == ["abc1110"]
    assert len(c.get("structor/sessions", {"limit": "1"})[1]["sessions"]) == 1


def test_weeks_serves_the_ledger_rows_with_gos_field_names(c: Client):
    rows = c.get("structor/weeks")[1]["weeks"]
    assert [f"{w['iso_week']}/{w['session_id']}" for w in rows] == [
        "2026-W37/abc111", "2026-W37/abc222", "2026-W36/abc1110"]
    assert rows[0] == {"iso_week": "2026-W37", "session_id": "abc111", "project": "/opt/Code/alpha", "events": 4,
                       "user_msgs": 2, "assistant_msgs": 2, "tool_calls": 1,
                       "first_ts": at(D, "16:59:00"), "last_ts": at(D, "19:00:00")}
    assert len(c.get("structor/weeks", {"week": "2026-W36"})[1]["weeks"]) == 1
    assert [w["session_id"] for w in c.get("structor/weeks", {"session": "abc222"})[1]["weeks"]] == ["abc222"]


def test_intake_summarises_tail_state_the_import_log_the_files_and_the_writers(c: Client):
    body = c.get("structor/intake")[1]
    assert body["summary"] == {
        "files": 4, "bytes_tracked": 610, "bytes_indexed": 560, "pending_files": 1, "pending_bytes": 50,
        "last_ingest": at(D1, "17:10:00"), "runs_today": 3, "inserted_today": 6, "hosts": "kvmlab1,m5"}
    runs = body["runs"]
    assert len(runs) == 4  # all history, like Go's ListRuns (the 40h-old row included)
    assert runs[0]["created"] >= runs[1]["created"]  # newest first
    beta = next(x for x in runs if x["session_id"] == "abc222")
    assert beta["project"] == "/opt/Code/beta" and beta["file_path"] == "/t/abc222.jsonl"
    assert beta["writer"] == "server-scan" and beta["host"] == "kvmlab1"
    assert [f["session_id"] for f in body["files"]] == ["abc111", "abc222", "abc1110", "zzz999"]  # updated DESC
    pending = c.get("structor/intake", {"pending": "1"})[1]["files"]
    assert [f["session_id"] for f in pending] == ["abc222"]
    writers = body["writers"]
    # every writer that ever wrote (Go's ListWriters), the quiet "old" host included, with 24h counters at 0
    assert sorted(f"{w['host']}/{w['writer']}" for w in writers) == ["kvmlab1/server-scan", "m5/cli", "old/cli"]
    quiet = next(w for w in writers if w["host"] == "old")
    assert quiet["runs_24h"] == 0 and quiet["inserted_24h"] == 0 and quiet["files"] == 1
    cli = next(w for w in writers if w["host"] == "m5")
    assert cli == {"host": "m5", "writer": "cli", "last_run": HOUR_AGO,
                   "runs_24h": 2, "inserted_24h": 5, "files": 1}
    assert body["connections"] == {"oauth_clients": 0, "active_tokens": 0}  # Go's keys
    # keys the console reads without guarding
    assert body["server_scan"] == {"enabled": False, "dir": "", "interval": ""}
    assert body["tz"] == "Asia/Bangkok" and body["host"] == "unit"
    assert isinstance(body["upload_dir"], str) and body["superuser"] is True
    assert body["scan_dir"] == "" and body["scan_interval"] == ""


# ---------------------------------------------------------------- refusals


def test_writes_tail_state_and_unknown_paths_are_refused_with_an_error_body(replica: Replica, c: Client):
    anon = Client(replica)
    for p in ("structor/scan", "structor/reconcile", "structor/upload", "structor/ingest"):
        status, body = c.call(p, method="POST")
        assert status == 405 and "PocketBase console" in body["error"]
        assert anon.call(p, method="POST")[0] == 401  # auth is checked first
    status, body = c.get("structor/state", {"path": "/t/abc111.jsonl"})
    assert status == 404 and body["error"]
    assert c.get("structor/nope")[0] == 404
    assert c.call("structor/status", method="POST")[0] == 405


def test_realtime_get_is_open_because_eventsource_cannot_authenticate_and_post_is_not(replica: Replica, c: Client):
    assert Client(replica).call("realtime", method="POST", body={"clientId": "x", "subscriptions": []})[0] == 401
    # the fixture target is a closed port: the proxy reports the upstream failure rather than 401
    status, body = Client(replica).get("realtime")
    assert status == 502 and "realtime upstream" in body["error"]
    assert c.call("realtime", method="PATCH")[0] == 405


def test_the_realtime_proxy_hands_control_back_instead_of_parking_a_worker(replica: Replica, monkeypatch: pytest.MonkeyPatch):
    """An idle SSE stream used to block the generator inside ``iter_raw()`` for as long as the
    browser stayed connected. ASGI runs that generator in the server's threadpool, so ~40 of
    them and the whole admin stopped answering. The reading now happens on its own thread and
    the generator only ever waits SSE_TICK_S on a queue."""
    closed: list[bool] = []
    gate = threading.Event()  # stands in for an upstream that has nothing to say

    class FakeUpstream:
        status_code = 200

        def iter_raw(self):
            yield b"data: hi\n\n"
            gate.wait(5)
            raise RuntimeError("upstream closed")

    class FakeCtx:
        def __enter__(self):
            return FakeUpstream()

        def __exit__(self, *_a):
            gate.set()
            closed.append(True)

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def stream(self, *_a, **_kw):
            return FakeCtx()

        def close(self):
            gate.set()

    monkeypatch.setattr(facade, "SSE_TICK_S", 0.05)
    monkeypatch.setattr(facade.httpx, "Client", FakeClient)

    reply = facade.realtime_get(replica)
    assert reply.status == 200 and reply.headers["content-type"] == "text/event-stream"
    body = reply.body
    assert not isinstance(body, bytes)
    assert next(body) == b"data: hi\n\n"
    assert next(body) == b": ping\n\n"  # nothing upstream: the worker is released, not held
    body.close()  # what a disconnected browser does

    assert closed  # the upstream response was closed, not leaked
    def alive() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name.startswith("realtime-") and t.is_alive()]

    deadline = time.time() + 3
    while alive() and time.time() < deadline:
        time.sleep(0.02)
    assert not alive()  # the reader thread ended with the stream


def test_a_failing_handler_answers_json_never_a_traceback(replica: Replica, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(facade, "store", lambda _r: (_ for _ in ()).throw(RuntimeError("lance exploded")))
    reply = facade.handle(replica, "structor/sessions", "GET", {}, {"Authorization": "x"}, b"")
    assert reply.status == 401  # auth first
    token = json.loads(facade.handle(replica, "collections/_superusers/auth-with-password", "POST", {}, {},
                                     json.dumps({"identity": EMAIL, "password": PASSWORD}).encode()).body)["token"]
    reply = facade.handle(replica, "structor/sessions", "GET", {}, {"Authorization": token}, b"")
    assert reply.status == 500
    assert reply.headers["content-type"].startswith("application/json")
    assert json.loads(reply.body)["error"] == "lance exploded"


# ---------------------------------------------------------------- the review regressions

S = "10:00:00"


@pytest.fixture(scope="module")
def fixes(tmp_path_factory: pytest.TempPathFactory) -> Client:
    """The facade-review fixture: mixed case, LIKE metacharacters, two projects."""
    root = tmp_path_factory.mktemp("facade-fixes")
    r = Replica(Target("unit", "http://127.0.0.1:1", "e@x", "pw"), root)
    stamp = at(D, S)
    seed(r, "projects", [
        {"id": "p1", "created": stamp, "updated": stamp, "path": "/a", "cwd": "/a", "name": "a", "host": "m5"},
        {"id": "p2", "created": stamp, "updated": stamp, "path": "/b", "cwd": "/b", "name": "b", "host": "m5"},
    ])
    seed(r, "sessions", [
        {"id": "s1", "created": stamp, "updated": stamp, "session_id": "Alpha-1", "project": "p1",
         "file_path": "/a/1.jsonl", "first_ts": stamp, "last_ts": stamp, "event_count": 2},
        {"id": "s2", "created": stamp, "updated": stamp, "session_id": "beta-2", "project": "p2",
         "file_path": "/b/2.jsonl", "first_ts": stamp, "last_ts": stamp, "event_count": 1},
    ])
    seed(r, "events", [
        {"id": "e1", "created": stamp, "session": "s1", "uuid": "u1", "ts": at(D, "10:00:01"), "iso_week": "2026-W37",
         "role": "user", "text": "the Structor replica", "line_no": 1},
        {"id": "e2", "created": stamp, "session": "s1", "uuid": "u2", "ts": at(D, "10:00:02"), "iso_week": "2026-W37",
         "role": "assistant", "text": "100% done_now", "line_no": 2},
        {"id": "e3", "created": stamp, "session": "s2", "uuid": "u3", "ts": at(D, "10:00:03"), "iso_week": "2026-W37",
         "role": "user", "text": "structor in project b", "line_no": 3},
        # the decoy every LIKE metacharacter would wrongly match if it were not escaped:
        # `_` would wildcard done_now onto donexnow, `%` would drop the sign off 100%, `\b` would escape the b in ab
        {"id": "e4", "created": stamp, "session": "s2", "uuid": "u4", "ts": at(D, "10:00:04"), "iso_week": "2026-W37",
         "role": "user", "text": "donexnow and 100 fine, ab pair", "line_no": 4},
    ])
    seed(r, "session_weeks", [])
    seed(r, "import_runs", [])
    anon = Client(r)
    tok = anon.call("collections/_superusers/auth-with-password", method="POST",
                    body={"identity": "e@x", "password": "pw"})[1]["token"]
    return Client(r, tok)


def test_search_is_a_case_insensitive_substring_match_mid_word_included(fixes: Client):
    mid = fixes.get("structor/search", {"q": "ructor"})[1]["hits"]
    assert sorted(h["uuid"] for h in mid) == ["u1", "u3"]
    assert len(fixes.get("structor/search", {"q": "STRUCTOR"})[1]["hits"]) == 2
    # the decoy row (u4) holds donexnow, "100 fine" and "ab": each metacharacter must stay a literal
    assert [h["uuid"] for h in fixes.get("structor/search", {"q": "100%"})[1]["hits"]] == ["u2"]
    assert [h["uuid"] for h in fixes.get("structor/search", {"q": "done_now"})[1]["hits"]] == ["u2"]
    assert fixes.get("structor/search", {"q": "done%now"})[1]["hits"] == []  # % is a literal, not a wildcard
    assert fixes.get("structor/search", {"q": "it's"})[0] == 200  # a quote is escaped, not a syntax error
    backslash = fixes.get("structor/search", {"q": "a\\b"})
    assert backslash[0] == 200 and backslash[1]["hits"] == []  # so is a backslash: it does not escape the b
    assert [h["uuid"] for h in fixes.get("structor/search", {"q": "ab"})[1]["hits"]] == ["u4"]


def test_a_project_scope_narrows_the_search_before_the_cut(fixes: Client):
    scoped = fixes.get("structor/search", {"q": "structor", "project_id": "p2", "limit": "1"})[1]["hits"]
    assert [h["uuid"] for h in scoped] == ["u3"]
    assert fixes.get("structor/search", {"q": "structor", "project_id": "nope"})[1]["hits"] == []


def test_scope_pushdown_stops_at_2000_ids():
    assert facade.scope_pred(None) is None
    assert facade.scope_pred(set()) == "session = ''"
    assert facade.scope_pred({"a", "b"}) == "session IN ('a', 'b')"
    assert facade.scope_pred({str(i) for i in range(facade.SCOPE_PUSHDOWN_MAX)}) is not None
    assert facade.scope_pred({str(i) for i in range(facade.SCOPE_PUSHDOWN_MAX + 1)}) is None


def test_days_refuses_impossible_dates_and_defaults_to_500_rows(fixes: Client):
    assert fixes.get("structor/days", {"from": "2026-02-31", "to": "2026-02-31"})[0] == 400
    assert fixes.get("structor/days", {"from": "2026-13-01", "to": "2026-13-01"})[0] == 400
    ok = fixes.get("structor/days", {"from": D, "to": D})
    assert ok[0] == 200
    assert sorted(x["session_id"] for x in ok[1]["days"]) == ["Alpha-1", "beta-2"]
    assert facade.limit_of(None, 500, 5000) == 500
    assert facade.limit_of("0", 500, 5000) == 500 and facade.limit_of("99999", 500, 5000) == 500
    assert facade.limit_of("nope", 500, 5000) == 500 and facade.limit_of("7", 500, 5000) == 7


def test_read_resolves_session_ids_case_insensitively(fixes: Client):
    assert fixes.get("structor/read", {"session": "ALPHA-1"})[0] == 200
    assert fixes.get("structor/read", {"session": "alp"})[0] == 200
    assert fixes.get("structor/read", {"session": "zzz"})[0] == 404


def test_a_token_is_bound_to_the_target_that_issued_it_and_expires(replica: Replica):
    tok = facade.issue("unit")
    assert facade.authorized(replica, {"authorization": tok}) is True
    facade._tokens[tok] = ("unit", time.time() - facade.TOKEN_TTL_S - 1)
    assert facade.authorized(replica, {"authorization": tok}) is False
    assert tok not in facade._tokens  # the expired entry is swept on the way out
