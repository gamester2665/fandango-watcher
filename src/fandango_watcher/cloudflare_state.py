from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from models import ParsedPageData
from state import TargetState, load_target_state, save_target_state

logger = logging.getLogger(__name__)

class D1StateProvider:
    """Cloudflare D1 implementation of state persistence."""

    def __init__(self, db: Any):
        self.db = db

    async def init_schema(self):
        """Create the state table if it doesn't exist."""
        await self.db.prepare(
            "CREATE TABLE IF NOT EXISTS target_states ("
            "  target_name TEXT PRIMARY KEY,"
            "  state_json TEXT NOT NULL,"
            "  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
            ")"
        ).run()

    async def load_target_state(self, target_name: str) -> TargetState:
        row = await self.db.prepare(
            "SELECT state_json FROM target_states WHERE target_name = ?"
        ).bind(target_name).first()

        if not row:
            return TargetState(target_name=target_name)
        
        try:
            return TargetState.model_validate_json(row["state_json"])
        except Exception:
            logger.exception("failed to load state from D1 for %s", target_name)
            return TargetState(target_name=target_name)

    async def save_target_state(self, state: TargetState):
        json_data = state.model_dump_json()
        await self.db.prepare(
            "INSERT INTO target_states (target_name, state_json, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(target_name) DO UPDATE SET "
            "  state_json = excluded.state_json, "
            "  updated_at = excluded.updated_at"
        ).bind(state.target_name, json_data, datetime.now(UTC).isoformat()).run()
