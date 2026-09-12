# Questions and assumptions

## Questions the brief left open

Where a decision was genuinely the requester's, I asked. Where a sensible default
existed, I took it, recorded why, and moved on.

### Which web framework?

The brief said Django was preferred. I planned the whole application around
Django before checking — models, admin, templates, the lot. I asked before writing
any code, and was told to use FastAPI instead.

**Outcome:** FastAPI, and the plan was rewritten. Worth noting that the question
cost a few minutes and saved a complete rewrite.

### Which LLM?

The brief specified Groq but named no model. I chose `llama-3.3-70b-versatile`.
It was retired by Groq mid-build, and the API started returning a model-not-found
error.

**Outcome:** moved to `openai/gpt-oss-120b`, and added a fallback chain
(`openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) so the next retirement degrades instead
of breaking. That is now a tested path rather than a hope.

### Deterministic extraction or an LLM?

The brief asked for structured person data but did not say how.

**Outcome:** both, because neither alone was sufficient. See
[difficulties](difficulties.md#the-pattern-extractor-produced-confident-nonsense)
for what went wrong with each. The LLM reads prose the pattern matcher cannot; the
pattern matcher works with no API key and no network. Person extraction now runs
either way — before this, the application's single most important feature silently
disappeared without an API key.

### Is a JavaScript-heavy page that yields nothing a success?

**Outcome: no.** It is `PARTIAL`, not `SUCCESS`. A `200` is evidence that a server
answered, not that content arrived. A page of navigation produces chunks that match
queries and answer nothing, which is worse than an empty result because it
displaces a page that would have answered.

### Should Crawl4AI be mandatory?

The brief listed it as the primary crawler. It needs a ~150 MB Chromium download
and brings a vendored litellm fork, `shapely`, `nltk` and three Playwright packages
with it.

**Outcome:** optional and additive, behind `CRAWL_ENGINE=crawl4ai`. The built-in
`httpx` + `trafilatura` pipeline is the default and is unchanged. Measured on real
sites, Crawl4AI is genuinely better — 2,237 → 5,533 characters on Oracle,
`BLOCKED` → 13,339 on Microsoft, 484 → 4,171 on a JavaScript SPA — but that is a
large dependency to make mandatory for a pipeline that already worked.

### How should a site that blocks scrapers be handled?

**Outcome: record it and move on.** The state is `BLOCKED` with the reason, and the
crawl continues with the remaining URLs. Amazon serves an AWS WAF JavaScript
challenge that must be solved before the page is released; solving it means
defeating a bot-protection control, and nothing here attempts that. This is a
deliberate boundary, not a gap.

### Does `raw_html` belong in the list endpoint?

The brief requires raw HTML through `GET /api/urls/`, and also wants that endpoint
usable. A single leadership page is ~200 KB of markup; returning it for every row
makes the response unusable.

**Outcome:** omitted by default with its length reported, available via
`?include_html=1`, and always included on `GET /api/urls/{id}`. The requirement is
satisfied literally without making the list response megabytes.

---

## Assumptions

### One row per CSV URL, by default

Link discovery — following `/leadership`, `/team`, `/about` from a crawled page —
is implemented but **off** unless `DISCOVER_LINKED_PAGES=1`. The brief supplies an
explicit URL list, so silently crawling an entire site would be surprising and
slow. When enabled it is bounded three ways at once: `MAX_PAGES_PER_URL`,
`MAX_CRAWL_DEPTH`, and `SAME_DOMAIN_ONLY`.

### The dataset is dozens of pages, not millions

`IndexFlatIP` is an exact, flat search. At this scale it is both exact and fast,
and an approximate index would trade recall for a speed gain nobody can perceive.
Swapping to `IndexHNSWFlat` later changes one constructor call.

### A single process owns the index

The FAISS index and the in-process job registry are per-process state. Multiple
uvicorn workers would each hold their own index and orphan each other's jobs, so
`workers=1` is structural rather than a preference. Scaling out means moving the
index and jobs into a dedicated worker process.

### `text_content` is the cleaned markdown

The brief listed a separate `markdown_content` field. Storing both would duplicate
the same string in two columns that can drift apart, so there is one field holding
markdown-ish text — headings are preserved as `## Heading` lines because the
chunker uses them to decide where a chunk may break.

### Local development may need loopback

The SSRF guard refuses `file://`, loopback, private ranges and cloud metadata
endpoints by default. `ALLOW_PRIVATE_HOSTS=1` is set in the local `.env` because
the demo PDF is served from `127.0.0.1:8099`. **That must be `0` in any deployment
that accepts uploads from other people** — it is the setting that stops an uploaded
CSV making the server probe its own network.

### Uploads are untrusted input

Accordingly: 10 MB file cap, 500 URLs per upload, http/https only, no credentials
in URLs, and the SSRF guard. Each refusal is counted and reported to the user by
reason rather than silently dropped.

### Failure is a record, not an exception

One unreachable URL must not fail a batch. Every network failure, timeout, block
and empty page ends as a stored row with a reason. This is why `FetchResult.error`
is populated for `PARTIAL` pages too — the reason text is what the UI and the API
show, so `error == ""` is not the test for success. `result.ok` is.
