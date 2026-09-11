"""Codex as the chat model, without running Codex: the command line it builds and how its reply is read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dream_fixtures import seed_store

from structor_dream import cli, codex


def test_codex_chat_builds_one_exec_call_per_prompt_and_yields_its_final_message(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []

    def fake_run(cmd, capture_output, text, timeout, check):
        seen.append(cmd)
        out = Path(cmd[cmd.index("-o") + 1])
        assert "--output-schema" not in cmd                                   # a permissive schema fails the run
        assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "read-only" and "--skip-git-repo-check" in cmd
        assert cmd[-1].startswith("<instructions>\nyou are the reducer\n</instructions>\n\nhere are the digests")
        assert cmd[-1].endswith(codex.JSON_ONLY_TAIL)
        out.write_text('{"patterns": ["a [1]"], "image_prompt": "a desk"}', encoding="utf-8")

        class Proc:
            returncode, stdout, stderr = 0, "", ""

        return Proc()

    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    monkeypatch.setattr(codex, "codex_binary", lambda: "/usr/local/bin/codex")
    chat = codex.CodexChat(model="gpt-6", timeout=7)
    reply = "".join(chat([{"role": "system", "content": "you are the reducer"}, {"role": "user", "content": "here are the digests"}]))
    assert json.loads(reply)["image_prompt"] == "a desk" and chat.calls == 1
    assert seen[0][0] == "/usr/local/bin/codex" and seen[0][1] == "exec" and "-m" in seen[0] and seen[0][seen[0].index("-m") + 1] == "gpt-6"
    assert not Path(seen[0][seen[0].index("-C") + 1]).exists()                     # the scratch dir is gone

    assert codex.model_of("codex") == "" and codex.model_of("codex:gpt-6") == "gpt-6"
    assert codex.prompt_of([{"role": "user", "content": "only"}]) == "only"


def test_codex_chat_reports_a_missing_binary_a_timeout_and_a_silent_exit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(codex, "codex_binary", lambda: "")
    with pytest.raises(RuntimeError, match="codex not found"):
        list(codex.CodexChat()([{"role": "user", "content": "x"}]))

    monkeypatch.setattr(codex, "codex_binary", lambda: "/bin/codex")

    def timeout_run(cmd, **kw):
        raise codex.subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(codex.subprocess, "run", timeout_run)
    with pytest.raises(RuntimeError, match="did not answer in 3s"):
        list(codex.CodexChat(timeout=3)([{"role": "user", "content": "x"}]))

    def silent_run(cmd, **kw):
        class Proc:
            returncode, stdout, stderr = 1, "", "error: not logged in"

        return Proc()

    monkeypatch.setattr(codex.subprocess, "run", silent_run)
    with pytest.raises(RuntimeError, match="exit 1 with no final message: error: not logged in"):
        list(codex.CodexChat()([{"role": "user", "content": "x"}]))


def test_model_codex_wires_the_asker_and_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    r, e = seed_store(tmp_path / "store", embed=False)
    monkeypatch.setenv("STRUCTOR_CONF_DIR", str(tmp_path / "conf"))
    monkeypatch.delenv("STRUCTOR_CHAT_URL", raising=False)
    monkeypatch.delenv("STRUCTOR_OLLAMA_URLS", raising=False)
    a = cli.asker_for(r, e, "codex:gpt-6")
    assert a.url == "codex" and a.model == "codex:gpt-6" and isinstance(a.chat_stream, codex.CodexChat)
    assert a.chat_stream.model == "gpt-6"
    monkeypatch.setattr(codex, "codex_binary", lambda: "")
    assert cli.probe_host(a) == "codex not found on PATH"
    monkeypatch.setattr(codex, "codex_binary", lambda: "/bin/codex")
    assert cli.probe_host(a) == ""

    # the Ollama path gets its context and reply budget
    monkeypatch.setenv("STRUCTOR_CHAT_URL", "http://fake:11434")
    b = cli.asker_for(r, e, "")
    assert b.options["num_ctx"] == cli.OLLAMA_OPTIONS["num_ctx"] and b.options["temperature"] == 0.2
