"""Root logging configuration for the CLI."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime


def configure_logging(level: str) -> None:
    """Configure root logging; use ``LOG_FORMAT=json`` in the environment for JSON lines."""
    root = logging.getLogger()
    level_no = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level_no)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt_env = (os.environ.get("LOG_FORMAT") or "").strip().lower()
    if fmt_env == "json":

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    payload["exc_info"] = self.formatException(record.exc_info)
                return json.dumps(payload, default=str)

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
        return

    logging.basicConfig(
        level=level_no,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
