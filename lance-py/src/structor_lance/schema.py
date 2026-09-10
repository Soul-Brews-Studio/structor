"""The five replicated tables as ORM-style models.

Each class is a ``lancedb.pydantic.LanceModel``: a Pydantic model that also
*is* the Arrow schema of its Lance table (``Model.to_arrow_schema()``), the row
type you insert, and the row type you read back. Column names, order, types and
nullability match the Bun edition (``app/lance/src/sync.ts``) exactly —
``str | None`` is a nullable Utf8, ``float | None`` Float64, ``bool | None``
Bool, ``created`` last — so both backends can open the same Lance directory.

PocketBase stays the source of truth. ``stamp`` names the column a replica
pages by: ``created`` for append-only tables, ``updated`` for rows that are
rewritten after every ingest.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from lancedb.pydantic import LanceModel
from pydantic import Field

Stamp = Literal["created", "updated"]


class Table(LanceModel):
    """Base for a replicated table: PocketBase's record id is the merge key."""

    __table__: ClassVar[str]
    __stamp__: ClassVar[Stamp] = "created"
    __fts__: ClassVar[str | None] = None  # column with a full-text index, if any

    # nullable like every other column so the Arrow schema is byte-identical to the Bun edition's; a row always has one
    id: str | None = Field(default="", description="PocketBase record id; merge key")


class Project(Table):
    __table__ = "projects"
    __stamp__ = "updated"

    path: str | None = ""
    name: str | None = ""
    encoded_dir: str | None = ""
    cwd: str | None = Field(default="", description="authoritative path learned from transcripts")
    host: str | None = ""
    created: str | None = Field(default="", description="PocketBase autodate, 'YYYY-MM-DD HH:MM:SS.mmmZ'")
    updated: str | None = ""


class Session(Table):
    __table__ = "sessions"
    __stamp__ = "updated"

    session_id: str | None = ""
    project: str | None = Field(default="", description="projects.id")
    file_path: str | None = ""
    tier: str | None = ""
    byte_offset: float | None = 0
    file_size: float | None = 0
    file_mtime: float | None = 0
    lines_seen: float | None = 0
    event_count: float | None = 0
    first_ts: str | None = ""
    last_ts: str | None = ""
    first_prompt: str | None = ""
    git_branch: str | None = ""
    cwd: str | None = ""
    model: str | None = ""
    created: str | None = ""
    updated: str | None = ""


class Event(Table):
    __table__ = "events"
    __stamp__ = "created"
    __fts__ = "text"

    session: str | None = Field(default="", description="sessions.id")
    uuid: str | None = ""
    parent_uuid: str | None = ""
    type: str | None = ""
    role: str | None = ""
    ts: str | None = ""
    iso_week: str | None = ""
    text: str | None = ""
    tools: str | None = Field(default="", description="JSON array, as text")
    model: str | None = ""
    sidechain: bool | None = False
    line_no: float | None = 0
    raw_bytes: float | None = 0
    created: str | None = ""


class SessionWeek(Table):
    __table__ = "session_weeks"
    __stamp__ = "updated"

    session: str | None = ""
    project: str | None = ""
    iso_week: str | None = ""
    event_count: float | None = 0
    user_count: float | None = 0
    assistant_count: float | None = 0
    tool_count: float | None = 0
    first_ts: str | None = ""
    last_ts: str | None = ""
    created: str | None = ""
    updated: str | None = ""


class ImportRun(Table):
    __table__ = "import_runs"
    __stamp__ = "created"

    session: str | None = ""
    project: str | None = ""
    from_offset: float | None = 0
    to_offset: float | None = 0
    lines: float | None = 0
    inserted: float | None = 0
    skipped: float | None = 0
    host: str | None = ""
    writer: str | None = ""
    created: str | None = ""


TABLES: tuple[type[Table], ...] = (Project, Session, Event, SessionWeek, ImportRun)
BY_NAME: dict[str, type[Table]] = {t.__table__: t for t in TABLES}


def model_for(name: str) -> type[Table]:
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown table {name!r}; have {', '.join(BY_NAME)}") from None


def base_type(ann: object) -> object:
    """``str | None`` → ``str``: the columns are nullable so a Bun-written table opens here, but rows are always filled."""
    import types
    import typing

    if isinstance(ann, types.UnionType) or typing.get_origin(ann) is typing.Union:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return args[0] if len(args) == 1 else ann
    return ann


def from_record(model: type[Table], rec: dict) -> Table:
    """Shape a PocketBase record into the model: unknown keys dropped, JSON fields stringified, numbers coerced."""
    import json

    out: dict = {}
    for name, field in model.model_fields.items():
        v = rec.get(name)
        ann = base_type(field.annotation)
        if ann is str:
            out[name] = "" if v is None else (v if isinstance(v, str) else json.dumps(v, separators=(",", ":")))
        elif ann is float:
            try:
                out[name] = float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                out[name] = 0.0
        elif ann is bool:
            out[name] = bool(v)
        else:
            out[name] = v
    return model(**out)
