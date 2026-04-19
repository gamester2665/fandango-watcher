"""xAI (Grok) summaries of public release/ticketing news for dashboard movies.

Uses the OpenAI-compatible Chat Completions API at ``https://api.x.ai/v1``.
Results are cached under ``<state_dir>/release_intel_cache.json`` with TTL
from config so the dashboard does not call the API on every auto-refresh.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, WatcherConfig

logger = logging.getLogger(__name__)

CACHE_BASENAME = "release_intel_cache.json"
XAI_CHAT_COMPLETIONS_URL = "https://api.x.ai/v1/chat/completions"


def _cache_path(state_dir: Path) -> Path:
    return state_dir / CACHE_BASENAME


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating ```json fences."""
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return json.loads(raw)


def _build_watch_context(cfg: WatcherConfig, state_dir: Path) -> list[dict[str, Any]]:
    """One row per configured movie with live Fandango crawl state."""
    target_state: dict[str, dict[str, Any]] = {}
    for t in cfg.targets:
        p = state_dir / f"{t.name}.json"
        if not p.is_file():
            continue
        try:
            target_state[t.name] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

    rows: list[dict[str, Any]] = []
    for m in cfg.movies:
        tstats: list[dict[str, Any]] = []
        for tn in m.fandango_targets:
            st = target_state.get(tn) or {}
            tstats.append(
                {
                    "target_name": tn,
                    "last_release_schema": st.get("last_release_schema"),
                    "current_state": st.get("current_state"),
                    "last_success_at": st.get("last_success_at"),
                    "total_ticks": st.get("total_ticks"),
                }
            )
        rows.append(
            {
                "key": m.key,
                "title": m.title,
                "preferred_formats": [f.value for f in (m.preferred_formats or [])],
                "x_handles": list(m.x_handles or []),
                "x_keywords": list(m.x_keywords or []),
                "fandango_targets": tstats,
            }
        )
    return rows


def _call_xai(
    *,
    api_key: str,
    model: str,
    user_prompt: str,
    timeout_seconds: float,
) -> str:
    payload = {
        "model": model,
        "temperature": 0.35,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize US theatrical release and ticketing news for films "
                    "the user is tracking. Be concise and factual in tone. If dates "
                    "or sale status are uncertain, say so. Output JSON only."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
    }
    with httpx.Client(timeout=timeout_seconds) as client:
        r = client.post(
            XAI_CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("xAI response missing choices")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("xAI response missing message content")
    return content


def _compose_prompt(rows: list[dict[str, Any]]) -> str:
    ctx = json.dumps(rows, indent=2, default=str)
    return f"""Below is JSON from our local watcher: each movie we track, its Fandango target crawl status (schema = ticket availability signal), and X handles we follow for hints.

{ctx}

For EACH movie ``key`` in the input, respond with ONLY a JSON object (no markdown) in exactly this shape:
{{
  "movies": {{
    "<movie_key>": {{
      "headline": "short title-style line",
      "summary": "2-5 sentences on public release window, formats, major marketing beats",
      "ticketing": "what is known about on-sale / presale / wide booking, or TBD",
      "notable_dates": "optional: key dates or seasons mentioned in press; null if unknown",
      "qualifier": "one sentence: this is general public-reporting knowledge, not live Fandango data"
    }}
  }}
}}

Include every movie key from the input. If a movie has no Fandango targets yet, still give useful release-news context."""


def refresh_release_intel(
    cfg: WatcherConfig,
    *,
    state_dir: Path,
    settings: Settings,
) -> dict[str, Any]:
    """Call xAI and write ``release_intel_cache.json``. Raises on HTTP/parse errors."""
    ri = cfg.release_intel
    api_key = (settings.xai_api_key or "").strip()
    if not api_key:
        raise ValueError("XAI_API_KEY is not set")

    model = (settings.xai_model or "").strip() or ri.model
    rows = _build_watch_context(cfg, state_dir)
    if not rows:
        payload = {
            "updated_at": datetime.now(UTC).isoformat(),
            "model": model,
            "movies": {},
            "note": "no movies in config registry",
        }
        _cache_path(state_dir).write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        return payload

    prompt = _compose_prompt(rows)
    content = _call_xai(
        api_key=api_key,
        model=model,
        user_prompt=prompt,
        timeout_seconds=float(ri.timeout_seconds),
    )
    parsed = _extract_json_object(content)
    movies_out = parsed.get("movies")
    if not isinstance(movies_out, dict):
        raise ValueError("model JSON missing movies object")

    out = {
        "updated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "movies": movies_out,
        "error": None,
    }
    _cache_path(state_dir).write_text(
        json.dumps(out, indent=2, default=str),
        encoding="utf-8",
    )
    return out


def load_cached_intel(state_dir: Path) -> dict[str, Any] | None:
    p = _cache_path(state_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_release_intel_for_dashboard(
    cfg: WatcherConfig,
    *,
    state_dir: Path,
    settings: Settings | None,
) -> dict[str, Any]:
    """Return a JSON-safe blob for ``/api/status`` and the HTML dashboard.

    Respects ``release_intel.enabled``, ``XAI_API_KEY``, and cache TTL.
    """
    ri = cfg.release_intel
    if not ri.enabled:
        return {"status": "disabled", "reason": "release_intel.enabled is false"}

    if settings is None or not (settings.xai_api_key or "").strip():
        return {
            "status": "unconfigured",
            "reason": "set XAI_API_KEY in .env for Grok release summaries",
        }

    cached = load_cached_intel(state_dir)
    now = datetime.now(UTC)

    if cached and cached.get("updated_at"):
        try:
            ts = cached["updated_at"]
            if isinstance(ts, str) and ts.endswith("Z"):
                ts = ts.replace("Z", "+00:00")
            updated = datetime.fromisoformat(ts)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            age = (now - updated.astimezone(UTC)).total_seconds()
            if age >= 0 and age < float(ri.cache_ttl_seconds):
                return {
                    "status": "ok",
                    "source": "cache",
                    "updated_at": cached.get("updated_at"),
                    "model": cached.get("model"),
                    "movies": cached.get("movies") or {},
                    "cache_age_seconds": int(age),
                }
        except (ValueError, OSError, TypeError):
            logger.debug("release intel cache parse failed; refreshing", exc_info=True)

    try:
        fresh = refresh_release_intel(cfg, state_dir=state_dir, settings=settings)
        return {
            "status": "ok",
            "source": "live",
            "updated_at": fresh.get("updated_at"),
            "model": fresh.get("model"),
            "movies": fresh.get("movies") or {},
            "cache_age_seconds": 0,
        }
    except Exception as e:  # noqa: BLE001 — dashboard must stay up
        logger.warning("release intel refresh failed: %s", e)
        stale = cached or {}
        return {
            "status": "stale_or_error",
            "error": f"{type(e).__name__}: {e}",
            "updated_at": stale.get("updated_at"),
            "model": stale.get("model"),
            "movies": stale.get("movies") or {},
        }
