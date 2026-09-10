"""Vectors, ORM-style: LanceDB's embedding API over an Ollama pool.

The replica's ``events`` table stays exactly as PocketBase shaped it. Vectors
live in a sibling table, ``event_vectors``, whose model declares the source
column and the vector column so LanceDB embeds on ``add()`` and on
``search(text)``::

    func = pool(["http://gpu1:11434", "http://gpu2:11434"], model="bge-m3")
    EventVector = event_vector_model(func)          # text -> vector, 1024 dims
    table.add([...rows with text...])               # embedded by the pool
    table.search("launchd agent")                   # query embedded the same way

Space: ``bge-m3`` (1024, cosine) over ``text[:2000]`` — the same space
lanceglass uses, so vectors from either tool are comparable. The pool shards
every batch across its hosts with one thread per host; a host that fails is
skipped for that batch and the shard is retried on the others.

Nothing here runs unless ``ollama_urls`` is configured
(``~/.config/structor/lance.json`` or ``STRUCTOR_OLLAMA_URLS``); the replica
and the admin work without vectors, on the full-text index alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from lancedb.embeddings import EmbeddingFunction, get_registry, register
from lancedb.pydantic import LanceModel, Vector
from pydantic import Field

from .sync import Replica, table_names

MODEL = "bge-m3"
DIMENSION = 1024
DISTANCE = "cosine"
TEXT_POLICY = "event.text[:2000]@v1"
SPACE_ID = f"ollama-{MODEL}-{DIMENSION}-{DISTANCE}-text-v1"
TEXT_CAP = 2000
TABLE = "event_vectors"

CONVERSATIONAL = "role <> '' AND text <> ''"


# ---------------------------------------------------------------- configuration


def ollama_urls() -> list[str]:
    """The Ollama pool: ``STRUCTOR_OLLAMA_URLS`` (comma list) or ``~/.config/structor/lance.json`` ``ollama_urls``."""
    env = os.environ.get("STRUCTOR_OLLAMA_URLS", "")
    if env.strip():
        return [u.strip().rstrip("/") for u in env.split(",") if u.strip()]
    conf = Path(os.environ.get("STRUCTOR_CONF_DIR", Path.home() / ".config" / "structor")) / "lance.json"
    try:
        j = json.loads(conf.read_text())
    except (OSError, ValueError):
        return []
    urls = j.get("ollama_urls") or []
    if isinstance(urls, str):
        urls = urls.split(",")
    return [str(u).strip().rstrip("/") for u in urls if str(u).strip()]


def embedding_model() -> str:
    conf = Path(os.environ.get("STRUCTOR_CONF_DIR", Path.home() / ".config" / "structor")) / "lance.json"
    try:
        return str(json.loads(conf.read_text()).get("embedding_model") or MODEL)
    except (OSError, ValueError):
        return MODEL


# ---------------------------------------------------------------- the pool


@register("ollama-pool")
class OllamaPool(EmbeddingFunction):
    """One embedding function, several Ollama hosts. Batches are split across hosts, one thread each."""

    name: str = MODEL
    hosts: list[str] = Field(default_factory=list)
    dims: int = DIMENSION
    batch: int = 64

    _clients: ClassVar[dict[str, Any]] = {}

    def ndims(self) -> int:
        return self.dims

    def _client(self, host: str) -> Any:
        c = OllamaPool._clients.get(host)
        if c is None:
            import ollama  # optional dependency, imported when a pool is used

            c = ollama.Client(host=host, timeout=120)
            OllamaPool._clients[host] = c
        return c

    def _embed_on(self, host: str, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = texts[i : i + self.batch]
            r = self._client(host).embed(model=self.name, input=chunk)
            out.extend(list(map(float, v)) for v in r["embeddings"])
        return out

    def compute_source_embeddings(self, texts: Iterable[str], *args: Any, **kwargs: Any) -> list[list[float]]:
        items = [str(t)[:TEXT_CAP] for t in texts]
        if not items:
            return []
        if not self.hosts:
            raise RuntimeError("no Ollama hosts configured (ollama_urls in ~/.config/structor/lance.json or STRUCTOR_OLLAMA_URLS)")
        hosts = list(self.hosts)
        # one shard per host, so two GPUs embed one batch in half the time
        shards = [items[i :: len(hosts)] for i in range(len(hosts))]
        results: list[list[list[float]] | None] = [None] * len(hosts)

        def run(idx: int) -> None:
            try:
                results[idx] = self._embed_on(hosts[idx], shards[idx])
            except Exception:  # noqa: BLE001 — a dead host is retried on the others below
                results[idx] = None

        with ThreadPoolExecutor(max_workers=len(hosts)) as ex:
            list(ex.map(run, range(len(hosts))))
        for idx, res in enumerate(results):
            if res is None:
                others = [h for j, h in enumerate(hosts) if j != idx and results[j] is not None] or hosts
                last: Exception | None = None
                for h in others:
                    try:
                        results[idx] = self._embed_on(h, shards[idx])
                        break
                    except Exception as e:  # noqa: BLE001
                        last = e
                if results[idx] is None:
                    raise RuntimeError(f"every Ollama host failed: {last}")
        # interleave back into the original order
        merged: list[list[float]] = [None] * len(items)  # type: ignore[list-item]
        for idx, res in enumerate(results):
            for k, vec in enumerate(res or []):
                merged[idx + k * len(hosts)] = vec
        return merged

    def compute_query_embeddings(self, query: str, *args: Any, **kwargs: Any) -> list[list[float]]:
        return self.compute_source_embeddings([query])


def pool(hosts: list[str] | None = None, model: str | None = None) -> OllamaPool:
    return get_registry().get("ollama-pool").create(name=model or embedding_model(), hosts=hosts or ollama_urls())


# ---------------------------------------------------------------- the model


def event_vector_model(func: EmbeddingFunction) -> type[LanceModel]:
    """``event_vectors`` bound to one embedding function (the vector column's dimension comes from it)."""

    class EventVector(LanceModel):
        event_id: str
        session: str = ""
        ts: str = ""
        role: str = ""
        text: str = func.SourceField()
        vector: Vector(func.ndims()) = func.VectorField()  # type: ignore[valid-type]
        space: str = SPACE_ID
        text_hash: str = ""

    return EventVector


def text_hash(text: str) -> str:
    return hashlib.sha256(text[:TEXT_CAP].encode()).hexdigest()[:16]


# ---------------------------------------------------------------- embedding runs


class Embedder:
    """Fills ``event_vectors`` for one replica: conversational events, newest first, resumable."""

    def __init__(self, replica: Replica, func: EmbeddingFunction | None = None):
        self.replica = replica
        self.func = func or pool()
        self.model = event_vector_model(self.func)
        self._lock = threading.Lock()

    def table(self) -> Any:
        db = self.replica.db()
        if TABLE in table_names(db):
            return db.open_table(TABLE)
        return db.create_table(TABLE, schema=self.model, exist_ok=True)

    def embedded_ids(self) -> set[str]:
        t = self.table()
        if t.count_rows() == 0:
            return set()
        return {r["event_id"] for r in t.search().select(["event_id"]).limit(10_000_000).to_list()}

    def pending(self, limit: int | None = None, where: str = "") -> list[dict]:
        """Conversational events without a vector, newest first."""
        done = self.embedded_ids()
        pred = CONVERSATIONAL + (f" AND ({where})" if where else "")
        events = self.replica.db().open_table("events")
        rows = events.search().where(pred).select(["id", "session", "ts", "role", "text"]).limit(10_000_000).to_list()
        rows = [r for r in rows if r["id"] not in done]
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows[:limit] if limit else rows

    def run(self, limit: int | None = None, batch: int = 128, where: str = "", log: Callable[[str], None] = print) -> int:
        """Embed up to ``limit`` pending rows in batches. Returns rows written."""
        with self._lock:
            todo = self.pending(limit, where)
            if not todo:
                log("nothing to embed")
                return 0
            t = self.table()
            written = 0
            t0 = time.time()
            for i in range(0, len(todo), batch):
                chunk = todo[i : i + batch]
                rows = [
                    {"event_id": r["id"], "session": r["session"], "ts": r["ts"], "role": r["role"], "text": r["text"][:TEXT_CAP],
                     "space": SPACE_ID, "text_hash": text_hash(r["text"])}
                    for r in chunk
                ]
                t.add(rows)  # LanceDB embeds `text` through the pool on the way in
                written += len(rows)
                rate = written / max(time.time() - t0, 1e-6)
                log(f"embedded {written}/{len(todo)} ({rate:.0f} rows/s, {len(self.func.hosts) if hasattr(self.func, 'hosts') else 1} host(s))")
            return written

    def search(self, query: str, limit: int = 20, where: str = "", mode: str = "vector") -> list[dict]:
        """Nearest events to ``query``; ``mode`` = vector | hybrid (vector + FTS on this table, RRF-fused)."""
        t = self.table()
        if mode == "hybrid":
            if not any(i.name == "text_idx" for i in t.list_indices()):
                from lancedb.index import FTS

                t.create_index("text", config=FTS(), replace=True)
            q = t.search(query, query_type="hybrid")
        else:
            q = t.search(query)
        if where:
            q = q.where(where)
        return q.limit(limit).to_list()
