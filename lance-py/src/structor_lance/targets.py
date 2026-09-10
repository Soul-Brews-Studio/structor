"""Which Structor stores to replicate.

Same rule as the Bun edition and ``scripts/agent.sh``: every
``~/.config/structor/<name>.json`` with ``url``, ``admin_email`` and
``admin_password`` is a target, and ``local`` exists even without a file (the
dev defaults from the Makefile). Passwords never leave this process.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

CONF_DIR = Path(os.environ.get("STRUCTOR_CONF_DIR", Path.home() / ".config" / "structor"))
RESERVED = {"tray", "lance"}  # config files that are not targets

LOCAL_DEFAULT = {"url": "http://127.0.0.1:8091", "email": "admin@structor.local", "password": "structor-dev-password"}


@dataclass(frozen=True)
class Target:
    name: str
    url: str
    email: str
    password: str

    def __repr__(self) -> str:  # never print the password
        return f"Target(name={self.name!r}, url={self.url!r}, email={self.email!r})"


def load_targets(only: list[str] | None = None) -> list[Target]:
    out: dict[str, Target] = {"local": Target("local", **LOCAL_DEFAULT)}
    if CONF_DIR.is_dir():
        for f in sorted(CONF_DIR.glob("*.json")):
            name = f.stem
            if name in RESERVED:
                continue
            try:
                j = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            base = LOCAL_DEFAULT if name == "local" else {"url": "", "email": "", "password": ""}
            url = (j.get("url") or base["url"]).rstrip("/")
            email = j.get("admin_email") or base["email"]
            password = j.get("admin_password") or base["password"]
            if url and email and password:
                out[name] = Target(name, url, email, password)
    targets = list(out.values())
    if only:
        targets = [t for t in targets if t.name in only]
    return targets
