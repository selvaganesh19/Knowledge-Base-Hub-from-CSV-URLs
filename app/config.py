"""Application settings, loaded from environment variables and .env.

Paths are derived from BASE_DIR rather than hard-coded so the app runs the same
way regardless of the directory it is launched from.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Root for everything the application writes: database, vector index, model cache,
# uploads and logs. Overridable so the test suite can point at a scratch directory
# and a container can point at a mounted volume, without either touching the
# checkout's own files. Read from the environment rather than .env because it has
# to be known before the settings object exists.
DATA_ROOT = Path(os.environ.get("KBHUB_DATA_DIR") or BASE_DIR).resolve()

DATA_DIR = DATA_ROOT / "data"
LOG_DIR = DATA_ROOT / "logs"
FAISS_DIR = DATA_DIR / "faiss"
# Original bytes of every non-HTML document that has been harvested, so the text
# can be re-derived later without re-fetching.
DOCUMENT_DIR = DATA_DIR / "documents"
# Overridable separately from the data root so a container can bake the embedding
# model into the image while keeping the database and index on a mounted volume.
MODEL_CACHE_DIR = Path(os.environ.get("KBHUB_MODEL_CACHE_DIR") or (DATA_DIR / "models")).resolve()
UPLOAD_DIR = DATA_ROOT / "uploads"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

FAISS_INDEX_PATH = FAISS_DIR / "index.faiss"
FAISS_META_PATH = FAISS_DIR / "meta.json"
DB_PATH = DATA_ROOT / "kbhub.db"

# A browser-like User-Agent. Many leadership pages sit behind bot filtering that
# rejects the default python-requests/httpx strings outright.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Knowledge Base Hub"

    # --- LLM ---------------------------------------------------------------
    llm_provider: str = "auto"  # groq | gemini | auto | none
    groq_api_key: str = ""
    # Groq retires models fairly often. If the primary name stops working, the
    # fallbacks below are tried in order before the provider gives up.
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_models: str = "openai/gpt-oss-20b,qwen/qwen3.8-27b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    llm_timeout: float = 30.0

    def groq_fallback_model_list(self) -> list[str]:
        return [
            name.strip() for name in (self.groq_fallback_models or "").split(",") if name.strip()
        ]

    # --- Embeddings --------------------------------------------------------
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_batch_size: int = 32

    # --- Scraping ----------------------------------------------------------
    scrape_workers: int = 6
    respect_robots: bool = True
    max_html_bytes: int = 5_000_000
    # Documents are binary and frequently much larger than a page of markup, so the
    # cap is separate and higher. PDFs of a few tens of megabytes are normal.
    max_document_bytes: int = 25_000_000
    parse_documents: bool = True
    connect_timeout: float = 5.0
    # How long one request may take. REQUEST_TIMEOUT is the documented name;
    # READ_TIMEOUT is still accepted so existing .env files keep working.
    request_timeout: float = Field(
        default=20.0, validation_alias=AliasChoices("REQUEST_TIMEOUT", "READ_TIMEOUT")
    )
    user_agent: str = DEFAULT_USER_AGENT

    # --- Resilience --------------------------------------------------------
    # Extra attempts after the first, so MAX_RETRIES=2 means up to 3 requests.
    # Only transient failures are retried - see RETRY_STATUSES in scraper.py.
    max_retries: int = 2
    # Base seconds for the exponential backoff: attempt n waits base * 2**n.
    retry_backoff: float = 0.75
    # Minimum gap between two requests to the same host. Politeness, not speed
    # control - a site that sees six simultaneous requests from one client is
    # entitled to treat that as an attack.
    crawl_delay: float = 1.0

    # --- Content quality ---------------------------------------------------
    # A 2xx response is not by itself a successful harvest. Below either floor the
    # page is stored as PARTIAL rather than SUCCESS, because a page that is all
    # navigation produces chunks that match queries and answer nothing.
    min_text_length: int = 500
    min_word_count: int = 100

    # --- Crawl engine ------------------------------------------------------
    # "http" is the built-in httpx + trafilatura pipeline and the default.
    # "crawl4ai" swaps in the optional Crawl4AI engine, which renders JavaScript
    # through a real browser and does its own markdown extraction.
    #
    # Safe to enable: if Crawl4AI is not installed, or its browser is missing, or it
    # errors while starting up, every URL falls back to the "http" path and the
    # harvest completes exactly as it would have. See app/services/crawl4ai_engine.py.
    crawl_engine: str = "http"

    # --- Browser rendering (optional) --------------------------------------    # Playwright renders JavaScript, which the HTTP fetcher cannot. It needs the
    # Chromium binary: `python -m playwright install chromium`. When the browser is
    # absent the crawler reports that clearly and carries on - it never fails a
    # page because an optional accelerator is missing.
    enable_playwright: bool = True
    playwright_timeout: int = 30_000
    # Rendering is slow (seconds per page), so it is only attempted when the HTTP
    # pass came back below the quality floor.
    playwright_min_improvement: float = 1.5

    # --- Link discovery ----------------------------------------------------
    # Off by default: the assignment supplies a URL list and expects one row per
    # URL. Turn it on to also follow priority internal links from each page.
    discover_linked_pages: bool = False
    max_pages_per_url: int = 25
    max_crawl_depth: int = 2
    same_domain_only: bool = True

    # --- Upload guards -----------------------------------------------------
    max_upload_bytes: int = 10_000_000
    max_urls_per_upload: int = 500
    # SSRF guard. Loopback, private ranges and cloud metadata endpoints are refused
    # by default so an uploaded CSV cannot make the server probe its own network.
    # Turn this on only for local development, where the demo documents are served
    # from 127.0.0.1.
    allow_private_hosts: bool = False

    # --- Chunking ----------------------------------------------------------
    chunk_size: int = 1000
    chunk_overlap: int = 150

    # --- Search ------------------------------------------------------------
    search_min_score: float = 0.25
    # TOP_K is the documented name; SEARCH_TOP_K is accepted as an alias.
    search_top_k: int = Field(default=8, validation_alias=AliasChoices("TOP_K", "SEARCH_TOP_K"))
    # A page must score at least this fraction of the best-matching page's score
    # before the people extracted from it are shown as answers. Without it, a query
    # about one company still surfaces another company's executives, because any
    # page that merely cleared the floor contributed its whole leadership team.
    person_relevance_ratio: float = 0.75

    # --- Pipeline ----------------------------------------------------------
    auto_index: bool = True
    auto_extract: bool = True

    # --- Logging -----------------------------------------------------------
    log_level: str = "INFO"
    log_to_file: bool = True
    log_filename: str = "kbhub.log"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3

    @property
    def database_url(self) -> str:
        return f"sqlite:///{DB_PATH}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_directories() -> None:
    """Create every directory the app writes to. Safe to call repeatedly."""
    for path in (DATA_DIR, LOG_DIR, FAISS_DIR, DOCUMENT_DIR, MODEL_CACHE_DIR, UPLOAD_DIR):
        path.mkdir(parents=True, exist_ok=True)
