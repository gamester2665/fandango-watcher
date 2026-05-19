from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cloudflare_browser import crawl_target_worker
from cloudflare_config import D1WatchlistProvider
from cloudflare_state import D1StateProvider
from config import Settings, load_config, merge_watchlist
from direct_api_detect import detect_target_direct_api
from loop import ERROR_STREAK_THRESHOLD, _apply_direct_api_meta, _emit_events
from notify import build_notifier
from pydantic import SecretStr
from worker_config_api import handle_config_fetch

from state import record_error, transition

logger = logging.getLogger(__name__)


def _resolve_worker_config_path(config_rel: str) -> Path:
    """Resolve ``WATCHER_CONFIG`` for the Python Worker bundle."""
    p = Path(config_rel)
    if p.is_file():
        return p.resolve()
    here = Path(__file__).resolve().parent
    for ancestor in [here, *here.parents]:
        cand = (ancestor / config_rel).resolve()
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return p.resolve()


async def on_fetch(request, env, ctx):
    """Entry point for HTTP requests (config API + optional manual tick)."""
    return await handle_config_fetch(request, env)


async def on_scheduled(event, env, ctx):
    """Entry point for Cron Triggers."""
    await run_tick(env)


fetch = on_fetch
scheduled = on_scheduled


async def _load_worker_config(env: Any):
    config_rel = os.environ.get("WATCHER_CONFIG", "config.yaml")
    policy_cfg = load_config(_resolve_worker_config_path(config_rel))
    provider = D1WatchlistProvider(env.DB)
    await provider.init_schema()
    watchlist = await provider.get_watchlist()
    if watchlist.get("revision", 0) > 0:
        from config import MovieConfig, TargetConfig

        targets = [TargetConfig.model_validate(t) for t in watchlist.get("targets") or []]
        movies = [MovieConfig.model_validate(m) for m in watchlist.get("movies") or []]
        cfg = merge_watchlist(policy_cfg, targets, movies)
    else:
        cfg = policy_cfg
    return cfg


async def run_tick(env: Any):
    """Run one iteration of the watch loop."""
    try:
        cfg = await _load_worker_config(env)
        settings_dict = {
            "twilio_account_sid": getattr(env, "TWILIO_ACCOUNT_SID", ""),
            "twilio_auth_token": SecretStr(getattr(env, "TWILIO_AUTH_TOKEN", "")),
            "twilio_from": getattr(env, "TWILIO_FROM", ""),
            "notify_to_e164": getattr(env, "NOTIFY_TO_E164", ""),
            "smtp_host": getattr(env, "SMTP_HOST", ""),
            "smtp_port": int(getattr(env, "SMTP_PORT", "465")),
            "smtp_user": getattr(env, "SMTP_USER", ""),
            "smtp_password": SecretStr(getattr(env, "SMTP_PASSWORD", "")),
            "smtp_from": getattr(env, "SMTP_FROM", ""),
            "notify_to_email": getattr(env, "NOTIFY_TO_EMAIL", ""),
        }
        settings = Settings(**settings_dict)
    except Exception as e:
        logger.exception("Failed to load config/settings")
        return {"status": "error", "message": f"Config load failed: {e}"}

    db = env.DB
    state_provider = D1StateProvider(db)
    await state_provider.init_schema()

    notifier = build_notifier(cfg.notify, settings)

    results = []
    for target in cfg.targets:
        prev_state = await state_provider.load_target_state(target.name)

        try:
            try:
                direct_result = detect_target_direct_api(target, cfg)
                parsed = direct_result.parsed
                meta = direct_result.meta
                fallback = False
            except Exception:
                if not cfg.direct_api.fallback_to_browser:
                    raise

                logger.warning("Direct API failed for %s; falling back to browser", target.name)
                parsed = await crawl_target_worker(
                    env.BROWSER,
                    target,
                    citywalk_anchor=cfg.theater.fandango_theater_anchor,
                )
                meta = None
                fallback = True

            ok_result = transition(prev_state, parsed)
            new_state = _apply_direct_api_meta(ok_result.state, meta, fallback=fallback)

            await state_provider.save_target_state(new_state)

            _emit_events(
                notifier,
                result=ok_result,
                cfg=cfg,
                target_name=target.name,
                target_url=target.url,
                parsed=parsed,
                error=None,
            )
            results.append({"target": target.name, "status": "ok"})

        except Exception as e:
            logger.exception("Tick failed for target %s", target.name)
            err_result = record_error(prev_state, e, error_streak_threshold=ERROR_STREAK_THRESHOLD)
            await state_provider.save_target_state(err_result.state)

            _emit_events(
                notifier,
                result=err_result,
                cfg=cfg,
                target_name=target.name,
                target_url=target.url,
                parsed=None,
                error=e,
            )
            results.append({"target": target.name, "status": "error", "message": str(e)})

    return {"status": "done", "targets": results}
