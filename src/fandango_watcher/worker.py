from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import SecretStr

from config import Settings, WatcherConfig, load_config
from cloudflare_state import D1StateProvider
from cloudflare_browser import crawl_target_worker
from direct_api_detect import detect_target_direct_api
from loop import _emit_events, _apply_direct_api_meta, ERROR_STREAK_THRESHOLD
from notify import build_notifier
from state import transition, record_error

logger = logging.getLogger(__name__)

async def on_fetch(request, env, ctx):
    """Entry point for HTTP requests (manual trigger/health check). Triggering new build."""
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
        # For Workers, we look for 'config.yaml' in the root of the worker bundle.
        config_path = os.environ.get("WATCHER_CONFIG", "config.yaml")
        
        # Manually load YAML to avoid 'yaml' module issues in Pyodide if it's not bundled correctly
        # But load_config uses it, so we must ensure it's available.
        cfg = load_config(config_path)
        
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
            except Exception as e:
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
