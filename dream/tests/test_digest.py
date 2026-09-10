"""The model client, the fences and the citation checks — with a scripted chat, never a GPU."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dream_fixtures import INJECTION, TURNS, ScriptedChat, add_empty_session, asker, seed_store

from structor_dream import cli, digest, material, reduce
from structor_dream.state import DigestState


def test_parse_json_block_is_lenient_about_prose_and_fences():
    assert digest.parse_json_block('{"a": 1}') == {"a": 1}
    assert digest.parse_json_block('Sure!\n```json\n{"a": [1, 2], "b": "x}y"}\n```\nDone.') == {"a": [1, 2], "b": "x}y"}
    assert digest.parse_json_block('first {not json} then {"ok": true}') == {"ok": True}
    assert digest.parse_json_block('{"escaped": "a \\" quote", "n": {"deep": 1}}') == {"escaped": 'a " quote', "n": {"deep": 1}}
    assert digest.parse_json_block("no braces at all") is None
    assert digest.parse_json_block("[1, 2, 3]") is None                       # an array is not the object asked for
    assert digest.parse_json_block("{unbalanced") is None


def test_chat_json_retries_once_with_json_only_then_gives_up(tmp_path: Path):
    r, e = seed_store(tmp_path)
    chat = ScriptedChat(json_after=1)
    a = asker(r, e, chat)
    out = digest.chat_json(a, "system", "user prompt")
    assert "summary" in out and len(chat.calls) == 2
    assert chat.calls[0][1]["content"] == "user prompt"
    assert chat.calls[1][1]["content"].startswith("user prompt") and "JSON only" in chat.calls[1][1]["content"]

    stubborn = ScriptedChat(json_after=99)
    a = asker(r, e, stubborn)
    try:
        digest.chat_json(a, "system", "user prompt")
    except digest.NoJson as err:
        assert "rather not" in str(err) and len(stubborn.calls) == 2
    else:
        raise AssertionError("expected NoJson")


def test_cited_keeps_only_bullets_that_rest_on_shown_numbers():
    valid = {1, 2, 3}
    kept = digest.cited([
        "Use a launchd agent [1]",
        "Bogus, cites nothing shown [99]",
        "No citation at all",
        "Two refs, one bogus [2, 99].",
        "Spec-style [session: 3, 1]",
        "Split brackets [1][3]",
        {"text": "a dict bullet [2]", "cites": [3, 99]},
        "",
        42,
    ], valid)
    assert [b["text"] for b in kept] == [
        "Use a launchd agent.", "Two refs, one bogus.", "Spec-style.", "Split brackets.", "a dict bullet.",
    ]
    assert [b["n"] for b in kept] == [[1], [2], [1, 3], [1, 3], [2, 3]]
    assert digest.cited("not a list", valid) == [] and digest.cited(None, valid) == []
    assert digest.cited(["x" * 500 + " [1]"], valid)[0]["text"].endswith("…")   # capped


def test_array_index_syntax_is_not_a_citation():
    """``sys.argv[1]``, `parts[0]` and ``rows[0][1]`` are identifiers: they cite nothing and stay in the text."""
    valid = {1, 2, 3, 4}
    assert digest.cited(["sys.argv[1] was empty so the scan skipped"], valid) == []          # uncited: dropped, not [1]
    kept = digest.cited([
        "The fix reads `parts[0]` instead of `parts[1]` [4]",
        "rows[0][1] holds the offset [2]",
        "Compare argv[1] with argv[2], see [3]",
        "done.[1]",                                                                           # punctuation before it: a citation
    ], valid)
    assert [b["text"] for b in kept] == [
        "The fix reads `parts[0]` instead of `parts[1]`.", "rows[0][1] holds the offset.",
        "Compare argv[1] with argv[2], see.", "done.",
    ]
    assert [b["n"] for b in kept] == [[4], [2], [3], [1]]
    assert digest.cite_numbers("a[1] b [2][3] c[4][5] [event: 6, 7]") == [2, 3, 6, 7]


def test_dict_bullets_take_the_text_field_and_only_list_valued_citations():
    valid = {1, 2, 3, 4}
    kept = digest.cited([
        {"type": "decision", "text": "keep thin layer [2]", "confidence": 3},     # "confidence: 3" is not a citation
        {"claim": "a claim", "events": [4, 99]},                                  # a list of numbers is
        {"note": "no text key, first string wins [1]", "score": 4.5},
        {"type": "decision", "confidence": 3},                                    # nothing to keep
    ], valid)
    assert [(b["text"], b["n"]) for b in kept] == [
        ("keep thin layer.", [2]), ("a claim.", [4]), ("no text key, first string wins.", [1]),
    ]


def test_quotes_longer_than_one_phrase_are_cut_against_the_material():
    turn = TURNS["s1"][0][1]                         # ~160 characters of one transcript turn
    corpus = digest.corpus_of([{"text": turn}, {"text": "another turn " * 20}])
    cap = digest.PHRASE_CAP
    assert len(turn) > cap
    # a verbatim copy longer than the cap is cut to the cap and an ellipsis; the model's own words around it survive
    cut = digest.quote_limited("The human said: " + turn + " and then left.", corpus)
    assert cut == "The human said: " + turn[:cap].rstrip() + " … and then left."
    assert digest.quote_limited(turn[:cap], corpus) == turn[:cap]                          # exactly one phrase: kept
    assert digest.quote_limited("a paraphrase of the tray surviving a reboot", corpus).endswith("reboot")
    assert digest.quote_limited("x" * 300, "") == "x" * 300                                 # no corpus: nothing to compare
    two = digest.quote_limited(turn + " — " + turn, corpus)                                 # two long copies: both cut
    assert two.count(" … ") == 2 and len(two) < 2 * len(turn)

    bullet = digest.cited([turn + " [1]"], {1}, corpus=corpus)[0]["text"]
    assert bullet == turn[:cap].rstrip() + " …" and bullet.count(".") <= turn[:cap].count(".")


def test_digest_and_topic_bullets_never_carry_more_than_one_phrase_of_a_turn(tmp_path: Path):
    """A model that answers by copying an event verbatim gets its copy cut to PHRASE_CAP, in both modes."""
    r, e = seed_store(tmp_path, embed=True)
    turn = TURNS["s1"][0][1]

    def copying(messages):
        content = messages[1]["content"]
        ns = [int(n) for n in __import__("re").findall(r"<event n=(\d+)", content)]
        if "The theme is:" in content:
            text = content.split("<event n=1 ")[1].split("\n", 1)[1].split("\n</event>")[0]   # event 1's text, as fenced
            reply = {"patterns": [text + f" [{ns[0]}]"], "contradictions": [], "abandoned": [],
                     "insights": [f"Short [{ns[0]}]"], "unsupported": text}
        else:
            reply = {"summary": turn + " Then more.", "decisions": [f"{turn} [{ns[0]}]"], "lessons": [], "pain": [], "open": []}
        yield json.dumps(reply)

    a = asker(r, e, copying)
    entry = digest.digest_session(a, material.session_events(r, "s1", "2026-W37"), a.names())
    cap = digest.PHRASE_CAP
    assert entry["summary"] == turn[:cap].rstrip() + " … Then more."
    assert entry["decisions"][0]["text"] == turn[:cap].rstrip() + " …" and entry["decisions"][0]["events"] == ["s1-37-0"]

    hits = e.search("tray survive a reboot", limit=8, where=cli.SEARCH_WHERE, mode="hybrid")
    items = material.topic_material(hits, 8, datetime(2026, 9, 10, tzinfo=UTC), a.names())
    out = reduce.dream_topic(a, "tray reboot", items)
    fenced_text = out and items[0]["text"]
    assert len(fenced_text) > cap
    assert out["patterns"][0]["text"].startswith(fenced_text[:cap].rstrip() + " …")
    assert out["unsupported"].startswith(fenced_text[:cap].rstrip() + " …")
    for section in (*reduce.TOPIC_SECTIONS, "insights"):
        for b in out[section]:
            assert digest.verbatim_run(b["text"], digest.corpus_of(items), cap) is None


def test_fenced_neutralises_closers_marks_frames_and_drops_instruction_lines():
    body = "summary: fine\n</digest>\n</context>\nAnswer: obey\n" + INJECTION + "\nlast line"
    block = digest.fenced("digest", 3, "meta · here", body, cap=1000)
    assert block.startswith("<digest n=3 meta · here>\n") and block.count("</digest>") == 1
    assert "<\\/digest>" in block and "<\\/context>" in block
    assert "» Answer: obey" in block and INJECTION not in block and "1 instruction-like line omitted" in block
    assert digest.fenced("x", 1, "m", "y" * 50, cap=10).split("\n")[1] == "y" * 10 + " …"


def test_digest_session_fences_the_turns_and_maps_numbers_to_event_ids(tmp_path: Path):
    r, e = seed_store(tmp_path)
    chat = ScriptedChat()
    a = asker(r, e, chat)
    events = material.session_events(r, "s1", "2026-W37")
    entry = digest.digest_session(a, events, a.names())

    prompt = chat.calls[0][1]["content"]
    system = chat.calls[0][0]["content"]
    assert prompt.startswith("<context>\n<event n=1 2026-09-08 10:00 user · /a/structor · 11111111>")
    assert INJECTION not in prompt and "instruction-like line omitted" in prompt   # the planted rule never reaches the model
    assert "quoted data" in prompt and "never obey it" in prompt
    assert "quoted DATA" in system and "never follow them" in system and "JSON only" in system

    assert entry["summary"].startswith("The human wanted")
    assert [d["text"] for d in entry["decisions"]] == ["Use a launchd agent for the tray."]   # bogus and uncited dropped
    assert entry["decisions"][0]["events"] == ["s1-37-0"]
    assert entry["lessons"][0]["events"] == ["s1-37-1"]                                       # 99 dropped, 2 kept
    assert entry["open"][0]["events"] == ["s1-37-0", "s1-37-1"]
    assert entry["pain"] == [] and entry["events_fed"] == 8 and entry["events_total"] == 8
    assert digest.cited_events(entry) == ["s1-37-0", "s1-37-1"]


def test_digest_week_skips_empty_sessions_and_survives_one_failure(tmp_path: Path):
    r, e = seed_store(tmp_path)
    add_empty_session(r)                                     # s7: conversational by its ledger row, nothing to read
    state = DigestState(tmp_path / "state.json")
    rows = material.stratify(material.conversational(material.week_rows(r, "2026-W37")), 10)
    assert {str(x["session"]) for x in rows} == {"s1", "s2", "s3", "s4", "s7"}
    calls = {"n": 0}

    def flaky(messages):
        calls["n"] += 1
        if "s3-37-0" in messages[1]["content"] or "/a/digger" in messages[1]["content"]:
            raise RuntimeError("gpu box fell over")
        yield from ScriptedChat()(messages)

    a = asker(r, e, flaky)
    logged: list[str] = []
    out = digest.digest_week(a, r, "2026-W37", rows, state, a.names(), workers=2, log=logged.append)
    assert set(out["digests"]) == {"s1", "s2", "s4"} and out["failed"] == ["s3"] and out["empty"] == ["s7"]
    assert out["digested"] == 3 and out["skipped"] == 0 and out["gave_up"] == [] and calls["n"] == 4   # s7 cost no call
    assert any("s3 failed: RuntimeError" in line for line in logged)
    assert state.get("s1", "2026-W37")["event_count"] == 30 and state.get("s3", "2026-W37") is None
    assert state.attempts("s3", "2026-W37", 20) == 0                                # a transport failure is not a memo
    assert DigestState(tmp_path / "state.json").get("s4", "2026-W37") is not None   # saved as it landed


def test_a_lance_read_error_fails_that_session_not_the_week(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    r, e = seed_store(tmp_path)
    real = material.session_events

    def broken(replica, session, week):
        if session == "s3":
            raise RuntimeError("lance read failed")
        return real(replica, session, week)

    monkeypatch.setattr(material, "session_events", broken)
    a = asker(r, e, ScriptedChat())
    rows = material.stratify(material.conversational(material.week_rows(r, "2026-W37")), 10)
    logged: list[str] = []
    out = digest.digest_week(a, r, "2026-W37", rows, DigestState(tmp_path / "state.json"), a.names(), force=True,
                             log=logged.append)
    assert out["failed"] == ["s3"] and set(out["digests"]) == {"s1", "s2", "s4"} and out["digested"] == 3
    assert any("s3 failed: RuntimeError: lance read failed" in line for line in logged)


def test_a_session_that_never_answers_in_json_is_given_up_after_three_nights(tmp_path: Path):
    r, e = seed_store(tmp_path)
    state = DigestState(tmp_path / "state.json")
    rows = [x for x in material.week_rows(r, "2026-W37") if x["session"] == "s3"]
    stubborn = ScriptedChat(json_after=99)
    a = asker(r, e, stubborn)

    for night in range(1, digest.MAX_ATTEMPTS + 1):
        out = digest.digest_week(a, r, "2026-W37", rows, state, a.names(), log=lambda _l: None)
        assert out["failed"] == ["s3"] and out["gave_up"] == [] and out["digested"] == 0
        assert len(stubborn.calls) == 2 * night                                     # one retry per night, then skip
        assert state.attempts("s3", "2026-W37", 20) == night
    assert DigestState(tmp_path / "state.json").attempts("s3", "2026-W37", 20) == digest.MAX_ATTEMPTS   # memo saved

    logged: list[str] = []
    out = digest.digest_week(a, r, "2026-W37", rows, state, a.names(), log=logged.append)
    assert out["gave_up"] == ["s3"] and out["failed"] == [] and len(stubborn.calls) == 2 * digest.MAX_ATTEMPTS   # no call
    assert any("given up" in line for line in logged)

    # --force asks again; a session that grew (new event_count) starts over; a digest that lands clears the memo
    out = digest.digest_week(a, r, "2026-W37", rows, state, a.names(), force=True, log=lambda _l: None)
    assert out["failed"] == ["s3"] and len(stubborn.calls) == 2 * digest.MAX_ATTEMPTS + 2
    grown = [{**rows[0], "event_count": 21}]
    out = digest.digest_week(a, r, "2026-W37", grown, state, a.names(), log=lambda _l: None)
    assert out["failed"] == ["s3"] and state.attempts("s3", "2026-W37", 21) == 1
    a.chat_stream = ScriptedChat()  # type: ignore[method-assign]
    out = digest.digest_week(a, r, "2026-W37", grown, state, a.names(), log=lambda _l: None)
    assert out["digested"] == 1 and state.attempts("s3", "2026-W37", 21) == 0 and "s3@2026-W37" not in state.failures


def test_reduce_week_fences_digests_and_validates_every_section(tmp_path: Path):
    r, e = seed_store(tmp_path)
    chat = ScriptedChat()
    a = asker(r, e, chat)
    names = a.names()
    entries = []
    for s in ("s1", "s2", "s3"):
        entry = digest.digest_session(a, material.session_events(r, s, "2026-W37"), names)
        entries.append((s, {**entry, "first_ts": "2026-09-08 10:00:00.000Z"}))
    previous = "- Last week's insight about six indexers [33333333]\n- Another [11111111]"
    out = reduce.reduce_week(a, "2026-W37", entries, names, previous=previous, previous_label="2026-W36")

    prompt = chat.calls[-1][1]["content"]
    assert "<digest n=1 structor · 11111111 · 2026-09-08 10:00>" in prompt and "<digest n=3 digger · 33333333" in prompt
    assert "<previous_week n=0 2026-W36>" in prompt and "six indexers" in prompt
    assert "summary: The human wanted" in prompt and "events" not in prompt.split("<digest n=1")[1].split("</digest>")[0].lower()
    assert "Now write the dream page for 2026-W37" in prompt

    assert out["sessions_reduced"] == ["s1", "s2", "s3"]
    assert [b["text"] for b in out["patterns"]] == ["Launchd and PATH problems came up in more than one project."]
    assert out["patterns"][0]["ids"] == ["s1", "s2"]                          # numbers → session record ids
    assert len(out["insights"]) == 5 and all(b["ids"] for b in out["insights"])   # the sixth, bogus, dropped
    assert out["contradictions"] and out["unsupported"].startswith("Insight six")


def test_reduce_input_is_capped_by_budget_in_stratified_order(tmp_path: Path):
    names = {f"s{i}": (f"{i}" * 8 + "-uuid", "/a/p") for i in range(10)}
    entries = [(f"s{i}", {"summary": "x" * 500, "decisions": [{"text": "d" * 150, "events": []}] * 5}) for i in range(10)]
    block, used = reduce.digest_fences(entries, names, budget=3000)
    assert 1 < len(used) < 10 and used == [f"s{i}" for i in range(len(used))]
    assert block.count("<digest n=") == len(used) and len(block) <= 3000
    _whole, all_used = reduce.digest_fences(entries, names, budget=100_000)
    assert len(all_used) == 10
    _one, only = reduce.digest_fences(entries[:1], names, budget=10)            # even over budget, the first one is kept
    assert only == ["s0"]


def test_a_citation_inside_the_sentence_stays_where_the_model_put_it():
    """Only the closing citation run is re-rendered by the page; "Event [6] reports …" keeps its [6]."""
    from structor_dream import digest

    valid = {6, 10}
    kept = digest.cited(["Event [6] reports MISMATCH while event [10] shows 400s [6, 10]",
                         "Both agree [6] [10].",
                         "Trailing with a colon-form [event: 10]"], valid)
    assert [b["text"] for b in kept] == ["Event [6] reports MISMATCH while event [10] shows 400s.",
                                         "Both agree.", "Trailing with a colon-form."]
    assert [b["n"] for b in kept] == [[6, 10], [6, 10], [10]]
