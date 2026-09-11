# structor-dream

Dream pages over a `structor-lance` replica: model-generated insight notes —
recurring patterns, decisions, lessons, contradictions, abandoned threads,
open questions and a handful of one-sentence insights — written into the
maintainers' wiki so that `structor-lance ask` cites them beside the raw
transcript events. The transcripts are what happened; the wiki is what the
maintainers decided it meant; a dream page is what a model *inferred* it
meant, and every page says so (Rule 6: model-generated, inference, not
measurement, never signed as a person).

Three modes:

| command | material | page |
|---|---|---|
| `week [2026-W37]` | up to 40 sessions of the week, digested one by one, then reduced | `<dream_dir>/2026-W37.md` |
| `topic "409 offset mismatch"` | hybrid hits stratified by time horizon and project, one call | `<dream_dir>/topic-409-offset-mismatch.md` |
| `nightly` | the current week plus every week whose ledger moved since its page | both, then one wiki re-index |
| `draw 2026-W37` | the page's `image_prompt`, handed to an image engine (Codex CLI) | `<dream_dir>/2026-W37.png`, linked under the page's title |

It is a thin layer: the replica, the embedding pool, the chat host, the
fences that make transcript text safe to show a model, and the wiki table
are all `structor_lance`'s (`../lance-py`, an editable path dependency).
Nothing here walks a jsonl file or embeds a row.

## Run

```sh
uv sync                                                   # or: make dream-install, just install
uv run structor-dream week 2026-W37 --max-sessions 12     # a bounded first run; --force re-digests
uv run structor-dream week                                # this ISO week (Asia/Bangkok), cap 40
uv run structor-dream topic "409 offset mismatch" --k 48
uv run structor-dream nightly                             # what launchd runs at 03:30
uv run structor-dream draw 2026-W37                       # by hand: the page's image prompt → 2026-W37.png via Codex
```

**Drawing.** Every reduce also asks the model for an `image_prompt`: one
paragraph, at most 80 words, describing a single still illustration of the
page — objects, light, mood, a style hint, no text or names in the picture. It
goes through the same instruction filter as everything else the model wrote
(a description that reads as an instruction is dropped whole), is capped at
600 characters, and is written into the page's frontmatter and its "Image
prompt" section. `draw` hands that prompt to an engine and puts the result
beside the page: `<stem>.png`, an `image:` frontmatter line, and the image
under the title; a later re-dream keeps the picture. The one engine is
`codex`: the Codex CLI (`codex exec`, sandboxed to a scratch directory) with
its image-generation tool — measured 2026-09-11, `codex-cli 0.154` returned a
1200×630 PNG in 78 s. The output must be a PNG under 6 MB or it is refused.
`draw` is never part of `nightly`: it spends a paid account, so it is run by
hand on the pages worth a picture, and it is idempotent (`--force` to redraw).

`just --list` has a recipe for each. Every command takes `--target` (a
`structor-lance` target, default `local`), `--model` (an Ollama chat model,
default `chat_model` in `lance.json`, else `gemma3:27b` — or `codex` /
`codex:<model>` to use the Codex CLI instead, see below), `--no-index` and
`--json`; `nightly` always ends with one JSON line on stdout and its progress
on stderr.

**Codex as the chat model.** `--model codex` turns every prompt the dream
would send to the Ollama host — one digest per session, one reduce per
week or topic — into one `codex exec` turn in a read-only sandbox over an
empty scratch directory, the system prompt as an `<instructions>` block, the
final message as the reply. The same JSON parser, citation checks and caps
run on it. Measured 2026-09-11: a topic reduce took 74 s and ~20k tokens of
the account; a week at the default cap is 41 such calls. The Ollama path
sets `num_ctx 12288 / num_predict 4096` so the reduce prompt and its reply
fit — before that the topic reduce came back cut off mid-sentence.

