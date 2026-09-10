"""The digest cache: one JSON file per replica, one entry per (session, week).

A digest costs a model call (10–50 s on gemma3:27b), so it is kept and reused
until the session's ``event_count`` for that week changes — the same signal
``session_weeks`` carries — and ``--force`` throws the check away. The key is
``<session record id>@<iso week>`` rather than the bare session id because one
session can have a row in two weeks (a Sunday-night session is two ledgers),
and the two digests are of different events.

Nothing is deleted here: re-dreaming a week overwrites its *page*, but the
cache keeps every digest it ever wrote, and an entry is only ever replaced
by a newer digest of the same session and week. The file is written
atomically (temp file + ``os.replace``) the way ``sync.json`` is.

Beside the digests the file keeps a small memo of *failures*: a session
whose reply was not JSON, with the event count it was tried at and how many
times. It exists so a session the model will not digest is not repaid every
night; a digest that lands clears it, and a different event count starts it
over. A transport failure (the host down) is never noted — that is retried.

One more thing lives here: ``RunLock``, the guard that keeps two
``structor-dream`` runs off the same replica at once. Every ``save`` writes
the whole cache, so two concurrent runs would each erase the other's new
digests; a launchd job and a hand run at the same hour is exactly how that
happens. The lock is an ``flock`` on a file beside the cache, held for the
life of the process.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

LOCK_FILE = "dream.lock"


class DigestState:
    """Load, query and atomically save the cache at ``path``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.entries, self.failures = self._load()

    @staticmethod
    def key(session: str, week: str) -> str:
        return f"{session}@{week}"

    def _load(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}, {}
        if not isinstance(data, dict):
            return {}, {}
        entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
        failures = data.get("failures") if isinstance(data.get("failures"), dict) else {}
        return entries, failures

    def get(self, session: str, week: str) -> dict[str, Any] | None:
        with self._lock:
            return self.entries.get(self.key(session, week))

    def put(self, session: str, week: str, event_count: int, ts: str, digest: dict[str, Any]) -> None:
        with self._lock:
            self.entries[self.key(session, week)] = {
                "session": session, "week": week, "event_count": int(event_count), "ts": ts, "digest": digest,
            }
            self.failures.pop(self.key(session, week), None)  # it digested: the memo has served

    def for_week(self, week: str) -> dict[str, dict[str, Any]]:
        """session → entry, for every cached digest of ``week``."""
        with self._lock:
            return {e["session"]: e for e in self.entries.values() if e.get("week") == week}

    # ---- the failure memo ----

    def attempts(self, session: str, week: str, event_count: int) -> int:
        """How many times this session, at this event count, answered without JSON; 0 when it never did or has grown."""
        with self._lock:
            memo = self.failures.get(self.key(session, week))
            if not memo or int(memo.get("event_count") or -1) != int(event_count):
                return 0
            return int(memo.get("attempts") or 0)

    def note_failure(self, session: str, week: str, event_count: int, error: str) -> int:
        """Count one non-JSON reply for (session, week) at ``event_count``; a new count restarts at one. Returns the total."""
        with self._lock:
            attempts = self.attempts(session, week, event_count) + 1
            self.failures[self.key(session, week)] = {
                "session": session, "week": week, "event_count": int(event_count), "attempts": attempts, "error": error,
            }
            return attempts

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="digest_state.", suffix=".json.tmp", dir=self.path.parent)
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 1, "entries": self.entries, "failures": self.failures}, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)

    def __len__(self) -> int:
        return len(self.entries)


class RunLock:
    """One ``structor-dream`` run per replica: an exclusive, non-blocking ``flock`` on ``path``.

    ``acquire`` returns ``True`` and records this pid in the file, or ``False``
    when another process holds it (``holder`` then says which pid, from what
    that process wrote). The kernel drops the lock when the process ends,
    however it ends, so a crashed run never leaves a stale lock behind — the
    file itself stays, and that is fine. ``release`` is for callers that go
    on living, such as a test that runs several commands in one process.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self.held = False
        self.holder = ""

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 — kept open on purpose: the lock lives as long as the handle
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            self.holder = fh.read(64).strip()
            fh.close()
            self.held = False
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        self.held = True
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        self.held = False
