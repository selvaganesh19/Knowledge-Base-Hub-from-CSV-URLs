"""Logging configuration.

One place that decides log format and destination, so the application, the job
runner, and uvicorn's own access logs all land in a consistent stream.

Long-running harvests are the interesting case: they run in background tasks, so
without a file handler their output only exists while someone is watching a
terminal. The rotating file handler is what makes a failed overnight batch
diagnosable after the fact.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import LOG_DIR, get_settings

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(filename)s:%(lineno)d] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# uvicorn installs its own handlers; routing them through ours avoids duplicate
# lines and keeps its records in the log file alongside the application's.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

_configured = False


def configure_logging(level: str | None = None, log_file: Path | None = None) -> None:
    """Set up console and rotating-file logging once per process."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    target = log_file or (LOG_DIR / settings.log_filename) if settings.log_to_file else None
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=settings.log_max_bytes,
                backupCount=settings.log_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:
            # A read-only or missing volume must not stop the app from starting.
            root.warning("file logging disabled (%s): %s", target, exc)

    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # Our middleware already logs every request with a duration and request id, so
    # uvicorn's own access line would be a duplicate of the same event.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # httpx logs every request it makes at INFO, which drowns out our own lines
    # during a harvest of dozens of URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)

    # faiss announces its AVX support detection in six INFO lines on every import,
    # which is noise in front of the first line of actual output from a CLI command.
    logging.getLogger("faiss").setLevel(logging.WARNING)
    logging.getLogger("faiss.loader").setLevel(logging.WARNING)

    # Trafilatura warns "discarding data: None" whenever an extraction yields
    # nothing. That is a normal outcome here, not a fault: choose_extraction()
    # falls back to the DOM walk, and a page with genuinely no text is stored with
    # an explicit error. Nothing is being discarded that we wanted to keep, so the
    # warning is reported for a case the caller already handles.
    logging.getLogger("trafilatura").setLevel(logging.ERROR)

    # Hugging Face emits an "unauthenticated requests" notice through
    # huggingface_hub.utils._http on every model load. It is advice for model
    # publishers, not a problem with this app, and the model loads from the local
    # cache in any case. Errors still surface.
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("transformers").setLevel(logging.ERROR)

    # Small libraries that log per-file chatter during a model load.
    logging.getLogger("filelock").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).debug(
        "logging configured at %s (file=%s)", resolved_level, target or "disabled"
    )


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(name)
