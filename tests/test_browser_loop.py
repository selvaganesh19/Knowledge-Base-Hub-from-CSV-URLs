"""Tests for the browser renderer's event-loop handling on Windows.

This exists because of a bug that the normal test setup cannot see. Playwright
launches Chromium as a subprocess, and on Windows `SelectorEventLoop` cannot spawn
one - it raises NotImplementedError. uvicorn installs a Selector loop whenever it
runs the app in a child process, which `reload=True` does, so the browser worked in
every standalone script and died under the documented entrypoint.

The tests below are mostly skipped off Windows, because the condition does not
exist there: a POSIX Selector loop spawns subprocesses perfectly well.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.services import browser as browser_module
from app.services.browser import _needs_own_loop, render_page

WINDOWS = sys.platform == "win32"


@pytest.mark.skipif(not WINDOWS, reason="event-loop policy is a Windows concern")
class TestLoopDetection:
    def test_a_proactor_loop_does_not_need_its_own(self):
        """The default on Windows. Spawning works, so no thread is needed."""

        async def check():
            assert isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop)
            return _needs_own_loop()

        assert asyncio.run(check()) is False

    def test_a_selector_loop_is_recognised(self):
        """The loop uvicorn builds when `use_subprocess` is true.

        A Selector loop cannot create a subprocess, so this is exactly the state
        that raised NotImplementedError from Playwright's transport.
        """
        loop = asyncio.windows_events._WindowsSelectorEventLoop()
        try:
            assert isinstance(loop, asyncio.SelectorEventLoop)

            async def check():
                return _needs_own_loop()

            assert loop.run_until_complete(check()) is True
        finally:
            loop.close()

    def test_a_selector_loop_really_cannot_spawn_a_subprocess(self):
        """The premise of the whole fix. If this ever passes, the fix is unnecessary."""
        loop = asyncio.windows_events._WindowsSelectorEventLoop()

        async def spawn():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "pass",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()

        try:
            with pytest.raises(NotImplementedError):
                loop.run_until_complete(spawn())
        finally:
            loop.close()


class TestRenderEntryPoint:
    def test_render_never_raises_when_no_browser_is_installed(self, monkeypatch):
        """A missing browser is a crawl outcome, not an exception."""
        monkeypatch.setattr(browser_module, "browser_available", lambda: (False, "none"))
        browser_module.reset_availability_cache()

        result = asyncio.run(render_page("https://example.test/"))

        assert result.ok is False
        assert result.available is False
        assert "none" in result.error

    def test_an_own_loop_render_is_used_when_the_loop_cannot_spawn(self, monkeypatch, tmp_path):
        """The dispatch itself, without needing a browser: the worker path must be
        taken, and its failure must come back as a RenderedPage rather than raising."""
        monkeypatch.setattr(browser_module, "browser_available", lambda: (True, ""))
        monkeypatch.setattr(browser_module, "_needs_own_loop", lambda: True)

        calls = {"n": 0}

        def fake_own_loop(url, timeout_ms):
            calls["n"] += 1
            return browser_module.RenderedPage(html="<html>rendered</html>", title="T")

        monkeypatch.setattr(browser_module, "_run_render_in_own_loop", fake_own_loop)
        result = asyncio.run(render_page("https://example.test/"))

        assert calls["n"] == 1
        assert result.html == "<html>rendered</html>"

    def test_the_own_loop_path_reports_failure_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(browser_module, "browser_available", lambda: (True, ""))
        monkeypatch.setattr(browser_module, "_needs_own_loop", lambda: True)

        def boom(url, timeout_ms):
            raise RuntimeError("no chromium")

        monkeypatch.setattr(browser_module, "_run_render_in_own_loop", boom)
        result = asyncio.run(render_page("https://example.test/"))

        assert result.ok is False
        assert "no chromium" in result.error
