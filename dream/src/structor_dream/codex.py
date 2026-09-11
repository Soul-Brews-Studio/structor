"""Codex as the dream's chat model: ``codex exec`` in a read-only sandbox, one call per prompt.

``--model codex`` (or ``codex:<model>``) on ``week`` / ``topic`` / ``nightly``
swaps the Ollama host for the Codex CLI. Every prompt the dream would have
sent to gemma3 — the digest of one session, the reduce of a week, the topic
reduce — becomes one ``codex exec`` turn whose final message is the reply.
JSON is forced with ``--output-schema`` (any object), so the reply parses
without a retry, and the same citation checks run on it afterwards.

Codex has no system role on the command line, so the system prompt goes
first as an ``<instructions>`` block and the user prompt follows. The
sandbox is read-only over an empty scratch directory: the model can run
nothing that touches the repo, and the only file it needs to write — its last
message — is written by the CLI, not by the model. A paid account is spent
per call, which is why ``nightly`` only uses it when asked.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

CODEX_TIMEOUT_S = 600
JSON_ONLY_TAIL = "\n\nReply with the JSON object only: no prose before or after it, no code fence."


def codex_binary() -> str:
    return shutil.which("codex") or ""


def model_of(label: str) -> str:
    """``codex`` → ``""`` (the CLI's default), ``codex:gpt-6`` → ``gpt-6``."""
    return label.split(":", 1)[1].strip() if ":" in label else ""


def prompt_of(messages: list[dict[str, str]]) -> str:
    """System prompt as an instructions block, then the user prompt — Codex reads one text."""
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    user = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
    return f"<instructions>\n{system}\n</instructions>\n\n{user}" if system else user


class CodexChat:
    """``Asker.chat_stream``-shaped: called with messages, yields the reply once."""

    def __init__(self, model: str = "", timeout: int = CODEX_TIMEOUT_S):
        self.model = model
        self.timeout = timeout
        self.calls = 0

    def __call__(self, messages: list[dict[str, str]]) -> Iterator[str]:
        codex = codex_binary()
        if not codex:
            raise RuntimeError("codex not found on PATH (install the Codex CLI and run 'codex login')")
        workspace = Path(tempfile.mkdtemp(prefix="structor-dream-codex-"))
        try:
            last = workspace / "last.md"
            # no --output-schema: Codex's structured output wants a closed schema (every key listed,
            # additionalProperties false) and a permissive one fails the run (measured 2026-09-11);
            # the lenient JSON parser plus the "JSON only" retry in chat_json is enough
            cmd = [codex, "exec", "-s", "read-only", "--skip-git-repo-check", "-C", str(workspace), "-o", str(last)]
            if self.model:
                cmd += ["-m", self.model]
            cmd.append(prompt_of(messages) + JSON_ONLY_TAIL)
            self.calls += 1
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as e:
                raise RuntimeError(f"codex did not answer in {self.timeout}s") from e
            if not last.is_file():
                tail = (proc.stderr or proc.stdout or "").strip().split("\n")[-1:]
                raise RuntimeError(f"codex exit {proc.returncode} with no final message" + (f": {tail[0][:160]}" if tail else ""))
            yield last.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
