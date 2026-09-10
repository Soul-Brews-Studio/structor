"""Sampling is deterministic: the same rows pick the same sessions and the same turns, every time."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from dream_fixtures import seed_store

from structor_dream import material

NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)


def test_conversational_filter_and_round_robin_stratification(tmp_path: Path):
    r, _ = seed_store(tmp_path)
    rows = material.week_rows(r, "2026-W37")
    assert {x["session"] for x in rows} == {"s1", "s2", "s3", "s4", "s5", "s6"}
    talk = material.conversational(rows)
    assert {x["session"] for x in talk} == {"s1", "s2", "s3", "s4"}      # s5 has one human turn, s6 five events

    chosen = [x["session"] for x in material.stratify(talk, max_sessions=10)]
    # every project's best session first (p1's s1 has the most human turns, then p2's s3, then p3's s4), then seconds
    assert chosen == ["s1", "s3", "s4", "s2"]
    assert [x["session"] for x in material.stratify(talk, max_sessions=2)] == ["s1", "s3"]
    assert material.stratify(talk, max_sessions=3) == material.stratify(list(reversed(talk)), max_sessions=3)
    assert material.stratify([], 5) == []
    assert material.week_rows(r, "2026-W36")[0]["session"] == "s3"


def test_week_label_is_validated_before_it_reaches_a_predicate(tmp_path: Path):
    r, _ = seed_store(tmp_path)
    for bad in ("2026-W99", "2026W37", "x' OR 1=1 --", ""):
        try:
            material.week_rows(r, bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_session_events_are_conversational_long_enough_and_in_time_order(tmp_path: Path):
    r, _ = seed_store(tmp_path)
    events = material.session_events(r, "s1", "2026-W37")
    assert [e["id"] for e in events] == [f"s1-37-{i}" for i in range(8)]     # no tool row, no two-letter prompt
    assert all(e["role"] in ("user", "assistant") and len(e["text"]) >= material.MIN_TEXT for e in events)
    assert material.session_events(r, "s3", "2026-W36") and not material.session_events(r, "s1", "2026-W36")


def test_budget_sample_keeps_every_human_turn_first_then_spaces_assistant_turns(tmp_path: Path):
    def turn(i: int, role: str, size: int) -> dict:
        return {"id": f"e{i}", "ts": f"2026-09-08 10:{i:02d}:00.000Z", "role": role, "text": ("u" if role == "user" else "a") * size}

    turns = [turn(i, "user" if i % 4 == 0 else "assistant", 100) for i in range(40)]   # 10 user, 30 assistant, 4,000 chars
    sample = material.budget_sample(turns, budget=2000)
    users = [e for e in sample if e["role"] == "user"]
    assistants = [e for e in sample if e["role"] == "assistant"]
    assert len(users) == 10                                                    # all of them
    assert 8 <= len(assistants) <= 9                                           # what the remaining ~1,000 chars hold
    assert [e["ts"] for e in sample] == sorted(e["ts"] for e in sample)         # time order, newest last
    gaps = [int(b["id"][1:]) - int(a["id"][1:]) for a, b in pairwise(assistants)]
    assert max(gaps) - min(gaps) <= 2                                          # evenly spaced, not the first nine
    assert sum(len(e["text"]) + 1 for e in sample) <= 2000

    # a turn is capped, and human turns that alone overflow the budget are thinned evenly rather than truncated at the end
    huge = [turn(i, "user", 5000) for i in range(6)]
    thinned = material.budget_sample(huge, budget=2000, item_cap=700)
    assert 2 <= len(thinned) <= 3 and all(len(e["text"]) <= 700 for e in thinned)
    assert thinned[0]["id"] == "e0" and thinned[-1]["id"] == "e5"
    assert material.budget_sample([]) == []
    assert material.evenly_spaced([1, 2, 3, 4, 5], 3) == [1, 3, 5] and material.evenly_spaced([1, 2], 5) == [1, 2]
    assert material.evenly_spaced([1, 2, 3], 1) == [2] and material.evenly_spaced([1], 0) == []


def test_horizons_are_computed_from_the_timestamp_relative_to_now():
    assert material.horizon("2026-09-09 10:00:00.000Z", NOW) == "short"
    assert material.horizon("2026-09-03 06:00:00.000Z", NOW) == "short"       # exactly seven days
    assert material.horizon("2026-08-20 10:00:00.000Z", NOW) == "mid"
    assert material.horizon("2026-07-01 10:00:00.000Z", NOW) == "long"
    assert material.horizon("2026-01-01 10:00:00.000Z", NOW) == "archive"
    assert material.horizon("", NOW) == "archive" and material.horizon("not a date", NOW) == "archive"


def test_topic_material_applies_the_floor_then_a_share_per_horizon_round_robin_over_projects():
    names = {"s1": ("uuid-1", "/a/structor"), "s2": ("uuid-2", "/a/digger"), "s3": ("uuid-3", "/a/tray")}

    def hit(n: int, ts: str, session: str, score: float) -> dict:
        return {"event_id": f"e{n}", "session": session, "ts": ts, "role": "user", "text": f"hit {n}",
                "_relevance_score": score, "vector": [0.0] * 8}

    hits = [
        hit(1, "2026-09-09 10:00:00.000Z", "s1", 1.00),   # short, structor
        hit(2, "2026-09-09 11:00:00.000Z", "s1", 0.95),   # short, structor
        hit(3, "2026-09-08 10:00:00.000Z", "s2", 0.90),   # short, digger
        hit(4, "2026-09-07 10:00:00.000Z", "s3", 0.85),   # short, tray
        hit(5, "2026-08-20 10:00:00.000Z", "s1", 0.80),   # mid
        hit(6, "2026-07-01 10:00:00.000Z", "s2", 0.70),   # long
        hit(7, "2026-01-01 10:00:00.000Z", "s3", 0.60),   # archive
        hit(8, "2026-09-09 12:00:00.000Z", "s2", 0.20),   # under the floor: dropped however fresh
    ]
    items = material.topic_material(hits, k=8, at=NOW, names=names)
    assert "e8" not in {i["event_id"] for i in items}
    short = [i for i in items if i["horizon"] == "short"]
    assert len(short) == 2                                                     # k/4 per horizon
    assert [i["event_id"] for i in short] == ["e1", "e3"]                      # round-robin: structor, then digger — not e2
    assert [i["horizon"] for i in items] == ["short", "short", "mid", "long", "archive"]
    assert [i["n"] for i in items] == [1, 2, 3, 4, 5]
    assert items[0]["session_id"] == "uuid-1" and items[0]["project"] == "/a/structor"
    assert all("vector" not in i for i in items)
    assert material.topic_material([], 8, NOW, names) == []


def test_tool_output_dumps_are_recognised_and_dropped_from_topic_material():
    from datetime import UTC, datetime

    from structor_dream import material

    dump_listing = "397 def read_lines(\n398     source: Source,\n399     cursor: dict,\n400     limit: int,"
    dump_numbered = "409: const body = (await c.req.parseBody()) as unknown as FormBody;\n430: const body = x"
    dump_diff = "diff --git a/x.ts b/x.ts\nindex 6ffb7a9..073f790 100644\n--- a/x.ts\n+++ b/x.ts\n@@ -1,3 +1,4 @@"
    dump_json = '{ "390": { "hOverflow": false, "docH": 3280 }, "768": { "clipped": [] } }\nmore'
    dump_tuples = "[(523, 567)] OK\n[(569, 618)] OK\n[(620, 634), (636, 649)] MISMATCH"
    human_list = "1. check the byte offset first\n2. then the 409 body\n3. only then the rewind"
    human_prose = "The 409 comes back when the stored offset moved on; the reader adopts the server's offset."
    one_code_line = "the fix was `parts[0]` — see 409 handling in ingest.go"
    for text in (dump_listing, dump_numbered, dump_diff, dump_json, dump_tuples):
        assert material.looks_like_tool_output(text), text[:30]
    for text in (human_list, human_prose, one_code_line, ""):
        assert not material.looks_like_tool_output(text), text[:30]

    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    names = {"s1": ("sid-1", "/p/one"), "s2": ("sid-2", "/p/two")}
    hits = [
        {"event_id": "e1", "session": "s1", "ts": "2026-09-09 10:00:00.000Z", "text": dump_listing, "_relevance_score": 0.05},
        {"event_id": "e2", "session": "s2", "ts": "2026-09-09 10:00:00.000Z", "text": human_prose, "_relevance_score": 0.04},
        {"event_id": "e3", "session": "s1", "ts": "2026-09-01 10:00:00.000Z", "text": dump_diff, "_relevance_score": 0.04},
        {"event_id": "e4", "session": "s2", "ts": "2026-09-01 10:00:00.000Z", "text": human_list, "_relevance_score": 0.03},
    ]
    items = material.topic_material(hits, 8, at, names)
    assert [it["event_id"] for it in items] == ["e2", "e4"]           # the two dumps are gone, order and n kept
    assert [it["n"] for it in items] == [1, 2] and [it["horizon"] for it in items] == ["short", "mid"]
