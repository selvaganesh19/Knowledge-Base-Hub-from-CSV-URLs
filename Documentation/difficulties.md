# Difficulties encountered

Fourteen problems, roughly in the order they appeared. I have included the four
that were my own mistakes, because they were the most instructive.

---

## The crawler lied

**The largest problem in the project.**

`microsoft.com` answered with HTTP 200 and the body "Your request has been
blocked." Nothing in the HTTP layer marks that as a failure, so it was stored as a
*successful* harvest with one chunk — and that chunk was embedded and made
searchable. The row looked healthy in the UI and contributed nothing.

`amazon.com` did the same with HTTP 202 and an empty body, recorded as "no
extractable text".

**Why it took a while.** Both are served with a 2xx status, so there is no signal
in the transport layer at all. The only evidence is in the content.

**The fix.** Classification moved from the HTTP status to the *content*: a
seven-state vocabulary where `SUCCESS` requires clearing a text-quality floor,
`BLOCKED` covers refusals, and a challenge page arriving with a 200 is caught by
matching the text and markup it carries.

---

## A block page extracts to nothing

The check above initially read only the extracted text — and Amazon's AWS WAF
challenge extracts to a **single character**. So it looked like an empty page
rather than a refusal, and the row read "no extractable text".

**The fix.** Scan the raw HTML for product markers that no real page contains:
`gokuProps` and `awsWafCookieDomainList` (AWS WAF), `cf-chl-` (Cloudflare),
`incapsula`, `perimeterx`, `datadome`. These are unambiguous, so they can be
matched against full markup without false positives.

---

## Trafilatura over-pruned a page into uselessness

Oracle's executive list came back as 1,248 characters of job titles with **every
name removed**. Trafilatura classified the linked cards holding the names as
navigational and dropped them.

**The fix, in two parts.** A DOM-walk fallback, and a heuristic — trafilatura wins
only if it captured at least half the text the DOM walk found. The DOM walk's tag
list had also omitted `<strong>` and `<a>`, which is precisely where executive
names live. Adding them took the same page from 0 people to 25.

---

## The pattern extractor produced confident nonsense

The deterministic extractor's first working version found a person called
**"Ron Sugar Former"** — the greedy regex had swallowed the word "Former" as a
third name word. Later versions produced "Gary Miller Customer Success" and
"Rob Duhart Chief Security".

**Why this mattered more than a miss.** A wrong name on a person card is presented
as a *fact*. Missing a person is a gap; inventing one is misinformation.

**The fix.** A name is truncated at the first role word or organisational noun
(`former`, `chief`, `customer`, `success`, …), and titles are cut at UI text
("Read Jane's bio"). The extractor now requires the name and title to be adjacent,
which is why it misses prose — deliberately.

---

## `reset()` was silently undone

After a rebuild, the index contained **stale vectors**. `persist()` called
`ensure_loaded()`, which saw the on-disk index as newer than a just-`reset()`
in-memory one and reloaded it over the top, writing the old vectors back.

**Why it was hard to see.** Every individual step looked correct; the bug was in
the interaction between two methods that were each fine alone.

**The fix.** A `_dirty` flag: uncommitted in-memory changes always win over the
disk copy. There is now a regression test named after it.

---

## A sync handler cannot create an async task

`POST /api/reindex/` returned 500. FastAPI runs a `def` handler in a threadpool,
where `asyncio.create_task` has no running event loop.

**The fix.** Declare the handler `async def`. One word.

---

## Windows: uvicorn's event loop cannot spawn a browser

Playwright launches Chromium as a subprocess. On Windows, `SelectorEventLoop`
raises `NotImplementedError` for that. uvicorn installs a Selector loop whenever it
runs the app in a child process — which `reload=True` and `workers > 1` both do.

So the browser worked in **every standalone script** and died under the documented
entrypoint. Verified directly:

```
loop=ProactorEventLoop           subprocess: OK
loop=_WindowsSelectorEventLoop   subprocess: NotImplementedError
```