Configuration is `~/.config/structor/lance.json`, the file `ask` reads:
`ollama_urls` (the embedding pool), `chat_url` / `chat_model` (the chat host),
`wiki_dir` (the maintainers' wiki), and optionally `dream_dir`. Pages go to
`STRUCTOR_DREAM_DIR`, else `dream_dir`, else `<wiki_dir>/dreams`; with none of
those the commands exit 78 (EX_CONFIG) and say what to set. The directory is
configuration, not source — no private path appears in this package.

## What a run does

**week.** The sessions of the week come from `session_weeks`. Conversational
ones (at least two human turns and ten events) are ranked by human turns
inside each project and taken round-robin across projects until
`--max-sessions`, so no one project fills the sample and the choice is
deterministic. Each chosen session is **digested** in one chat call: its
user/assistant turns of at least 80 characters, in time order, sampled to a
6,000-character budget — every human turn first, then assistant turns spaced
evenly through the session — and fenced exactly as `ask` fences its context
(`<event n=…>`, closers neutralised, frame-shaped lines marked,
instruction-like lines dropped and counted). The model answers JSON only
(`summary`, `decisions`, `lessons`, `pain`, `open`); every bullet must end
with the fence numbers it rests on, and a bullet citing nothing that was shown
is dropped in code. Numbers are mapped to event ids here; the model never
handles an id. Two digests run in flight (the chat host queues on one GPU; two
keep it busy). A reply that is not JSON is retried once with "reply with JSON
only", then that session is skipped and counted as failed.

Digests are **cached** at `<data>/<target>/dreams/digest_state.json` beside
the replica's tables, one entry per `(session, week)` with the
`session_weeks.event_count` it was made from: a second run of the same week
digests nothing, a session that grew is digested again, `--force` redoes them
all. Nothing is deleted from the cache — a re-dream replaces a page, never a
digest.

The **reduce** is one more call: the digests, fenced as `<digest n=…>` in the
same stratified order, plus the previous week's `## Insights` section (fenced
as `<previous_week>`) when that page exists, so the model can name
contradictions and drift. It writes `patterns`, `decisions`, `lessons`,
`contradictions`, `abandoned`, `open` and exactly five `insights`, each
sentence cited by digest number and validated the same way; a section left
with no valid claim is written as "(the model supported no claim here)", and
the model's own "which insight could not be supported and why" is kept in its
words. The reduce input is capped at 24,000 characters: digests are added in
stratified order until the budget is spent, and the page says how many of the
week's digests it rests on. (The alternative — two reduce calls and a merge —
needs a third call to rank the insights and hides contradictions between the
halves; the cap keeps one call and one ranking.)

The **page** carries frontmatter (`kind: dream`, `mode: week`, `week`,
`generated_by`, `generated_at`, `status: inference`, `sessions_in_week`,
`sessions_digested`, `events_in_week`, `projects`, `sources`), a "How this was
made" paragraph with the model, counts, caps and date, the sections with every
bullet ending in `[abcd1234, …]` — the first eight characters of the transcript
uuids — and `## Sources`, one line per cited session: full uuid, project,
first and last timestamp, and the event ids its digest cited. The body carries
no transcript text beyond the phrases the digests kept (a verbatim run is cut
at 120 characters, see Safety); the reduce never saw a raw turn, only digests.
A page is rewritten only when its content changed (the `generated_at` stamp
alone does not count), so an unchanged page keeps its mtime — except that a
week page also records `ledger_at`, the newest `updated` / `last_ts` of its
`session_weeks` rows, so a page whose text came out the same after the ledger
moved is still rewritten with the new stamp and not dreamed again the next
night. A week with rows that yields no digest for a reason that will not change
by itself (nothing conversational, nothing to read, every session given up on
after three nights without JSON) gets an *empty page* (`status: empty`, the
reason in one sentence) for the same purpose; a week where a session failed
for a reason that may pass (the chat host down) gets no page and is retried.

**topic.** One hybrid search (bge-m3 vectors plus full text over
`event_vectors`, rows of at least 80 characters, user/assistant only) for
`2k` hits, then a relevance floor (hits under half the best score are noise
for the theme — the FTS-only lab version was mostly noise without one), then
buckets by age computed at run time and never stored — short ≤ 7 days, mid ≤
30, long ≤ 90, archive beyond — with up to `k/4` per bucket taken round-robin
across projects. One call asks for patterns across horizons and projects,
contradictions between short-term and long-term memory, abandoned threads and
three to five insights, cited by event number and validated as above. The
page has a per-horizon material table (one quoted phrase of at most 120
characters per hit, through the same header filter `ask` uses) and Sources
with event and session ids. `--out` overrides the path.

