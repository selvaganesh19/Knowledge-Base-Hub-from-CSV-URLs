"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import STATIC_DIR, ensure_directories, get_settings
from app.db import init_db
from app.logging_config import configure_logging
from app.middleware import RequestLoggingMiddleware
from app.routers import (
    admin_api,
    jobs_api,
    pages,
    people_api,
    search_api,
    upload_api,
    urls_api,
)
from app.services import jobs as job_registry

settings = get_settings()

# Configured at import time so anything logging during start-up is formatted
# consistently, including uvicorn's own records.
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_directories()
    init_db()

    # A job's task lives in this process only, so any job still marked running
    # belongs to a previous run. Marking those failed at startup stops the UI
    # from showing a progress bar for work nothing is doing.
    orphans = job_registry.mark_orphans_failed()
    if orphans:
        logger.warning("marked %d interrupted job(s) as failed", orphans)

    logger.info(
        "Knowledge Base Hub ready | llm=%s | embeddings=%s | db=%s",
        _provider_name(),
        settings.embedding_model,
        settings.database_url,
    )
    yield
    logger.info("Knowledge Base Hub shutting down")


def _provider_name() -> str:
    from app.services.llm import get_provider

    return get_provider().name


app = FastAPI(
    title=settings.app_name,
    description=(
        "Upload a CSV of URLs, harvest their content, index it for semantic "
        "search, and query it in natural language."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)

# Mounted before the routers so /static never falls through to a page handler.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(urls_api.router)
app.include_router(upload_api.router)
app.include_router(jobs_api.router)
app.include_router(search_api.router)
app.include_router(people_api.router)
app.include_router(admin_api.router)
app.include_router(pages.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness probe: reports process health plus the active provider and index size."""
    from app.services.vector_store import store

    return {
        "status": "ok",
        "llm_provider": _provider_name(),
        "embedding_model": settings.embedding_model,
        "vectors": store.count,
    }
