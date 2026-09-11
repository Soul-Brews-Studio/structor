"""The live relay: one upstream ``structor/live`` subscription per target, fanned out to N browsers.

PocketBase publishes one ``LiveMessage`` per ingest on its custom realtime
topic (``app/internal/ingest/ingest.go``): which session grew, in which
project, and up to 40 of the new rows. A browser cannot subscribe to it
directly — the topic is superuser-only and ``EventSource`` sends no headers —
so the admin holds the subscription with the replica's own credentials and
re-publishes it as a plain SSE stream on ``GET /api/{target}/live``.

The shape is Stoa's SharedTail: one upstream per target, started by the first
subscriber and stopped a grace period after the last one leaves; a ring buffer
of the last 256 messages with monotonic ids, so a browser that reconnects with
``Last-Event-ID`` (or a page that opens with ``?last_id=``) gets what it missed
and nothing twice; a heartbeat comment every 15 s of silence; and a cap of 24
subscribers so a forgotten tab farm cannot pin the process.

Only ``PB.live`` reads the upstream (``pb.py``); this module never opens a
socket of its own. ``LiveHub`` is thread-safe: the upstream thread publishes,
request threads subscribe, the timer thread stops.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

TOPIC = "structor/live"

BUFFER = 256            # messages kept for replay; at one ingest every ~12 s that is most of an hour
MAX_SUBSCRIBERS = 24    # the 25th browser gets a 503, not a slower stream for everyone
GRACE_S = 60.0          # how long the upstream outlives the last subscriber (a reload is not a departure)
RECONNECT_S = 5.0       # backoff after an upstream error, the same as Replica._live_loop
SHORT_STREAM_S = 1.0    # a stream the server closed sooner than this is treated as an error and backed off
HEARTBEAT_S = 15.0      # an SSE comment after this much silence keeps proxies and browsers from giving up
POLL_S = 0.5            # how often a waiting stream looks at its stop flag (a gone browser frees its seat this fast)
SUBSCRIBER_QUEUE = 512  # a subscriber this far behind is dropped; its EventSource reconnects and replays

SSE_HEADERS = {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
}

Live = Callable[[str, Callable[[Any], None], threading.Event], None]


class Full(Exception):
    """The hub already has ``MAX_SUBSCRIBERS`` subscribers."""


class LiveHub:
    """One target's upstream subscription, its ring buffer, and its subscribers.

    ``live`` is the blocking upstream call — ``PB.live(topic, on_message, stop)``
    by default, resolved from the replica at each (re)start so a test can patch
    ``replica.pb.live`` whenever it likes. ``grace_s`` and ``reconnect_s`` are
    instance attributes for the same reason.
    """

    def __init__(self, replica: Any, *, live: Live | None = None,
                 grace_s: float | None = None, reconnect_s: float | None = None):
        self.replica = replica
        self.name: str = replica.target.name
        self._live = live
        self.grace_s = GRACE_S if grace_s is None else grace_s
        self.reconnect_s = RECONNECT_S if reconnect_s is None else reconnect_s
        self._lock = threading.Lock()
        self._buffer: deque[tuple[int, Any]] = deque(maxlen=BUFFER)
        self._next_id = 1
        self._subs: list[queue.Queue] = []
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None  # the running upstream thread's stop flag
        self._grace: threading.Timer | None = None
        self._upstream = "off"  # "off" | "on" | "error: …" — state, not thread liveness (see _run)
        self._closed = False

    # ---- subscribers --------------------------------------------------------

    def subscribe(self, last_id: int | None = None) -> queue.Queue:
        """A queue that receives every message from now on as ``(id, message)``.

        With ``last_id`` the queue is pre-filled with the buffered messages
        after it, so a reconnecting browser resumes where it stopped; without
        it the subscriber starts live (the page fetches ``recent()`` first).
        Raises ``Full`` at the cap. The upstream starts with the first subscriber.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("hub closed")
            if len(self._subs) >= MAX_SUBSCRIBERS:
                raise Full(f"{MAX_SUBSCRIBERS} live subscribers already; try again later")
            q: queue.Queue = queue.Queue()
            if last_id is not None:
                for item in self._buffer:
                    if item[0] > last_id:
                        q.put_nowait(item)
            self._subs.append(q)
            if self._grace is not None:  # a subscriber came back within the grace period
                self._grace.cancel()
                self._grace = None
            if self._thread is None:
                self._start_locked()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Forget ``q``; when it was the last one, arm the grace timer that stops the upstream."""
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
            if not self._subs and self._thread is not None and self._grace is None and not self._closed:
                self._grace = threading.Timer(self.grace_s, self._grace_expired)
                self._grace.daemon = True
                self._grace.start()

    def _grace_expired(self) -> None:
        with self._lock:
            self._grace = None
            if not self._subs:
                self._stop_locked()

    # ---- buffer -------------------------------------------------------------

    def recent(self, limit: int = 50) -> dict[str, Any]:
        """The last ``limit`` buffered messages, oldest first, and the id of the newest one seen."""
        with self._lock:
            items = list(self._buffer)[-max(0, limit):] if limit > 0 else []
            return {"messages": [m for _i, m in items], "last_id": self._next_id - 1}

    def stats(self) -> dict[str, Any]:
        """What ``/api/status`` reports under ``targets[].live``."""
        with self._lock:
            return {"subscribers": len(self._subs), "buffered": len(self._buffer), "upstream": self._upstream}

    def publish(self, message: Any) -> int:
        """Stamp ``message`` with the next id, buffer it, hand it to every subscriber. Returns the id."""
        with self._lock:
            item = (self._next_id, message)
            self._next_id += 1
            self._buffer.append(item)
            for q in list(self._subs):
                if q.qsize() >= SUBSCRIBER_QUEUE:  # nobody is reading that stream: end it, keep the rest fast
                    self._subs.remove(q)
                    q.put_nowait(None)
                    continue
                q.put_nowait(item)
            return item[0]

    # ---- upstream -----------------------------------------------------------

    def _start_locked(self) -> None:
        stop = threading.Event()
        self._stop = stop
        self._upstream = "on"
        self._thread = threading.Thread(target=self._run, args=(stop,), name=f"live-hub-{self.name}", daemon=True)
        self._thread.start()

    def _stop_locked(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._thread = None
        self._stop = None
        self._upstream = "off"

    def _run(self, stop: threading.Event) -> None:
        """The upstream loop: subscribe, deliver, resubscribe when the stream ends.

        PocketBase closes every realtime connection on a timer (30 min of
        life, 5 min idle), and ``PB.live`` returns cleanly when it does. Those
        closes are resubscribed at once: any ingest published while this thread
        waited would never reach the ring buffer, so no ``Last-Event-ID`` could
        replay it. Only a failure — an exception, or a stream that the server
        ended within ``SHORT_STREAM_S`` of opening, which is a refusal wearing a
        clean close — waits ``reconnect_s`` first, so a broken upstream never
        becomes a tight loop.

        PocketBase sends no keepalive on the realtime stream, so a thread told to
        stop may sit in the socket read until the next message (or the server's
        idle cutoff) before it notices. That is why the hub's state is set by
        whoever starts and stops the thread, not by the thread itself: a stopped
        thread is already "off" while it lingers, and a new subscriber gets a
        fresh thread meanwhile. The lingering one delivers nothing — ``PB.live``
        checks ``stop`` before every frame — and ends on its own.
        """
        live = self._live or self.replica.pb.live

        def on_message(message: Any) -> None:
            if not stop.is_set() and isinstance(message, dict):
                self.publish(message)

        while not stop.is_set():
            started = time.monotonic()
            try:
                self._set_upstream(stop, "on")
                live(TOPIC, on_message, stop)
                if stop.is_set():
                    break
                if time.monotonic() - started >= SHORT_STREAM_S:
                    continue  # the server's routine cutoff: straight back, before the next ingest
                self._set_upstream(stop, "error: stream closed at once, reconnecting")
            except Exception as e:  # noqa: BLE001 — every upstream failure is a state to show, never a dead loop
                self._set_upstream(stop, f"error: {str(e)[:200] or type(e).__name__}")
            stop.wait(self.reconnect_s)

    def _set_upstream(self, stop: threading.Event, state: str) -> None:
        with self._lock:
            if self._stop is stop:  # only the current thread speaks for the hub
                self._upstream = state

    # ---- shutdown -----------------------------------------------------------

    def close(self) -> None:
        """Stop the upstream and end every subscriber's stream (tests, process exit)."""
        with self._lock:
            self._closed = True
            if self._grace is not None:
                self._grace.cancel()
                self._grace = None
            self._stop_locked()
            subs, self._subs = self._subs, []
        for q in subs:
            q.put_nowait(None)


