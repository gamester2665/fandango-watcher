from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cloudflare_browser import crawl_target_worker
from cloudflare_state import D1StateProvider
from config import Settings, load_config
from direct_api_detect import detect_target_direct_api
from loop import ERROR_STREAK_THRESHOLD, _apply_direct_api_meta, _emit_events
from notify import build_notifier
from pydantic import SecretStr

from state import record_error, transition

logger = logging.getLogger(__name__)


def _resolve_worker_config_path(config_rel: str) -> Path:
    """Resolve ``WATCHER_CONFIG`` for the Python Worker bundle.

    The isolate working directory is not guaranteed. Walk upward from this
    module so ``worker-config.yaml`` at the repo root is found when bundled.
    """
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
    """Entry point for HTTP requests (manual trigger/health check)."""
    res = await run_tick(env)
    from js import Response
    return Response.new(json.dumps(res), headers={"content-type": "application/json"})

async def on_scheduled(event, env, ctx):
    """Entry point for Cron Triggers."""
    await run_tick(env)

# Cloudflare Python Workers expect 'fetch' and 'scheduled' to be exported at the top level
# if using the 'on_fetch'/'on_scheduled' naming convention in some environments,
# but the standard export names are 'fetch' and 'scheduled'.
fetch = on_fetch
scheduled = on_scheduled

async def run_tick(env: Any):
    """Run one iteration of the watch loop."""
    # 1. Load config and settings from environment
    try:
        config_rel = os.environ.get("WATCHER_CONFIG", "config.yaml")
        cfg = load_config(_resolve_worker_config_path(config_rel))

        # Pydantic-settings will pick up env vars from env
        # Cloudflare Workers provides env vars as attributes on the 'env' object
        # We need to map them to the Settings model.
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
        # Return a 500-ish response if this is an HTTP trigger
        return {"status": "error", "message": f"Config load failed: {e}"}

    db = env.DB
    state_provider = D1StateProvider(db)
    await state_provider.init_schema()

    notifier = build_notifier(cfg.notify, settings)
    
    results = []
    for target in cfg.targets:
        prev_state = await state_provider.load_target_state(target.name)
        
        try:
            # 1. Try Direct API first
            try:
                direct_result = detect_target_direct_api(target, cfg)
                parsed = direct_result.parsed
                meta = direct_result.meta
                fallback = False
            except Exception:
                if not cfg.direct_api.fallback_to_browser:
                    raise
                
                logger.warning("Direct API failed for %s; falling back to browser", target.name)
                # 2. Fallback to Cloudflare Browser Rendering
                parsed = await crawl_target_worker(
                    env.BROWSER, 
                    target, 
                    citywalk_anchor=cfg.theater.fandango_theater_anchor
                )
                meta = None # We don't have direct API meta for browser crawls
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
                error=None
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
                error=e
            )
            results.append({"target": target.name, "status": "error", "message": str(e)})

    return {"status": "done", "targets": results}
