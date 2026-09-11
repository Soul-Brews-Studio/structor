"""The live relay: ``live.LiveHub`` and the ``/api/{t}/live`` routes.

No PocketBase: ``PB.live`` is replaced by ``FakeFeed``, which hands the hub N
messages and then blocks until told to stop, exactly like the real stream when
nothing is being ingested. The replica points at a closed port and is never
synced.

The stream itself is read through a real uvicorn on an ephemeral loopback
port. Starlette's ``TestClient`` and httpx's ``ASGITransport`` both collect a
response body to its end before handing it back, and an SSE body has no end;
a real socket also makes the disconnect path (uvicorn → Starlette cancel →
pump stop → ``unsubscribe``) the one that is tested, not a stand-in.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from structor_lance import live
from structor_lance.admin import create_app
from structor_lance.live import Full, LiveHub, frame, parse_last_id, sse_frames
from structor_lance.sync import Replica
from structor_lance.targets import Target

UNIT = Target("unit", "http://127.0.0.1:1", "e", "p")
BASE = "http://127.0.0.1:8094"  # the Host header has to be loopback or every request is a 403


def message(i: int, text: str = "") -> dict[str, Any]:
    """One ``LiveMessage`` as ``ingest.go`` marshals it (field names matter: the page reads them)."""
    return {
        "at": f"2026-09-11 13:00:{i % 60:02d}.000Z", "session_id": f"session-{i}", "project": "/repo/one",
        "host": "m", "writer": "watcher", "inserted": 1, "skipped": 0, "byte_offset": 100 * i,
        "events": [{"uuid": f"u{i}", "ts": f"2026-09-11 13:00:{i % 60:02d}.000Z", "role": "user",
                    "type": "user", "text": text or f"hello {i}", "line_no": i}],
        "truncated": False,
    }


class FakeFeed:
    """Stands in for ``PB.live``: delivers ``n`` messages, then blocks until ``stop``.

    ``fail`` makes every call raise (an upstream that is down); ``fail_first``
    only the first (an upstream that came back). ``close`` makes every call
    return cleanly after delivering (a server that ends each stream at once);
    ``close_first`` only the first (PocketBase's routine max-lifetime cutoff).
    """

    def __init__(self, n: int = 3, *, fail: bool = False, fail_first: bool = False,
                 close: bool = False, close_first: bool = False):
        self.n, self.fail, self.fail_first = n, fail, fail_first
        self.close, self.close_first = close, close_first
        self.calls = 0
        self.topics: list[str] = []
        self.stops: list[threading.Event] = []
        self.started = threading.Event()
        self.delivered = threading.Event()

    def __call__(self, topic: str, on_message: Callable[[Any], None], stop: threading.Event) -> None:
        self.calls += 1
        self.topics.append(topic)
        self.stops.append(stop)
        self.started.set()
        if self.fail or (self.fail_first and self.calls == 1):
            raise RuntimeError("boom")
        for i in range(1, self.n + 1):
            on_message(message(i))
        self.delivered.set()
        if self.close or (self.close_first and self.calls == 1):
            return  # the server closed the stream; PB.live returns without raising
        stop.wait(10)


def until(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``pred`` for up to ``timeout`` seconds; threads and timers need a moment, not a sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def drain(q: Any) -> list[Any]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.fixture
def replica(tmp_path: Path) -> Replica:
    return Replica(UNIT, tmp_path / "data")


def build(tmp_path: Path, feed: FakeFeed, *, read_only: bool = False) -> tuple[FastAPI, LiveHub]:
    ui = tmp_path / "ui"
    ui.mkdir(exist_ok=True)
    (ui / "index.html").write_text("<!doctype html><title>admin</title>")
    r = Replica(UNIT, tmp_path / "data")
    r.pb.live = feed  # type: ignore[method-assign] — the hub resolves replica.pb.live at each start
    app = create_app({"unit": r}, version="test", data_root=tmp_path / "data", read_only=read_only, ui_dir=ui,
                     console_dir=tmp_path / "console", embedder=lambda _r: None)
    return app, app.state.hubs["unit"]


def client(app: FastAPI) -> TestClient:
    """For the responses that end: recent, status, the refusals."""
    return TestClient(app, base_url=BASE)


@contextlib.contextmanager
def served(app: FastAPI) -> Iterator[httpx.Client]:
    """The app on a real loopback port, for the response that does not end (see the module docstring)."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    assert until(lambda: server.started, 10), "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
            yield c
    finally:
        server.should_exit = True
        thread.join(10)


def read_until(r: httpx.Response, stop_at: str, *, prefix: bool = False) -> list[str]:
    """SSE lines up to and including the first ``stop_at`` line (or one starting with it), then the caller closes."""
    lines: list[str] = []
    for line in r.iter_lines():
        lines.append(line)
        if line.startswith(stop_at) if prefix else line == stop_at:
            break
    return lines


# ---- the hub ---------------------------------------------------------------


def test_the_upstream_starts_with_the_first_subscriber_and_is_shared_by_the_rest(replica: Replica):
    feed = FakeFeed(n=3)
    hub = LiveHub(replica, live=feed)
    assert feed.calls == 0 and hub.stats() == {"subscribers": 0, "buffered": 0, "upstream": "off"}

    q1 = hub.subscribe()
    assert feed.delivered.wait(2) and feed.topics == ["structor/live"]
    assert until(lambda: q1.qsize() == 3)
    ids = [i for i, _m in drain(q1)]
    assert ids == [1, 2, 3]
    assert hub.stats() == {"subscribers": 1, "buffered": 3, "upstream": "on"}

    q2 = hub.subscribe()  # no last_id: live only, nothing replayed
    assert q2.empty()
    hub.publish(message(4))
    assert q1.get(timeout=1)[0] == 4 and q2.get(timeout=1)[0] == 4
    assert feed.calls == 1  # two subscribers, one upstream
    hub.close()


def test_last_event_id_replays_what_was_missed_and_the_buffer_is_a_ring(replica: Replica):
    hub = LiveHub(replica, live=FakeFeed(n=0))
    for i in range(1, 301):
        hub.publish(message(i))
    assert hub.stats()["buffered"] == live.BUFFER == 256

    late = hub.subscribe(last_id=290)
    assert [i for i, _m in drain(late)] == list(range(291, 301))
    everything = hub.subscribe(last_id=0)
    replayed = [i for i, _m in drain(everything)]
    assert replayed[0] == 45 and replayed[-1] == 300 and len(replayed) == 256
    beyond = hub.subscribe(last_id=999)
    assert beyond.empty()

    recent = hub.recent(5)
    assert [m["session_id"] for m in recent["messages"]] == [f"session-{i}" for i in range(296, 301)]
    assert recent["last_id"] == 300
    assert len(hub.recent()["messages"]) == 50
    hub.close()


def test_the_twenty_fifth_subscriber_is_refused_until_a_seat_frees_up(replica: Replica):
    hub = LiveHub(replica, live=FakeFeed(n=0))
    seats = [hub.subscribe() for _ in range(live.MAX_SUBSCRIBERS)]
    assert hub.stats()["subscribers"] == 24
    with pytest.raises(Full):
        hub.subscribe()
    hub.unsubscribe(seats[0])
    assert hub.subscribe() is not None
    hub.close()


def test_the_upstream_stops_a_grace_period_after_the_last_subscriber_leaves(replica: Replica):
    feed = FakeFeed(n=0)
    hub = LiveHub(replica, live=feed, grace_s=0.1)
    q = hub.subscribe()
    assert feed.started.wait(2)

    hub.unsubscribe(q)
    assert hub.stats()["upstream"] == "on"  # a reload is not a departure: the grace period holds it
    q2 = hub.subscribe()  # back before the grace ran out
    time.sleep(0.2)
    assert hub.stats()["upstream"] == "on" and feed.calls == 1 and not feed.stops[0].is_set()

    hub.unsubscribe(q2)
    assert until(lambda: hub.stats()["upstream"] == "off")
    assert feed.stops[0].is_set() and hub.stats()["subscribers"] == 0

    hub.subscribe()  # a later visitor gets a fresh upstream
    assert until(lambda: feed.calls == 2)
    assert hub.stats()["upstream"] == "on"
    hub.close()


def test_an_upstream_error_is_reported_and_retried_with_backoff(replica: Replica):
    down = FakeFeed(fail=True)
    hub = LiveHub(replica, live=down, reconnect_s=0.02)
    hub.subscribe()
    assert until(lambda: down.calls >= 3)
    assert hub.stats()["upstream"] == "error: boom"
    hub.close()

    back = FakeFeed(n=2, fail_first=True)
    hub2 = LiveHub(replica, live=back, reconnect_s=0.02)
    q = hub2.subscribe()
    assert back.delivered.wait(2)
    assert until(lambda: q.qsize() == 2)
    assert back.calls == 2 and hub2.stats()["upstream"] == "on"
    hub2.close()


def test_a_clean_upstream_close_is_resubscribed_at_once_not_after_the_backoff(replica: Replica, monkeypatch: pytest.MonkeyPatch):
    """PocketBase ends every realtime stream on a timer; an ingest during a 5 s wait would never reach the buffer."""
    monkeypatch.setattr(live, "SHORT_STREAM_S", 0.0)  # the fake's stream lives microseconds; treat that as a long one
    feed = FakeFeed(n=1, close_first=True)
    hub = LiveHub(replica, live=feed, reconnect_s=5.0)
    q = hub.subscribe()
    assert until(lambda: feed.calls == 2, timeout=1.0)  # well inside the 5 s an error would wait
    assert hub.stats()["upstream"] == "on" and q.get(timeout=1)[0] == 1
    hub.close()


def test_a_stream_the_server_ends_at_once_is_backed_off_like_an_error(replica: Replica):
    """A refusal can look like a clean close; resubscribing at once would be a tight loop against the server."""
    feed = FakeFeed(n=0, close=True)
    hub = LiveHub(replica, live=feed, reconnect_s=0.2)
    hub.subscribe()
    time.sleep(0.5)
    assert 1 <= feed.calls <= 4  # one open per backoff, not hundreds
    assert hub.stats()["upstream"].startswith("error: stream closed at once")
    hub.close()


def test_a_subscriber_that_stops_reading_is_dropped_and_nobody_else_slows(replica: Replica, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "SUBSCRIBER_QUEUE", 5)
    hub = LiveHub(replica, live=FakeFeed(n=0))
    stuck, fine = hub.subscribe(), hub.subscribe()
    for i in range(1, 7):
        hub.publish(message(i))
        drain(fine)  # this one keeps up
    items = drain(stuck)
    assert len(items) == 6 and items[-1] is None  # five it never read, then the end-of-stream sentinel
    assert hub.stats()["subscribers"] == 1
    hub.close()


def test_close_ends_every_stream(replica: Replica):
    hub = LiveHub(replica, live=FakeFeed(n=0))
    q = hub.subscribe()
    hub.close()
    assert q.get(timeout=1) is None
    with pytest.raises(RuntimeError):
        hub.subscribe()


# ---- framing ---------------------------------------------------------------


def test_frames_carry_id_event_and_one_json_line_and_heartbeats_fill_the_silence(replica: Replica):
    feed = FakeFeed(n=1)
    hub = LiveHub(replica, live=feed)
    body = sse_frames(hub, None, heartbeat_s=0.05)
    assert next(body) == b": connected\n\n"
    first = next(body)
    assert first.startswith(b"id: 1\nevent: live\ndata: {") and first.endswith(b"}\n\n")
    assert first.count(b"\n") == 4  # id, event, one data line, the blank terminator
    assert next(body) == b": heartbeat\n\n"  # nothing for 50 ms: the connection is kept warm
    assert hub.stats()["subscribers"] == 1
    body.close()  # what a disconnected browser does, through the pump
    assert hub.stats()["subscribers"] == 0
    hub.close()


def test_the_stream_ends_within_a_poll_of_its_stop_flag_not_at_the_next_heartbeat(replica: Replica):
    """The pump sets the flag when the browser leaves; a stream waiting a full heartbeat kept the seat 15 s."""
    hub = LiveHub(replica, live=FakeFeed(n=0))
    gone = threading.Event()
    body = sse_frames(hub, None, stop=gone)  # heartbeat_s left at 15 s on purpose
    assert next(body) == b": connected\n\n"
    assert hub.stats()["subscribers"] == 1
    t0 = time.monotonic()
    gone.set()
    with pytest.raises(StopIteration):
        next(body)
    assert time.monotonic() - t0 < 1.0 and hub.stats()["subscribers"] == 0
    hub.close()


def test_frame_keeps_unicode_and_never_breaks_the_data_line():
    f = frame(7, message(1, text="สวัสดี\nsecond line"))
    assert f.startswith(b"id: 7\nevent: live\ndata: ")
    assert "สวัสดี".encode() in f  # not \\u escaped: the page shows Thai, and the bytes are a third the size
    data = f.split(b"data: ", 1)[1]
    assert data.endswith(b"\n\n") and b"\n" not in data[:-2]  # the newline in the text is escaped, the frame intact


def test_parse_last_id_prefers_the_header_and_ignores_junk():
    assert parse_last_id("12", None) == 12
    assert parse_last_id(None, "9") == 9
    assert parse_last_id("12", "9") == 12
    assert parse_last_id("abc", "9") == 9
    assert parse_last_id("-3", None) == 0
    assert parse_last_id("", " ") is None
    assert parse_last_id(None, None) is None


# ---- the routes ------------------------------------------------------------


def test_recent_and_status_read_the_ring_buffer(tmp_path: Path):
    app, hub = build(tmp_path, FakeFeed(n=0))
    c = client(app)
    assert c.get("/api/unit/live/recent").json() == {"messages": [], "last_id": 0}
    hub.publish(message(1))
    hub.publish(message(2))
    j = c.get("/api/unit/live/recent", params={"limit": 1}).json()
    assert [m["session_id"] for m in j["messages"]] == ["session-2"] and j["last_id"] == 2
    assert len(c.get("/api/unit/live/recent", params={"limit": "abc"}).json()["messages"]) == 2
    assert c.get("/api/unit/live/recent", params={"limit": 10_000}).status_code == 200
    assert c.get("/api/status").json()["targets"][0]["live"] == {"subscribers": 0, "buffered": 2, "upstream": "off"}
    assert c.get("/api/nope/live/recent").status_code == 404
    hub.close()


def test_the_stream_replays_after_last_event_id_then_heartbeats_and_the_seat_is_freed_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(live, "HEARTBEAT_S", 0.05)
    feed = FakeFeed(n=0)
    app, hub = build(tmp_path, feed)
    hub.publish(message(1))
    hub.publish(message(2))

    with served(app) as c:
        with c.stream("GET", "/api/unit/live", headers={"Last-Event-ID": "1"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
            lines = read_until(r, ": heartbeat")
        assert lines[0] == ": connected"
        assert "id: 2" in lines and "id: 1" not in lines  # replayed what was missed, not what was seen
        assert any(line.startswith("data: {") and '"session_id":"session-2"' in line for line in lines)
        assert feed.started.wait(2)  # the browser's arrival started the upstream
        assert until(lambda: hub.stats()["subscribers"] == 0)  # …and its departure released the seat
        assert c.get("/api/status").json()["targets"][0]["live"]["upstream"] == "on"  # for the grace period
    hub.close()


def test_a_closed_browser_frees_its_seat_within_a_second_not_a_heartbeat(tmp_path: Path):
    """HEARTBEAT_S stays at 15 s here: with the old blocking wait, 24 reloads inside a heartbeat hit the 503."""
    app, hub = build(tmp_path, FakeFeed(n=0))
    with served(app) as c:
        with c.stream("GET", "/api/unit/live") as r:
            assert read_until(r, ": connected") == [": connected"]
            assert hub.stats()["subscribers"] == 1
        t0 = time.monotonic()
        assert until(lambda: hub.stats()["subscribers"] == 0, timeout=3.0)
        assert time.monotonic() - t0 < 2.0
    hub.close()


def test_a_query_last_id_works_where_eventsource_cannot_send_a_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "HEARTBEAT_S", 0.05)
    app, hub = build(tmp_path, FakeFeed(n=0))
    for i in range(1, 4):
        hub.publish(message(i))
    with served(app) as c, c.stream("GET", "/api/unit/live", params={"last_id": 2}) as r:
        seen = read_until(r, ": heartbeat")
    assert [x for x in seen if x.startswith("id: ")] == ["id: 3"]
    hub.close()


def test_a_live_message_reaches_an_open_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """End to end: the upstream feed → hub → SSE frame in a browser that was already connected."""
    monkeypatch.setattr(live, "HEARTBEAT_S", 0.05)
    feed = FakeFeed(n=1)  # delivers once the first subscriber starts it
    app, hub = build(tmp_path, feed)
    with served(app) as c, c.stream("GET", "/api/unit/live") as r:
        lines = read_until(r, "data: ", prefix=True)  # the first frame's payload line
    assert lines[0] == ": connected" and lines[2] == "id: 1" and lines[3] == "event: live"
    assert lines[4].startswith('data: {"at":') and '"events":[{"uuid":"u1"' in lines[4]
    assert hub.stats()["buffered"] == 1
    hub.close()


def test_unknown_target_is_404_and_the_twenty_fifth_browser_is_503(tmp_path: Path):
    app, hub = build(tmp_path, FakeFeed(n=0))
    c = client(app)
    assert c.get("/api/nope/live").status_code == 404
    seats = [hub.subscribe() for _ in range(live.MAX_SUBSCRIBERS)]
    r = c.get("/api/unit/live")
    assert r.status_code == 503 and "24" in r.json()["error"]
    assert r.headers["content-type"].startswith("application/json")
    for q in seats:
        hub.unsubscribe(q)
    hub.close()


def test_the_relay_is_a_read_and_works_on_a_no_sync_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "HEARTBEAT_S", 0.05)
    app, hub = build(tmp_path, FakeFeed(n=0), read_only=True)
    assert client(app).get("/api/unit/live/recent").status_code == 200
    assert client(app).post("/api/unit/sync").status_code == 405  # the instance really is read-only
    with served(app) as c:
        with c.stream("GET", "/api/unit/live") as r:
            assert r.status_code == 200
            assert read_until(r, ": connected") == [": connected"]
        assert until(lambda: hub.stats()["subscribers"] == 0)
    hub.close()


def test_the_loopback_guard_covers_the_relay(tmp_path: Path):
    app, hub = build(tmp_path, FakeFeed(n=0))
    c = client(app)
    r = c.get("/api/unit/live", headers={"Host": "evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "loopback only"
    assert c.get("/api/unit/live/recent", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/unit/live/recent", headers={"Origin": BASE}).status_code == 200
    assert hub.stats()["subscribers"] == 0 and hub.stats()["upstream"] == "off"  # a refusal never opened a seat
    hub.close()
