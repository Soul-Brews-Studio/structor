"""lance/ui/live.js, the parts that need no browser.

The page's script also loads under node (no ``document``: the DOM binding
only runs in a browser) and exports its pure helpers and the state they read.
Each test evaluates a few lines against those exports and reads back one JSON
line. What only a browser can show — cards, lanes, the EventSource — is
checked by hand against the running replica, not here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIVE_JS = Path(__file__).resolve().parents[2] / "lance" / "ui" / "live.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def page(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A ``.cjs`` copy of the script.

    ``lance/package.json`` says ``"type": "module"``, which makes node read any
    ``.js`` under it as ESM — where ``module.exports`` does not exist. The
    ``.cjs`` extension pins CommonJS whatever the package says; the browser
    loads the original as a classic script and never sees either.
    """
    p = tmp_path_factory.mktemp("live") / "live.cjs"
    p.write_text(LIVE_JS.read_text())
    return p


def run_js(page: Path, body: str) -> dict:
    """Evaluate ``body`` in node with the page's exports bound to ``m``; it must print one JSON object."""
    src = f"const m = require({json.dumps(str(page))});\n{body}"
    res = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_the_script_parses_and_loads_without_a_document(page: Path):
    assert subprocess.run(["node", "--check", str(LIVE_JS)], capture_output=True, text=True, check=False).returncode == 0
    out = run_js(page, "console.log(JSON.stringify({mode: m.S.mode, keys: Object.keys(m).length}))")
    assert out["mode"] == "live" and out["keys"] > 10


def test_short_session_ids_tell_workflow_subagents_apart(page: Path):
    """``agent-<hex>@wf_<run>`` sessions all read ``agent-a0`` when the raw id was cut; the hex is the identity."""
    out = run_js(page, """
      console.log(JSON.stringify({
        wf: m.shortSid('agent-ae7077d21796afacd@wf_c17770e2-c01'),
        wf2: m.shortSid('agent-a0b1c2d3e4f5a6b7c@wf_c17770e2-c01'),
        uuid: m.shortSid('f1e856a2-53bd-46b9-b26a-7dca05a201e6'),
        empty: m.shortSid(''), none: m.shortSid(null), plain: m.shortSid('abc'),
      }))""")
    assert out["wf"] == "ae7077d2" and out["wf2"] == "a0b1c2d3"
    assert out["uuid"] == "f1e856a2"
    assert out["empty"] == "–" and out["none"] == "–" and out["plain"] == "abc"


def test_the_replay_window_is_narrowed_to_fit_the_rows_cap(page: Path):
    """802 rows in 180 min do not fit one 500-row read: the window shrinks; a window that fits is left alone."""
    out = run_js(page, """
      console.log(JSON.stringify({
        over: m.trimMinutes(180, 802, 500), fits: m.trimMinutes(180, 300, 500), exact: m.trimMinutes(60, 500, 500),
        floor: m.trimMinutes(2, 100000, 500), one: m.trimMinutes(1, 5000, 500),
      }))""")
    assert 1 <= out["over"] < 180 and out["over"] == int(180 * 500 / 802 * 0.8)
    assert out["fits"] == 180 and out["exact"] == 60
    assert out["floor"] == 1 and out["one"] == 1


def test_the_stream_url_carries_the_last_id_the_page_has_seen(page: Path):
    """A first open after /live/recent, or a reopen after a CLOSED stream, resumes instead of gapping."""
    out = run_js(page, """
      console.log(JSON.stringify({
        fresh: m.streamUrl('local', null), resumed: m.streamUrl('local', 42), zero: m.streamUrl('a b', 0),
      }))""")
    assert out["fresh"] == "/api/local/live"
    assert out["resumed"] == "/api/local/live?last_id=42"
    assert out["zero"] == "/api/a%20b/live?last_id=0"


def test_notices_stack_instead_of_replacing_each_other(page: Path):
    """The 'no relay on this edition' line must outlive whatever the replay it fell back to adds."""
    out = run_js(page, """
      m.el.notice = {innerHTML: '', hidden: true};
      m.notice('relay', 'no live relay on this edition');
      m.notice('window', 'the window holds 822 events & more');
      const both = m.el.notice.innerHTML, hiddenWithBoth = m.el.notice.hidden;
      m.notice('window', '');
      const one = m.el.notice.innerHTML;
      m.notice('relay', '');
      console.log(JSON.stringify({both, hiddenWithBoth, one, hiddenAtEnd: m.el.notice.hidden}))""")
    assert out["both"] == "<span>no live relay on this edition</span><span>the window holds 822 events &amp; more</span>"
    assert out["hiddenWithBoth"] is False
    assert out["one"] == "<span>no live relay on this edition</span>"
    assert out["hiddenAtEnd"] is True


def test_staleness_counts_from_the_event_timestamp_not_from_when_its_card_landed(page: Path):
    """Live: a 47 s old event reads 47 s. Replay: the replayed clock, running on at ×speed, then standing still."""
    out = run_js(page, """
      const now = Date.now();
      m.S.mode = 'live'; m.S.lastEventT = now - 47000;
      const live = m.ageText();
      m.S.lastEventT = 0;
      const none = m.ageText();
      // replay: the last card played 1 s of wall time ago at ×60, the next card is 10 min later
      m.S.mode = 'replay'; m.opts.speed = 60;
      const T = now - 3600000;
      m.S.replay = {events: [{t: T}, {t: T + 600000}], i: 1};
      m.S.clock = T; m.S.clockWall = now - 1000; m.S.lastEventT = T;
      const running = m.nowMs() - T;
      m.S.replay.events[1].t = T + 30000;   // a gap shorter than the clock has run: capped at the next card
      const capped = m.nowMs() - T;
      m.S.paused = true;
      const paused = m.nowMs() - T;
      console.log(JSON.stringify({live, none, running, capped, paused}))""")
    assert out["live"] == "last event 47 s ago"
    assert out["none"] == "no events yet"
    assert 59_000 <= out["running"] <= 62_000
    assert out["capped"] == 30_000
    assert out["paused"] == 0


def test_the_replay_badge_dates_itself_from_the_first_event_played_not_the_window_asked_for(page: Path):
    out = run_js(page, """
      m.el.badge = {className: '', innerHTML: ''};
      m.S.target = 'local';
      m.S.sinceMs = Date.UTC(2026, 8, 11, 10, 51, 10);   // asked for: three hours back
      m.S.fromMs = 0;
      m.setBadgeReplay();
      const before = m.el.badge.innerHTML;
      m.S.fromMs = Date.UTC(2026, 8, 11, 13, 13, 16);    // played: the trimmed window's first row
      m.setBadgeReplay();
      console.log(JSON.stringify({before, after: m.el.badge.innerHTML, cls: m.el.badge.className,
        since: m.hms(m.S.sinceMs), from: m.hms(m.S.fromMs)}))""")
    assert out["cls"] == "badge replay"
    assert "Lance replica local" in out["after"] and "events table" in out["after"]
    assert out["since"] in out["before"] and "window start: 2026-09-11T10:51:10" in out["before"]
    assert out["from"] in out["after"] and out["since"] not in out["after"]
    assert "first event played: 2026-09-11T13:13:16" in out["after"]
