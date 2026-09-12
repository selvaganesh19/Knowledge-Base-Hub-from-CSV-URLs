"""Development entrypoint: python run.py

Uvicorn is deliberately started with a single worker. The FAISS index and the
background-job registry are per-process state, so multiple workers would each
hold their own index and orphan each other's jobs. Scaling out means moving the
index and jobs into a separate worker process, not adding uvicorn workers.

Both `reload` and `workers > 1` make uvicorn run the application in a child
process, and on Windows that child gets a `SelectorEventLoop` - which cannot spawn
a subprocess, which is how Playwright launches Chromium. The browser renderer
detects that and runs on a loop of its own (see `app.services.browser`), so reload
still works; it is off here only because this entrypoint is meant to mirror
production, and a reload watcher is not part of that.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    # `RELOAD=1 python run.py` during development. Off by default so the entrypoint
    # behaves like the documented single-process deployment.
    reload = os.environ.get("RELOAD", "").strip() in {"1", "true", "yes"}

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=reload,
        workers=1,
    )


if __name__ == "__main__":
    main()
