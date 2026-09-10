"""Process entry: replicas + admin API in one process.

    structor-lance serve [--http 127.0.0.1:8094] [--data ../lance_data_py] [--targets local,kvmlab1]
                         [--interval 15] [--no-sync] [--once]

Env: STRUCTOR_LANCE_PY_HTTP, STRUCTOR_LANCE_PY_DATA, STRUCTOR_LANCE_TARGETS, STRUCTOR_LANCE_INTERVAL

The admin port is the writer mutex: ``--once`` asks a running process on that
port to pull instead of opening the same tables from here; ``--no-sync`` starts
the admin read-only (every POST under /api/ answers 405).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import FrameType

import httpx
import uvicorn

from .admin import create_app
from .sync import Replica
from .targets import load_targets

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8094  # 8092 is the Bun edition, 8093 an unrelated lab on this Mac
DEFAULT_HTTP = f"{DEFAULT_HOST}:{DEFAULT_PORT}"
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "lance_data_py"  # app/lance_data_py
MIN_INTERVAL = 3.0
NO_TARGETS = 78  # EX_CONFIG
BAD_USAGE = 64  # EX_USAGE


def log(line: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def _port(raw: str) -> int:
    """A port number, or a usage error — never a host silently parsed as one."""
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not 1 <= port <= 65535:
        print(f"--http: {raw!r} is not a port number (1-65535); expected host:port, :port or port", file=sys.stderr)
        raise SystemExit(BAD_USAGE)
    return port


def split_http(http: str) -> tuple[str, int]:
    """``host:port`` → both, with every short form the flag actually gets typed as.

    ``:8094`` and a bare ``8094`` are the default host on that port; a bare
    hostname is that host on the default port; ``[::1]:8094`` keeps its
    brackets. A port that is not a number is a usage error, because guessing
    (the old ``rpartition``) turned ``--http 8102`` into a host named 8102 and
    bound the default port instead.
    """
    s = (http or "").strip()
    if not s:
        return DEFAULT_HOST, DEFAULT_PORT
    if ":" not in s or (s.startswith("[") and s.endswith("]")):  # bare token, or a bracketed IPv6 with no port
        return (DEFAULT_HOST, _port(s)) if s.isdigit() else (s, DEFAULT_PORT)
    if s.count(":") > 1 and not s.startswith("["):  # an unbracketed IPv6 address (::1): no port can be told apart
        return s, DEFAULT_PORT
    host, _, port = s.rpartition(":")
    return host or DEFAULT_HOST, _port(port) if port else DEFAULT_PORT


def serve(http: str = DEFAULT_HTTP, data: Path = DEFAULT_DATA, targets: list[str] | None = None,
          interval: float = 15.0, no_sync: bool = False, once: bool = False) -> None:
    # normalised once, here: --once builds a url out of it and uvicorn binds it
    host, port = split_http(http)
    http = f"{host}:{port}"
    data_root = Path(data).resolve()
    chosen = load_targets(targets or None)
    if not chosen:
        print("no targets: expected ~/.config/structor/<name>.json with url, admin_email, admin_password", file=sys.stderr)
        raise SystemExit(NO_TARGETS)

    # A failure in a sync loop or a realtime stream must not take the admin down
    # with it; log and carry on. Both loops catch their own errors, this is the
    # backstop.
    _install_exception_logging()

    replicas: dict[str, Replica] = {t.name: Replica(t, data_root) for t in chosen}
    log(f"targets: {', '.join(f'{t.name} ({t.url})' for t in chosen)} → {data_root}")

    if once:
        _sync_once(replicas, http, data_root)
        return

    version = os.environ.get("STRUCTOR_LANCE_VERSION", "dev")
    app = create_app(replicas, version=version, data_root=data_root, read_only=no_sync)
    log(f"admin: http://{host}:{port}/{' (read-only: --no-sync)' if no_sync else ''}")

    def shutdown(_sig: int, _frame: FrameType | None) -> None:
        for r in replicas.values():
            r.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if not no_sync:
        for r in replicas.values():
            threading.Thread(target=_follow, args=(r, max(MIN_INTERVAL, interval)), name=f"follow-{r.target.name}",
                             daemon=True).start()

    # uvicorn takes SIGINT/SIGTERM over while it runs and returns once it has
    # drained, so the replicas are stopped on the way out rather than in a handler.
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    try:
        server.run()
    finally:
        for r in replicas.values():
            r.stop()


def _follow(r: Replica, interval: float) -> None:
    try:
        r.follow(interval, log)
    except Exception as e:  # noqa: BLE001
        log(f"{r.target.name}: follow stopped: {e}")


def _sync_once(replicas: dict[str, Replica], http: str, data_root: Path) -> None:
    """One pull per target. The admin port is the writer mutex: when a replica is
    already serving this same data directory, ask that process to pull instead of
    opening the same tables from here."""
    delegated = False
    try:
        r = httpx.get(f"http://{http}/api/status", timeout=2.0)
        if r.status_code == 200 and r.json().get("dataRoot") == str(data_root):
            delegated = True
    except Exception as e:  # noqa: BLE001 — nothing listening on that port: sync directly
        log(f"no replica on {http} ({e.__class__.__name__}); syncing here")
    for rep in replicas.values():
        if delegated:
            res = httpx.post(f"http://{http}/api/{rep.target.name}/sync", timeout=600.0)
            j = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
            err = f" (error: {j['error']})" if j.get("error") else ""
            log(f"{rep.target.name}: +{j.get('pulled', 0)} rows (via the running replica on {http}){err}")
        else:
            n = rep.sync_now()
            err = f" (error: {rep.state['lastError']})" if rep.state.get("lastError") else ""
            log(f"{rep.target.name}: +{n} rows{err}")


def _install_exception_logging() -> None:
    def on_exception(kind: type[BaseException], value: BaseException, tb: object) -> None:
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(kind, value, tb)  # type: ignore[arg-type]
            return
        log(f"uncaught exception: {value}")

    sys.excepthook = on_exception  # type: ignore[assignment]
    threading.excepthook = lambda a: log(f"uncaught exception in {a.thread.name if a.thread else '?'}: {a.exc_value}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="structor-lance-py", description="LanceDB replica of a Structor PocketBase store")
    p.add_argument("--http", default=os.environ.get("STRUCTOR_LANCE_PY_HTTP", DEFAULT_HTTP), help="host:port for the admin API")
    p.add_argument("--data", default=os.environ.get("STRUCTOR_LANCE_PY_DATA", str(DEFAULT_DATA)), help="data root holding <target>/")
    p.add_argument("--targets", default=os.environ.get("STRUCTOR_LANCE_TARGETS", ""), help="comma-separated target names")
    p.add_argument("--interval", type=float, default=float(os.environ.get("STRUCTOR_LANCE_INTERVAL", "15")), help="seconds between pulls")
    p.add_argument("--no-sync", action="store_true", help="serve the admin read-only: every POST under /api/ answers 405")
    p.add_argument("--once", action="store_true", help="one pull for every target, then exit")
    a = p.parse_args(argv)
    only = [s.strip() for s in a.targets.split(",") if s.strip()]
    serve(http=a.http, data=Path(a.data), targets=only, interval=a.interval, no_sync=a.no_sync, once=a.once)


if __name__ == "__main__":
    main()
