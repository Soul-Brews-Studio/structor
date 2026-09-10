"""Minimal PocketBase client for the replica.

Three endpoints, ``httpx`` only: superuser login, paged record listing ordered
by a ``(stamp, id)`` cursor, and the realtime SSE stream for the custom
``structor/live`` topic.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

RETRY_429 = (1.0, 3.0, 8.0)
MAX_PER_PAGE = 1000  # PocketBase tools/search/provider.go MaxPerPage


@dataclass
class Cursor:
    stamp: str  # value of the ordering column (created or updated) of the last row seen
    id: str  # tiebreak


def pb_quote(v: str) -> str:
    """Quote a value for the PocketBase filter grammar (single quotes, escaped)."""
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


class PB:
    def __init__(self, url: str, email: str, password: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self._email = email
        self._password = password
        self._token = ""
        self._lock = threading.Lock()
        self._client = httpx.Client(base_url=self.url, timeout=timeout)

    # ---- auth -------------------------------------------------------------

    def login(self) -> str:
        with self._lock:
            r = self._client.post(
                "/api/collections/_superusers/auth-with-password",
                json={"identity": self._email, "password": self._password},
            )
            if r.status_code != 200:
                raise RuntimeError(f"login {r.status_code} at {self.url}")
            self._token = r.json()["token"]
            return self._token

    def bearer(self) -> str:
        """A valid superuser token (logs in when needed). Used by the realtime proxy."""
        return self._token or self.login()

    def invalidate(self) -> None:
        self._token = ""

    def _request(self, method: str, path: str, *, attempt: int = 0, **kw: Any) -> httpx.Response:
        if not self._token:
            self.login()
        headers = dict(kw.pop("headers", {}) or {})
        headers["Authorization"] = self._token
        r = self._client.request(method, path, headers=headers, **kw)
        if r.status_code == 401 and attempt == 0:
            self._token = ""
            return self._request(method, path, attempt=1, headers=headers, **kw)
        if r.status_code == 429 and attempt < len(RETRY_429):
            time.sleep(RETRY_429[attempt])
            return self._request(method, path, attempt=attempt + 1, headers=headers, **kw)
        return r

    def get_json(self, path: str, **params: Any) -> Any:
        r = self._request("GET", path, params=params or None)
        if r.status_code != 200:
            raise RuntimeError(f"{path} → {r.status_code} {r.text[:200]}")
        return r.json()

    # ---- records ----------------------------------------------------------

    def page_after(self, collection: str, stamp_field: str, cursor: Cursor | None, per_page: int = MAX_PER_PAGE) -> list[dict]:
        """One page of ``collection`` strictly after ``cursor``, ordered by (stamp_field, id)."""
        params: dict[str, Any] = {"perPage": min(per_page, MAX_PER_PAGE), "sort": f"{stamp_field},id", "skipTotal": 1}
        if cursor:
            s, i = pb_quote(cursor.stamp), pb_quote(cursor.id)
            params["filter"] = f"({stamp_field} > {s}) || ({stamp_field} = {s} && id > {i})"
        page = self.get_json(f"/api/collections/{collection}/records", **params)
        return list(page.get("items", []))

    def count(self, collection: str) -> int:
        page = self.get_json(f"/api/collections/{collection}/records", perPage=1, fields="id")
        return int(page.get("totalItems", 0))

    def status(self) -> dict:
        return self.get_json("/api/structor/status")

    # ---- realtime ---------------------------------------------------------

    def live(self, topic: str, on_message: Callable[[Any], None], stop: threading.Event) -> None:
        """Subscribe to the SSE stream and call ``on_message`` for every ``topic`` frame.

        PocketBase protocol: ``GET /api/realtime`` opens the stream and sends
        ``PB_CONNECT {clientId}``; ``POST /api/realtime {clientId, subscriptions}``
        (with Authorization) selects topics. Returns when the stream closes or
        ``stop`` is set.
        """
        if not self._token:
            self.login()
        with self._client.stream("GET", "/api/realtime", headers={"Accept": "text/event-stream"}, timeout=None) as r:
            if r.status_code != 200:
                raise RuntimeError(f"realtime {r.status_code}")
            for event, data in _sse_frames(r.iter_lines(), stop):
                if event == "PB_CONNECT":
                    client_id = json.loads(data or "{}").get("clientId", "")
                    sub = self._request("POST", "/api/realtime", json={"clientId": client_id, "subscriptions": [topic]})
                    if sub.status_code >= 300:
                        raise RuntimeError(f"subscribe {sub.status_code}")
                elif event == topic and data:
                    try:
                        on_message(json.loads(data))
                    except ValueError:
                        pass


def _sse_frames(lines: Iterator[str], stop: threading.Event) -> Iterator[tuple[str, str]]:
    event, data = "", []
    for line in lines:
        if stop.is_set():
            return
        if line == "":
            if event or data:
                yield event, "\n".join(data)
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
