# Other Observations

Observations, improvements and suggestions, in roughly descending order of how
much I think they matter.

---

## Retrieval is the weak point, not crawling

I spent most of the effort on crawling because that is what the brief emphasised,
and by the end the crawler was honest and resilient. But the thing that actually
degrades answer quality is retrieval, and it is the least sophisticated part of the
system.

**The evidence.** A question about Perplexity's leadership returned an answer
saying the source "does not list any individuals" — while the person cards beside
it correctly named three executives with their titles. The retrieval had picked a
header chunk that contained no names; the extraction pass had seen the whole page.

Two consequences of the same design:

1. **Chunking is fixed-size** (1,000 characters, 150 overlap) with no semantic
   awareness. It will happily split a table of executives down the middle, or
   return a nav header because it happens to contain query words.
2. **Person records do not participate in retrieval.** They are extracted at index
   time and *attached* to results afterwards. Asking "who is the CFO?" retrieves
   chunks by text similarity and then hopes a person record is attached to one of
   them. The structured data — name, title, company — is never actually searched.

**What I would do.** Sentence-aware or heading-aware chunking, so a chunk
corresponds to a section rather than to a character count. And make person records
first-class retrieval targets: a query containing "CFO" should match the person
whose title is "Chief Financial Officer" directly, not by embedding proximity.

I did not make these changes because they alter what search returns and I had no
labelled question set to measure against. Changing ranking on a hunch is guessing,
and it would have been unverifiable.

---

## The LLM/pattern merge prefers the wrong source

Person extraction runs twice: once by the LLM, once by a deterministic pattern
matcher. They are merged with the **model's fields winning** on conflicts.

That is the wrong way round for precision, and I know it is:

| | Strengths | Weaknesses |
|---|---|---|
| **LLM** | Reads prose ("Jane has led the company since 2019 as its chief executive"), extracts biographies and contact details | Needs a key; over-extracts — Apple's board list yields people whose listed organisations are *other* companies (~90 records from 12 URLs) |
| **Pattern matcher** | Precise, deterministic, needs no network | Only reads structure — a name adjacent to a role. Misses prose entirely (~50 records) |

**Suggestion:** invert the precedence. Let the pattern matcher win on `name` and
`title` — the two fields a person card is actually *about* — and use the model only
for biography, email, phone and location, which it alone can read.

Same reasoning as retrieval: I did not do it because it changes output and I had no
labelled set to measure the change against. It is a one-line precedence flip in
`merge_records` and worth doing once there is a test set.

---

## Crawl4AI earns its place, but only conditionally

Measured on real sites rather than assumed:

| URL | Built-in (`httpx`) | Crawl4AI |
|---|---|---|
| `oracle.com/in/corporate/executives/` | 2,237 chars | **5,533 chars** |
| `microsoft.com` | `BLOCKED` | **13,339 chars** |
| `theorg.com/.../leadership-team` (SPA) | `PARTIAL`, 484 chars | **`SUCCESS`, 4,171 chars** |
| `amazon.com` | `PARTIAL` | `BLOCKED` — AWS WAF |

Note that Crawl4AI beat the built-in engine on two sites **with no browser at
all**, through better request fingerprinting alone. The JavaScript SPA is where
Chromium is genuinely required — 8.6× more text.

**Against it:** a vendored litellm fork, `shapely`, `alphashape`, `nltk`,
`rank-bm25` and three Playwright packages. Its markdown also retains more
navigation chrome than trafilatura — the Oracle output opens with
`* [Skip to content](...) * [Accessibility Policy](...)`.

**Suggestion:** worth it if your sources are JavaScript-heavy. Not worth it
otherwise. It is already wired as an opt-in (`CRAWL_ENGINE=crawl4ai`) that degrades
to the built-in engine on any failure, so trying it costs nothing.

---

## Person ranking should use the query, not just similarity

`_rank_people` promotes anyone named in the query or in the synthesised answer to
the front of the cards. That works well — it is what turns "who is the CFO of
Oracle?" into a card for the CFO rather than the first twelve of twenty-five
executives — but it is a *post-hoc* fix for retrieval not knowing about people.

If person records participated in retrieval (see above), this heuristic would be
unnecessary.

---

## The UI reports failures better than it explains successes

The crawl status vocabulary is the strongest part of the UI: `BLOCKED` is visually
distinct from `FAILED` because one is a decision a site made and may reverse, the
other usually will not change. Failure reasons are humanised — "The website denied
automated access to this page" instead of `HTTP 403`.

**The gap:** a `PARTIAL` row says the content was thin, but not *why*. Was it a
JavaScript shell, a cookie wall, a paywall? The crawler knows — it is in
`meta_json` as `browser_unavailable`, `quality`, `blocked_snippet` — but the UI does
not surface it. A "why was this thin?" line on the detail page would be cheap and
would save the next person exactly the debugging I did.

---

## Small things worth doing

* **Git LFS for the demo video.** It is 83 MB in a normal git object. Under
  GitHub's 100 MB limit, so it works, but every clone pays for it.
* **A labelled question set.** Ten questions with expected answers would turn
  every ranking change from an argument into a measurement. This is the single
  highest-value addition to the project, and I would do it before touching
  retrieval.
* **Move jobs to a worker process.** The single-worker constraint is structural,
  not a preference. The index and job registry being per-process is what blocks
  horizontal scaling.
* **Sentence-aware chunking.** Chunk on paragraph and section boundaries rather
  than a character count, so a retrieved chunk is a coherent answer.
* **`robots.txt` crawl-delay.** `CRAWL_DELAY` is currently our own politeness
  setting. Honouring `Crawl-delay` from a site's robots.txt would be more correct.

---

## What I would do differently

**Test the deployed path, not just the library.** The Windows event-loop bug
existed because the browser worked in every script I wrote and failed under the
actual entrypoint. I verified the component and never verified the integration.
That is a category of mistake, not a one-off.

**Write the failure tests first.** The most valuable tests in the suite are the
ones asserting what *should not* happen: a block page must not be a success, a
vendor name must not become a person's surname, a private address must not be
fetched. Each of those was written after the corresponding bug. Writing them first
would have been faster.

**Ask about scope earlier.** I planned the entire application around Django before
checking, and rebuilt it. One question up front would have saved that entirely.
