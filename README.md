# Knowledge Base Hub from CSV URLs

Upload a CSV (or Excel) file of URLs, harvest the content behind them, store it in
SQLite, index it in a local vector database, and search it in natural language.

Search combines two kinds of retrieval: **chunk-level semantic search** over
embedded page text, and **structured person records** extracted from each page at
index time. A question like "who is the CFO?" returns both the passage that answers
it and a card for the person it is about.

![stack](https://img.shields.io/badge/FastAPI-0.115+-009688)
![stack](https://img.shields.io/badge/FAISS-local_index-4b8bbe)
![stack](https://img.shields.io/badge/tests-374_passing-brightgreen)

---

## Demo

**▶ [`Demo Video/knowledge base url.mp4`](Demo%20Video/knowledge%20base%20url.mp4)** — end-to-end walkthrough:
upload a CSV, watch the crawl progress and its per-URL outcomes, then query the
knowledge base and get an answer with citations and person cards.

> GitHub does not render embedded video in a README, so the file is committed
> alongside it — click through to play. It is 83 MB, which is why the clone is
> larger than the code.

---

## Contents

- [Demo](#demo)
- [How it fits together](#how-it-fits-together)
- [Quick start](#quick-start)
- [Docker](#docker)
- [Using it](#using-it)
- [REST API](#rest-api)
- [Working without an LLM key](#working-without-an-llm-key)
- [Verifying the storage and retrieval chain](#verifying-the-storage-and-retrieval-chain)
- [Command line](#command-line)
- [Tests](#tests)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development notes](#development-notes)
- [Notes and limitations](#notes-and-limitations)

---

## How it fits together

```
CSV / XLSX upload
      |
      v
 reader.py  -----> URL column auto-detected, other columns preserved
      |
      v
 scraper.py -----> async fetch (robots-aware, size-capped)
      |           HTML: trafilatura + DOM-walk fallback
      |           documents: pypdf / python-docx, originals kept on disk
      v
 SQLite: harvested_urls (raw HTML or extracted document text + status + metadata)
      |
      v
 chunking.py ----> ~1000-char heading-aware chunks with overlap
      |
      v
 embedder.py ----> all-MiniLM-L6-v2, 384-d, normalized
      |
      v
 FAISS: IndexIDMap2(IndexFlatIP)   id == chunk id
      |
      +--> extractor.py -----> person_records via LLM (index time)
      |
      v
 search.py ------> group hits by page, attach people, synthesise answer
      |
      v
 Web UI  +  REST API (/api/urls/ ...)
```

### Why these choices

**FAISS with `IndexIDMap2` keyed on the chunk id.** The index stores only
`(id, vector)`; a search result resolves to text, offsets and a source URL with one
SQL query. There is no separate id-mapping table to keep in sync.

**Inner product on normalized vectors.** Normalization happens at write time, so
inner product *is* cosine similarity and the returned scores need no conversion.
`IndexFlatL2` would need a `1 - d²/2` transform and would still lose the sign.

**Chunking at ~1000 characters.** `all-MiniLM-L6-v2` truncates at 256 tokens;
1000 characters of English sits inside that, so a chunk is never silently cut
before it reaches the model. Headings only start a new chunk once the current one
is at least half full — flushing on every heading turns a page with one heading per
executive into 25 fragments instead of 5 usable chunks.

**Two extractors, best result wins.** Trafilatura removes boilerplate better, but
it over-prunes card-style pages: Oracle's executive list came back as job titles
with every name removed, because the names live in `<strong>` inside linked cards
that it classifies as navigation. So the DOM walk runs too, and trafilatura is only
preferred when it captured at least half of what the DOM walk found. That single
rule took Oracle from 0 extracted people to 25.

**URLs are not always web pages.** A leadership list is often a PDF or a `.docx`
press release, so documents are fetched, parsed and stored like anything else.
Pages are kept as `## Page N` headings, which the chunker turns into chunk headings
— so a retrieved passage can say *which page* of a report it came from. The original
bytes are written to disk before parsing, so the text can be re-derived later
without re-fetching. See [`examples/`](examples/) for a runnable demonstration.

**Extraction at index time, not query time.** One LLM call per page up front means
search keeps returning person cards even when the key is gone or the quota is
exhausted. The failure-prone work happens where failing is survivable.

**Async scraping with a single writer.** Fetches run concurrently under a semaphore
and results are written as they complete, so only one coroutine ever holds a writing
session. SQLite never sees two concurrent writers, which removes the usual
"database is locked" problem rather than retrying around it.

**Storing raw HTML.** It is the reason `reextract` can rebuild the whole corpus from
disk after an extraction bug is fixed, with no re-scraping and no rate limits.

---

## Quick start

Requires Python 3.11+. Verified on Python 3.13 / Windows 11.

`faiss-cpu` needs 1.11 or newer for a Python 3.13 wheel; older releases stop at 3.12
and would fall back to a source build.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip

# Install the CPU-only PyTorch wheel first. The default PyPI wheel on Windows can
# pull the CUDA build, which is over 2 GB; this keeps it near 200 MB.
pip install torch --index-url https://download.pytorch.org/whl/cpu

pip install -r requirements.txt
copy .env.example .env      # then add GROQ_API_KEY or GEMINI_API_KEY if you have one
python run.py
```

Open <http://127.0.0.1:8000>. Interactive API docs are at `/docs`.

The first run downloads the embedding model (~90 MB) into `data/models/`; after
that the app works offline apart from the LLM calls and the pages being harvested.

### Run it with a single worker

`run.py` starts uvicorn with `workers=1` on purpose. The FAISS index and the
background-job registry are per-process state, so a multi-worker setup would give
each worker its own index and orphan jobs started by another. See
[Deployment](#deployment).

---

## Docker

```bash
cp .env.example .env        # optional; add GROQ_API_KEY for synthesized answers
docker compose up --build
```

Open <http://127.0.0.1:8000>.

The image installs the CPU-only PyTorch wheel and bakes the embedding model in at
build time, so the first search does not wait on a 90 MB download. Everything
mutable — database, FAISS index, uploads, logs — lives on the `kbhub-data` volume,
so replacing the container does not lose the knowledge base.

Point the app at a different data directory (also how the test suite isolates
itself):

```bash
docker run -e KBHUB_DATA_DIR=/data -v mydata:/data -p 8000:8000 kbhub:latest
```

| Variable | Purpose |
|---|---|
| `KBHUB_DATA_DIR` | Root for database, index, uploads and logs (default: the checkout) |
| `KBHUB_MODEL_CACHE_DIR` | Where the embedding model is cached (set to `/opt/models` in the image) |

`docker compose` reads `.env` from this directory for variable substitution, so
`GROQ_API_KEY` in `.env` reaches the container without being baked into the image.

> The Dockerfile and compose file have not been executed in this environment —
> Docker is not installed on the machine this was built on. The compose file parses
> and its assumptions match the code (`/health`, `KBHUB_DATA_DIR`,
> `KBHUB_MODEL_CACHE_DIR`), but the image build itself is unverified.

---

## Using it

### 1. Upload

**Upload** takes a `.csv` or `.xlsx` file — drag and drop, or click to browse. The
sample `Leadership URL.xlsx` and an equivalent `leadership_urls.csv` are in the repo.

The URL column is detected automatically: a header matching `url|link|website|...`
gets a strong prior, and the fraction of values that look like `http(s)://...` does
the rest. A column of real URLs wins on value evidence alone, so unhelpful headers
still work. If nothing scores, the response lists the headers and you can name the
column explicitly.

Every other column is preserved on the harvested row under `metadata.row`, so no
input data is lost whatever the schema.

Harvesting starts immediately, followed by indexing and person extraction. The
progress bar reports which stage is running and which URL it is on.

### 2. Inspect

**URLs** lists every harvested row: status badge, title, chunk and person counts,
and any failure reason. The detail page shows the response metadata, extracted
text, chunks, person cards, and the raw HTML in a collapsible viewer.

Failures are stored rather than dropped — a 403 or a 404 stays visible as a row
with the reason attached.

### 3. Search

**Search** runs a natural-language query and shows:

* a synthesized answer with `[n]` citations linking to the source pages,
* person cards for anyone mentioned, ranked with the answer's own named people first,
* the retrieved passages with similarity scores and per-source context.

### 4. Documents

A URL may point at a PDF, a `.docx` or a plain-text file rather than a web page.
Those are fetched, parsed and treated identically from that point on: the text is
stored in SQLite, chunked, embedded, and searchable. Person records are extracted
from documents too, so a leadership PDF produces cards like any HTML page.

The original file is saved under `data/documents/` (content-addressed, so the same
PDF in two batches is stored once), and its path is recorded in the row's metadata.
That is what lets `python -m app.cli reextract` rebuild the text without
re-downloading anything.

[`examples/`](examples/) contains a runnable demonstration using a PDF whose
contents are invented, which is the cleanest way to confirm that answers come from
the harvested data rather than from the model.

---

## Verifying the storage and retrieval chain

The pipeline is: **SQLite stores the content → chunks are embedded → FAISS holds the
vectors → the answer is generated from the retrieved chunks.** Each link is
inspectable.

**1. SQLite holds the raw content and the chunks.**

```bash
python -m app.cli stats
```

```sql
-- The content behind each URL, with its HTTP status.
SELECT id, url, http_status, length(raw_html) AS html_len,
       length(text_content) AS text_len, chunk_count, person_count
FROM harvested_urls;

-- The chunks those pages were split into.
SELECT id, url_id, ordinal, heading, substr(text, 1, 60) FROM chunks;
```

**2. FAISS holds one vector per chunk.**

```bash
curl -s http://127.0.0.1:8000/api/stats/
```

```json
{ "chunks": 6, "vectors": 6, "index_type": "IndexIDMap2",
  "index_metric": "inner product (cosine on normalized vectors)", "index_dim": 384 }
```

`chunks` and `vectors` matching is the point: the FAISS id **is** the `chunks.id`,
so a vector search hit resolves to its text with one query and no mapping table to
fall out of sync.

**3. A query becomes a vector, and the nearest chunks are returned.**

```bash
curl -s "http://127.0.0.1:8000/api/search/?q=who+is+the+CFO" | python -m json.tool
```

Each result carries the `chunk_id` that matched and its cosine similarity, so the
retrieved passage can be looked up directly:

```bash
python -m app.cli search "who is the CFO?"
```

**4. The answer is generated from those chunks.**

The prompt hands the model numbered context entries built from the retrieved
chunks and instructs it to answer *only* from that context, citing `[n]`. If the
context does not contain the answer it is required to say so — which is why a
question the corpus cannot answer returns "the provided context does not contain…"
rather than a plausible invention.

The clearest proof is a fact the model cannot already know. `examples/` ships a PDF
describing a company that does not exist; asking about a role in it returns the
invented name, cited to the PDF.

---

## REST API

Interactive docs at `/docs`; the OpenAPI schema is at `/openapi.json`.

### `GET /api/urls/`

The endpoint the task specifies: harvested URL information including the URL, HTTP
status code, raw content, and metadata.

```bash
curl "http://127.0.0.1:8000/api/urls/"
```

```json
{
  "count": 5,
  "page": 1,
  "page_size": 25,
  "pages": 1,
  "results": [
    {
      "id": 1,
      "url": "https://www.apple.com/in/leadership/",
      "final_url": "https://www.apple.com/in/leadership/",
      "http_status": 200,
      "http_status_text": "OK",
      "content_type": "text/html;charset=utf-8",
      "title": "Apple Leadership",
      "meta_description": "...",
      "metadata": { "og:title": "...", "canonical": "...", "row": { "ID": "1" } },
      "fetched_at": "2026-09-12T11:00:15Z",
      "fetch_ms": 3700,
      "error": "",
      "batch_id": 1,
      "raw_html_length": 155620,
      "text_length": 1309,
      "chunk_count": 2,
      "person_count": 24,
      "raw_html": null,
      "text_content": null
    }
  ]
}
```

**About raw HTML.** A leadership page is often 200 KB of markup. Returning that for
every row would make the list response megabytes of JSON, so it is omitted by
default and its length reported instead. Get it either way:

```bash
# In the list response
curl "http://127.0.0.1:8000/api/urls/?include_html=1&include_content=1"

# Or from the detail endpoint, which always includes both
curl "http://127.0.0.1:8000/api/urls/1"
```

**Filters:** `batch`, `http_status`, `has_error`, `q`, `ordering`, `page`,
`page_size` (max 200).

### Other endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/urls/{id}` | One URL with full raw HTML and text |
| `POST` | `/api/upload/` | Upload a file; starts the pipeline |
| `GET` | `/api/batches/` | List upload batches |
| `GET` | `/api/jobs/{id}` | Job progress |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a running job |
| `GET` | `/api/people/` | Extracted person records |
| `GET`/`POST` | `/api/search/` | Semantic search |
| `POST` | `/api/reindex/` | Start an indexing job |
| `POST` | `/api/extract/` | Start a person-extraction pass |
| `GET` | `/api/stats/` | Dataset and index counts |
| `GET` | `/health` | Liveness probe |

### Search

```bash
curl -X POST http://127.0.0.1:8000/api/search/ \
  -H "Content-Type: application/json" \
  -d '{"query": "Who is the CFO of Oracle?"}'
```

```json
{
  "query": "Who is the CFO of Oracle?",
  "answer": "**Hilary Maxson** – Chief Financial Officer, Oracle [3]",
  "llm_used": true,
  "provider": "groq",
  "low_confidence": false,
  "note": "",
  "latency_ms": 1203,
  "sources": [{ "index": 3, "url": "https://www.oracle.com/in/corporate/executives/", "title": "..." }],
  "results": [
    {
      "url_id": 2,
      "url": "https://www.oracle.com/in/corporate/executives/",
      "title": "Oracle Executive Leadership",
      "score": 0.53,
      "heading": "Oracle Leadership",
      "text": "...",
      "matched_chunks": [{ "chunk_id": 12, "score": 0.53, "heading": "...", "text": "..." }],
      "people": []
    }
  ],
  "people": [
    { "name": "Hilary Maxson", "title": "Chief Financial Officer", "company": "Oracle",
      "bio": "", "email": "", "linkedin_url": "", "confidence": 0.99,
      "source_url": "https://www.oracle.com/in/corporate/executives/", "url_id": 2 }
  ]
}
```

There is also a GET form for browsers and shell convenience:

```bash
curl "http://127.0.0.1:8000/api/search/?q=who+leads+Apple%3F"
```

---

## Working without an LLM key

The app runs fully without any API key. When no provider is reachable:

* `/api/search/` returns `llm_used: false` and `provider: "none"`,
* ranked source passages are still returned with similarity scores,
* person cards still appear for any dataset indexed while a key was available,
* the search page shows a banner instead of an answer panel.

Person extraction is skipped with a clear message rather than failing a job. The
retrieval half of search is computed before the model is called, so a missing key,
an exhausted quota, or a provider outage degrades to "here are the matching
passages" instead of an error.

Providers are tried in the order `groq`, then `gemini`, then none. Set
`LLM_PROVIDER` to pin one, or `none` to skip the LLM entirely.

### Model retirement

Groq retires models on its own schedule — `llama-3.3-70b-versatile` was removed
during this build. `GROQ_FALLBACK_MODELS` exists for that: if the configured model
comes back as a 400/404 naming the model, the next candidate is tried automatically
and the working model is reused for the rest of the process. With the primary and
all fallbacks exhausted, the provider degrades to the no-LLM path rather than
failing the job.

To see what your key can currently reach:

```bash
curl -s https://api.groq.com/openai/v1/models \
  -H "Authorization: Bearer $GROQ_API_KEY" | grep '"id"'
```

---

## Command line

The same service layer, without the web server:

```bash
python -m app.cli stats
python -m app.cli import-urls "Leadership URL.xlsx"
python -m app.cli harvest --batch 1
python -m app.cli index --rebuild
python -m app.cli reextract --batch 1 --index
python -m app.cli extract --force
python -m app.cli search "who is the CFO?" --no-llm
```

`import-urls` runs the whole pipeline in the foreground, which is the most reliable
way to reproduce a run end to end.

`reextract` re-runs text cleaning over the **stored raw HTML** with no network
access at all. This is the practical payoff of persisting raw HTML: when the
extraction rules change or improve, the corpus can be rebuilt from what is already
on disk instead of re-scraping the origin servers.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

374 tests, no network access, about 14 seconds.

The suite runs against a scratch data directory (`KBHUB_DATA_DIR`) so it never
touches your database, index or uploads, and it replaces the embedding model with a
deterministic bag-of-words vectoriser. That is a real retrieval signal — texts
sharing vocabulary land close together — so ranking assertions test the pipeline's
logic rather than a stub returning constants, and no 90 MB download or API quota is
needed per run.

Coverage focuses on the parts most likely to break silently:

| Area | What is pinned down |
|---|---|
| `test_reader.py` | Delimiter sniffing, Excel reading, URL column detection, dedupe, normalisation |
| `test_chunking.py` | Size bounds, overlap, and the "one heading per person" fragmentation regression |
| `test_vector_store.py` | Cosine scores, id replacement, persistence, and the `reset()` dirty-flag regression |
| `test_scraper.py` | The card-layout regression that lost every name on Oracle's page, extractor selection, encoding, robots.txt |
| `test_documents.py` | PDF/DOCX/text parsing, page markers, scanned-PDF detection, storage, and the full fetch path for a document URL |
| `test_search.py` | Citation formats, Unicode whitespace in names, page dedupe, person ranking, degradation |
| `test_api.py` | Every endpoint, raw-HTML inclusion, upload validation, and async job scheduling |

Lint and format:

```bash
python -m ruff check app tests run.py
python -m ruff format --check app tests run.py
```

---

## Configuration

All settings are environment variables, documented in `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `groq`, `gemini`, `auto`, or `none` |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | empty | API keys |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Primary Groq model |
| `GROQ_FALLBACK_MODELS` | `openai/gpt-oss-20b,qwen/qwen3.8-27b` | Tried in order if the primary is retired |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `SCRAPE_WORKERS` | `6` | Concurrent fetches |
| `RESPECT_ROBOTS` | `1` | Honour robots.txt |
| `REQUEST_TIMEOUT` | `20` | Seconds one request may take (`READ_TIMEOUT` still accepted) |
| `MAX_RETRIES` | `2` | Extra attempts for transient failures only |
| `CRAWL_DELAY` | `1.0` | Minimum gap between requests to the same host |
| `MIN_TEXT_LENGTH` | `500` | Below this a 2xx page is `PARTIAL`, not `SUCCESS` |
| `MIN_WORD_COUNT` | `100` | Second quality floor, catches navigation-only pages |
| `ENABLE_PLAYWRIGHT` | `1` | Use a browser for JavaScript pages (needs Chromium) |
| `PLAYWRIGHT_TIMEOUT` | `30000` | Browser page-load timeout, ms |
| `DISCOVER_LINKED_PAGES` | `0` | Also follow priority internal links |
| `MAX_PAGES_PER_URL` | `25` | Cap when link discovery is on |
| `MAX_CRAWL_DEPTH` | `2` | How deep link discovery follows |
| `SAME_DOMAIN_ONLY` | `1` | Never follow a link off-site |
| `MAX_UPLOAD_BYTES` | `10000000` | Upload size cap |
| `MAX_URLS_PER_UPLOAD` | `500` | Rows accepted per file |
| `ALLOW_PRIVATE_HOSTS` | `0` | SSRF guard. **Leave `0` unless developing locally** |
| `SEARCH_MIN_SCORE` | `0.25` | Cosine cutoff, below which hits are dropped |
| `TOP_K` | `8` | Chunks retrieved and shown to the model |
| `PERSON_RELEVANCE_RATIO` | `0.75` | How close to the best hit a page must score before its people are shown |
| `AUTO_INDEX` / `AUTO_EXTRACT` | `1` | Chain the pipeline stages |
| `MAX_HTML_BYTES` | `5000000` | Response size cap |
| `PARSE_DOCUMENTS` | `1` | Parse PDF/DOCX/text URLs (set `0` to skip them) |
| `MAX_DOCUMENT_BYTES` | `25000000` | Document size cap, separate because they are binary |
| `LOG_LEVEL` | `INFO` | Console and file log level |
| `LOG_TO_FILE` | `1` | Write a rotating log to `logs/` |
| `KBHUB_DATA_DIR` | repo root | Root for database, index, uploads, logs |
| `KBHUB_MODEL_CACHE_DIR` | `<data>/models` | Embedding model cache |

Changing `EMBEDDING_MODEL` requires a rebuild: `python -m app.cli index --rebuild`.

### Crawl outcomes

Every URL ends in exactly one state, and the UI badge, the API and the search
filters all read the same value:

| Status | Meaning |
|---|---|
| `SUCCESS` | Fetched and yielded more than `MIN_TEXT_LENGTH` / `MIN_WORD_COUNT` |
| `PARTIAL` | Fetched, but the text is below the floor — usually a JavaScript shell |
| `BLOCKED` | 401, 403, 429, 451, a bot-protection page, or a login wall |
| `FAILED` | 404, 5xx after retries, DNS/TLS/connection failure |
| `SKIPPED` | robots.txt, or refused by the SSRF guard |
| `PENDING` | Queued, never attempted |

Two distinctions carry weight. A `200` is not a success — a page that returns a
cookie banner or a JavaScript shell is `PARTIAL`, because its chunks would match
queries and answer nothing. And `BLOCKED` is not `FAILED`: one is a decision the
site made and may reverse, the other usually will not change.

Only transient failures are retried (`408`, `425`, `429`, `500`, `502`, `503`,
`504`, timeouts, connection resets) with exponential backoff and jitter. A `403` or
a TLS certificate error repeats identically, so retrying only burns the budget.

### JavaScript rendering (optional)

Pages built in the browser come back as an empty shell over HTTP. There are two
ways to render them, and both are optional.

**1. The built-in engine's Playwright fallback.** When the HTTP pass falls below
the quality floor, the crawler renders the page and keeps the result only if it
beats what HTTP returned:

```bash
pip install playwright
python -m playwright install chromium     # ~150 MB, once
```

**2. Crawl4AI as an alternative engine.** Crawl4AI ships its own markdown
extraction, link discovery and browser handling:

```bash
pip install crawl4ai
python -m playwright install chromium     # only needed for its browser strategy
```

Then, in `.env`:

```bash
CRAWL_ENGINE=crawl4ai     # default is "http"
```

Crawl4AI picks its own strategy automatically and records which it used on every
row, so you can always tell what fetched a page:

| `scraping_method` | Needs | Does |
|---|---|---|
| `crawl4ai:browser` | Chromium | Full JavaScript rendering |
| `crawl4ai:http` | nothing | Its own HTTP fetcher, which gets through some sites that block a plain client |

**This is safe to enable.** If Crawl4AI is not installed, its browser is missing,
or it errors while starting up, every URL falls back to the built-in engine and the
harvest completes exactly as it would have. The failure is recorded in the row's
metadata as `crawl4ai_error`.

Measured on real sites:

| URL | Built-in (httpx) | Crawl4AI |
|---|---|---|
| `oracle.com/in/corporate/executives/` | 2,237 chars | **5,533 chars** |
| `microsoft.com` | `BLOCKED` | **13,339 chars** |
| `theorg.com/.../leadership-team` (SPA) | `PARTIAL`, 484 chars | **`SUCCESS`, 4,171 chars** |
| `amazon.com` | `PARTIAL` | `BLOCKED` — AWS WAF challenge |

`amazon.com` is not a bug to be fixed. It serves an AWS WAF JavaScript challenge
that must be solved before the page is released; solving it means defeating a bot
protection control, and nothing here attempts that. It is recorded as `BLOCKED`
with a reason and the crawl moves on.

Running Crawl4AI without Chromium gives no JavaScript rendering — the browser is
what provides it.

### A note for Windows

Playwright launches Chromium as a subprocess, and on Windows a `SelectorEventLoop`
cannot spawn one — it raises `NotImplementedError`. uvicorn installs a Selector
loop whenever it runs the application in a child process, which `reload=True` and
`workers > 1` both do. The renderer detects this and runs the browser on a
Proactor loop of its own, so it works under either entrypoint. See
`tests/test_browser_loop.py`.

### Schema upgrades

`app.db.sync_columns()` runs at startup and adds any column the models define but
the database lacks, then backfills it from data already on the row. There is no
migration history to maintain and no Alembic dependency; see the docstring for why.
An existing database upgrades in place, and rows crawled before the crawl-status
column existed are reconstructed from their status code and reason rather than
left showing as queued.

### Logging

Console output plus a rotating file at `logs/kbhub.log` (5 MB × 3). Every request
is logged with its duration and a request id, which is also returned in the
`X-Request-ID` response header — so a slow request in the browser's network tab can
be traced to its log line. Job lifecycles log at start and finish, and an
interrupted job is reported at startup.

Harvests run in background tasks, so without the file handler their output would
only exist while someone watched a terminal. The file is what makes a failed
overnight batch diagnosable afterwards.

---

## Deployment

### The single-worker constraint

Run exactly one uvicorn worker:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Two pieces of state live in the process:

* the **FAISS index**, loaded into memory and written back on index jobs,
* the **job registry**, a dict of running asyncio tasks.

With multiple workers, each process holds its own copy of the index and would
overwrite the others, and a progress poll would hit a worker that has never heard
of the job. Scaling out means moving the index and the jobs into a dedicated worker
process — the job coroutines are already independent of the request cycle, so
`run_harvest_job` and `run_index_job` can be called from a worker unchanged. The
storage layer would need to move off SQLite at the same time.

If you do scale the web tier, the request path is stateless except for reads of the
index, which each process loads from disk at start-up.

### Behind a reverse proxy

Terminate TLS at the proxy and forward to the app. Give the proxy a body-size limit
above `MAX_HTML_BYTES`, and raise its read timeout above `SCRAPE_WORKERS` × the
per-URL timeout, since an upload request returns immediately but a long harvest is
still running in the background.

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;
    client_max_body_size 20m;
}
```

### Volumes and backups

Everything that matters is under `KBHUB_DATA_DIR`:

```
kbhub.db          SQLite database (raw HTML, text, chunks, person records)
kbhub.db-wal      write-ahead log - must be copied with the database
data/faiss/       vector index and its metadata sidecar
uploads/          the original uploaded files
logs/             rotating logs
```

Back up by copying the database together with its `-wal` file, or use
`sqlite3 kbhub.db ".backup backup.db"`, which is safe while the app is running.
The FAISS index is derived data and can be rebuilt with
`python -m app.cli index --rebuild`; raw HTML is not, so the database is the part
worth keeping.

### Upgrading

```bash
git pull
pip install -r requirements.txt
python -m app.cli index            # picks up pages whose text changed
```

Tables are created on start-up; there is no migration tool in this build. A schema
change means either a fresh database or a manual `ALTER TABLE`.

### Health and monitoring

`GET /health` returns the process status, the active LLM provider, the embedding
model and the current vector count. Use it for container health checks and uptime
monitoring:

```bash
curl -fsS http://127.0.0.1:8000/health
```

### Resource expectations

The embedding model is loaded lazily on first index or search, not at start-up, so
a container can serve pages while the model is still cold. Budget roughly 500 MB
resident once it is loaded, plus whatever the harvest holds in memory — the HTML
cap is per response, and `SCRAPE_WORKERS` responses can be in flight at once.

---

## Development notes

Everything below is the working record: what I had to assume, what went wrong, how
long each piece took, and what I would do differently. The time figures are
estimates from the session, not stopwatch measurements, and cover implementation
and testing for each task rather than elapsed wall-clock.

### Questions I had to answer myself

The brief left several things open. Where a decision was genuinely the requester's
I asked; where a sensible default existed I took it and recorded why.

| Question | Decision |
|---|---|
| Which framework? The brief said Django preferred. | Asked. **FastAPI**, as directed. |
| Which LLM model? The brief named nothing. | Chose Groq, then had to replace `llama-3.3-70b-versatile` when Groq retired it mid-build. |
| How should person data be extracted — deterministic or LLM? | **Both.** Neither alone was sufficient; see below. |
| Should a JavaScript-heavy page that yields nothing be a success or a failure? | **A failure.** `PARTIAL`, not `SUCCESS`. A `200` is not evidence of content. |
| Should Crawl4AI be mandatory? | Asked. Kept optional, since it needs a ~150 MB browser and a heavy dependency tree. |
| How to handle a site that actively blocks scrapers? | **Record it and move on.** `BLOCKED`, with a reason. Bypassing a WAF or CAPTCHA is out of scope by design. |

### Assumptions I made

* **One row per CSV URL, by default.** Link discovery is off unless
  `DISCOVER_LINKED_PAGES=1`. The brief supplies an explicit URL list, so silently
  crawling a whole site would be surprising.
* **The dataset is dozens of pages, not millions.** `IndexFlatIP` is exact and
  fast at this size; an approximate index would add recall loss for no benefit.
* **A single process owns the index.** The FAISS index and the job registry are
  per-process state, so `workers=1` is structural rather than a preference.
* **`text_content` is the cleaned markdown.** The brief listed a separate
  `markdown_content` field; storing both would duplicate the same string in two
  columns that can drift apart, so there is one field.
* **Local development may need loopback.** The SSRF guard is on by default, with
  `ALLOW_PRIVATE_HOSTS=1` set locally because the demo PDF is served from
  `127.0.0.1`. That must be `0` in any real deployment.

### Difficulties encountered

Each of these cost real time, and most were found by testing against live sites
rather than by reasoning about the code.

**The crawler lied.** The largest problem in the project. `microsoft.com` answered
with HTTP 200 and "Your request has been blocked." — stored as a *successful*
harvest with one chunk, which was then embedded and made searchable. `amazon.com`
returned 202 with an empty body and was recorded as "no extractable text". Both
looked healthy in the UI while contributing nothing. The fix was to stop treating
the HTTP status as the outcome and classify the *content*.

**A block page extracts to nothing.** The check above initially read only the
extracted text — and Amazon's AWS WAF challenge extracts to a single character, so
it looked like an empty page rather than a refusal. It only worked once the check
also scanned the raw HTML for product markers (`gokuProps`, `cf-chl-`).

**Trafilatura over-pruned a page into uselessness.** Oracle's executive list came
back as 1,248 characters of job titles with every name stripped, because the names
sit in `<strong>` tags inside linked cards that it classifies as navigational. The
DOM-walk fallback's tag list omitted `strong` and `a` — exactly where the data was.

**The pattern extractor produced confident nonsense.** First working version found
"Ron Sugar Former" as a person — the regex swallowed the word "Former" as a third
name word. Later, "Gary Miller Customer Success" and "Rob Duhart Chief Security".
A wrong name on a person card is presented as fact, which is worse than no card,
so the extractor now truncates at the first role word and at UI text.

**`reset()` was silently undone.** `persist()` called `ensure_loaded()`, which saw
the on-disk index as newer than a just-reset in-memory one and reloaded it over the
top — writing stale vectors back. Only caught by asserting on the index contents
after a rebuild.

**A sync handler cannot create an async task.** `POST /api/reindex/` returned 500
because FastAPI ran the sync handler in a threadpool where `asyncio.create_task`
has no running loop. Declaring it `async` was the whole fix.

**Windows: uvicorn's event loop cannot spawn a browser.** Playwright launches
Chromium as a subprocess; `SelectorEventLoop` on Windows raises
`NotImplementedError` for that, and uvicorn installs a Selector loop whenever it
runs the app in a child process — which `reload=True` does. The browser worked in
every standalone script and died under the documented entrypoint. See the note on
testing below.

**My own test was wrong.** While investigating the above I "proved" the loop was
fine — because I set the event-loop policy *inside* a running `asyncio.run()`,
where it has no effect. Both runs used the same loop and both passed. Constructing
the loop explicitly exposed the real behaviour.

**Console encoding produced two false alarms.** A CLI crash printing non-ASCII to
a cp1252 console, and a person name that looked like it contained a lone surrogate
but was really a U+FFFD from the source page. Both cost time chasing data bugs that
did not exist.

**I broke my own index twice.** Deleting rows with direct SQL bypassed the
application's vector removal, orphaning 80 and then 27 vectors. The app never does
this — the indexer removes vectors before deleting chunks — but it showed the
rebuild-from-SQLite path works, which is what it exists for.

### Time taken per task

Estimates, ordered as the work happened.

| # | Task | Time |
|---|---|---|
| 1 | Repository audit, scaffold, config, database models | ~40 min |
| 2 | CSV/XLSX reader, URL-column detection, upload endpoint | ~35 min |
| 3 | Async scraper, robots.txt, extraction fallbacks | ~60 min |
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
| 15 | Deterministic person extraction (no API key needed) | ~65 min |
| 16 | Schema sync + backfill for existing databases | ~40 min |
| 17 | UI: summary cards, status badges, section headings, People tab removal | ~50 min |
| 18 | Crawl4AI engine as an optional, additive crawler | ~60 min |
| 19 | Windows event-loop fix + URL table layout bug | ~55 min |
| | **Total** | **~16 h** |

The figures are honest but approximate. Roughly a third of the total went on
testing and fixing what testing found, rather than writing new features.

### Other Observations

**On the brief.** The requirement to expose raw HTML through `GET /api/urls/`
conflicts with the requirement to make that endpoint pleasant to use. A single
leadership page is ~200 KB of markup; returning it for every row makes the list
response unusable. The resolution — omit the heavy fields by default, report their
lengths, and always include them on the detail endpoint — is documented in the
README, and `include_html=1` satisfies the requirement literally.

**On person extraction, the most interesting problem here.** Neither approach is
sufficient alone:

* The **LLM** reads prose ("Jane has led the company since 2019 as its chief
  executive") but is unavailable without a key, and on these pages it
  over-extracts — Apple's board list yields people whose listed organisations are
  *other* companies. It produced ~90 records from 12 URLs.
* The **pattern matcher** is precise and needs nothing, but only reads structure:
  a name adjacent to a role. It misses prose entirely and found ~50.

They are merged with the model's fields winning, which is the wrong way round for
precision. **I would invert that** and let the pattern matcher win on name and
title, using the model only for biography and contact details it alone can read.
I did not make that change because it alters what search returns, and I had no
labelled set to measure it against — changing it on a hunch would be guessing.

**On crawling, the honest position.** No crawler reliably handles arbitrary URLs,
and this one does not either. Amazon is blocked by AWS WAF. Microsoft is blocked
over plain HTTP but readable through Crawl4AI's fetcher. `theorg.com` yields 484
characters without a browser and 4,171 with one. The valuable property is not
success but **honesty about failure**: every URL ends in a specific state with a
reason, and a page that yields nothing is never recorded as a success. A blocked
page that says "blocked" is more useful than a silent partial success.

**Retrieval is the weak point, not crawling.** Chunking is fixed-size with overlap
and no semantic awareness. On a question about Perplexity the answer text said the
source "does not list any individuals" while the person cards beside it correctly
named three executives — the retrieval had picked a header chunk that lacked
names, while the extraction pass had seen the whole page. Sentence-aware or
heading-aware chunking would fix this, and structured person records should
participate in retrieval rather than only being attached to results afterwards.

**Crawl4AI earns its place, but only conditionally.** With Chromium it recovers
8.6× more text on a JavaScript SPA. Without it, it still beat httpx on two real
sites through better fingerprinting alone. Against that: it pulls a vendored
litellm fork, `shapely`, `nltk` and three Playwright packages, and its markdown
keeps more navigation chrome than trafilatura. Worth it if JavaScript matters to
your sources; not worth it otherwise.

**What I would do next, in order.** Retrieval quality (semantic chunking, people
in retrieval); a labelled question set so ranking changes can be measured rather
than argued; then moving the index and job registry into a dedicated worker
process, which is what unlocks horizontal scaling.

---

## Notes and limitations

* **JavaScript-rendered pages need a browser.** `theorg.com` is a client-rendered
  SPA. The built-in HTTP engine gets 484 characters from it and reports `PARTIAL`;
  Crawl4AI with Chromium gets 4,171 and reports `SUCCESS`. Without a browser the
  page is still stored with its status, raw HTML and a reason, never as a success.
  See [JavaScript rendering](#javascript-rendering-optional).
* **Some sites cannot be crawled at all, by design.** `amazon.com` is behind AWS
  WAF, which serves a JavaScript challenge that must be solved before the page is
  released. It is recorded as `BLOCKED` with that reason. Solving the challenge —
  or any CAPTCHA — is defeating a security control, and nothing here attempts it.
* **PDFs and other documents** — `pypdf` for PDFs, `python-docx` for `.docx`, and
  plain `.txt`/`.md`/`.csv`/`.json`/`.xml`. Extracted text is chunked and embedded
  like any page, so documents are searchable and contribute person records. Web
  pages are read by the HTML extractors instead; the two paths are chosen by
  content type, with the URL suffix as a tiebreaker for servers that mislabel a PDF
  as `application/octet-stream`.
* **Scanned PDFs** have pages but no text layer. Those are reported as
  "no extractable text … would need OCR" rather than looking like an empty success.
  OCR is out of scope.
* **Images, video and archives** (`.zip`, `.mp4`, `.png`) are stored as rows with
  the content type and reason recorded, but not parsed.
* **Job lifetime.** Jobs are asyncio tasks in the server process; a restart
  interrupts them. Startup marks any interrupted job as failed so the UI never
  shows a phantom progress bar. For long-running production use, a worker process
  can call the same coroutines.
* **Authentication.** Upload, reindex and extract are unauthenticated for demo
  purposes. They should be behind auth in any real deployment.
* **Citation formats vary.** Models emit `[1]`, `【1】` and `【1†L9-L11】` for the
  same thing; all three are normalised to `[1]` before sources are resolved. The
  prompt asks for plain ASCII brackets, but the parser does not depend on it.
