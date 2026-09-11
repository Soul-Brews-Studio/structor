#!/usr/bin/env python3
"""Record the live jsonl page (lance/ui/live.html) as a short WebM and GIF.

    uvx --with playwright python scripts/record-live.py \
        --url 'http://127.0.0.1:8094/live.html?target=local&mode=replay&minutes=180&speed=60&seconds=18' \
        --seconds 20 --fps 4 --width 1280 --height 720 --out recordings --name live-jsonl

The pipeline is the one that has worked in this fleet, deliberately simple:
Playwright opens the system Chrome (``channel="chrome"``; the bundled Chromium
when Chrome is missing) headless at the given viewport, navigates to the URL,
waits until the page's ``#lanes`` element exists (``--wait-for`` names another
selector for another page), then takes a screenshot every
``1/fps`` seconds into a temp directory (``frame-%04d.png``). ffmpeg turns
those frames into ``<name>.webm`` (libvpx-vp9, crf 32) and ``<name>.gif``
(palettegen / paletteuse, 800 px wide). The last line on stdout is one JSON
object with both paths and their byte sizes; everything else goes to stderr.

Two flags skip the browser: ``--dry-run`` prints the plan (viewport, frame
count, the exact ffmpeg commands) as JSON and exits 0 without importing
Playwright; ``--frames DIR`` encodes frames already on disk — the temp
directory is kept and named when ffmpeg fails, so a bad encode can be redone
without recording again.

Exit codes follow sysexits.h, like the rest of Structor's CLIs:
  0   done            64  usage (bad flag values)
  66  the page did not load: no ``--wait-for`` element within the timeout
  69  Chrome / Chromium could not start (or Playwright is not installed —
      run it through ``uvx --with playwright python …``)
  70  ffmpeg is missing or failed
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EX_USAGE = 64
EX_NOINPUT = 66
EX_UNAVAILABLE = 69
EX_SOFTWARE = 70

FRAME_PATTERN = "frame-%04d.png"
DEFAULT_WAIT_FOR = "#lanes"  # live.html's lane grid; another page names its own element with --wait-for
PAGE_TIMEOUT_MS = 30_000  # navigation + waiting for the --wait-for element
SHOT_TIMEOUT_MS = 10_000  # one screenshot; the console pages are known to hang here, live.html must not
GIF_WIDTH = 800
VP9_CRF = 32


def log(msg: str) -> None:
    """Progress and errors go to stderr; stdout carries only the final JSON line."""
    print(msg, file=sys.stderr, flush=True)


# --- the plan ----------------------------------------------------------------


def ffmpeg_commands(frames_dir: Path, fps: int, webm: Path, gif: Path) -> dict[str, list[str]]:
    """The two ffmpeg invocations, as argv lists, for a frame directory.

    Both read ``frames_dir/frame-%04d.png`` at ``fps`` frames per second.
    WebM: VP9 in constant-quality mode (``-b:v 0`` is what makes ``-crf`` the
    only knob), dimensions rounded down to even because yuv420p needs that.
    GIF: two-pass palette — ``palettegen`` on the frame-to-frame differences,
    ``paletteuse`` rewriting only the rectangle that changed — which is what
    keeps a mostly-static dark UI small.
    """
    inp = str(frames_dir / FRAME_PATTERN)
    common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps), "-i", inp]
    webm_cmd = common + [
        "-an",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libvpx-vp9", "-crf", str(VP9_CRF), "-b:v", "0",
        "-row-mt", "1", "-deadline", "good", "-cpu-used", "2",
        "-pix_fmt", "yuv420p",
        str(webm),
    ]
    gif_filter = (
        f"fps={fps},scale={GIF_WIDTH}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    gif_cmd = common + ["-vf", gif_filter, "-loop", "0", str(gif)]
    return {"webm": webm_cmd, "gif": gif_cmd}


def plan(args: argparse.Namespace, frames_dir: Path) -> dict:
    """Everything the run will do, as data — what ``--dry-run`` prints."""
    out = Path(args.out)
    webm = out / f"{args.name}.webm"
    gif = out / f"{args.name}.gif"
    return {
        "url": args.url,
        "viewport": {"width": args.width, "height": args.height},
        "seconds": args.seconds,
        "fps": args.fps,
        "frames": args.seconds * args.fps,
        "warmup": args.warmup,
        "browser": ["chrome", "chromium"],  # tried in this order
        "wait_for": args.wait_for,
        "frames_dir": str(frames_dir),
        "frame_pattern": FRAME_PATTERN,
        "out": {"webm": str(webm), "gif": str(gif)},
        "ffmpeg": ffmpeg_commands(frames_dir, args.fps, webm, gif),
    }


# --- capture -----------------------------------------------------------------


def launch(playwright):
    """System Chrome first, bundled Chromium second. Returns (browser, label)."""
    from playwright.sync_api import Error as PlaywrightError  # every launch failure is one of these

    try:
        return playwright.chromium.launch(channel="chrome", headless=True), "chrome"
    except PlaywrightError as e:
        log(f"record-live: system Chrome did not start ({str(e).splitlines()[0]}); trying bundled Chromium")
    return playwright.chromium.launch(headless=True), "chromium"


def capture(args: argparse.Namespace, frames_dir: Path) -> dict:
    """Open the page and screenshot it on a fixed schedule. Returns capture stats."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("record-live: Playwright is not installed here — run:  uvx --with playwright python scripts/record-live.py …")
        sys.exit(EX_UNAVAILABLE)

    total = args.seconds * args.fps
    interval = 1.0 / args.fps
    with sync_playwright() as p:
        try:
            browser, label = launch(p)
        except PlaywrightError as e:
            log(f"record-live: no browser could start: {str(e).splitlines()[0]}")
            log("record-live: install one with:  uvx --with playwright playwright install chromium")
            sys.exit(EX_UNAVAILABLE)
        try:
            # dark scheme and motion allowed on purpose: the page is dark and the
            # card "arrive" transition is the thing being recorded
            context = browser.new_context(
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=1,
                color_scheme="dark",
                reduced_motion="no-preference",
            )
            page = context.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_selector(args.wait_for, state="attached", timeout=PAGE_TIMEOUT_MS)
            except PlaywrightError as e:  # navigation errors and timeouts are both this class
                log(f"record-live: {args.url} did not produce a page with {args.wait_for}: {str(e).splitlines()[0]}")
                sys.exit(EX_NOINPUT)
            if args.warmup > 0:
                time.sleep(args.warmup)  # let the first fetch land so frame 0 is not an empty grid

            log(f"record-live: {label} at {args.width}x{args.height}, {total} frames over {args.seconds}s")
            started = time.monotonic()
            for i in range(total):
                # a fixed schedule: frame i is due at t0 + i/fps, whatever the
                # previous screenshot cost; a slow capture shows up in wall_seconds
                due = started + i * interval
                now = time.monotonic()
                if due > now:
                    time.sleep(due - now)
                page.screenshot(path=str(frames_dir / (FRAME_PATTERN % i)), timeout=SHOT_TIMEOUT_MS)
            wall = time.monotonic() - started
        finally:
            browser.close()
    return {"browser": label, "frames": total, "wall_seconds": round(wall, 2)}


