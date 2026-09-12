# Documentation

The working record for this project: what I had to decide, what went wrong, how
long it took, and what I would do differently.

| Document | Contents |
|---|---|
| [Questions and assumptions](questions-and-assumptions.md) | Decisions the brief left open, what I chose, and why |
| [Difficulties encountered](difficulties.md) | Fourteen problems, including four that were my own mistakes |
| [Development time](development-time.md) | Time taken per task, and where it actually went |
| [Other observations](other-observations.md) | Observations, improvements and suggestions |

---

## The short version

**What was built.** A CSV (or Excel) file of URLs goes in; a searchable knowledge
base comes out. The pipeline is `CSV → validate → crawl → extract → SQLite →
chunk → embed → FAISS → retrieve → Groq → answer with citations`.

**The problem worth solving.** The original crawler reported success on pages it
had not actually retrieved. `microsoft.com` answered HTTP 200 with "Your request
has been blocked" and was stored as a *successful* harvest with one chunk, which
was then embedded and made searchable. The page looked healthy in the UI and
contributed nothing.

The fix was to stop treating the HTTP status as the outcome and classify the
*content* instead. Every URL now ends in a specific state — `SUCCESS`, `PARTIAL`,
`BLOCKED`, `FAILED`, `SKIPPED` — with a reason. A blocked page that says "blocked"
is more useful than a silent partial success.

**The honest position.** No crawler reliably handles arbitrary URLs, and this one
does not either. Amazon sits behind AWS WAF and stays blocked. Microsoft is
blocked over plain HTTP but readable through Crawl4AI. `theorg.com` yields 484
characters without a browser and 4,171 with one. The valuable property is not
success but honesty about failure.

**Where it is weakest.** Retrieval, not crawling. Chunking is fixed-size with no
semantic awareness, and structured person records are attached to results rather
than participating in retrieval. See
[Other observations](other-observations.md#retrieval-is-the-weak-point-not-crawling).

---

## Numbers

| | |
|---|---|
| Tests | 374, offline, ~14 s |
| Estimated effort | ~16 h across 19 tasks |
| Rust code | none — Python 3.10+ |
| Crawl outcomes | 7 states, each with a reason |
| Optional dependencies | Crawl4AI, Playwright, Groq |

---

[← Back to the main README](../README.md)
