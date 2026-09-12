"""Request logging middleware.

Every request gets a short id that is echoed in the response header and included
in the log line, so a slow or failing request can be traced from the browser's
network tab to the server log without guessing which line belongs to it.
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Paths the browser requests for its own reasons, where the status carries no
# information about this app: static assets it may or may not have asked for, and
# the probe Chrome DevTools issues on every page load. The app does not serve a
# DevTools descriptor, so the 404 is the correct answer and not worth a log line.
SILENT_PATHS = ("/static", "/favicon.ico", "/.well-known")

# Polled on a timer by the progress UI. Quiet when they succeed - left at INFO, a
# single harvest buries every other line under its own progress updates - but a
# poll that starts returning 500 is exactly what someone needs to see, so a
# failure here is still logged.
QUIET_PATHS = ("/health", "/api/jobs")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, duration and request id for each request."""

    async def dispatch(self, request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # The traceback matters more than the timing here, and the exception
            # still propagates so FastAPI can render its 500 response.
            logger.exception(
                "%s %s failed after %.0f ms [%s]",
                request.method,
                request.url.path,
                (time.perf_counter() - started) * 1000,
                request_id,
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        path = request.url.path
        quiet = path.startswith(SILENT_PATHS) or (
            path.startswith(QUIET_PATHS) and response.status_code < 400
        )
        if quiet:
            logger.debug("%s %s -> %s [%s]", request.method, path, response.status_code, request_id)
        else:
            logger.info(
                "%s %s -> %s (%.0f ms) [%s]",
                request.method,
                path,
                response.status_code,
                duration_ms,
                request_id,
            )

        return response