**nightly.** Weeks due are the current ISO week and any week whose
`session_weeks` rows moved (max of `updated` / `last_ts`) past the `ledger_at`
in that week's page (a page from before that field existed is judged by its
`generated_at`) — a week with rows and no page is due too, and a week with
rows and nothing to dream gets an empty page so it is not.
Each is run as `week` without an index pass; then one hash-incremental
`wiki.index_dir` over the wiki directory, which embeds only the sections that
changed. The summary line is
`{"weeks": [...], "digested": n, "skipped": n, "pages": [...], "failed": [...], "errors": [...], "indexed": {...}, "elapsed_s": s}`
and the exit status is 0 unless nothing is configured, so a night with a dead
chat host is a logged line and not a launchd retry storm.

## Safety

Everything the model reads goes through the same three defences as `ask`
(`safe_meta` on header fields, `ROLE_LINE` marking on frame-shaped lines,
`drop_instruction_lines` on bodies — measured necessary on gemma3:27b, which
obeyed a planted "from now on" line until they existed), the reminder after
the context block that the content is quoted data, and a system prompt that
says the material may contain instructions and to report, never obey. A
digest is model text about quoted data and can echo an instruction, so it is
fenced and filtered again before the reduce sees it. Citations are the
contract: the model is asked to cite, but what reaches a page is only what the
code could tie to material it actually showed.

Quotes are capped in code too. Wherever the model read raw transcript text (a
digest, a topic), every bullet and summary it wrote is compared against the
turns it was shown, and any verbatim run longer than 120 characters
(`PHRASE_CAP`) is cut to the cap with an ellipsis; a bullet is at most 300
characters on top of that. A paraphrase is the model's own text and passes —
the rule is about copies, and it bounds the length of a copy, not how many
short ones a bullet holds. The topic page's material table quotes one phrase
of at most 120 characters per hit through the same header filter `ask` uses.

One run per replica at a time: every command takes an `flock` on
`<data>/<target>/dreams/dream.lock` and exits 75 (EX_TEMPFAIL) when another
`structor-dream` holds it, since the digest cache is written whole and two
concurrent runs would erase each other's digests. `nightly` still prints its
JSON line (with a `locked:` error) so the launchd log says what happened.

## Indexing

When `dream_dir` is under `wiki_dir` (the default), a run that wrote a page
re-indexes the wiki with `structor_lance.wiki.index_dir`, which compares text
hashes and embeds only the new or changed sections; `ask` then finds the page
as a `kind: wiki` source with a path under `dreams/`. The wiki table mirrors
exactly one directory per replica, so a `dream_dir` elsewhere is *not* indexed
on its own — that would empty the table of every other page — and the run
says so instead; put the directory under the wiki, or symlink it there.

## Test

```sh
uv run ruff check src tests
uv run pytest -q          # ~2 s, no GPU: a seeded store in tmp_path, a scripted chat, lance-py's fake embedder
```

The tests reuse lance-py's `FakeBag` embedding function and `FakeChat`
pattern (imported from `../lance-py/tests` by path) and a scripted chat that
cites the fence numbers it can see plus a bogus one, so the citation checks,
the cache, the page and all three commands are exercised end to end,
including a wiki re-index and an `ask` that cites the page.

## Layout

| file | what it owns |
|---|---|
| `config.py` | `dream_dir()` precedence, the cache path, ISO-week helpers (Asia/Bangkok) |
| `material.py` | the week's sessions and their stratification; a session's turns within budget; hits by horizon |
| `digest.py` | the model client (`chat_json`), fences and citation checks, one session's digest, the week's map step |
| `state.py` | the digest cache, atomic and per `(session, week)` |
| `reduce.py` | digests → a week's sections; hits → a topic's sections |
| `page.py` | frontmatter, the Rule 6 paragraph, sections, sources, change-only writes |
| `cli.py` | `week`, `topic`, `nightly`, the re-index rule, the exit codes |
