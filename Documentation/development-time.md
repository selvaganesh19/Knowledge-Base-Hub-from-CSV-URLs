# Development time

## Per task

Estimates from the session — implementation **and** testing for each task, not
elapsed wall-clock. Ordered as the work happened.

| # | Task | Time |
|---|---|---|
| 1 | Repository audit, scaffold, configuration, database models | ~40 min |
| 2 | CSV/XLSX reader, URL-column detection, upload endpoint | ~35 min |
| 3 | Async scraper, robots.txt handling, extraction fallbacks | ~60 min |
| 4 | Chunking, embeddings, FAISS store, indexing job | ~50 min |
| 5 | Search pipeline, ranking, citations, person cards | ~55 min |
| 6 | Groq integration, graceful degradation, provider fallbacks | ~30 min |
| 7 | Server-rendered UI: dashboard, upload, list, detail, search | ~75 min |
| 8 | Test suite foundation and isolation strategy | ~45 min |
| 9 | Docker, logging, README, deployment notes | ~35 min |
| 10 | Groq model replacement after retirement | ~15 min |
| 11 | Document ingestion (PDF, DOCX, text) | ~50 min |
| 12 | Log noise, false-success pages, stale-chunk bug | ~55 min |
| 13 | Crawl outcomes, content-quality gate, retry with backoff | ~70 min |
| 14 | SSRF guard, upload limits, validation summary | ~40 min |
| 15 | Deterministic person extraction (works without an API key) | ~65 min |
| 16 | Schema sync and backfill for existing databases | ~40 min |
| 17 | UI: summary cards, status badges, headings, People tab removal | ~50 min |
| 18 | Crawl4AI engine as an optional, additive crawler | ~60 min |
| 19 | Windows event-loop fix, URL table layout bug, documentation | ~75 min |
| | **Total** | **~16 h** |

---

## Where the time actually went

**Roughly a third of it was testing and fixing what testing found**, not writing
new features. That is the honest headline. Tasks 12, 13 and 19 are almost entirely
that — each began with something that worked and ended with it not lying.

**Debugging against live sites dominated the crawler work.** Mock data would not
have found any of the interesting problems. `microsoft.com` returning a block page
with HTTP 200, `amazon.com` returning an empty 202, Oracle's names vanishing into
trafilatura's boilerplate filter — every one of those needed a real site behaving
badly in its own particular way.

**Two tasks were shorter than expected.** The Groq model replacement (15 min) took
minutes because the provider abstraction already had a fallback chain. The upload
limits (part of task 14) reused the URL guard that already existed.

**Two were longer.** The deterministic extractor (65 min) needed four rounds of
fixing false positives against real pages — each round required looking at what the
extractor produced on actual Apple and Oracle markup and working out why. The
Windows event-loop fix (part of task 19) took time mostly because my *first*
investigation was wrong and I had to redo it.

**Documentation is not separately counted** beyond task 19. It was written
alongside the code it describes.

---

## Method, and why that matters for the numbers

I wrote tests throughout rather than at the end, which is why several bugs have a
test named after them — the `reset()` dirty-flag undo, the `strong`-tag extraction
loss, the chunker's heading fragmentation, the three citation formats, the
`NotImplementedError` premise.

The suite is **374 tests, offline, ~14 seconds**. Offline is what makes it
practical: two decisions carry it — `KBHUB_DATA_DIR` redirects every writable path
to a scratch directory so the suite never touches a real database, and the
embedding model is replaced by a deterministic bag-of-words vectoriser so no 90 MB
download happens per run. That vectoriser produces a real retrieval signal (texts
sharing vocabulary land close together), so ranking tests exercise the pipeline
rather than a stub.

---

## What the estimates do not include

* **Model download time.** The first run downloads ~90 MB of embedding model, and
  `python -m playwright install chromium` is another ~150 MB.
* **Dependency installation.** PyTorch is the slow one; the README documents the
  CPU-only index URL because the default Windows wheel pulls a ~2.5 GB CUDA build.
* **Reading and re-reading the brief**, and the back-and-forth on framework and
  crawler choices.
