"""Optional JavaScript rendering, used when the HTTP fetch comes back thin.

Many modern sites ship a shell of markup and build the content in the browser. The
HTTP fetcher sees the shell, so the page comes back as a handful of characters and
is recorded as PARTIAL. Playwright runs a real browser against the same URL and the
content appears.

Three properties matter more than the rendering itself:

* **It is optional.** Playwright needs a Chromium binary that is a ~150 MB download,
  so the application must run perfectly well without it. Every entry point here
  checks availability and returns a reason rather than raising.
* **It is the fallback, not the default.** A browser costs seconds and hundreds of
  megabytes of memory per page. Rendering every URL would make a 25-URL harvest
  take minutes for no benefit on the pages that were already fine.
* **It never resolves to a wrong answer.** If rendering produces less text than the
  HTTP pass, the HTTP result is kept. Browser rendering can trip bot checks that
  the plain client passed, and silently swapping in a CAPTCHA page would be a
  regression dressed as a fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Element types worth removing before extraction - identical to the DOM-walk
#: fallback's list, because the same chrome has to go whatever produced the HTML.
_STRIP_SELECTORS = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
)

_availability: tuple[bool, str] | None = None


@dataclass
class RenderedPage:
    """What a browser render produced, or why it produced nothing."""

    html: str = ""
    final_url: str = ""
    title: str = ""
    available: bool = True
    error: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.error


def browser_available() -> tuple[bool, str]:
    """Whether a browser can actually be launched. Cached after the first check.

    Installation is checked in two steps because the two halves fail independently:
    the `playwright` package can be installed with no browser downloaded, which is
    exactly the state a fresh `pip install -r requirements.txt` leaves behind.
    Reporting which half is missing is the difference between a usable error and a
    confusing one.

    The browser is looked for on disk rather than by asking Playwright
    (`playwright.chromium.executable_path`), because that property is only readable
    from inside a started Playwright context and its sync form refuses to run on a
    thread that has an event loop - which is precisely where the crawler calls this
    from. A directory listing has no such restriction.
    """
    global _availability
    if _availability is not None:
        return _availability

    try:
        import playwright  # noqa: F401
    except ImportError:
        _availability = (False, "playwright is not installed (pip install playwright)")
        return _availability

    root = browsers_path()
    if not root.exists():
        _availability = (
            False,
            "no browser downloaded (run: python -m playwright install chromium)",
        )
        return _availability

    # Playwright names each install chromium-<build>, and keeps a .links marker for
    # installs it shares between package versions.
    found = [entry for entry in root.glob("chromium*") if entry.is_dir()]
    if not found:
        _availability = (
            False,
            "chromium not found in the playwright browser directory "
            "(run: python -m playwright install chromium)",
        )
        return _availability

    _availability = (True, "")
    return _availability


def browsers_path() -> Path:
    """Where Playwright keeps its browser builds for this user."""
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def reset_availability_cache() -> None:
    """Forget the cached probe. Used by tests and after installing a browser."""
    global _availability
    _availability = None


async def render_page(url: str, timeout_ms: int = 30_000) -> RenderedPage:
    """Load a URL in a real browser and return the rendered HTML.

    Never raises: a browser failure is a crawl outcome, not an application error.
    """
    available, why = browser_available()
    if not available:
        return RenderedPage(available=False, error=why)

    try:
        if _needs_own_loop():
            # See _needs_own_loop: on Windows the server's loop may be unable to
            # spawn the browser process, so the render runs on a loop that can.
            return await asyncio.to_thread(_run_render_in_own_loop, url, timeout_ms)
        return await _run_render(url, timeout_ms)
    except Exception as exc:  # noqa: BLE001 - any browser problem is a soft failure
        logger.info("browser render failed for %s: %s", url, exc)
        return RenderedPage(error=f"{type(exc).__name__}: {exc}")


def _needs_own_loop() -> bool:
    """Whether the running event loop cannot spawn a subprocess.

    Playwright launches Chromium as a subprocess, and on Windows
    `SelectorEventLoop` raises NotImplementedError from
    `_make_subprocess_transport` rather than doing it. That is not a rare state: it
    is the default under `uvicorn --reload` and under `workers > 1`, because
    uvicorn picks SelectorEventLoop whenever it runs the app in a child process
    (`use_subprocess = reload or workers > 1`).

    So this is not an edge case to tolerate - it is the normal development setup.
    Detecting it and rendering on a loop that *can* spawn the browser is what makes
    the browser work under the documented entrypoint rather than only in a script.
    """
    if sys.platform != "win32":
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # No loop yet; asyncio.run will make a Proactor one.

    return isinstance(loop, asyncio.SelectorEventLoop)


def _run_render_in_own_loop(url: str, timeout_ms: int) -> RenderedPage:
    """Run one render on a Proactor loop belonging to this thread.

    The policy is not touched: `asyncio.set_event_loop_policy` is global, so
    changing it from a worker thread to fix this call would silently change how
    every future loop in the process is created. A loop constructed directly is
    local to the work and closed with it.
    """
    loop = asyncio.ProactorEventLoop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run_render(url, timeout_ms))
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


async def _run_render(url: str, timeout_ms: int) -> RenderedPage:
    """The actual Playwright run. Assumes a loop that can spawn a subprocess."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 900},
                )
                page = await context.new_page()
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # `domcontentloaded` fires before most single-page apps have painted.
                # The settled state is what carries the content, and a failure here
                # means the page keeps polling - not that the render failed.
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms // 2)

                html = await page.content()
                title = await page.title()
                final_url = page.url
            finally:
                await browser.close()

    except Exception as exc:  # noqa: BLE001 - any browser problem is a soft failure
        logger.info("browser render failed for %s: %s", url, exc)
        return RenderedPage(error=f"{type(exc).__name__}: {exc}")

    return RenderedPage(html=html, title=title or "", final_url=final_url)


def render_page_sync(url: str, timeout_ms: int = 30_000) -> RenderedPage:
    """Blocking wrapper, for the CLI and tests."""
    return asyncio.run(render_page(url, timeout_ms=timeout_ms))
