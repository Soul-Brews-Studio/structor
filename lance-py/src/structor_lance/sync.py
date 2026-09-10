"""One replica per target: five Lance tables mirroring PocketBase's, pulled
through the records API in ``(stamp, id)`` order and upserted by ``id``.
PocketBase stays the source of truth; this side only ever catches up.

Cursors: append-only tables (events, import_runs) page by ``created``;
rewritten ones (projects, sessions, session_weeks) page by ``updated`` so a
changed row comes around again. PocketBase ids are random, so every run
starts ``REWIND_MS`` before the saved cursor (upserts make the overlap free)
to cover a row committed in the same millisecond with a lower id.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lancedb
from lancedb.index import FTS

from .pb import PB, Cursor
from .schema import TABLES, Table, from_record
from .targets import Target

REWIND_MS = 2000
OPTIMIZE_EVERY_S = 300
PRUNE_AFTER = timedelta(hours=1)  # versions older than this are dropped; the 7-day default is for readers that pin them


def table_names(db: lancedb.DBConnection) -> set[str]:
    """Names in the database. ``list_tables()`` (0.38) returns a page object with ``.tables``; older ``table_names()`` a list."""
    if hasattr(db, "list_tables"):
        page = db.list_tables()
        names = getattr(page, "tables", page)
        return {str(n) for n in names}
    return set(db.table_names())


STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%SZ", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")


def stamp_to_dt(stamp: str) -> datetime | None:
    """'2026-09-09 15:00:00.100Z' (or without the fraction / the Z) → aware datetime, or None."""
    s = stamp.strip().replace("T", " ")
    for fmt in STAMP_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def dt_to_stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def rewind(c: Cursor, ms: int) -> Cursor:
    """Cursor ``ms`` earlier than ``c``, id cleared so every row at that stamp qualifies."""
    dt = stamp_to_dt(c.stamp)
    if dt is None:
        return c
    return Cursor(dt_to_stamp(dt - timedelta(milliseconds=ms)), "")


def cmp_cursor(a: Cursor, b: Cursor) -> int:
    if a.stamp != b.stamp:
        return -1 if a.stamp < b.stamp else 1
    return 0 if a.id == b.id else (-1 if a.id < b.id else 1)


class Replica:
    """A target's Lance directory, its sync state, and the pull loop."""

    def __init__(self, target: Target, data_root: Path):
        self.target = target
        self.dir = Path(data_root) / target.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pb = PB(target.url, target.email, target.password)
        self.state: dict[str, Any] = self._load_state()
        self._db: lancedb.DBConnection | None = None
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()  # the follow thread writes state, request threads read it
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._optimize_due: dict[str, float] = {}

    # ---- state ------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.dir / "sync.json"

    def _load_state(self) -> dict[str, Any]:
        empty = {"target": self.target.name, "url": self.target.url, "tables": {}, "lastRun": "", "lastError": "", "lastDurationMs": 0}
        if not self.state_path.exists():
            return empty
        try:
            return {**empty, **json.loads(self.state_path.read_text())}
        except (OSError, ValueError):
            return empty

    def save_state(self) -> None:
        """Atomic write of sync.json under the state lock. A failure is recorded, never raised."""
        with self._state_lock:
            try:
                fd, tmp = tempfile.mkstemp(prefix="sync.", suffix=".json.tmp", dir=self.dir)
                with os.fdopen(fd, "w") as f:
                    json.dump(self.state, f, indent=2)
                os.replace(tmp, self.state_path)
            except OSError as e:
                self.state["lastError"] = f"state: {e}"[:500]

    def state_copy(self) -> dict[str, Any]:
        """A snapshot for readers on other threads (the admin's /api/status)."""
        with self._state_lock:
            return copy.deepcopy(self.state)

    def mark_fts_built(self, table: str) -> None:
        with self._state_lock:
            st = self.state["tables"].setdefault(table, {"cursor": None, "rows": 0, "lastPull": ""})
            st["ftsBuilt"] = True
            self.save_state()

    # ---- tables -----------------------------------------------------------

    def db(self) -> lancedb.DBConnection:
        if self._db is None:
            self._db = lancedb.connect(str(self.dir))
        return self._db

    def table(self, model: type[Table]) -> lancedb.table.Table:
        db = self.db()
        if model.__table__ in table_names(db):
            return db.open_table(model.__table__)
        return db.create_table(model.__table__, schema=model, exist_ok=True)

    # ---- sync -------------------------------------------------------------

    def sync_now(self) -> int:
        """Pull everything new for every table; returns rows upserted. Serialised."""
        with self._run_lock:
            t0 = time.time()
            total = 0
            try:
                for model in TABLES:
                    total += self._sync_table(model)
                with self._state_lock:
                    self.state["lastError"] = ""
            except Exception as e:  # noqa: BLE001 — the loop must survive anything
                with self._state_lock:
                    self.state["lastError"] = str(e)[:500]
            with self._state_lock:
                self.state["lastRun"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                self.state["lastDurationMs"] = int((time.time() - t0) * 1000)
                self.save_state()
            return total

    def _sync_table(self, model: type[Table]) -> int:
        tbl = self.table(model)
        with self._state_lock:
            st = self.state["tables"].setdefault(model.__table__, {"cursor": None, "rows": 0, "lastPull": ""})
            saved = Cursor(**st["cursor"]) if st.get("cursor") else None
        cursor = rewind(saved, REWIND_MS) if saved else None
        pulled = 0
        while True:
            items = self.pb.page_after(model.__table__, model.__stamp__, cursor)
            if not items:
                break
            rows = [from_record(model, r) for r in items]
            tbl.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)
            last = items[-1]
            cursor = Cursor(str(last.get(model.__stamp__) or last.get("created", "")), str(last["id"]))
            with self._state_lock:
                if saved is None or cmp_cursor(cursor, saved) > 0:  # never move the saved cursor backwards
                    saved = cursor
                    st["cursor"] = {"stamp": cursor.stamp, "id": cursor.id}
                st["rows"] = int(st.get("rows", 0)) + len(rows)
                st["lastPull"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                self.save_state()
            pulled += len(rows)
            if len(items) < 1000:
                break
        if model.__fts__ and pulled > 0:
            self._ensure_fts(model, tbl, st)
        if pulled > 0:
            self._maybe_optimize(model, tbl)
        return pulled

    def _maybe_optimize(self, model: type[Table], tbl: Any) -> None:
        """Compact fragments, fold new rows into the FTS index, prune old versions — every table, every few minutes.

        Every merge page is a new version and a new fragment; left alone a busy
        day is thousands of versions and tens of gigabytes.
        """
        due = self._optimize_due.get(model.__table__, 0.0)
        if time.time() < due:
            return
        self._optimize_due[model.__table__] = time.time() + OPTIMIZE_EVERY_S
        tbl.optimize(cleanup_older_than=PRUNE_AFTER)

    def _ensure_fts(self, model: type[Table], tbl: Any, st: dict) -> None:
        if not st.get("ftsBuilt"):
            if tbl.count_rows() == 0:
                return
            tbl.create_index(model.__fts__, config=FTS(), replace=True)
            with self._state_lock:
                st["ftsBuilt"] = True
        # new rows are folded into the index by _maybe_optimize()

    # ---- follow -----------------------------------------------------------

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def follow(self, interval_s: float, log: Callable[[str], None]) -> None:
        """Sync now, then again on every live message or every ``interval_s``. Blocks; run in a thread."""
        threading.Thread(target=self._live_loop, args=(log,), name=f"live-{self.target.name}", daemon=True).start()
        last_logged = ""
        while not self._stop.is_set():
            try:
                n = self.sync_now()
                if n > 0:
                    log(f"{self.target.name}: +{n} rows")
                elif self.state["lastError"] and self.state["lastError"] != last_logged:
                    log(f"{self.target.name}: {self.state['lastError']}")
                last_logged = self.state["lastError"]
            except Exception as e:  # noqa: BLE001
                log(f"{self.target.name}: {e}")
            self._wake.wait(interval_s)
            self._wake.clear()
            time.sleep(1.5)  # let a burst of ingests coalesce into one pull

    def _live_loop(self, log: Callable[[str], None]) -> None:
        while not self._stop.is_set():
            try:
                self.pb.live("structor/live", lambda _msg: self.wake(), self._stop)
            except Exception as e:  # noqa: BLE001
                log(f"live feed {self.target.name}: {e}")
            self._stop.wait(5)

    # ---- lag --------------------------------------------------------------

    def lag(self) -> dict[str, dict[str, int]]:
        """Rows on the PocketBase side minus rows here, per table."""
        out: dict[str, dict[str, int]] = {}
        for model in TABLES:
            try:
                remote = self.pb.count(model.__table__)
            except Exception:  # noqa: BLE001
                remote = -1
            try:
                local = self.table(model).count_rows()
            except Exception:  # noqa: BLE001
                local = -1
            out[model.__table__] = {"remote": remote, "local": local}
        return out