**The fix.** The renderer detects a loop that cannot spawn and runs the browser on
a Proactor loop in a worker thread. The global event-loop policy is deliberately
not touched — it is process-wide, and changing it from a thread to fix one call
would quietly change how every later loop in the process is created.

---

## My own test was wrong (my mistake)

While investigating the above, I wrote a test that appeared to prove the event loop
was fine. It set the event-loop policy **inside** a running `asyncio.run()`, where
it has no effect. Both runs used the same loop and both passed.

I reported the loop as fine on the strength of it. Constructing the loop explicitly
exposed the real behaviour, and the real bug.

**The lesson.** A test that passes is evidence only if it *can* fail. I added a
test that asserts the premise — that a Selector loop really does raise
`NotImplementedError` — so if a future Python fixes this, the test says the
workaround is now unnecessary.

---

## I broke my own index twice (my mistake)

Cleaning up test data with direct SQL deletes bypassed the application's vector
removal, first orphaning **80 vectors**, then **27**. Vectors that no longer resolve
to a chunk row remain matchable, so search can return results that point at nothing.

The application never does this — the indexer removes vectors *before* deleting the
chunk rows they point at. But my cleanup did it twice.

**Silver lining.** It demonstrated the rebuild-from-SQLite path works, which is
exactly what it exists for: `python -m app.cli index --rebuild` restored the 1:1
chunk-to-vector mapping.

---

## My own verification script was wrong (my mistake)

Twice, while checking that no secrets would be committed:

1. `grep -c "^$pattern"` returned `"0\n0"` because of a broken `|| echo 0` inside a
   command substitution — so every check printed "PROBLEM".
2. `grep "^.env"` treats `.` as a **regex wildcard**, so it matched
   `.env.example` and reported `.env` as staged when it was not.

**The fix.** `grep -qxF` for literal whole-line matching. Worth noting because a
security check that cries wolf is as bad as one that stays silent — I nearly
"fixed" a non-problem twice.

---

## Console encoding produced two false alarms

A CLI command crashed with `UnicodeEncodeError` printing non-ASCII to a cp1252
console — fixed with `stream.reconfigure(encoding="utf-8")`.

Separately, a person's name appeared to contain a **lone surrogate** (`\udc9d`),
which would be a genuine data-integrity bug. It turned out to be two `U+FFFD`
replacement characters from the source page, displayed wrongly by the terminal.
The stored data was fine.

**The lesson.** Verify against the database, not against console output. Console
encoding lied to me twice in one project.

---

## The chunker fragmented pages into confetti

Every heading flushed a chunk, so a page with one heading per executive produced
**25 tiny chunks from 2,760 characters** — each too small to retrieve anything
useful and each competing with the others.

**The fix.** Flush on a heading only when the current chunk is at least half full.
The same page now produces 5 chunks.

---

## The UI table was broken two ways at once

Found by screenshotting the page with Chromium and measuring, rather than guessing:

1. **Overflow.** The status column carried a 46-character failure reason, and
   `.narrow` applies `white-space: nowrap` — so the column could not shrink and
   forced the table `scrollWidth 1253` against a `clientWidth` of 1154. The last
   column was clipped. Fixed with a bounded, ellipsised column.

2. **The header covered its own first row.** Measured `overlap: 56` — the sticky
   `th` had `top: 56px`, meant to clear the fixed topbar. But `.table-wrap` has
   `overflow-x: auto`, which makes it a **scroll container**, so `top` is an offset
   *inside the table*, where there is no topbar. The header was pushed 56px down
   over the first row. Fixed with `top: 0`.

---

## A page can score well and answer nothing

A question about IBM Watson returned **Accenture's** executives as person cards.
Both pages cleared the score floor, and every page that cleared it contributed its
entire leadership team.

**The fix.** The floor is now *relative*: a page must score at least 75% of the
best hit (`PERSON_RELEVANCE_RATIO`). An absolute floor cannot work here, because on
a dataset of company home pages everything scores in a narrow band around 0.3 —
any fixed cutoff either admits every page or rejects every page.

A weakly-matching page still shows its passage; it just no longer answers with its
executives.
