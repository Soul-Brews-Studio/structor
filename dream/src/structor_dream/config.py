"""Where dreams go, where their cache lives, and what week it is — configuration, never source.

The dream directory is configuration exactly as the wiki directory is in
``structor_lance.wiki``: ``STRUCTOR_DREAM_DIR``, else ``dream_dir`` in
``~/.config/structor/lance.json``, else ``<wiki_dir>/dreams``. No path of a
private notes tree ever appears in this package, which is mirrored publicly.

The digest cache is not a page and not configuration: it lives beside the
replica's Lance tables (``<data>/<target>/dreams/digest_state.json``), because
it is derived from that replica's rows and means nothing next to another one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from structor_lance import wiki as wiki_table
from structor_lance.sync import Replica

TZ = ZoneInfo("Asia/Bangkok")  # the fleet's day, not UTC's — same choice as rag.plan()
CACHE_DIRNAME = "dreams"
STATE_FILE = "digest_state.json"
WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def conf() -> dict[str, Any]:
    """``~/.config/structor/lance.json`` as a dict (``STRUCTOR_CONF_DIR`` overrides the directory); ``{}`` when absent."""
    path = Path(os.environ.get("STRUCTOR_CONF_DIR", Path.home() / ".config" / "structor")) / "lance.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def dream_dir() -> str:
    """``STRUCTOR_DREAM_DIR`` > lance.json ``dream_dir`` > ``<wiki_dir>/dreams`` > ``""`` (nothing configured)."""
    env = os.environ.get("STRUCTOR_DREAM_DIR", "").strip()
    if env:
        return env
    configured = str(conf().get("dream_dir") or "").strip()
    if configured:
        return configured
    wiki = wiki_table.wiki_dir()
    return str(Path(wiki).expanduser() / CACHE_DIRNAME) if wiki else ""


def cache_dir(replica: Replica) -> Path:
    """The replica-local directory holding the digest cache."""
    return Path(replica.dir) / CACHE_DIRNAME


def state_path(replica: Replica) -> Path:
    return cache_dir(replica) / STATE_FILE


def under(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or inside it (both resolved, so a symlinked dream_dir counts)."""
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- weeks


def now() -> datetime:
    return datetime.now(tz=TZ)


def iso_week(day: date) -> str:
    """``2026-09-10`` → ``2026-W37``: the label ``events.iso_week`` and ``session_weeks.iso_week`` use."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def current_week(at: datetime | None = None) -> str:
    return iso_week((at or now()).astimezone(TZ).date())


def parse_week(label: str) -> tuple[int, int]:
    """``2026-W37`` → ``(2026, 37)``; anything else is a ``ValueError``, because the label goes into a predicate."""
    m = WEEK.match(label.strip())
    if not m:
        raise ValueError(f"not an ISO week label: {label!r} (expected YYYY-Www)")
    year, week = int(m.group(1)), int(m.group(2))
    date.fromisocalendar(year, week, 1)  # raises on week 54, week 0
    return year, week


def previous_week(label: str) -> str:
    year, week = parse_week(label)
    return iso_week(date.fromisocalendar(year, week, 1) - timedelta(days=7))


def week_monday(label: str) -> date:
    year, week = parse_week(label)
    return date.fromisocalendar(year, week, 1)
