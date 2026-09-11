"""scripts/record-live.py without a browser: the plan it prints, its exit codes, and the ffmpeg step.

The script is spawned with this venv's python, which has no Playwright
installed — so any code path that reached the browser would fail loudly
(exit 69, "Playwright is not installed") instead of silently opening Chrome.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "record-live.py"
URL = "http://127.0.0.1:8094/live.html?target=local&mode=replay&minutes=180&speed=60&seconds=18"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def last_json(stdout: str) -> dict:
    """The script promises one JSON object as the last stdout line."""
    return json.loads(stdout.strip().splitlines()[-1])


def png(path: Path, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    """A flat-colour 8-bit RGB PNG written by hand — no imaging library in this venv."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))  # filter byte 0 + one row, per row

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, colour type 2 (RGB)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture()
def frames(tmp_path: Path) -> Path:
    """Four 64x36 frames that brighten one step each, standing in for a card arriving."""
    d = tmp_path / "frames"
    d.mkdir()
    for i in range(4):
        png(d / f"frame-{i:04d}.png", 64, 36, (24 + i * 40, 26, 32))
    return d


# --- --dry-run: the plan, no browser -----------------------------------------


def test_dry_run_prints_the_plan_and_the_ffmpeg_commands():
    res = run("--url", URL, "--seconds", "20", "--fps", "4", "--out", "somewhere", "--name", "clip", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "Playwright" not in res.stderr  # the browser code was never reached
    plan = last_json(res.stdout)

    assert plan["dry_run"] is True
    assert plan["url"] == URL
    assert plan["viewport"] == {"width": 1280, "height": 720}
    assert plan["frames"] == 80 and plan["fps"] == 4 and plan["seconds"] == 20
    assert plan["wait_for"] == "#lanes"
    assert plan["browser"] == ["chrome", "chromium"]  # system Chrome first, bundled Chromium as the fallback
    assert plan["out"] == {"webm": "somewhere/clip.webm", "gif": "somewhere/clip.gif"}

    webm, gif = plan["ffmpeg"]["webm"], plan["ffmpeg"]["gif"]
    for cmd in (webm, gif):
        assert cmd[0] == "ffmpeg"
        assert cmd[cmd.index("-framerate") + 1] == "4"
        assert cmd[cmd.index("-i") + 1].endswith("/frame-%04d.png")
    # webm: VP9 in constant-quality mode at crf 32
    assert webm[webm.index("-c:v") + 1] == "libvpx-vp9"
    assert webm[webm.index("-crf") + 1] == "32" and webm[webm.index("-b:v") + 1] == "0"
    assert webm[-1] == "somewhere/clip.webm"
    # gif: two-pass palette at 800 px wide and the recording's fps, looping
    vf = gif[gif.index("-vf") + 1]
    assert "palettegen" in vf and "paletteuse" in vf and "scale=800:" in vf and vf.startswith("fps=4,")
    assert gif[gif.index("-loop") + 1] == "0" and gif[-1] == "somewhere/clip.gif"


def test_wait_for_names_another_pages_element():
    # live.html has #lanes; another page (session-viewer's fleet list, say)
    # names the element that appears with its data, so frame 0 is not a shell.
    res = run("--url", URL, "--wait-for", 'input[placeholder^="filter"]', "--dry-run")
    assert res.returncode == 0, res.stderr
    plan = json.loads(res.stdout.strip().splitlines()[-1])
    assert plan["wait_for"] == 'input[placeholder^="filter"]'


def test_dry_run_frame_count_follows_seconds_and_fps():
    res = run("--url", URL, "--seconds", "3", "--fps", "10", "--width", "640", "--height", "360", "--dry-run")
    assert res.returncode == 0, res.stderr
    plan = last_json(res.stdout)
    assert plan["frames"] == 30 and plan["viewport"] == {"width": 640, "height": 360}
    assert plan["out"] == {"webm": "recordings/live-jsonl.webm", "gif": "recordings/live-jsonl.gif"}  # the defaults


# --- usage errors: exit 64, like the other Structor CLIs ---------------------


@pytest.mark.parametrize(
    "args, complaint",
    [
        (["--dry-run"], "--url is required"),
        (["--url", URL, "--fps", "0", "--dry-run"], "1 or more"),
        (["--url", URL, "--seconds", "0", "--dry-run"], "1 or more"),
        (["--url", URL, "--name", "a/b", "--dry-run"], "basename"),
        (["--frames", "/nonexistent/frames-dir"], "not a directory"),
    ],
)
def test_usage_errors_exit_64(args: list[str], complaint: str):
    res = run(*args)
    assert res.returncode == 64, res.stderr
    assert complaint in res.stderr
    assert res.stdout == ""


# --- --frames: the encode step alone ------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_frames_are_encoded_to_webm_and_gif(frames: Path, tmp_path: Path):
    out = tmp_path / "out"  # does not exist yet: the script creates it
    res = run("--frames", str(frames), "--fps", "4", "--out", str(out), "--name", "clip")
    assert res.returncode == 0, res.stderr
    result = last_json(res.stdout)

    webm, gif = Path(result["webm"]), Path(result["gif"])
    assert webm == out / "clip.webm" and gif == out / "clip.gif"
    assert webm.read_bytes()[:4] == b"\x1a\x45\xdf\xa3"  # EBML header: a Matroska/WebM file
    assert gif.read_bytes()[:6] == b"GIF89a"
    assert result["webm_bytes"] == webm.stat().st_size > 0
    assert result["gif_bytes"] == gif.stat().st_size > 0
    assert result["frames"] == 4 and result["fps"] == 4
    assert sorted(p.name for p in frames.iterdir()) == [f"frame-{i:04d}.png" for i in range(4)]  # not ours, kept


def test_failing_ffmpeg_exits_70_and_keeps_the_frames(frames: Path, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'Unknown encoder' >&2\nexit 1\n")
    fake.chmod(0o755)

    res = run("--frames", str(frames), "--out", str(tmp_path / "out"), env={**os.environ, "PATH": str(bin_dir)})
    assert res.returncode == 70, res.stderr
    assert "ffmpeg failed on the webm" in res.stderr and "Unknown encoder" in res.stderr
    assert f"--frames {frames}" in res.stderr  # how to retry the encode
    assert (frames / "frame-0000.png").exists()
    assert res.stdout == ""


def test_missing_ffmpeg_exits_70(frames: Path, tmp_path: Path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    res = run("--frames", str(frames), "--out", str(tmp_path / "out"), env={**os.environ, "PATH": str(empty)})
    assert res.returncode == 70, res.stderr
    assert "ffmpeg not found" in res.stderr


def test_a_capture_that_never_started_leaves_no_temp_directory_behind(tmp_path: Path):
    """Exit 69 here (no Playwright in this venv) is the same path as a browser or page that never came up."""
    res = run("--url", URL, "--out", str(tmp_path / "out"), env={**os.environ, "TMPDIR": str(tmp_path)})
    assert res.returncode == 69, res.stderr
    assert "Playwright is not installed" in res.stderr
    assert not list(tmp_path.glob("record-live-*"))  # a retry loop must not pile these up


def test_frames_dir_without_frame_0000_exits_66(tmp_path: Path):
    empty = tmp_path / "no-frames"
    empty.mkdir()
    res = run("--frames", str(empty), "--out", str(tmp_path / "out"))
    assert res.returncode == 66, res.stderr
    assert "frame-0000.png" in res.stderr
