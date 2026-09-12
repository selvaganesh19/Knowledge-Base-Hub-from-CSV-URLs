"""Quick check of the optional Crawl4AI engine, without running the pipeline.

    python scripts/try_crawl4ai.py                       # a default URL
    python scripts/try_crawl4ai.py https://example.com/  # your own

Prints what the engine returned and how it classified it, so you can see whether it
is working before switching the application over with CRAWL_ENGINE=crawl4ai.

Nothing here touches the database, the index, or the running application.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Crawl4AI's errors quote page content and library advice, which a cp1252 console
# cannot encode. Without this the script dies while reporting a failure.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings  # noqa: E402
from app.services.crawl4ai_engine import crawl4ai_available  # noqa: E402

DEFAULT_URLS = (
    "https://www.oracle.com/in/corporate/executives/",
    "https://www.amazon.com/",
)


async def probe(url: str, settings) -> None:
    from app.services.crawl4ai_engine import fetch_with_crawl4ai

    print(f"\n--- {url}")
    started = time.perf_counter()
    try:
        result = await fetch_with_crawl4ai(url, settings)
    except Exception as exc:  # noqa: BLE001 - this script exists to show failures
        elapsed = (time.perf_counter() - started) * 1000
        print(f"    ENGINE UNAVAILABLE after {elapsed:.0f} ms")
        print(f"      {type(exc).__name__}: {exc}")
        print("      The application would fall back to the HTTP engine for this URL.")
        return

    elapsed = (time.perf_counter() - started) * 1000
    print(f"    status   {result.crawl_status}   http {result.status_code}   {elapsed:.0f} ms")
    print(f"    method   {result.scraping_method}")
    print(f"    text     {result.char_count} chars, {result.word_count} words")
    print(f"    title    {result.title[:70]!r}")
    if result.error:
        print(f"    error    {result.error}")
    if result.text:
        print(f"    sample   {' '.join(result.text.split())[:150]!r}")


async def main() -> int:
    settings = get_settings()
    urls = sys.argv[1:] or list(DEFAULT_URLS)

    available, why = crawl4ai_available()
    print(f"crawl4ai importable : {available}" + (f"  ({why})" if why else ""))
    if not available:
        print("\nInstall it with:  python -m pip install crawl4ai")
        return 1

    print(f"CRAWL_ENGINE        : {settings.crawl_engine}  (http = the built-in engine)")
    print(f"ENABLE_PLAYWRIGHT   : {settings.enable_playwright}")

    for url in urls:
        await probe(url, settings)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