# --- encode ------------------------------------------------------------------


def encode(frames_dir: Path, fps: int, webm: Path, gif: Path) -> None:
    """Run both ffmpeg commands; any failure is exit 70 with the frames kept."""
    if shutil.which("ffmpeg") is None:
        log("record-live: ffmpeg not found on PATH")
        sys.exit(EX_SOFTWARE)
    if not (frames_dir / (FRAME_PATTERN % 0)).exists():
        log(f"record-live: no {FRAME_PATTERN % 0} in {frames_dir}")
        sys.exit(EX_NOINPUT)
    webm.parent.mkdir(parents=True, exist_ok=True)
    for kind, cmd in ffmpeg_commands(frames_dir, fps, webm, gif).items():
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)  # the exit code is handled here
        if res.returncode != 0:
            log(f"record-live: ffmpeg failed on the {kind} (exit {res.returncode}):\n{res.stderr.strip()}")
            log(f"record-live: frames kept in {frames_dir} — re-encode with:  --frames {frames_dir}")
            sys.exit(EX_SOFTWARE)


# --- main --------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; Structor's CLIs use 64 (EX_USAGE)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        log(f"record-live: {message}")
        sys.exit(EX_USAGE)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = Parser(description="Record the live jsonl page as WebM + GIF.")
    p.add_argument("--url", help="page to record, e.g. http://127.0.0.1:8094/live.html?target=local&mode=replay")
    p.add_argument("--seconds", type=int, default=20, help="recording length (default 20)")
    p.add_argument("--fps", type=int, default=4, help="screenshots per second (default 4)")
    p.add_argument("--width", type=int, default=1280, help="viewport width (default 1280)")
    p.add_argument("--height", type=int, default=720, help="viewport height (default 720)")
    p.add_argument("--wait-for", default=DEFAULT_WAIT_FOR, metavar="SELECTOR",
                   help=f"CSS selector that must exist before frame 0 (default {DEFAULT_WAIT_FOR}); "
                        "pick one that appears with the data, not the empty shell")
    p.add_argument("--warmup", type=float, default=1.0, help="seconds to wait after --wait-for before frame 0 (default 1)")
    p.add_argument("--out", default="recordings", help="output directory, created if missing (default recordings)")
    p.add_argument("--name", default="live-jsonl", help="basename of <name>.webm and <name>.gif (default live-jsonl)")
    p.add_argument("--frames", metavar="DIR", help="encode this frame directory instead of recording (no browser)")
    p.add_argument("--dry-run", action="store_true", help="print the plan as JSON and exit; no browser, no ffmpeg")
    args = p.parse_args(argv)

    if args.seconds < 1 or args.fps < 1:
        p.error("--seconds and --fps must be 1 or more")
    if args.width < 16 or args.height < 16:
        p.error("--width and --height must be 16 or more")
    if args.warmup < 0:
        p.error("--warmup cannot be negative")
    if not args.frames and not args.url:
        p.error("--url is required (or --frames DIR to encode without a browser)")
    if args.frames and not Path(args.frames).is_dir():
        p.error(f"--frames {args.frames} is not a directory")
    if "/" in args.name or not args.name:
        p.error("--name is a basename, not a path")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)

    if args.dry_run:
        # a placeholder path: the real temp dir only exists once a recording starts
        print(json.dumps({"dry_run": True, **plan(args, Path(tempfile.gettempdir()) / "record-live-XXXXXX")}))
        return 0

    stats: dict = {}
    if args.frames:
        frames_dir = Path(args.frames)
        stats["frames"] = sum(1 for _ in frames_dir.glob("frame-*.png"))
    else:
        frames_dir = Path(tempfile.mkdtemp(prefix="record-live-"))
        try:
            stats = capture(args, frames_dir)
        except BaseException:
            # exit 66/69 from inside capture(), or Ctrl-C: no recording exists, so
            # nothing is worth keeping — only a failed *encode* keeps its frames
            shutil.rmtree(frames_dir, ignore_errors=True)
            raise

    out = Path(args.out)
    webm, gif = out / f"{args.name}.webm", out / f"{args.name}.gif"
    encode(frames_dir, args.fps, webm, gif)
    if not args.frames:
        shutil.rmtree(frames_dir, ignore_errors=True)  # frames were ours; the encode succeeded

    result = {
        "webm": str(webm), "webm_bytes": webm.stat().st_size,
        "gif": str(gif), "gif_bytes": gif.stat().st_size,
        "fps": args.fps, "seconds": args.seconds, **stats,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
