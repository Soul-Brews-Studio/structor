"""Vectors without a GPU: a fake embedding function stands in for the Ollama pool."""

from __future__ import annotations

import math
from pathlib import Path

from lancedb.embeddings import EmbeddingFunction, get_registry, register

from structor_lance.schema import Event, from_record
from structor_lance.sync import Replica
from structor_lance.targets import Target
from structor_lance.vectors import DIMENSION, SPACE_ID, Embedder, event_vector_model, text_hash

UNIT = Target("unit", "http://127.0.0.1:1", "e", "p")


@register("fake-bag")
class FakeBag(EmbeddingFunction):
    """Bag-of-characters vector in 8 dims: deterministic, no network, similar strings land close."""

    dims: int = 8

    def ndims(self) -> int:
        return self.dims

    def compute_source_embeddings(self, texts, *a, **k):
        out = []
        for t in texts:
            v = [0.0] * self.dims
            for ch in str(t).lower():
                v[ord(ch) % self.dims] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    def compute_query_embeddings(self, query, *a, **k):
        return self.compute_source_embeddings([query])


def fake() -> FakeBag:
    # LanceDB requires functions to come from the registry's create(), which records the args it re-hydrates with
    return get_registry().get("fake-bag").create(dims=8)


def seed(replica: Replica) -> None:
    t = replica.table(Event)
    rows = [
        {"id": "e1", "created": "c", "session": "s1", "ts": "2026-09-09 10:00:01.000Z", "role": "user", "text": "launchd agent so the tray survives a reboot"},
        {"id": "e2", "created": "c", "session": "s1", "ts": "2026-09-09 10:00:02.000Z", "role": "assistant", "text": "the tray is a launchd agent now"},
        {"id": "e3", "created": "c", "session": "s2", "ts": "2026-09-09 10:00:03.000Z", "role": "user", "text": "bananas and apples at the market"},
        {"id": "e4", "created": "c", "session": "s2", "ts": "2026-09-09 10:00:04.000Z", "role": "", "text": "hook attachment, not conversational"},
        {"id": "e5", "created": "c", "session": "s2", "ts": "2026-09-09 10:00:05.000Z", "role": "user", "text": ""},
    ]
    t.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute([from_record(Event, r) for r in rows])


def test_event_vector_model_binds_source_and_vector_columns():
    m = event_vector_model(fake())
    names = m.to_arrow_schema().names
    assert names == ["event_id", "session", "ts", "role", "text", "vector", "space", "text_hash"]
    assert str(m.to_arrow_schema().field("vector").type).startswith("fixed_size_list<item: float>[8]")


def test_embedder_is_resumable_and_skips_non_conversational_rows(tmp_path: Path):
    r = Replica(UNIT, tmp_path)
    seed(r)
    e = Embedder(r, fake())
    assert [x["id"] for x in e.pending()] == ["e3", "e2", "e1"]  # newest first, e4/e5 excluded
    assert e.run(limit=2, log=lambda s: None) == 2
    assert e.embedded_ids() == {"e3", "e2"}
    assert [x["id"] for x in e.pending()] == ["e1"]
    assert e.run(log=lambda s: None) == 1
    assert e.run(log=lambda s: None) == 0
    rows = e.table().search().select(["event_id", "space", "text_hash"]).limit(10).to_list()
    assert {x["space"] for x in rows} == {SPACE_ID}
    assert next(x for x in rows if x["event_id"] == "e1")["text_hash"] == text_hash("launchd agent so the tray survives a reboot")


def test_vector_search_ranks_the_similar_text_first(tmp_path: Path):
    r = Replica(UNIT, tmp_path)
    seed(r)
    e = Embedder(r, fake())
    e.run(log=lambda s: None)
    hits = e.search("launchd tray reboot", limit=3)
    assert hits[0]["event_id"] in {"e1", "e2"} and hits[-1]["event_id"] == "e3"
    scoped = e.search("launchd tray reboot", limit=3, where="session = 's2'")
    assert [h["event_id"] for h in scoped] == ["e3"]
    hybrid = e.search("bananas", limit=2, mode="hybrid")
    assert hybrid[0]["event_id"] == "e3"


def test_pool_shards_a_batch_across_hosts_and_keeps_order(monkeypatch):
    calls: list[tuple[str, list[str]]] = []

    class FakeClient:
        def __init__(self, host):
            self.host = host

        def embed(self, model, input):
            calls.append((self.host, list(input)))
            return {"embeddings": [[float(len(t))] * DIMENSION for t in input]}

    p = get_registry().get("ollama-pool").create(name="bge-m3", hosts=["http://a:11434", "http://b:11434"], batch=2)
    monkeypatch.setattr(p, "_client", lambda host: FakeClient(host))
    out = p.compute_source_embeddings(["x", "yy", "zzz", "wwww", "vvvvv"])
    assert [v[0] for v in out] == [1.0, 2.0, 3.0, 4.0, 5.0]  # original order restored after sharding
    hosts_used = {h for h, _ in calls}
    assert hosts_used == {"http://a:11434", "http://b:11434"}
    assert p.ndims() == DIMENSION and p.compute_query_embeddings("q")[0][0] == 1.0