# ---- SSE framing ---------------------------------------------------------------


def frame(event_id: int, message: Any) -> bytes:
    """One SSE frame: ``id``, ``event: live`` and the message as a single JSON line."""
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: live\ndata: {data}\n\n".encode()


def sse_frames(hub: LiveHub, last_id: int | None = None, heartbeat_s: float | None = None,
               stop: threading.Event | None = None) -> Iterator[bytes]:
    """The body of ``GET /api/{t}/live``: subscribe, then frames and heartbeats until the client goes away.

    Blocking by design — ``admin.stream_off_threadpool`` pumps it from a thread
    of its own. ``stop`` is the pump's flag for "the browser is gone": the
    generator looks at it every ``POLL_S`` while it waits, and returns, so the
    seat is free within half a second of the disconnect — not at the next
    heartbeat, which is when the pump would otherwise get to close it. Either
    way the generator's ``finally`` is the one place a subscription ends. It
    subscribes at the first ``next()``, so the route checks the cap up front to
    answer 503; in the rare race where two browsers take the last slot at once
    the pump swallows ``Full`` and the later one sees an empty stream, then a
    503 when its EventSource retries.
    """
    q = hub.subscribe(last_id)
    wait = HEARTBEAT_S if heartbeat_s is None else heartbeat_s
    gone = stop if stop is not None else threading.Event()
    try:
        yield b": connected\n\n"  # something on the wire at once, so the browser fires `open` behind any buffer
        last_sent = time.monotonic()
        while not gone.is_set():
            due = last_sent + wait - time.monotonic()  # time left until the heartbeat is owed
            try:
                item = q.get(timeout=max(0.0, min(POLL_S, due)))
            except queue.Empty:
                if time.monotonic() - last_sent >= wait:
                    yield b": heartbeat\n\n"
                    last_sent = time.monotonic()
                continue
            if item is None:  # dropped as too slow, or the hub closed: end the stream, EventSource reconnects
                return
            yield frame(*item)
            last_sent = time.monotonic()
    finally:
        hub.unsubscribe(q)


def parse_last_id(header: str | None, query: str | None) -> int | None:
    """``Last-Event-ID`` (what EventSource sends on reconnect) first, else ``?last_id=`` (a page's first open)."""
    for raw in (header, query):
        if raw is None or not str(raw).strip():
            continue
        try:
            return max(0, int(str(raw).strip()))
        except ValueError:
            continue
    return None
