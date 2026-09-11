"""Rendering a dream page: frontmatter, the Rule 6 paragraph, the sections, the sources.

A page is derived data. Its frontmatter says what made it (``kind: dream``,
``mode``, ``generated_by``, ``generated_at``, ``status: inference``,
``sources``), its first paragraph says how, in words, and every bullet ends
with the sessions or events it rests on — rendered as ``[abcd1234]`` (the
first eight characters of a transcript uuid) in the body, with the full id
in ``## Sources``. A page never carries a verbatim stretch of transcript
longer than ``PHRASE_CAP`` characters: the topic table's phrase is cut there
through ``safe_meta``, and every bullet or summary the model wrote after
reading raw turns was cut wherever it copied more than that
(``digest.quote_limited``) — so at most one short phrase of a turn at a time
reaches a note that the wiki indexes and ``ask`` quotes.

A week page also records ``ledger_at``, the newest ``updated`` / ``last_ts``
among the week's ``session_weeks`` rows when it was made. That is what the
nightly job compares against: when the ledger moves the field moves, so the
page is rewritten even when the model's text came out the same, and the
week is not dreamed again the night after.

``write_if_changed`` keeps an existing page's mtime when only the
``generated_at`` stamp would change: the wiki re-index compares text hashes,
and a page rewritten with identical content would still not re-embed, but a
reader looking at the directory should see the date the content last moved.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from structor_lance import wiki as wiki_table
from structor_lance.rag import safe_meta

from . import material
from .digest import PHRASE_CAP, cited_events
from .reduce import INSIGHTS, TOPIC_INSIGHTS, TOPIC_SECTIONS, WEEK_SECTIONS

NO_CLAIM = "(the model supported no claim here)"
SLUG_CAP = 60
# PHRASE_CAP (from digest, shared with the bullet cutter) is the one quoted phrase per cited event on a topic page
STAMP = re.compile(r"^generated_at:.*$", re.MULTILINE)
WEEK_TITLES = {
    "patterns": "Patterns", "decisions": "Decisions", "lessons": "Lessons",
    "contradictions": "Contradictions with last week", "abandoned": "Abandoned threads", "open": "Open questions",
}
TOPIC_TITLES = {"patterns": "Patterns across horizons and projects",
                "contradictions": "Contradictions (short-term vs long-term)", "abandoned": "Abandoned threads"}
RULE6 = ("This page is model-generated and is inference, not measurement: the transcripts are what happened, "
         "the page is what a model made of them. Every claim ends with the sources it rests on, and those "
         "citations were checked in code against the material the model was shown. Written by structor-dream, "
         "an AI tool, not a person.")


# ---------------------------------------------------------------- pieces


def yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, list):
        return "[" + ", ".join(yaml_value(v) for v in value) + "]"
    text = str(value)
    # quote anything YAML could misread: colons, brackets, a leading symbol, a bare number
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r'[:#\[\]{},"\']|^\s|\s$|^[-?&*!|>%@`]|^\d', text) or not text else text


def frontmatter(fields: dict[str, Any]) -> str:
    return "---\n" + "".join(f"{key}: {yaml_value(value)}\n" for key, value in fields.items()) + "---\n"


def short(sid: str) -> str:
    return (sid or "")[:8] or "unknown"


def cite(ids: list[str], label_of: dict[str, str]) -> str:
    """``[abcd1234, 5678efgh]`` — the labels of the ids in the order they were cited, each once."""
    labels: list[str] = []
    for i in ids:
        label = label_of.get(i, short(i))
        if label not in labels:
            labels.append(label)
    return "[" + ", ".join(labels) + "]"


def bullets(items: list[dict[str, Any]], label_of: dict[str, str]) -> list[str]:
    if not items:
        return [NO_CLAIM]
    return [f"- {b['text']} {cite(b.get('ids') or [], label_of)}" for b in items]


def sources_of(reduced: dict[str, Any], sections: tuple[str, ...]) -> list[str]:
    """Every id cited anywhere in the reduced sections, first-cited order."""
    seen: list[str] = []
    for section in (*sections, "insights"):
        for b in reduced.get(section) or []:
            for i in b.get("ids") or []:
                if i and i not in seen:
                    seen.append(i)
    return seen


def insights_block(reduced: dict[str, Any], label_of: dict[str, str], asked: int | tuple[int, int]) -> list[str]:
    lines = bullets(reduced.get("insights") or [], label_of)
    got = len(reduced.get("insights") or [])
    want = asked if isinstance(asked, int) else asked[0]
    if got < want:
        lines.append(f"\n({got} of {want} insights survived citation checking.)")
    if reduced.get("unsupported"):
        lines.append(f"\n_Unsupported, in the model's words: {reduced['unsupported']}_")
    return lines


def project_name(path: str) -> str:
    return safe_meta(path.rstrip("/").rsplit("/", 1)[-1] if path else "")


NO_IMAGE_PROMPT = "(the model gave no image prompt)"
IMAGE_SUFFIXES = (".png", ".svg", ".jpg", ".webp")


def image_prompt_lines(reduced: dict[str, Any]) -> list[str]:
    """The scene description ``structor-dream draw`` hands to the image engine, or the placeholder."""
    prompt = str(reduced.get("image_prompt") or "").strip()
    return [prompt if prompt else NO_IMAGE_PROMPT]


def image_lines(image: str | None, alt: str) -> list[str]:
    """The markdown image line for a drawn illustration, plus a blank; nothing when there is no image yet."""
    return [f"![{safe_meta(alt)}]({image})", ""] if image else []


def existing_image(page_path: Path) -> str:
    """The illustration beside a page — ``<stem>.png`` (or .svg/.jpg/.webp) — as a filename, or ``""``."""
    for suffix in IMAGE_SUFFIXES:
        candidate = page_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate.name
    return ""


def attach_image(page_path: Path, image: str, alt: str) -> bool:
    """Put a drawn image on an existing page: an ``image:`` frontmatter line and the image line under the H1.

    ``True`` when the page changed. A page that already references the file
    is left alone, so ``draw`` is idempotent. The page keeps its
    ``generated_at``: an illustration is decoration, not a new dream.
    """
    text = page_path.read_text(encoding="utf-8")
    if f"]({image})" in text:
        return False
    lines = text.split("\n")
    if lines and lines[0] == "---":
        try:
            close = lines.index("---", 1)
        except ValueError:
            close = -1
        if close > 0:
            head = [ln for ln in lines[1:close] if not ln.startswith("image:")]
            lines = ["---", *head, f"image: {yaml_value(image)}", *lines[close:]]
    for i, line in enumerate(lines):
        if line.startswith("# "):
            lines[i + 1:i + 1] = ["", *image_lines(image, alt)[:1]]
            break
    page_path.write_text("\n".join(lines), encoding="utf-8")
    return True


# ---------------------------------------------------------------- the week page


def week_page(week: str, made: dict[str, Any], reduced: dict[str, Any], digests: dict[str, dict[str, Any]],
              rows: dict[str, dict[str, Any]], names: dict[str, tuple[str, str]]) -> str:
    """The whole page for one week.

    ``made`` carries the numbers for the frontmatter and the paragraph:
    model, generated_at, ledger_at, sessions_in_week, conversational,
    events_in_week, projects, max_sessions, chosen, digested, skipped, failed,
    previous (the previous week's label when its insights were fed, else "").
    ``digests`` is session → digest entry for every chosen session,
    ``rows`` session → its session_weeks row.
    """
    label_of = {session: short(names.get(session, ("", ""))[0]) for session in digests}
    cited_sessions = sources_of(reduced, WEEK_SECTIONS)
    sources = [names.get(s, ("", ""))[0] or s for s in cited_sessions]
    head = frontmatter({
        "kind": "dream", "mode": "week", "week": week, "generated_by": made["model"],
        "generated_at": made["generated_at"], "ledger_at": made.get("ledger_at") or "", "status": "inference",
        # the description is indexed as searchable text ahead of the page: say what a reader would ask for
        "description": (f"Model-generated dream of {week}: what {len(digests)} sessions across {made['projects']} "
                        "projects kept struggling with, decided, learned, abandoned and left open this week "
                        "(inference, not measurement)"),
        "sessions_in_week": made["sessions_in_week"], "sessions_digested": len(digests),
        "events_in_week": made["events_in_week"], "projects": made["projects"], "sources": sources,
        "image_prompt": reduced.get("image_prompt") or "", "image": made.get("image") or "",
    })
    reduced_n = len(reduced.get("sessions_reduced") or [])
    how = (
        f"Generated by `{made['model']}` on {made['generated_at'][:10]} from the transcripts of {week}: "
        f"{made['sessions_in_week']} sessions with events in the week ({made['conversational']} conversational — "
        f"at least {material.MIN_USER_TURNS} human turns and {material.MIN_EVENTS} events) across "
        f"{made['projects']} projects, {made['events_in_week']} events in all. {made['chosen']} sessions were "
        f"chosen (cap {made['max_sessions']}, ranked by human turns inside each project and spread across "
        f"projects round-robin) and {len(digests)} of them were digested, each digest reading at most "
        f"{material.DIGEST_BUDGET} characters of that session's turns. "
        f"{reduced_n} digests fit the reduce call's {made['reduce_budget']} character budget"
        + (f", with the Insights of {made['previous']} fed for contradictions" if made.get("previous") else "")
        + (f"; {len(made['failed'])} session(s) failed to digest and were left out" if made.get("failed") else "")
        + f". {RULE6}"
    )
    body = [f"# Dream — {week}", "", *image_lines(made.get("image"), f"Illustration of {week}, drawn from the image prompt below"), how, ""]
    # every heading carries the week: the wiki indexes one row per section and searches its text, so a
    # "Dream — 2026-W37" query has to find the Patterns and Open questions rows, not only the preamble
    for section in WEEK_SECTIONS:
        body += [f"## {WEEK_TITLES[section]} — {week}", "", *bullets(reduced.get(section) or [], label_of), ""]
    body += [f"## Insights — {week}", "", *insights_block(reduced, label_of, INSIGHTS), ""]
    body += [f"## Image prompt — {week}", "", *image_prompt_lines(reduced), "", "## Sources", ""]
    for session in cited_sessions:
        sid, project = names.get(session, ("", ""))
        row = rows.get(session) or {}
        events = cited_events(digests.get(session) or {})
        body.append(f"- `{sid or session}` — {project_name(project)} · {str(row.get('first_ts') or '')[:16]} → "
                    f"{str(row.get('last_ts') or '')[:16]} · events cited by its digest: "
                    + (", ".join(f"`{e}`" for e in events) if events else "none"))
    others = [label_of[s] for s in digests if s not in cited_sessions]
    if others:
        body.append(f"\n{len(others)} more session(s) were digested and not cited: {', '.join(others)}.")
    return head + "\n" + "\n".join(body).rstrip() + "\n"


def empty_week_page(week: str, made: dict[str, Any], reason: str) -> str:
    """The page for a week that had rows in the ledger but yielded no digest — so the night after knows it was tried.

    ``status: empty`` instead of ``inference``: the model made nothing here.
    ``reason`` says why in one sentence (no conversational session; every
    chosen session had no turn to read; every one was given up on). The page
    carries ``ledger_at`` like any other, so the week is dreamed again exactly
    when its ledger moves, not every night.
    """
    head = frontmatter({
        "kind": "dream", "mode": "week", "week": week, "generated_by": made["model"],
        "generated_at": made["generated_at"], "ledger_at": made.get("ledger_at") or "", "status": "empty",
        "sessions_in_week": made["sessions_in_week"], "sessions_digested": 0,
        "events_in_week": made["events_in_week"], "projects": made["projects"], "sources": [],
    })
    how = (
        f"Nothing was dreamed for {week} on {made['generated_at'][:10]}: {reason} "
        f"The ledger had {made['sessions_in_week']} sessions with events in the week ({made['conversational']} "
        f"conversational — at least {material.MIN_USER_TURNS} human turns and {material.MIN_EVENTS} events) across "
        f"{made['projects']} projects, {made['events_in_week']} events in all. This page exists so the nightly job "
        f"does not try the same week again until its events change; it is replaced by a real dream when they do. "
        f"Written by structor-dream, an AI tool, not a person."
    )
    return head + "\n" + "\n".join([f"# Dream — {week}", "", how]) + "\n"


# ---------------------------------------------------------------- the topic page


def topic_slug(query: str, cap: int = SLUG_CAP) -> str:
    return wiki_table.slug(query)[:cap].strip("-") or "topic"


def topic_page(query: str, made: dict[str, Any], reduced: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """The whole page for one theme: the material table per horizon, the sections, the sources."""
    by_event = {str(it.get("event_id") or ""): it for it in items}
    label_of = {event_id: f"{it['n']}" for event_id, it in by_event.items()}
    cited_ids = sources_of(reduced, TOPIC_SECTIONS)
    head = frontmatter({
        "kind": "dream", "mode": "topic", "query": safe_meta(query), "generated_by": made["model"],
        "generated_at": made["generated_at"], "status": "inference", "hits": len(items),
        "description": (f"Model-generated dream about \"{safe_meta(query)}\": recurring patterns, short-term vs "
                        "long-term contradictions and abandoned threads across time horizons (inference)"),
        "image_prompt": reduced.get("image_prompt") or "", "image": made.get("image") or "",
        "sources": sorted({by_event[i].get("session_id") or "" for i in cited_ids if i in by_event} - {""}),
    })
    counts = {h: sum(1 for it in items if it.get("horizon") == h) for h in material.HORIZONS}
    how = (
        f"Generated by `{made['model']}` on {made['generated_at'][:10]} for the theme \"{safe_meta(query)}\": "
        f"a hybrid search (bge-m3 vectors + full text) over the conversational events returned {made['retrieved']} "
        f"hits; those under {int(material.RELEVANCE_FLOOR * 100)}% of the best score were dropped"
        + (f", as were {made['dumps']} that read as tool output (line-numbered listings, diffs, JSON blobs)"
           if made.get("dumps") else "")
        + ", the rest were "
        f"bucketed by age at run time (short ≤ {material.HORIZON_DAYS['short']} days, mid ≤ "
        f"{material.HORIZON_DAYS['mid']}, long ≤ {material.HORIZON_DAYS['long']}, archive beyond) and up to "
        f"{made['share']} per bucket were taken round-robin across projects: "
        + ", ".join(f"{counts[h]} {h}" for h in material.HORIZONS)
        + f". The model read {len(reduced.get('used') or [])} of them. {RULE6}"
    )
    body = [f"# Dream — topic: {safe_meta(query)}", "",
            *image_lines(made.get("image"), f"Illustration for the theme \"{safe_meta(query)}\", drawn from the image prompt below"),
            how, "", "## Material", "",
            "| n | horizon | when | project | phrase |", "|---|---|---|---|---|"]
    for it in items:
        phrase = safe_meta(str(it.get("text") or ""), PHRASE_CAP).replace("|", "¦")
        body.append(f"| {it['n']} | {it.get('horizon')} | {str(it.get('ts') or '')[:16]} | "
                    f"{project_name(it.get('project') or '')} | \"{phrase}\" |")
    body.append("")
    for section in TOPIC_SECTIONS:
        body += [f"## {TOPIC_TITLES[section]}", "", *bullets(reduced.get(section) or [], label_of), ""]
    body += ["## Insights", "", *insights_block(reduced, label_of, TOPIC_INSIGHTS), ""]
    body += ["## Image prompt", "", *image_prompt_lines(reduced), "", "## Sources", ""]
    for event_id in cited_ids:
        it = by_event.get(event_id) or {}
        body.append(f"- [{it.get('n', '?')}] event `{event_id}` · session `{it.get('session_id') or 'unknown'}` · "
                    f"{project_name(it.get('project') or '')} · {str(it.get('ts') or '')[:16]} · {it.get('horizon')}")
    if not cited_ids:
        body.append(NO_CLAIM)
    return head + "\n" + "\n".join(body).rstrip() + "\n"


# ---------------------------------------------------------------- files


def same_content(old: str, new: str) -> bool:
    """Equal apart from the ``generated_at`` line — a re-run that changed nothing keeps the older stamp."""
    return STAMP.sub("", old) == STAMP.sub("", new)


def write_if_changed(path: Path, text: str) -> bool:
    """Write the page atomically; ``False`` (and no write, so the mtime stays) when the content is unchanged."""
    path = Path(path)
    try:
        if same_content(path.read_text(encoding="utf-8"), text):
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return True


def read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        return wiki_table.frontmatter(Path(path).read_text(encoding="utf-8"))[0]
    except OSError:
        return {}
