"""Cloudflare Worker entry for the D1 watchlist config API only.

This module intentionally avoids importing the poll loop, Playwright, or Twilio
so ``pywrangler deploy`` can bundle a small Worker compatible with Pyodide.
Full tick logic remains in ``worker.py`` for a future split deploy.
"""

from __future__ import annotations

from workers import WorkerEntrypoint

from worker_config_api import handle_config_fetch


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await handle_config_fetch(request, self.env)
