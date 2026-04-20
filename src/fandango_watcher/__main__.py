"""Allow ``python -m fandango_watcher`` (avoids locking ``fandango-watcher.exe`` during ``uv sync``)."""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    raise SystemExit(main())
