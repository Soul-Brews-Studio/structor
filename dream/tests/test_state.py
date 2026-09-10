"""The digest cache: a second run pays for nothing, a changed count pays for one, --force pays for all.

And the run lock beside it: one structor-dream per replica, because every save writes the cache whole.
"""

from __future__ import annotations

import os
from pathlib import Path

from dream_fixtures import ScriptedChat, asker, seed_store, upsert
from structor_lance.schema import SessionWeek

from structor_dream import cli, config
from structor_dream.state import LOCK_FILE, DigestState, RunLock


def digest_calls(chat: ScriptedChat) -> int:
    return sum(1 for m in chat.calls if "<digest n=" not in m[1]["content"] and "<event n=" in m[1]["content"])


def test_second_run_digests_nothing_changed_count_redigests_one_and_force_redigests_all(tmp_path: Path):
    r, e = seed_store(tmp_path / "store")
    root = tmp_path / "dreams"
    root.mkdir()
    chat = ScriptedChat()
    a = asker(r, e, chat)

    first = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=False, log=lambda _l: None)
    assert first["chosen"] == 4 and first["digested"] == 4 and first["skipped"] == 0 and first["written"]
    assert digest_calls(chat) == 4 and (root / "2026-W37.md").is_file()
    state = DigestState(config.state_path(r))
    assert len(state) == 4 and state.get("s1", "2026-W37")["event_count"] == 30

    chat.calls.clear()
    second = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=False, log=lambda _l: None)
    assert second["digested"] == 0 and second["skipped"] == 4 and digest_calls(chat) == 0
    assert len(chat.calls) == 1                                                 # the reduce call still happens

    # the session grew: its session_weeks row carries a new event_count, so it alone is digested again
    upsert(r, SessionWeek, [{"id": "s2-2026-W37", "session": "s2", "project": "p1", "iso_week": "2026-W37", "event_count": 13,
                             "user_count": 3, "assistant_count": 4, "first_ts": "2026-09-08 10:00:00.000Z",
                             "last_ts": "2026-09-08 13:00:00.000Z", "created": "c", "updated": "2026-09-08 13:00:05.000Z"}])
    chat.calls.clear()
    third = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=False, log=lambda _l: None)
    assert third["digested"] == 1 and third["skipped"] == 3 and digest_calls(chat) == 1
    assert DigestState(config.state_path(r)).get("s2", "2026-W37")["event_count"] == 13

    chat.calls.clear()
    forced = cli.run_week(r, a, "2026-W37", root, max_sessions=10, force=True, log=lambda _l: None)
    assert forced["digested"] == 4 and forced["skipped"] == 0 and digest_calls(chat) == 4

    # the other week of the same session is its own entry: dreaming W36 does not touch W37's digest
    chat.calls.clear()
    w36 = cli.run_week(r, a, "2026-W36", root, max_sessions=10, force=False, log=lambda _l: None)
    assert w36["chosen"] == 1 and w36["digested"] == 1
    again = DigestState(config.state_path(r))
    assert again.get("s3", "2026-W36")["event_count"] == 14 and again.get("s3", "2026-W37")["event_count"] == 20
    assert set(again.for_week("2026-W37")) == {"s1", "s2", "s3", "s4"}


def test_state_file_is_atomic_and_tolerates_junk(tmp_path: Path):
    path = tmp_path / "deep" / "digest_state.json"
    state = DigestState(path)
    state.put("s1", "2026-W37", 30, "2026-09-08 12:00:00.000Z", {"summary": "x"})
    state.save()
    assert not list(path.parent.glob("*.tmp")) and DigestState(path).get("s1", "2026-W37")["digest"] == {"summary": "x"}
    path.write_text("not json")
    assert len(DigestState(path)) == 0
    path.write_text('{"entries": "nope"}')
    assert len(DigestState(path)) == 0
    assert DigestState.key("abc", "2026-W37") == "abc@2026-W37"


def test_run_lock_admits_one_holder_per_path_and_names_it(tmp_path: Path):
    """Two runs on one replica (launchd at 03:30 meeting a hand run from 03:10): the second sees the lock held
    and by whom, and gets in once the first lets go. flock is per open file description, so two handles in one
    process conflict exactly as two processes do."""
    path = tmp_path / "deep" / LOCK_FILE
    first = RunLock(path)
    assert first.acquire() and first.held and path.read_text().strip() == str(os.getpid())
    second = RunLock(path)
    assert not second.acquire() and not second.held and second.holder == str(os.getpid())
    first.release()
    assert not first.held and second.acquire() and second.held
    second.release()
    third = RunLock(path)
    assert path.is_file() and third.acquire()                                  # the file stays; only the flock went
    third.release()
    third.release()                                                            # releasing twice is harmless
