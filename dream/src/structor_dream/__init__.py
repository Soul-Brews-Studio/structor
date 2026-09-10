"""structor-dream: dream pages over a structor-lance replica.

A *dream page* is a model-generated insight note — recurring patterns,
decisions, lessons, contradictions, abandoned threads, open questions and a
handful of one-sentence insights — written into the maintainers' wiki so that
``structor-lance ask`` cites it beside the raw transcript events. Three modes:

- ``week``    one page per ISO week, built from per-session digests (map) and
              one reduce call over them;
- ``topic``   one page per question, built from hybrid retrieval stratified by
              time horizon and project;
- ``nightly`` the weeks that changed since their page was written, then one
              wiki re-index — what launchd runs at 03:30.

Everything the model reads goes through the same fences and filters as
``ask`` (``structor_lance.rag``), every citation it writes is checked in code
against the material it was shown, and every page says it is inference, not
measurement (Rule 6). Nothing here is measurement: the transcripts are what
happened, the page is what a model made of them.
"""

__version__ = "0.1.0"
