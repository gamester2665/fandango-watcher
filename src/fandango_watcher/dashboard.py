"""Read-only HTML + JSON dashboard over persisted state and artifacts."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from .config import Settings, WatcherConfig
from .release_intel import get_release_intel_for_dashboard
from .social_x import load_social_x_state

_PT = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DashboardPaths:
    """Filesystem roots the dashboard may read (never write)."""

    state_dir: Path
    screenshot_dir: Path
    video_dir: Path
    trace_dir: Path
    purchase_dir: Path
    # ``state/social_x.json`` — X poller persistence (same as social_x._state_path).
    social_x_state_path: Path
    artifacts_root: Path

    @classmethod
    def from_config(cls, cfg: WatcherConfig) -> DashboardPaths:
        state_dir = Path(cfg.state.dir).resolve()
        screenshot_dir = Path(cfg.screenshots.dir).resolve()
        video_dir = Path(cfg.browser.record_video_dir).resolve()
        trace_dir = Path(cfg.browser.record_trace_dir).resolve()
        purchase_dir = Path(cfg.screenshots.per_purchase_dir).resolve()
        social_x_state_path = state_dir / "social_x.json"
        artifacts_root = screenshot_dir.parent.resolve()
        return cls(
            state_dir=state_dir,
            screenshot_dir=screenshot_dir,
            video_dir=video_dir,
            trace_dir=trace_dir,
            purchase_dir=purchase_dir,
            social_x_state_path=social_x_state_path,
            artifacts_root=artifacts_root,
        )


@dataclass
class DashboardData:
    """Everything the HTTP handler needs to render one snapshot."""

    cfg: WatcherConfig
    paths: DashboardPaths
    # :class:`~fandango_watcher.healthz.Heartbeat` (avoid circular import).
    heartbeat: Any | None = None
    # Env for optional xAI (Grok) release-intel summaries on the dashboard.
    settings: Settings | None = None
    # HTML meta refresh interval; 0 disables auto-reload.
    refresh_seconds: int = 10
    # Set after the HTTP server binds (actual listen address for dashboard URL copy).
    public_host: str | None = None
    public_port: int | None = None
    # ``(revision_hex, raw_fingerprint)`` — skip re-hashing when inputs unchanged.
    _revision_cache: tuple[str, str] | None = field(default=None, repr=False)


def _latest_artifact_for_target(
    name: str,
    directory: Path,
    suffix: str,
) -> Path | None:
    """Newest file named ``{name}-*.{suffix}`` under ``directory``."""
    if not directory.is_dir():
        return None
    prefix = f"{name}-"
    candidates: list[Path] = []
    for p in directory.iterdir():
        if (
            p.is_file()
            and p.suffix.lower() == suffix.lower()
            and p.name.startswith(prefix)
        ):
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def artifact_url(artifacts_root: Path, file_path: Path | None) -> str | None:
    """``/artifacts/...`` URL for a file under ``artifacts_root``, or ``None``."""
    if file_path is None:
        return None
    try:
        resolved = file_path.resolve()
        root = artifacts_root.resolve()
        rel = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return "/artifacts/" + rel.as_posix()


def _load_target_state_json(state_dir: Path, name: str) -> dict[str, Any]:
    p = state_dir / f"{name}.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _fmt_pt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(_PT).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, OSError, TypeError):
        return str(iso)


def _tail_purchases_jsonl(state_dir: Path, *, max_lines: int) -> list[dict[str, Any]]:
    """Last ``max_lines`` non-empty JSON objects from ``state/purchases.jsonl``."""
    path = state_dir / "purchases.jsonl"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tail = raw_lines[-max_lines:]
    out: list[dict[str, Any]] = []
    for ln in tail:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _relative_ago(
    iso: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Short relative time for crawl timestamps (server clock)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
        ref = (now or datetime.now(UTC)).astimezone(UTC)
        secs = int((ref - dt).total_seconds())
        if secs < 45:
            return "just now"
        if secs < 3600:
            return f"{max(1, secs // 60)}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, OSError, TypeError):
        return ""


def _fmt_duration(seconds: int | float | str | None) -> str:
    """Human-friendly duration for operator-facing cadence labels."""
    try:
        total = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


def _fmt_duration_range(min_seconds: Any, max_seconds: Any) -> str:
    """Format a jittered poll range, e.g. ``270..330`` -> ``4m 30s-5m 30s``."""
    left = _fmt_duration(min_seconds)
    right = _fmt_duration(max_seconds)
    return left if left == right else f"{left}-{right}"


def collect_dashboard_state(data: DashboardData) -> dict[str, Any]:
    """JSON-serializable snapshot for ``/api/status`` and HTML rendering."""
    paths = data.paths
    hb = data.heartbeat
    healthz: dict[str, Any]
    if hb is not None and hasattr(hb, "snapshot"):
        healthz = cast(dict[str, Any], hb.snapshot())
    else:
        healthz = {"status": "ok", "started_at": None, "note": "no heartbeat"}
    if hb is not None:
        lt = healthz.get("last_tick_at")
        healthz["last_tick_at_pt"] = _fmt_pt(lt) if isinstance(lt, str) else None

    targets_out: list[dict[str, Any]] = []
    for t in data.cfg.targets:
        st = _load_target_state_json(paths.state_dir, t.name)
        direct_api_state = {
            "status": st.get("direct_api_last_status"),
            "used_direct_api": st.get("direct_api_last_used"),
            "used_browser_fallback": st.get("direct_api_last_fallback"),
            "inspected_dates": st.get("direct_api_last_inspected_dates") or [],
            "formats_seen": st.get("direct_api_last_formats_seen") or [],
            "unknown_formats": st.get("direct_api_last_unknown_formats") or [],
            "matching_showtime_hashes": st.get("direct_api_last_matching_hashes") or [],
            "fallback_count": st.get("direct_api_fallback_count") or 0,
            "last_drift_warning": st.get("direct_api_last_drift_warning"),
        }
        shot = _latest_artifact_for_target(t.name, paths.screenshot_dir, ".png")
        vid = _latest_artifact_for_target(t.name, paths.video_dir, ".webm")
        tr = _latest_artifact_for_target(t.name, paths.trace_dir, ".zip")
        targets_out.append(
            {
                "name": t.name,
                "url": t.url,
                "state": st,
                "direct_api": direct_api_state,
                "latest_screenshot": str(shot) if shot else None,
                "latest_screenshot_url": artifact_url(paths.artifacts_root, shot),
                "latest_video": str(vid) if vid else None,
                "latest_video_url": artifact_url(paths.artifacts_root, vid),
                "latest_trace": str(tr) if tr else None,
                "latest_trace_url": artifact_url(paths.artifacts_root, tr),
            }
        )

    sx = load_social_x_state(paths.state_dir)
    social_x = sx.model_dump(mode="json")

    movies = [m.model_dump(mode="json") for m in data.cfg.movies]

    release_intel = get_release_intel_for_dashboard(
        data.cfg,
        state_dir=paths.state_dir,
        settings=data.settings,
    )

    dash = data.cfg.dashboard
    purchases_path = paths.state_dir / "purchases.jsonl"
    purchases_history: list[dict[str, Any]] = []
    if dash.show_purchase_history:
        purchases_history = _tail_purchases_jsonl(
            paths.state_dir,
            max_lines=dash.purchase_history_max_lines,
        )

    bind_host = data.public_host or "127.0.0.1"
    bind_port = data.public_port if data.public_port is not None else 8787
    public_base = f"http://{bind_host}:{bind_port}/"

    return {
        "healthz": healthz,
        "targets": targets_out,
        "social_x": social_x,
        "movies": movies,
        "release_intel": release_intel,
        "purchases_history": purchases_history,
        "dashboard": {
            "show_purchase_history": dash.show_purchase_history,
            "purchase_history_max_lines": dash.purchase_history_max_lines,
        },
        "runtime": {
            "host": bind_host,
            "dashboard_port": bind_port,
            "public_base_url": public_base,
            "target_count": len(data.cfg.targets),
            "state_dir": str(paths.state_dir),
            "artifacts_root": str(paths.artifacts_root),
            "browser_profile": str(Path(data.cfg.browser.user_data_dir).resolve()),
            "purchase_mode": data.cfg.purchase.mode,
            "purchase_enabled": data.cfg.purchase.enabled,
            "notify_channels": list(data.cfg.notify.channels),
            "fandango_poll": {
                "min_seconds": data.cfg.poll.min_seconds,
                "max_seconds": data.cfg.poll.max_seconds,
                "error_backoff_multiplier": data.cfg.poll.error_backoff_multiplier,
                "error_backoff_cap_seconds": data.cfg.poll.error_backoff_cap_seconds,
            },
            "direct_api": {
                "enabled": data.cfg.direct_api.enabled,
                "fallback_to_browser": data.cfg.direct_api.fallback_to_browser,
                "theater_id": data.cfg.direct_api.theater_id,
                "max_dates_per_tick": data.cfg.direct_api.max_dates_per_tick,
                "stop_on_first_match": data.cfg.direct_api.stop_on_first_match,
                "alert_unknown_formats": data.cfg.direct_api.alert_unknown_formats,
            },
            "social_x_poll": {
                "enabled": data.cfg.social_x.enabled,
                "min_seconds": data.cfg.social_x.min_seconds,
                "max_seconds": data.cfg.social_x.max_seconds,
                "max_results_per_handle": data.cfg.social_x.max_results_per_handle,
                "state_path": str(paths.social_x_state_path),
            },
        },
        "paths": {
            "state_dir": str(paths.state_dir),
            "social_x_state_path": str(paths.social_x_state_path),
            "artifacts_root": str(paths.artifacts_root),
            "purchases_jsonl": str(purchases_path),
        },
    }


def compute_dashboard_revision(data: DashboardData) -> str:
    """Short fingerprint that changes when the rendered dashboard would change.

    Used by ``/api/revision`` and the HTML live-reload script so the **same tab**
    refreshes as soon as crawl state, artifacts, or heartbeat data updates — without
    relying on a fixed full-page interval only.
    """
    parts: list[str] = []
    hb = data.heartbeat
    if hb is not None and hasattr(hb, "revision_fingerprint_parts"):
        parts.extend(hb.revision_fingerprint_parts())
    elif hb is not None:
        parts.append(str(getattr(hb, "total_ticks", 0)))
        parts.append(str(getattr(hb, "total_errors", 0)))
        lt = getattr(hb, "last_tick_at", None)
        parts.append(lt.isoformat() if lt is not None else "")
        extra = getattr(hb, "extra", None)
        if isinstance(extra, dict) and extra:
            parts.append(json.dumps(extra, sort_keys=True, default=str))
    paths = data.paths
    for t in data.cfg.targets:
        sp = paths.state_dir / f"{t.name}.json"
        parts.append(str(sp.stat().st_mtime_ns) if sp.is_file() else "0")
        shot = _latest_artifact_for_target(t.name, paths.screenshot_dir, ".png")
        vid = _latest_artifact_for_target(t.name, paths.video_dir, ".webm")
        tr = _latest_artifact_for_target(t.name, paths.trace_dir, ".zip")
        parts.append(str(shot.stat().st_mtime_ns) if shot else "0")
        parts.append(str(vid.stat().st_mtime_ns) if vid else "0")
        parts.append(str(tr.stat().st_mtime_ns) if tr else "0")
    sx = paths.social_x_state_path
    parts.append(str(sx.stat().st_mtime_ns) if sx.is_file() else "0")
    ric = paths.state_dir / "release_intel_cache.json"
    parts.append(str(ric.stat().st_mtime_ns) if ric.is_file() else "0")
    pj = paths.state_dir / "purchases.jsonl"
    parts.append(str(pj.stat().st_mtime_ns) if pj.is_file() else "0")
    raw = "|".join(parts)
    if data._revision_cache is not None:
        prev_rev, prev_raw = data._revision_cache
        if prev_raw == raw:
            return prev_rev
    rev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    data._revision_cache = (rev, raw)
    return rev


def _parse_iso_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, OSError, TypeError):
        return None


def _stale_threshold_seconds(fandango_poll: dict[str, Any]) -> int:
    """If last_success is older than this (vs now), flag as possibly stale."""
    try:
        mx = int(fandango_poll.get("max_seconds") or 330)
    except (TypeError, ValueError):
        mx = 330
    try:
        cap = int(fandango_poll.get("error_backoff_cap_seconds") or 1800)
    except (TypeError, ValueError):
        cap = 1800
    return mx * 3 + cap


def _target_route_label(name: str) -> str:
    n = name.lower()
    if "imax" in n and "70" in n:
        return "IMAX 70mm"
    if "overview" in n:
        return "Overview"
    return "Target"


def _artifact_basename(path_str: str | None) -> str | None:
    if not path_str:
        return None
    try:
        return Path(path_str).name
    except (OSError, TypeError, ValueError):
        return None


def _html_id_slug(s: str) -> str:
    """Safe fragment for use in id=; keeps alnum, dash, underscore."""
    out: list[str] = []
    for c in s:
        if c.isalnum() or c in ("_", "-"):
            out.append(c)
        elif c in " ./\\":
            out.append("-")
    t = "".join(out).strip("-")
    return t or "x"


def dashboard_css() -> str:
    """Apple-style dashboard stylesheet (single source of truth)."""
    return """    :root {
      --bg: #f5f5f7;
      --bg-elevated: #ffffff;
      --surface: rgba(255, 255, 255, 0.88);
      --surface2: #f2f2f7;
      --border: rgba(60, 60, 67, 0.16);
      --border-bright: rgba(255, 255, 255, 0.9);
      --text: #1d1d1f;
      --muted: #6e6e73;
      --accent: #007aff;
      --accent-dim: rgba(0, 122, 255, 0.1);
      --accent2: #5856d6;
      --violet: #5856d6;
      --ok: #248a3d;
      --warn: #b26a00;
      --bad: #d70015;
      --radius: 18px;
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.08);
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04), 0 10px 30px rgba(0, 0, 0, 0.05);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0b0b0f;
        --bg-elevated: #161618;
        --surface: rgba(28, 28, 30, 0.88);
        --surface2: #242426;
        --border: rgba(235, 235, 245, 0.12);
        --border-bright: rgba(235, 235, 245, 0.08);
        --text: #f5f5f7;
        --muted: #a1a1aa;
        --accent: #0a84ff;
        --accent-dim: rgba(10, 132, 255, 0.14);
        --accent2: #9897ff;
        --violet: #9897ff;
        --ok: #30d158;
        --warn: #ffd60a;
        --bad: #ff453a;
        --shadow: 0 22px 60px rgba(0, 0, 0, 0.32);
        --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.24), 0 16px 34px rgba(0, 0, 0, 0.22);
      }
    }
    * { box-sizing: border-box; }
    body {
      max-width: 1120px;
      margin-left: auto;
      margin-right: auto;
      padding: 0 1.35rem 2.75rem;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      color: var(--text);
      letter-spacing: -0.01em;
      line-height: 1.5;
      font-size: 0.95rem;
      min-height: 100vh;
      background:
        radial-gradient(circle at 50% -18rem, rgba(0, 122, 255, 0.08), transparent 34rem),
        var(--bg);
    }
    main.dash { display: flex; flex-direction: column; gap: 1rem; }
    header.dash-header {
      padding: 2.15rem 0 1.4rem;
      border-bottom: 0;
      position: relative;
    }
    header.dash-header::after { display: none; }
    .dash-kicker {
      font-size: 0.72rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--muted);
      margin: 0 0 0.4rem 0;
    }
    h1.dash-title {
      max-width: 12ch;
      margin: 0 0 0.85rem 0;
      color: var(--text);
      font-size: clamp(2rem, 5vw, 3.4rem);
      font-weight: 700;
      letter-spacing: -0.06em;
      line-height: 1.1;
    }
    header.dash-header p { margin: 0.28rem 0; font-size: 0.86rem; color: var(--muted); }
    header .hb-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      margin-top: 0.65rem;
      align-items: center;
    }
    .hb-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.78rem;
      font-weight: 600;
      padding: 0.36rem 0.72rem;
      border-radius: 999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      box-shadow: none;
    }
    .hb-pill .dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--ok);
      box-shadow: none;
    }
    .section-head {
      margin: 0;
      padding: 0.6rem 0 0;
    }
    .section-head .panel-tagline { max-width: 70ch; }
    .section-label {
      margin: 0 0 0.35rem;
      color: var(--muted);
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.11em;
    }
    .panel-tagline {
      margin: 0 0 0.9rem;
      color: var(--muted);
      font-size: 0.92rem;
    }
    .triage-panel,
    .runtime-panel,
    .intel-panel,
    .panel-fold,
    footer.dash-foot,
    .grid .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow-sm);
      backdrop-filter: blur(20px) saturate(1.08);
      -webkit-backdrop-filter: blur(20px) saturate(1.08);
    }
    .triage-panel,
    .runtime-panel,
    .intel-panel {
      padding: 1.15rem;
    }
    section.panel { margin: 0; }
    .panel-secondary { margin-top: 0.25rem; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 260px), 1fr));
      gap: 0.85rem;
    }
    .grid .card {
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.72rem;
      transition: border-color 0.18s ease, background-color 0.18s ease;
    }
    .grid .card:hover {
      transform: none;
      box-shadow: var(--shadow-sm);
      border-color: rgba(0, 122, 255, 0.28);
    }
    .card { border-radius: var(--radius); }
    .card h2 {
      margin: 0;
      color: var(--text);
      font-size: 1.08rem;
      font-weight: 650;
      letter-spacing: -0.035em;
    }
    .card p { margin: 0; }
    .card-stats { font-size: 0.82rem; color: var(--muted); margin: 0.15rem 0 0 0; }
    .card-stats .rel { font-size: 0.78rem; opacity: 0.88; font-weight: 450; }
    .card-topline {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      align-items: center;
    }
    .card-link a {
      font-size: 0.86rem;
      font-weight: 600;
    }
    .card-facts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.45rem;
      margin: 0;
    }
    .card-facts div {
      min-width: 0;
      padding: 0.55rem;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--surface2);
    }
    .card-facts dt {
      margin: 0 0 0.18rem;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .card-facts dd {
      margin: 0;
      min-width: 0;
      color: var(--text);
      font-size: 0.8rem;
      overflow-wrap: anywhere;
    }
    .pill {
      display: inline-block;
      padding: 0.22rem 0.56rem;
      border-radius: 999px;
      background: var(--surface2);
      color: var(--muted);
      border: 1px solid var(--border);
      font-size: 0.73rem;
      font-weight: 650;
    }
    .pill-ok { background: rgba(52, 199, 89, 0.14); color: var(--ok); border-color: transparent; }
    .pill-warn { background: rgba(255, 159, 10, 0.16); color: var(--warn); border-color: transparent; }
    .pill-muted { background: var(--surface2); color: var(--muted); }
    .card-kind { margin: 0 0 0.15rem 0; }
    a {
      color: var(--accent);
      text-underline-offset: 3px;
      text-decoration-color: rgba(0, 122, 255, 0.35);
      transition: color 0.15s;
    }
    a:hover { color: var(--accent); text-decoration-color: var(--accent); }
    .thumb img {
      max-width: 100%;
      height: auto;
      border-radius: 10px;
      border: 1px solid var(--border);
      box-shadow: 0 4px 20px rgba(0,0,0,0.2);
    }
    video { max-width: 100%; border-radius: 10px; background: #000; border: 1px solid var(--border); }
    details { color: var(--text); }
    summary {
      cursor: pointer;
      list-style: none;
      user-select: none;
      color: var(--accent);
      font-size: 0.86rem;
      font-weight: 650;
      padding: 0.35rem 0;
    }
    summary::-webkit-details-marker { display: none; }
    summary::before {
      content: "\\25B8";
      display: inline-block;
      margin-right: 0.4rem;
      transition: transform 0.15s ease;
      opacity: 0.75;
      font-size: 0.75rem;
      color: var(--muted);
    }
    details[open] > summary::before { transform: rotate(90deg); }
    .card-expand, .intel-expand { margin-top: 0.25rem; }
    .card-expand-body, .intel-expand-body {
      margin-top: 0.45rem;
      padding: 0.72rem 0 0 0.78rem;
      border-left: 2px solid var(--border);
      font-size: 0.88rem;
    }
    .card-err,
    .card-stale,
    .purchase-err,
    .sx-err {
      color: var(--warn);
      font-size: 0.8rem;
    }
    .card-media-meta { font-size: 0.76rem; color: var(--muted); margin: 0 0 0.35rem 0; }
    .runtime-panel { order: 10; }
    .intel-panel { order: 11; }
    #purchase { order: 12; }
    #x { order: 13; }
    #registry { order: 14; }
    .runtime-panel .panel-tagline { margin-bottom: 0.75rem; }
    .meta-grid,
    .triage-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
      gap: 0.62rem;
    }
    .meta-grid > div,
    .triage-grid > div,
    .intel-card {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 14px;
      box-shadow: none;
      min-width: 0;
      padding: 0.76rem;
      transition: border-color 0.2s;
    }
    .meta-grid > div:hover,
    .triage-grid > div:hover { border-color: rgba(0, 122, 255, 0.2); }
    .triage-grid strong,
    .meta-grid strong {
      display: block;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.2rem;
    }
    .triage-grid span,
    .meta-grid span {
      display: block;
      font-size: 0.9rem;
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .intel-panel .section-label { margin-top: 0; }
    .intel-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
      gap: 0.75rem;
      margin-top: 0.65rem;
    }
    .intel-card h3 {
      font-size: 0.95rem;
      margin: 0 0 0.25rem 0;
      color: var(--text);
      font-weight: 600;
    }
    .intel-headline {
      font-weight: 600;
      margin: 0 0 0.4rem 0;
      font-size: 0.9rem;
      color: var(--text);
    }
    p.qualifier { font-size: 0.78rem; opacity: 0.8; margin: 0.5rem 0 0 0; font-style: italic; color: var(--muted); }
    .pill-warn-inline {
      border-radius: 8px;
      display: inline-block;
      font-size: 0.82rem;
      padding: 0.35rem 0.55rem;
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.1);
      color: var(--warn);
    }
    .panel-fold {
      overflow: hidden;
    }
    .panel-fold > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.82rem 1rem;
      border-bottom: 1px solid transparent;
      font-size: 0.92rem;
      background: transparent;
    }
    .panel-fold[open] > summary { border-bottom-color: var(--border); }
    .fold-title { font-weight: 600; color: var(--text); }
    .fold-badge {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      background: var(--surface2);
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      border: 1px solid var(--border);
    }
    .fold-body { padding: 0.75rem 1rem 1rem; }
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 0.82rem;
    }
    th, td {
      border: 0;
      border-bottom: 1px solid var(--border);
      padding: 0.58rem 0.62rem;
      text-align: left;
    }
    th {
      background: transparent;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    table.data-table { min-width: 520px; }
    tr:nth-child(even) td { background: transparent; }
    .purchase-err { font-size: 0.78rem; }
    code {
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
      font-size: 0.74rem;
      word-break: break-all;
      color: var(--text);
      background: rgba(127, 127, 127, 0.12);
      padding: 0.1rem 0.35rem;
      border-radius: 7px;
      border: 1px solid transparent;
    }
    p.hint { font-size: 0.88rem; opacity: 0.9; margin: 0.5rem 0 0 0; color: var(--muted); }
    p.hint.meta { font-size: 0.78rem; opacity: 0.85; margin-bottom: 0.65rem; }
    footer.dash-foot {
      margin-top: 2.25rem;
      padding: 1.15rem 1.15rem 1.25rem;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      font-size: 0.82rem;
      color: var(--muted);
      box-shadow: var(--shadow-sm);
    }
    p.refresh-hint { margin: 0 0 0.65rem 0; font-size: 0.78rem; opacity: 0.92; }
    .skip-link {
      position: absolute; left: -9999px; z-index: 100;
      padding: 0.55rem 1rem;
      background: var(--text);
      color: var(--bg);
      font-weight: 700;
      border-radius: 8px;
      box-shadow: var(--shadow-sm);
    }
    .skip-link:focus { left: 1rem; top: 1rem; outline: 2px solid var(--accent); outline-offset: 3px; }
    a:focus-visible, summary:focus-visible, .skip-link:focus {
      outline: 2px solid var(--accent); outline-offset: 3px;
    }
    .jump-nav {
      position: sticky;
      top: 0.75rem;
      z-index: 20;
      padding: 0.45rem 0.7rem;
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 500;
      color: var(--muted);
      backdrop-filter: blur(14px) saturate(1.3);
      -webkit-backdrop-filter: blur(14px) saturate(1.3);
      box-shadow: var(--shadow-sm);
      background: rgba(255, 255, 255, 0.72);
    }
    .jump-nav-list {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: center;
      row-gap: 0.4rem;
      column-gap: 0.2rem;
    }
    .jump-nav-list a { white-space: nowrap; opacity: 0.92; }
    .jump-nav-list a:not(:last-child)::after {
      content: "\\00B7";
      display: inline-block;
      margin-left: 0.4rem;
      color: var(--muted);
      opacity: 0.5;
      font-weight: 400;
      pointer-events: none;
      user-select: none;
    }
    .jump-nav a:hover { opacity: 1; }
    .triage-attention {
      margin-top: 0.75rem;
      padding: 0.85rem 0.95rem;
      border-radius: 15px;
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.08);
    }
    .triage-attention .section-label { margin-top: 0; }
    ul.attention-list { margin: 0.35rem 0 0 1rem; padding: 0; color: var(--text); font-size: 0.9rem; }
    ul.attention-list li { margin: 0.25rem 0; }
    .subhead-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 1rem;
      margin: 0.9rem 0 0.35rem;
    }
    .subhead-row .section-label { margin: 0; }
    .subhead-row a { font-size: 0.8rem; font-weight: 650; }
    .triage-priority .hint.meta { margin-top: 0.25rem; }
    .triage-table-wrap { margin-top: 0.35rem; }
    table.triage-table { min-width: 640px; font-size: 0.8rem; }
    .triage-table td { vertical-align: top; }
    .triage-pill {
      display: inline-block;
      font-size: 0.64rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.18rem 0.48rem;
      border-radius: 999px;
      white-space: nowrap;
    }
    tr.triage-tier-0 td { background: rgba(255, 69, 58, 0.08); }
    tr.triage-tier-1 td { background: rgba(255, 159, 10, 0.08); }
    tr.triage-tier-2 td { background: rgba(52, 199, 89, 0.08); }
    .triage-pill-0 { background: rgba(255, 69, 58, 0.14); color: var(--bad); }
    .triage-pill-1 { background: rgba(255, 159, 10, 0.16); color: var(--warn); }
    .triage-pill-2 { background: rgba(52, 199, 89, 0.14); color: var(--ok); }
    .triage-pill-3 { background: var(--surface2); color: var(--muted); }
    .movie-group { margin: 0; }
    .movie-group-title {
      margin: 0 0 0.65rem;
      color: var(--text);
      font-size: 1rem;
      font-weight: 600;
      letter-spacing: -0.025em;
    }
    .panel-warn {
      border-radius: 12px;
      border-left: 3px solid rgba(255, 159, 10, 0.45);
      padding: 0.72rem 0.85rem;
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.08);
    }
    .conn-line { font-size: 0.8rem; margin: 0.5rem 0 0 0; }
    .conn-label { color: var(--muted); }
    .conn-ok { color: var(--ok); }
    .conn-bad { color: var(--bad); }
    .conn-static { color: var(--muted); }
    .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .sx-snapshot { margin: 0.35rem 0 0.85rem; }
    .sx-tweet-preview-cell {
      max-width: 36ch;
      font-size: 0.82rem;
      color: var(--text);
      line-height: 1.45;
      vertical-align: top;
      word-break: break-word;
    }
    .sx-preview-missing { color: var(--muted); }
    .sx-cards {
      display: flex;
      flex-direction: column;
      gap: 0.7rem;
      margin-top: 0.65rem;
    }
    .sx-card {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
      box-shadow: none;
    }
    .sx-handle {
      margin: 0 0 0.35rem 0;
      font-size: 1.05rem;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.02em;
    }
    .sx-meta, .sx-tweet-idline, .sx-tweet-when {
      font-size: 0.78rem;
      color: var(--muted);
      margin: 0.2rem 0;
    }
    code.tweet-snowflake {
      font-size: 0.85rem;
      letter-spacing: 0.02em;
      word-break: break-all;
    }
    .sx-tweet-body {
      margin: 0.65rem 0 0 0;
      padding: 0.85rem 1rem;
      border-left: 3px solid var(--accent);
      background: rgba(0, 122, 255, 0.08);
      border-radius: 0 12px 12px 0;
      font-size: 0.9rem;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .sx-tweet-body em.sx-no-text { color: var(--muted); font-style: italic; }
    .sx-err { margin: 0.5rem 0 0 0; }
    .inline-fold {
      margin-top: 0.65rem;
      border-top: 1px solid var(--border);
      padding-top: 0.55rem;
    }
    @media (prefers-color-scheme: dark) {
      .jump-nav { background: rgba(28, 28, 30, 0.72); }
    }
    @media (max-width: 700px) {
      table.data-table { font-size: 0.78rem; }
      body { padding-inline: 0.85rem; }
      header.dash-header { padding-top: 1.45rem; }
      h1.dash-title { font-size: 2.15rem; }
      .triage-panel,
      .runtime-panel,
      .intel-panel,
      .grid .card,
      footer.dash-foot { border-radius: 16px; }
      .card-facts { grid-template-columns: 1fr; }
      .jump-nav {
        border-radius: 16px;
        font-size: 0.76rem;
        line-height: 1.5;
        padding: 0.5rem 0.7rem;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      summary::before { transition: none !important; }
      .grid .card { transition: none !important; }
      .grid .card:hover { transform: none !important; }
    }
"""


def not_found_css() -> str:
    """404 page tokens (minimal; unrelated to dashboard component classes)."""
    return """    :root {
      --bg: #f5f5f7;
      --surface: rgba(255, 255, 255, 0.88);
      --surface2: #f2f2f7;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --border: rgba(60, 60, 67, 0.16);
      --accent: #007aff;
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.08);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0b0b0f;
        --surface: rgba(28, 28, 30, 0.88);
        --surface2: #242426;
        --text: #f5f5f7;
        --muted: #a1a1aa;
        --border: rgba(235, 235, 245, 0.12);
        --accent: #0a84ff;
        --shadow: 0 22px 60px rgba(0, 0, 0, 0.32);
      }
    }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      color: var(--text);
      margin: 0;
      padding: 2.5rem 1.25rem 3rem;
      max-width: 560px;
      margin-left: auto;
      margin-right: auto;
      line-height: 1.55;
      font-size: 0.95rem;
      min-height: 100vh;
      background:
        radial-gradient(circle at 50% -12rem, rgba(0, 122, 255, 0.08), transparent 28rem),
        var(--bg);
    }
    p.kicker {
      font-size: 0.68rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.2em;
      color: var(--muted);
      margin: 0 0 0.4rem 0;
    }
    h1 {
      font-size: clamp(1.35rem, 3.5vw, 1.7rem);
      font-weight: 700;
      letter-spacing: -0.03em;
      margin: 0 0 1rem 0;
      line-height: 1.2;
      color: var(--text);
    }
    p { color: var(--muted); margin: 0.65rem 0; }
    a {
      color: var(--accent);
      text-underline-offset: 3px;
      text-decoration-color: rgba(0, 122, 255, 0.35);
    }
    a:hover { color: var(--accent); text-decoration-color: var(--accent); }
    code {
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Consolas, monospace;
      font-size: 0.85rem;
      color: var(--text);
      background: rgba(127, 127, 127, 0.12);
      padding: 0.15rem 0.4rem;
      border-radius: 7px;
      border: 1px solid transparent;
    }
    .card {
      margin-top: 1.5rem;
      padding: 1.1rem 1.15rem 1.2rem;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: var(--surface);
      box-shadow: var(--shadow);
    }
    .card p { color: var(--text); font-size: 0.9rem; margin: 0; }
    .card .links {
      display: flex;
      flex-wrap: wrap;
      gap: 0.3rem 0.55rem;
      margin-top: 0.5rem;
    }
    .card a:not(:last-child)::after {
      content: "·";
      margin-left: 0.45rem;
      color: var(--muted);
      opacity: 0.5;
      pointer-events: none;
    }
"""


def _dash_esc_attr(attrs: dict[str, str]) -> str:
    parts: list[str] = []
    for k_raw, v in attrs.items():
        k = str(k_raw).strip().lower().replace("_", "-")
        if not k:
            continue
        ve = html.escape(v, quote=True)
        parts.append(f' {html.escape(k)}="{ve}"')
    return "".join(parts)


def render_panel(
    inner: str,
    *,
    css_classes: tuple[str, ...] | None = None,
    section_id: str | None = None,
    aria_label: str | None = None,
) -> str:
    """Standard section/card shell (escaped attributes; ``inner`` is trusted HTML fragments)."""
    cls = "panel"
    extra = css_classes or ()
    css = " ".join([cls] + list(extra)).strip()
    attrs: dict[str, str] = {"class": css}
    if section_id:
        attrs["id"] = section_id
    if aria_label is not None:
        attrs["aria-label"] = aria_label
    return f"<section{_dash_esc_attr(attrs)}>{inner}</section>"


def render_fold_panel(
    inner: str,
    *,
    fold_id: str | None,
    summary_html: str,
    open_: bool,
) -> str:
    """`<details class="panel panel-fold">` wrapper (summary/content are trusted HTML)."""
    open_attr = " open" if open_ else ""
    id_attr = f' id="{html.escape(str(fold_id), quote=True)}"' if fold_id else ""
    return (
        f'<details class="panel panel-fold"{id_attr}{open_attr}>'
        f"<summary>{summary_html}</summary>"
        f'<div class="fold-body">{inner}</div>'
        "</details>"
    )


def render_metric_grid(metrics_html: str, *, css_class: str | None = None) -> str:
    cls = html.escape(css_class.strip(), quote=True) if css_class else "meta-grid"
    return f'<div class="{cls}">{metrics_html}</div>'


def render_status_pill(
    text_esc: str,
    *,
    variants: tuple[str, ...] = (),
    title_esc: str | None = None,
) -> str:
    parts = ["pill"] + list(variants)
    cls_esc = html.escape(
        " ".join(p.strip() for p in parts if p.strip()),
        quote=True,
    )
    title_part = ""
    if title_esc is not None:
        title_part = f' title="{html.escape(title_esc, quote=True)}"'
    return f'<span class="{cls_esc}"{title_part}>{text_esc}</span>'


def render_data_table(
    *,
    thead_row: str | None,
    tbody_rows_html: str,
    table_classes: tuple[str, ...],
    caption: str | None = None,
    caption_class: str = "visually-hidden",
    wrapper_class: str = "table-wrap",
    outer_prefix: str = "",
    outer_suffix: str = "",
) -> str:
    """Escaped ``caption``, trusted ``thead_row`` / ``tbody_rows_html`` fragments."""
    cblock = ""
    if caption:
        ce = html.escape(caption)
        cc_esc = html.escape(caption_class, quote=True)
        cblock = f'<caption class="{cc_esc}">{ce}</caption>'
    thead = ""
    if thead_row:
        thead = f"<thead><tr>{thead_row}</tr></thead>"
    tc = html.escape(" ".join(table_classes).strip(), quote=True)
    we = html.escape(wrapper_class.strip(), quote=True)
    return (
        f"{outer_prefix}"
        f'<div class="{we}"><table class="{tc}">{cblock}{thead}'
        f"<tbody>{tbody_rows_html}</tbody></table></div>"
        f"{outer_suffix}"
    )


def _jump_nav_html(
    anchors: Iterable[tuple[str, str]],
    *,
    aria_label: str = "On this page",
) -> str:
    """``anchors``: (href, label_plain) pairs; emits escaped links."""
    lis: list[str] = []
    for href_raw, lbl in anchors:
        href_esc = html.escape(href_raw, quote=True)
        lbl_esc = html.escape(lbl)
        lis.append(f'<a href="{href_esc}">{lbl_esc}</a>')
    ale = html.escape(aria_label, quote=True)
    return (
        f'<nav class="jump-nav" aria-label="{ale}">'
        f'<div class="jump-nav-list">{"".join(lis)}</div></nav>'
    )


def render_inline_disclosure(
    *,
    css_class: str,
    summary_html: str,
    inner_html: str,
    open_: bool = False,
) -> str:
    open_attr = " open" if open_ else ""
    ce = html.escape(css_class.strip(), quote=True)
    return (
        f'<details class="{ce}"{open_attr}>'
        f"<summary>{summary_html}</summary>{inner_html}</details>"
    )


def render_fact_grid(entries: list[tuple[str, str]]) -> str:
    """Facts grid whose ``entries`` contain pre-escaped fragments for dt/dd."""
    cols = max(1, min(3, len(entries)))
    inner = [f"<div><dt>{dt_esc}</dt><dd>{dd_esc}</dd></div>" for dt_esc, dd_esc in entries]
    col_style = html.escape(f"repeat({cols}, minmax(0, 1fr))", quote=True)
    return (
        f'<dl class="card-facts" style="grid-template-columns: {col_style}">' + "".join(inner) + "</dl>"
    )


def _render_target_card(
    t: dict[str, Any],
    *,
    fandango_poll: dict[str, Any],
    now: datetime,
) -> str:
    raw_name = str(t.get("name", ""))
    name = html.escape(raw_name)
    name_attr = html.escape(raw_name, quote=True)
    url = str(t.get("url") or "")
    url_e = html.escape(url, quote=True)
    st = t.get("state") or {}
    if not isinstance(st, dict):
        st = {}
    schema = html.escape(str(st.get("last_release_schema") or "—"))
    cur = html.escape(str(st.get("current_state") or "—"))
    tticks = html.escape(str(st.get("total_ticks", "—")))
    su_raw = st.get("last_success_at")
    su = html.escape(str(su_raw or "—"))
    rel = _relative_ago(str(su_raw) if su_raw is not None else None, now=now)
    rel_html = f' <span class="rel">({html.escape(rel)})</span>' if rel else ""

    err_at = st.get("last_error_at")
    err_msg = st.get("last_error_message")
    err_bits: list[str] = []
    if err_at:
        err_bits.append(f"last_error_at {html.escape(str(err_at))}")
    if err_msg:
        em = str(err_msg).replace("\n", " ").strip()
        if len(em) > 200:
            em = em[:197] + "…"
        err_bits.append(html.escape(em))
    err_html = ""
    if err_bits:
        err_html = f'<p class="card-err">{" · ".join(err_bits)}</p>'

    te = st.get("total_errors")
    ce = st.get("consecutive_errors")
    err_meta = ""
    if te is not None or ce is not None:
        err_meta = (
            f'<p class="card-stats"><strong>total_errors</strong> '
            f'{html.escape(str(te if te is not None else "—"))} · '
            f'<strong>consecutive_errors</strong> '
            f"{html.escape(str(ce if ce is not None else 0))}</p>"
        )

    stale_thr = _stale_threshold_seconds(fandango_poll)
    su_dt = _parse_iso_dt(str(su_raw) if su_raw is not None else None)
    stale_html = ""
    stale_chip = ""
    if su_dt is not None:
        age = int((now.astimezone(UTC) - su_dt.astimezone(UTC)).total_seconds())
        if age > stale_thr:
            stale_chip = render_status_pill(
                html.escape("stale"),
                variants=("pill-warn",),
            )
            stale_html = (
                f'<p class="card-stale"><strong>Stale crawl</strong> · no successful crawl '
                f"in ~{html.escape(_fmt_duration(age))}. Expected under normal polling: "
                f"≤ ~{html.escape(_fmt_duration(stale_thr))}.</p>"
            )

    pill_variants: tuple[str, ...] = ()
    cur_l = str(st.get("current_state") or "").lower()
    schema_l = str(st.get("last_release_schema") or "").lower()
    if cur_l == "error" or (st.get("consecutive_errors") or 0) > 0:
        pill_variants = ("pill-warn",)
    elif "alert" in cur_l or "purchas" in cur_l or "released" in cur_l or "live" in cur_l:
        pill_variants = ("pill-ok",)
    elif "partial" in schema_l or "full" in schema_l:
        pill_variants = ("pill-ok",)
    state_pill = render_status_pill(cur, variants=pill_variants)

    route_lbl = html.escape(_target_route_label(raw_name))
    route_pill = render_status_pill(route_lbl, variants=("pill-muted",))

    shot_base = _artifact_basename(
        t.get("latest_screenshot") if isinstance(t.get("latest_screenshot"), str) else None
    )
    vid_base = _artifact_basename(
        t.get("latest_video") if isinstance(t.get("latest_video"), str) else None
    )
    tr_base = _artifact_basename(
        t.get("latest_trace") if isinstance(t.get("latest_trace"), str) else None
    )
    media_meta: list[str] = []
    if shot_base:
        media_meta.append(f"screenshot <code>{html.escape(shot_base)}</code>")
    if vid_base:
        media_meta.append(f"video <code>{html.escape(vid_base)}</code>")
    if tr_base:
        media_meta.append(f"trace <code>{html.escape(tr_base)}</code>")
    media_meta_p = (
        f'<p class="card-media-meta">{" · ".join(media_meta)}</p>' if media_meta else ""
    )

    img_html = ""
    su_url = t.get("latest_screenshot_url")
    if su_url:
        img_html = (
            f'<p class="thumb"><img loading="lazy" src="{html.escape(su_url)}" '
            f'alt="screenshot {name}" /></p>'
        )

    vid_html = ""
    vu = t.get("latest_video_url")
    if vu:
        vid_html = (
            f'<p class="vid"><video controls preload="metadata" title="crawl video {name}" '
            f'src="{html.escape(vu)}"></video></p>'
        )

    trace_html = ""
    tz = t.get("latest_trace_url")
    if tz:
        trace_html = f'<p><a href="{html.escape(tz)}">latest trace (.zip)</a></p>'

    media_inner = f"{media_meta_p}{img_html}{vid_html}{trace_html}"
    direct_api_raw = t.get("direct_api")
    direct_api: dict[str, Any] = direct_api_raw if isinstance(direct_api_raw, dict) else {}
    api_status = str(direct_api.get("status") or st.get("direct_api_last_status") or "—")
    api_dates = direct_api.get("inspected_dates") or st.get("direct_api_last_inspected_dates") or []
    api_formats = direct_api.get("formats_seen") or st.get("direct_api_last_formats_seen") or []
    api_unknown = direct_api.get("unknown_formats") or st.get("direct_api_last_unknown_formats") or []
    api_fallbacks = direct_api.get("fallback_count") or st.get("direct_api_fallback_count") or 0
    api_warning = direct_api.get("last_drift_warning") or st.get("direct_api_last_drift_warning")
    api_bits = [
        f"status <code>{html.escape(api_status)}</code>",
        f"dates <code>{html.escape(str(len(api_dates)))}</code>",
        f"fallbacks <code>{html.escape(str(api_fallbacks))}</code>",
    ]
    if api_formats:
        api_bits.append(
            "formats <code>"
            + html.escape(", ".join(str(x) for x in api_formats[:8]))
            + ("…" if len(api_formats) > 8 else "")
            + "</code>"
        )
    if api_unknown:
        api_bits.append(
            "unknown <code>"
            + html.escape(", ".join(str(x) for x in api_unknown))
            + "</code>"
        )
    if api_warning:
        api_bits.append("warning " + html.escape(str(api_warning)))
    api_html = f'<p class="card-api-meta"><strong>Direct API</strong> · {" · ".join(api_bits)}</p>'
    details_inner = f"{err_meta}{err_html}{stale_html}{api_html}{media_inner}"
    details_block = ""
    if details_inner.strip():
        details_block = render_inline_disclosure(
            css_class="card-expand",
            summary_html="Diagnostics &amp; media",
            inner_html=f'<div class="card-expand-body">{details_inner}</div>',
        )

    facts = render_fact_grid(
        [
            (html.escape("Schema"), f"<code>{schema}</code>"),
            (html.escape("Last OK"), f"{su}{rel_html}"),
            (html.escape("Ticks"), tticks),
            (html.escape("Direct API"), f"<code>{html.escape(api_status)}</code>"),
        ]
    )

    return f"""
<section class="card" data-target="{name_attr}">
  <div class="card-topline">
    {route_pill}
    {state_pill}
    {stale_chip}
  </div>
  <h2>{name}</h2>
  <p class="card-link"><a href="{url_e}" target="_blank" rel="noopener">Open on Fandango</a></p>
  {facts}
  {details_block}
</section>
"""


def _triage_tier(st: dict[str, Any], *, now: datetime, stale_threshold_sec: int) -> int:
    """0 = error streak, 1 = stale last OK, 2 = on-sale / alerted signal, 3 = routine."""
    cur_l = str(st.get("current_state") or "").lower()
    try:
        ce = int(st.get("consecutive_errors") or 0)
    except (TypeError, ValueError):
        ce = 0
    if cur_l == "error" or ce > 0:
        return 0
    su = st.get("last_success_at")
    su_dt = _parse_iso_dt(str(su) if su is not None else None)
    if su_dt is not None:
        age = int((now.astimezone(UTC) - su_dt.astimezone(UTC)).total_seconds())
        if age > stale_threshold_sec:
            return 1
    sch = str(st.get("last_release_schema") or "").lower()
    if "partial" in sch or "full" in sch:
        return 2
    if "alert" in cur_l or "purchas" in cur_l:
        return 2
    return 3


def _render_triage_priority_table(
    targets: list[dict[str, Any]],
    *,
    now: datetime,
    stale_threshold_sec: int,
) -> str:
    """Compact table: most urgent targets first (errors → stale → on-sale → routine)."""
    if not targets:
        return """<div class="triage-priority">
<p class="section-label" style="margin:0.75rem 0 0.35rem 0">Target priority</p>
<p class="hint meta" style="margin-top:0">No targets configured — add <code>targets:</code> in <code>config.yaml</code>.</p>
</div>
"""
    rows_out: list[tuple[int, str, str]] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        st = t.get("state") or {}
        if not isinstance(st, dict):
            st = {}
        tier = _triage_tier(st, now=now, stale_threshold_sec=stale_threshold_sec)
        name = html.escape(str(t.get("name") or "—"))
        url = str(t.get("url") or "")
        url_e = html.escape(url, quote=True) if url else ""
        cur = html.escape(str(st.get("current_state") or "—"))
        sch = html.escape(str(st.get("last_release_schema") or "—"))
        su_raw = st.get("last_success_at")
        su_rel = _relative_ago(
            str(su_raw) if su_raw is not None else None,
            now=now,
        )
        last_ok = html.escape(su_rel or "—")
        try:
            ce = int(st.get("consecutive_errors") or 0)
        except (TypeError, ValueError):
            ce = 0
        link_cell = (
            f'<a href="{url_e}" target="_blank" rel="noopener">Open</a>'
            if url_e
            else "—"
        )
        tier_label = ("Error / streak", "Stale crawl", "On-sale signal", "Routine")[
            min(tier, 3)
        ]
        row = (
            f"<tr class=\"triage-tier-{tier}\">"
            f"<td><span class=\"triage-pill triage-pill-{tier}\">"
            f"{html.escape(tier_label)}</span></td>"
            f"<td><strong>{name}</strong></td>"
            f"<td>{cur}</td>"
            f"<td><code>{sch}</code></td>"
            f"<td>{last_ok}</td>"
            f"<td>{ce}</td>"
            f"<td>{link_cell}</td>"
            "</tr>"
        )
        rows_out.append((tier, str(t.get("name") or ""), row))

    rows_out.sort(key=lambda x: (x[0], x[1].lower()))
    body = "".join(r[2] for r in rows_out)

    thead = "".join(
        f'<th scope="col">{html.escape(col)}</th>'
        for col in (
            "Priority",
            "Target",
            "State",
            "Schema",
            "Last OK",
            "CE",
            "Fandango",
        )
    )
    tbl = render_data_table(
        thead_row=thead,
        tbody_rows_html=body,
        table_classes=("data-table", "triage-table"),
        caption="Target priority ranking",
        wrapper_class="triage-table-wrap",
    )

    extra = ""
    sub = (
        '<div class="subhead-row"><p class="section-label">Target priority</p>'
        '<a href="#crawl">Full cards</a></div>'
        f"{tbl}"
    )
    return f'<div class="triage-priority">{extra}{sub}</div>'


def _render_triage_panel(
    *,
    targets: list[dict[str, Any]],
    movies: list[Any],
    release_intel: dict[str, Any],
    runtime: dict[str, Any],
    fandango_poll: dict[str, Any],
    now: datetime,
) -> str:
    n_targets = len(targets)
    n_shots = sum(
        1
        for t in targets
        if isinstance(t.get("latest_screenshot_url"), str) and t.get("latest_screenshot_url")
    )
    n_movies = sum(1 for m in movies if isinstance(m, dict))
    ri_status = (release_intel or {}).get("status")
    pur_mode = str(runtime.get("purchase_mode") or "—")
    pur_en = bool(runtime.get("purchase_enabled", True))

    alerted = 0
    watching = 0
    errish = 0
    stale_n = 0
    good_schema = 0
    thr = _stale_threshold_seconds(fandango_poll)
    for t in targets:
        st = t.get("state") or {}
        if not isinstance(st, dict):
            st = {}
        cur_l = str(st.get("current_state") or "").lower()
        if "alert" in cur_l or "purchas" in cur_l:
            alerted += 1
        elif cur_l == "watching" or cur_l == "idle":
            watching += 1
        sch = str(st.get("last_release_schema") or "").lower()
        if "partial" in sch or "full" in sch:
            good_schema += 1
        if cur_l == "error" or (st.get("consecutive_errors") or 0) > 0:
            errish += 1
        su = st.get("last_success_at")
        su_dt = _parse_iso_dt(str(su) if su is not None else None)
        if su_dt is not None:
            if int((now.astimezone(UTC) - su_dt.astimezone(UTC)).total_seconds()) > thr:
                stale_n += 1

    attention: list[str] = []
    if n_targets == 0:
        attention.append(
            "<strong>No Fandango targets</strong> in <code>config.yaml</code> — add <code>targets:</code> entries."
        )
    if ri_status == "unconfigured":
        attention.append(
            "<strong>Release intel</strong> is not configured (set an xAI key in <code>.env</code> for Grok summaries)."
        )
    if pur_en and pur_mode == "notify_only":
        attention.append(
            f"Purchase tier is <code>{html.escape(pur_mode)}</code> — no scripted checkout until you calibrate invariants."
        )
    if stale_n > 0:
        attention.append(
            f"<strong>{stale_n}</strong> target(s) have a stale <code>last_success_at</code> vs expected poll cadence — inspect errors or process health."
        )
    if errish > 0:
        attention.append(
            f"<strong>{errish}</strong> target(s) show error state or a consecutive error streak."
        )

    att_html = (
        "<ul class=\"attention-list\">"
        + "".join(f"<li>{a}</li>" for a in attention)
        + "</ul>"
        if attention
        else '<p class="hint meta">No extra attention flags. Fandango targets look routine.</p>'
    )

    priority_table = _render_triage_priority_table(
        [x for x in targets if isinstance(x, dict)],
        now=now,
        stale_threshold_sec=thr,
    )

    metrics_html = "".join(
        (
            "<div><strong>Targets</strong>"
            f"<span>{n_targets} configured · {n_shots} with screenshot</span></div>",
            "<div><strong>Ticket signals</strong>"
            f"<span>{alerted} alerted · {good_schema} with release schema</span></div>",
            "<div><strong>Health</strong>"
            "<span>"
            f"{html.escape(str(errish))} error streak · {html.escape(str(stale_n))} stale beyond "
            f"~{html.escape(_fmt_duration(thr))}"
            "</span></div>",
            "<div><strong>Registry</strong>"
            f"<span>{n_movies} movies · {watching} watching/idle</span></div>",
        )
    )

    glance = render_metric_grid(metrics_html, css_class="triage-grid")

    inner = f"""
<h2 class="section-label">At a glance</h2>
<p class="panel-tagline">The shortest path to what needs action.</p>
{glance}
<div class="triage-attention">
<p class="section-label">Needs attention</p>
{att_html}
</div>
{priority_table}
"""
    return render_panel(
        inner,
        css_classes=("triage-panel",),
        section_id="triage",
        aria_label="At a glance",
    )


def _render_release_intel_panel(
    movies: list[Any], release_intel: dict[str, Any]
) -> str:
    """HTML for xAI-backed release summaries (one sub-card per movie)."""
    if not release_intel:
        inner = (
            "<h2 class=\"section-label\">Release intel</h2>"
            '<p class="panel-tagline">xAI Grok</p>'
            "<p class=\"hint\">The release-intel payload is empty. If you expected Grok "
            "summaries, check <code>release_intel</code> in <code>config.yaml</code> and "
            "API keys; otherwise this panel may appear while the cache is warming up.</p>"
        )
        return render_panel(
            inner,
            css_classes=("intel-panel",),
            section_id="release-intel",
        )
    status = release_intel.get("status")
    if status == "disabled":
        inner = (
            "<h2 class=\"section-label\">Release intel</h2>"
            '<p class="panel-tagline">xAI Grok</p>'
            f'<p class="hint">{html.escape(str(release_intel.get("reason") or "disabled"))}</p>'
        )
        return render_panel(
            inner,
            css_classes=("intel-panel",),
            section_id="release-intel",
        )
    if status == "unconfigured":
        inner = (
            "<h2 class=\"section-label\">Release intel</h2>"
            '<p class="panel-tagline">xAI Grok · not configured</p>'
            '<p class="hint">Set <code>XAI_API_KEY</code> (or <code>GROK_API_KEY</code>) in '
            "<code>.env</code> with a key from <a href=\"https://console.x.ai\" "
            'target="_blank" rel="noopener">console.x.ai</a> — OpenAI keys do not work '
            "on api.x.ai. Summaries are advisory; Fandango crawl state below is the "
            "source of truth for on-sale detection.</p>"
        )
        return render_panel(
            inner,
            css_classes=("intel-panel",),
            section_id="release-intel",
        )

    meta_parts: list[str] = []
    if release_intel.get("updated_at"):
        meta_parts.append(f"updated {html.escape(str(release_intel['updated_at']))}")
    if release_intel.get("model"):
        meta_parts.append(f"model {html.escape(str(release_intel['model']))}")
    src = release_intel.get("source")
    if src:
        meta_parts.append(html.escape(str(src)))
    if release_intel.get("cache_age_seconds") is not None:
        meta_parts.append(
            f"cache age {int(release_intel['cache_age_seconds'])}s"
        )
    meta_line = " · ".join(meta_parts) if meta_parts else ""

    err = release_intel.get("error")
    err_html = ""
    if err:
        err_html = (
            f'<p class="hint pill-warn-inline">{html.escape(str(err))}</p>'
        )

    intel_map = release_intel.get("movies")
    if not isinstance(intel_map, dict):
        intel_map = {}

    blocks: list[str] = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or "")
        title = html.escape(str(m.get("title") or key))
        raw = intel_map.get(key)
        if not isinstance(raw, dict):
            raw = {}
        headline = html.escape(str(raw.get("headline") or "—"))
        summary = html.escape(str(raw.get("summary") or "—"))
        ticketing = html.escape(str(raw.get("ticketing") or "—"))
        notable = raw.get("notable_dates")
        notable_e = html.escape(str(notable)) if notable else ""
        qual = html.escape(str(raw.get("qualifier") or ""))

        nd_line = ""
        if notable_e:
            nd_line = f"<p><strong>Notable dates</strong>: {notable_e}</p>"

        expand_inner = (
            f"<p>{summary}</p>"
            f"<p><strong>Ticketing</strong>: {ticketing}</p>"
            f"{nd_line}"
            f'<p class="qualifier">{qual}</p>'
        )
        disclosure = render_inline_disclosure(
            css_class="intel-expand",
            summary_html="Summary, ticketing &amp; notes",
            inner_html=f'<div class="intel-expand-body">{expand_inner}</div>',
        )

        blocks.append(
            f"""
<article class="intel-card">
  <h3>{title}</h3>
  <p class="intel-headline">{headline}</p>
  {disclosure}
</article>
"""
        )

    body = "".join(blocks) if blocks else "<p class=\"hint\">No movies in registry.</p>"
    inner = f"""
<h2 class="section-label">Release intel</h2>
<p class="panel-tagline">xAI Grok · advisory context (Fandango crawl is authoritative)</p>
<p class="hint meta">{meta_line}</p>
{err_html}
<div class="intel-grid">{body}</div>
"""
    return render_panel(
        inner,
        css_classes=("intel-panel",),
        section_id="release-intel",
    )


def _render_purchases_panel(
    rows: list[dict[str, Any]],
    *,
    file_path: str,
    purchase_enabled: bool = True,
    purchase_mode: str = "—",
) -> str:
    """Collapsible table of recent purchase attempts from ``purchases.jsonl``."""
    if not rows:
        pe = "enabled" if purchase_enabled else "disabled in config"
        inner = (
            f"<p class=\"hint meta\">Purchase tier: <code>{html.escape(purchase_mode)}</code> "
            f"({html.escape(pe)}). "
            "No purchase attempts are logged to <code>state/purchases.jsonl</code> until "
            "the scripted purchaser runs (or a prior run wrote no rows).</p>"
            "<p class=\"hint\">No rows in <code>"
            f"{html.escape(file_path)}</code> yet.</p>"
        )
        fold = render_fold_panel(
            inner,
            fold_id=None,
            summary_html=(
                '<span class="fold-title">Purchase history</span>'
                '<span class="fold-badge">0 lines</span>'
            ),
            open_=True,
        )
        return render_panel(fold, css_classes=("panel-secondary",), section_id="purchase")
    pr_rows: list[str] = []
    for row in reversed(rows):
        at = html.escape(str(row.get("at") or "—"))
        tgt = html.escape(str(row.get("target") or "—"))
        att = row.get("attempt")
        oc = "—"
        err = ""
        if isinstance(att, dict):
            oc = html.escape(str(att.get("outcome") or "—"))
            e_raw = att.get("error")
            if e_raw:
                es = str(e_raw).replace("\n", " ").strip()
                if len(es) > 120:
                    es = es[:117] + "…"
                err = html.escape(es)
        err_cell = f'<span class="purchase-err">{err}</span>' if err else "—"
        pr_rows.append(
            f"<tr><td>{at}</td><td>{tgt}</td><td>{oc}</td><td>{err_cell}</td></tr>"
        )
    body = "".join(pr_rows)
    n = len(rows)
    pe_label = "enabled" if purchase_enabled else "disabled"
    ph_meta = (
        f"Tail of <code>{html.escape(file_path)}</code> (newest first). "
        f"Purchase: <code>{html.escape(purchase_mode)}</code> ({pe_label})."
    )
    thead = "".join(
        f'<th scope="col">{html.escape(col)}</th>'
        for col in ("at (UTC)", "target", "outcome", "error")
    )
    tbl = render_data_table(
        thead_row=thead,
        tbody_rows_html=body,
        table_classes=("data-table",),
        caption=None,
        wrapper_class="table-wrap",
    )
    inner = f'<p class="hint meta">{ph_meta}</p>{tbl}'
    fold = render_fold_panel(
        inner,
        fold_id=None,
        summary_html=(
            '<span class="fold-title">Purchase history</span>'
            f'<span class="fold-badge">{n} lines</span>'
        ),
        open_=True,
    )
    return render_panel(fold, css_classes=("panel-secondary",), section_id="purchase")


def render_dashboard_not_found_html(*, request_path: str) -> str:
    """Branded HTML for unknown routes when the dashboard server is enabled."""
    esc = html.escape(request_path)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="color-scheme" content="light dark" />
  <title>Not found — fandango-watcher</title>
  <style>
{not_found_css()}
  </style>
</head>
<body>
  <p class="kicker">fandango-watcher</p>
  <h1>404 — not found</h1>
  <p>No route for <code>{esc}</code>.</p>
  <div class="card" aria-label="Useful links">
    <p>Try the dashboard and APIs:</p>
    <p class="links">
      <a href="/">Dashboard</a>
      <a href="/api/status">/api/status</a>
      <a href="/api/revision">/api/revision</a>
      <a href="/healthz">/healthz</a>
      <a href="/metrics">/metrics</a>
    </p>
  </div>
</body>
</html>
"""


def render_index_html(
    snapshot: dict[str, Any],
    *,
    refresh_seconds: int = 10,
    live_revision: str | None = None,
) -> str:
    """Single-page HTML with a stylesheet from :func:`dashboard_css`.

    When ``live_revision`` is set and ``refresh_seconds`` > 0, injects a small
    script that polls ``/api/revision`` and reloads when the fingerprint changes
    (same browser tab). ``<noscript>`` still uses meta refresh as a fallback.
    When ``live_revision`` is omitted, uses only meta refresh (legacy/tests).
    """
    healthz = snapshot.get("healthz") or {}
    targets = snapshot.get("targets") or []
    social_x = snapshot.get("social_x") or {}
    movies = snapshot.get("movies") or []
    release_intel = snapshot.get("release_intel") or {}
    dash_meta = snapshot.get("dashboard") or {}
    paths_meta = snapshot.get("paths") or {}
    runtime = snapshot.get("runtime") or {}
    purchases_raw = snapshot.get("purchases_history")
    if purchases_raw is None:
        show_ph = False
        purchases_history: list[Any] = []
    else:
        show_ph = bool(dash_meta.get("show_purchase_history", True))
        purchases_history = purchases_raw if isinstance(purchases_raw, list) else []
    pj_path = str(paths_meta.get("purchases_jsonl") or "state/purchases.jsonl")

    ticks = healthz.get("total_ticks", "—")
    errs = healthz.get("total_errors", "—")
    started = html.escape(str(healthz.get("started_at") or "—"))
    last_utc = html.escape(str(healthz.get("last_tick_at") or "—"))
    last_pt = html.escape(str(healthz.get("last_tick_at_pt") or "—"))
    runtime_state_dir = html.escape(
        str(runtime.get("state_dir") or paths_meta.get("state_dir") or "state")
    )
    runtime_artifacts_root = html.escape(
        str(runtime.get("artifacts_root") or paths_meta.get("artifacts_root") or "artifacts")
    )
    browser_profile = html.escape(str(runtime.get("browser_profile") or "browser-profile"))
    purchase_mode_raw = str(runtime.get("purchase_mode") or "—")
    purchase_mode = html.escape(purchase_mode_raw)
    purchase_enabled = bool(runtime.get("purchase_enabled", True))
    public_base = str(runtime.get("public_base_url") or "http://127.0.0.1:8787/")
    public_base_e = html.escape(public_base)
    notify_channels = runtime.get("notify_channels") or []
    notify_line = html.escape(", ".join(str(x) for x in notify_channels) or "none")
    fandango_poll = runtime.get("fandango_poll") if isinstance(runtime, dict) else {}
    if not isinstance(fandango_poll, dict):
        fandango_poll = {}
    social_poll = runtime.get("social_x_poll") if isinstance(runtime, dict) else {}
    if not isinstance(social_poll, dict):
        social_poll = {}
    fandango_cadence = html.escape(
        _fmt_duration_range(
            fandango_poll.get("min_seconds"),
            fandango_poll.get("max_seconds"),
        )
    )
    fandango_backoff = html.escape(
        _fmt_duration(fandango_poll.get("error_backoff_cap_seconds"))
    )
    social_enabled = "enabled" if social_poll.get("enabled") else "disabled"
    social_cadence = html.escape(
        _fmt_duration_range(
            social_poll.get("min_seconds"),
            social_poll.get("max_seconds"),
        )
    )
    social_max_results = html.escape(str(social_poll.get("max_results_per_handle") or "—"))
    social_state_path = html.escape(
        str(social_poll.get("state_path") or paths_meta.get("social_x_state_path") or "state/social_x.json")
    )

    no_target_history = False
    if targets:
        no_target_history = all(
            not (isinstance(t.get("state"), dict) and t.get("state"))
            for t in targets
        )

    now = datetime.now(UTC)
    target_by_name: dict[str, dict[str, Any]] = {}
    for t in targets:
        if isinstance(t, dict) and t.get("name") is not None:
            target_by_name[str(t["name"])] = t

    triage_panel = _render_triage_panel(
        targets=[x for x in targets if isinstance(x, dict)],
        movies=movies,
        release_intel=release_intel if isinstance(release_intel, dict) else {},
        runtime=runtime if isinstance(runtime, dict) else {},
        fandango_poll=fandango_poll,
        now=now,
    )

    assigned: set[str] = set()
    crawl_blocks: list[str] = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        ft = m.get("fandango_targets")
        if not isinstance(ft, list):
            continue
        mtitle = html.escape(str(m.get("title") or m.get("key") or "Movie"))
        mkey = str(m.get("key") or "movie")
        mkey_slug = _html_id_slug(mkey)
        subcards: list[str] = []
        for tn in ft:
            tname = str(tn)
            if tname in target_by_name and tname not in assigned:
                subcards.append(
                    _render_target_card(
                        target_by_name[tname],
                        fandango_poll=fandango_poll,
                        now=now,
                    )
                )
                assigned.add(tname)
        if subcards:
            crawl_blocks.append(
                f'<section class="movie-group" id="movie-{html.escape(mkey_slug, quote=True)}">'
                f'<h3 class="movie-group-title">{mtitle}</h3>'
                f'<div class="grid">{"".join(subcards)}</div></section>'
            )

    rest: list[dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        n = str(t.get("name", ""))
        if n and n not in assigned:
            rest.append(t)
    if rest:
        rest_html = "".join(
            _render_target_card(x, fandango_poll=fandango_poll, now=now) for x in rest
        )
        crawl_blocks.append(
            f'<section class="movie-group" id="crawl-ungrouped">'
            f'<h3 class="movie-group-title">Other targets</h3>'
            f'<div class="grid">{rest_html}</div></section>'
        )
    if not targets:
        crawl_blocks.append(
            '<p class="hint">No Fandango targets in config — add <code>targets:</code> '
            "in <code>config.yaml</code> and restart <code>watch</code> or use "
            "<code>once</code> with <code>--write-state</code> when you add URLs.</p>"
        )
    elif not crawl_blocks:
        crawl_blocks.append(
            '<div class="grid">'
            + "".join(
                _render_target_card(t, fandango_poll=fandango_poll, now=now)
                for t in targets
                if isinstance(t, dict)
            )
            + "</div>"
        )

    crawl_body = "\n".join(crawl_blocks)
    anchors: list[tuple[str, str]] = [
        ("#triage", "At a glance"),
        ("#runtime", "Runtime"),
        ("#release-intel", "Release intel"),
        ("#crawl", "Fandango"),
        ("#x", "X / Twitter"),
        ("#registry", "Movies"),
    ]
    if show_ph:
        anchors.insert(3, ("#purchase", "Purchase"))
    jump_nav = _jump_nav_html(anchors, aria_label="On this page")

    sx_handles = social_x.get("handles") or {}
    sx_last_polled = html.escape(str(social_x.get("last_polled_at") or "—"))
    sx_cards: list[str] = []
    sx_table_rows: list[str] = []
    n_social = 0
    for hkey, hst in sorted(sx_handles.items(), key=lambda x: str(x[0]).lower()):
        if not isinstance(hst, dict):
            continue
        n_social += 1
        h_raw = str(hst.get("handle") or hkey)
        handle_display = html.escape(h_raw)
        uid = html.escape(str(hst.get("user_id") or "—"))
        polled = html.escape(str(hst.get("last_polled_at") or "—"))
        tid_raw = hst.get("last_seen_tweet_id")
        tid_disp = html.escape(str(tid_raw) if tid_raw else "—")
        path_handle = h_raw.lstrip("@")
        tw_text = hst.get("last_seen_tweet_text")
        tw_at = hst.get("last_seen_tweet_created_at")
        ticket_analysis = hst.get("last_seen_ticket_analysis")
        ce = html.escape(str(hst.get("consecutive_errors") or 0))
        err = hst.get("last_error_message")
        err_html = (
            f'<p class="sx-err">Last error: {html.escape(str(err))}</p>'
            if err
            else ""
        )
        if isinstance(tw_text, str) and tw_text.strip():
            body = html.escape(tw_text)
        else:
            body = (
                "<em class=\"sx-no-text\">No tweet text in state yet. The poller will save "
                "the latest tweet body after new posts, or <strong>backfill by id</strong> on the "
                "next run when the timeline is empty. Run <code>x-poll</code> or wait for "
                "<code>watch</code>.</em>"
            )
        t_flat = " ".join(str(tw_text).split()) if isinstance(tw_text, str) else ""
        if t_flat:
            t_prev = t_flat[:220] + ("…" if len(t_flat) > 220 else "")
            preview_cell = html.escape(t_prev)
        else:
            preview_cell = (
                "<span class=\"sx-preview-missing\" "
                "title=\"Text arrives after the next X poll fetches the tweet body.\">"
                "—</span>"
            )
        at_line = (
            f'<p class="sx-tweet-when">Post time (from API): {html.escape(str(tw_at))}</p>'
            if tw_at
            else ""
        )
        if tid_raw:
            tweet_href = f"https://x.com/{path_handle}/status/{tid_raw}"
            href_e = html.escape(tweet_href, quote=True)
            tid_row = (
                f'<p class="sx-tweet-idline">Tweet id <code class="tweet-snowflake" title="Snowflake id">{tid_disp}</code> '
                f'· <a class="sx-tweet-link" href="{href_e}" target="_blank" rel="noopener">Open on X</a></p>'
            )
            open_cell = f'<a href="{href_e}" target="_blank" rel="noopener">Open</a>'
        else:
            tid_row = f'<p class="sx-tweet-idline">Tweet id: {tid_disp}</p>'
            open_cell = "—"
        le_short = "—" if not err else html.escape(str(err).replace("\n", " ")[:100])
        if err and len(str(err)) > 100:
            le_short += "…"
        analysis_cell = "—"
        analysis_card = ""
        if isinstance(ticket_analysis, dict):
            status = str(ticket_analysis.get("status") or "unknown")
            announces = bool(ticket_analysis.get("announces_tickets"))
            confidence = str(ticket_analysis.get("confidence") or "unknown")
            reason = str(ticket_analysis.get("reason") or "")
            phrases = ticket_analysis.get("matched_phrases") or []
            phrase_text = ", ".join(str(p) for p in phrases if str(p).strip())
            title_bits = [f"confidence: {confidence}"]
            if reason:
                title_bits.append(reason)
            if phrase_text:
                title_bits.append(f"matched: {phrase_text}")
            variants = ("pill-ok",) if announces else ("pill-muted",)
            if announces and status == "soon":
                variants = ("pill-warn",)
            analysis_cell = render_status_pill(
                html.escape(status.replace("_", " ")),
                variants=variants,
                title_esc=" · ".join(title_bits),
            )
            analysis_card = (
                '<p class="sx-ticket-analysis"><strong>Ticket analysis:</strong> '
                f"{analysis_cell}</p>"
            )
        sx_table_rows.append(
            f"<tr><td><strong>@{handle_display}</strong></td><td><code>{uid}</code></td>"
            f"<td><code>{polled}</code></td><td><code>{tid_disp}</code></td>"
            f'<td class="sx-tweet-preview-cell">{preview_cell}</td>'
            f"<td>{analysis_cell}</td><td>{ce}</td><td>{le_short}</td><td>{open_cell}</td></tr>"
        )

        sx_cards.append(
            f'<article class="sx-card" aria-label="X handle {handle_display}">'
            f'<h4 class="sx-handle">@{handle_display}</h4>'
            f'<p class="sx-meta">user_id <code>{uid}</code> · polled <code>{polled}</code> · err streak {ce}</p>'
            f"{tid_row}"
            f"{at_line}"
            f"{analysis_card}"
            f'<blockquote class="sx-tweet-body">{body}</blockquote>'
            f"{err_html}"
            f"</article>"
        )
    thead = "".join(
        f'<th scope="col">{html.escape(col)}</th>'
        for col in (
            "Handle",
            "user_id",
            "last_polled_at",
            "last_seen_tweet_id",
            "tweet text (preview)",
            "ticket analysis",
            "errors",
            "last_error",
            "open",
        )
    )
    sx_table_html = (
        render_data_table(
            thead_row=thead,
            tbody_rows_html="".join(sx_table_rows),
            table_classes=("data-table",),
            caption="Per-handle poller state and last tweet text preview",
            caption_class="visually-hidden",
            wrapper_class="table-wrap sx-snapshot",
            outer_prefix='<div role="region" aria-label="X handles, tweet text snapshot">',
            outer_suffix="</div>",
        )
        if sx_table_rows
        else ""
    )
    sx_cards_html = ""
    if sx_cards:
        sx_cards_html = render_inline_disclosure(
            css_class="inline-fold sx-detail-fold",
            summary_html="Per-handle details",
            inner_html=f'<div class="sx-cards">{"".join(sx_cards)}</div>',
        )
    else:
        sx_cards_html = '<p class="hint">No X handles in state.</p>'
    sx_block = f"""      <p class="hint meta">
        Last global X poll: <code>{sx_last_polled}</code>. Cadence: {social_cadence};
        fetches up to {social_max_results} tweets per handle when needed.
      </p>
      <p class="hint">
        Compact poller state first. Per-handle tweet bodies remain available below for deeper review.
        Use <code>x-poll</code> or <code>x-poll --reset</code> to refresh <code>{social_state_path}</code>.
      </p>
      {sx_table_html}
      {sx_cards_html}
"""

    movie_rows: list[str] = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        title = html.escape(str(m.get("title") or m.get("key") or ""))
        key = html.escape(str(m.get("key") or ""))
        ftargets = html.escape(json.dumps(m.get("fandango_targets") or []))
        xh = html.escape(json.dumps(m.get("x_handles") or []))
        movie_rows.append(
            f"<tr><td>{key}</td><td>{title}</td><td><code>{ftargets}</code></td>"
            f"<td><code>{xh}</code></td></tr>"
        )

    n_registry = len(movie_rows)
    sx_fold = render_fold_panel(
        sx_block,
        fold_id=None,
        summary_html=(
            '<span class="fold-title">X / Twitter poller</span>'
            f'<span class="fold-badge">{n_social} handles</span>'
        ),
        open_=True,
    )
    social_fold = render_panel(
        sx_fold,
        css_classes=("panel-secondary",),
        section_id="x",
        aria_label="X / Twitter poller",
    )

    thead_reg = "".join(
        f'<th scope="col">{html.escape(col)}</th>'
        for col in ("key", "title", "fandango_targets", "x_handles")
    )
    reg_tbl = render_data_table(
        thead_row=thead_reg,
        tbody_rows_html="".join(movie_rows),
        table_classes=("data-table",),
    )
    reg_fold = render_fold_panel(
        reg_tbl,
        fold_id=None,
        summary_html=(
            '<span class="fold-title">Movies registry</span>'
            f'<span class="fold-badge">{n_registry} movies</span>'
        ),
        open_=False,
    )
    registry_fold = render_panel(
        reg_fold,
        css_classes=("panel-secondary",),
        section_id="registry",
        aria_label="Movies registry",
    )


    intel_panel = _render_release_intel_panel(movies, release_intel)
    purchases_panel = ""
    if show_ph:
        ph_rows = purchases_history if isinstance(purchases_history, list) else []
        purchases_panel = _render_purchases_panel(
            [x for x in ph_rows if isinstance(x, dict)],
            file_path=pj_path,
            purchase_enabled=purchase_enabled,
            purchase_mode=purchase_mode_raw,
        )

    metrics_html = (
        "<div><strong>Fandango poll</strong>"
        f"<span>{fandango_cadence} with backoff up to {fandango_backoff}</span></div>"
        "<div><strong>X / Twitter poll</strong>"
        f"<span>{html.escape(social_enabled)} · {social_cadence} · max {social_max_results} tweets/handle</span></div>"
        "<div><strong>State lives in</strong>"
        f"<span><code>{runtime_state_dir}</code></span></div>"
        "<div><strong>Artifacts live in</strong>"
        f"<span><code>{runtime_artifacts_root}</code></span></div>"
        "<div><strong>Browser profile</strong>"
        f"<span><code>{browser_profile}</code></span></div>"
        "<div><strong>Purchase / notify</strong>"
        f"<span><code>{purchase_mode}</code> · {notify_line}</span></div>"
    )
    runtime_inner = f"""
<h2 class="section-label">Runtime &amp; cadence</h2>
<p class="panel-tagline">This snapshot is served at <code>{public_base_e}</code> (read-only; bind address comes from the running process).</p>
{render_metric_grid(metrics_html)}
"""
    runtime_panel = render_panel(
        runtime_inner,
        css_classes=("runtime-panel",),
        section_id="runtime",
    )


    rs = max(0, int(refresh_seconds))
    use_live = rs > 0 and live_revision is not None
    meta_refresh = ""
    noscript_meta = ""
    if rs > 0:
        if use_live:
            noscript_meta = (
                f'  <noscript><meta http-equiv="refresh" content="{rs}" />'
                f"</noscript>\n"
            )
        else:
            meta_refresh = f'  <meta http-equiv="refresh" content="{rs}" />\n'
    poll_ms = 0
    if use_live:
        poll_ms = max(2000, min(30_000, rs * 1000))
    if use_live:
        refresh_note = (
            f"Live reload when data changes (check every {poll_ms // 1000}s). "
            f"No-JS fallback: full refresh every {rs}s."
        )
    elif rs > 0:
        refresh_note = (
            f"Auto-refresh every {rs}s (disable with --refresh-seconds 0)."
        )
    else:
        refresh_note = "Auto-refresh off — reload the page to update."
    rev_json = json.dumps(live_revision) if live_revision is not None else "null"
    if use_live:
        conn_badge = (
            '<p class="conn-line" aria-live="polite">'
            '<span class="conn-label">Live updates: </span>'
            '<span class="conn-status" id="dash-conn">Starting…</span></p>'
        )
    else:
        conn_badge = (
            '<p class="conn-line conn-static">'
            "Static render — <code>/api/revision</code> poll runs when the page is "
            "served with live refresh from <code>watch</code> / <code>dashboard</code>."
            "</p>"
        )
    empty_cfg = ""
    if not targets:
        empty_cfg = (
            '<p class="hint panel-warn">No <code>targets:</code> in this config — add '
            "Fandango URLs under <code>config.yaml</code> → <code>targets</code>.</p>"
        )
    no_hist_block = (
        (
            '<p class="hint">No per-target crawl history yet — the dashboard only '
            '<strong>reads</strong> <code>state/&lt;target&gt;.json</code>. Run '
            '<code>fandango-watcher watch</code> (or <code>once</code>) so ticks, '
            "schema, and screenshots populate. <code>dashboard</code> alone does "
            "not crawl.</p>"
        )
        if (no_target_history and targets)
        else ""
    )
    live_script = ""
    if use_live:
        live_script = f"""
  <script>
(function () {{
  var rev = {rev_json};
  var ms = {poll_ms};
  var conn = document.getElementById("dash-conn");
  function restoreScroll() {{
    var y = sessionStorage.getItem("dashScrollY");
    if (y !== null) {{
      sessionStorage.removeItem("dashScrollY");
      var n = parseInt(y, 10);
      if (!isNaN(n)) {{
        requestAnimationFrame(function () {{ window.scrollTo(0, n); }});
      }}
    }}
  }}
  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", restoreScroll);
  }} else {{
    restoreScroll();
  }}
  function poll() {{
    if (conn) {{ conn.textContent = "Checking…"; conn.className = "conn-status"; }}
    fetch("/api/revision", {{ cache: "no-store" }})
      .then(function (r) {{
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }})
      .then(function (d) {{
        if (conn) {{
          conn.textContent = "OK · " + new Date().toLocaleTimeString();
          conn.className = "conn-status conn-ok";
        }}
        if (d && d.revision && d.revision !== rev) {{
          try {{
            sessionStorage.setItem("dashScrollY", String(window.scrollY));
          }} catch (e) {{}}
          location.reload();
        }}
      }})
      .catch(function () {{
        if (conn) {{
          conn.textContent = "Cannot reach /api/revision";
          conn.className = "conn-status conn-bad";
        }}
      }});
  }}
  setInterval(poll, ms);
  poll();
}})();
  </script>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="color-scheme" content="light dark" />
{meta_refresh}{noscript_meta}  <title>fandango-watcher</title>
  <style>
{dashboard_css()}
  </style>
</head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <header class="dash-header">
    <p class="dash-kicker">Operator console</p>
    <h1 class="dash-title">fandango-watcher</h1>
    <div class="hb-row" aria-label="Heartbeat summary">
      <span class="hb-pill"><span class="dot" aria-hidden="true"></span>ticks {html.escape(str(ticks))}</span>
      <span class="hb-pill">errors {html.escape(str(errs))}</span>
    </div>
    <p>Started (UTC): {started} · Last tick (UTC): {last_utc}</p>
    <p>Last tick (Pacific): {last_pt}</p>
    {conn_badge}
    {empty_cfg}
    {no_hist_block}
  </header>
  <main class="dash" id="main" tabindex="-1">
  {triage_panel}
  {jump_nav}
  <section class="section-head" id="crawl" aria-label="Fandango crawl">
    <h2 class="section-label">Fandango crawl</h2>
    <p class="panel-tagline">Primary ticket-watch targets. Open diagnostics only when you need errors, screenshots, video, or traces.</p>
  </section>
  {crawl_body}
  {runtime_panel}
  {intel_panel}
  {purchases_panel}
  {social_fold}
  {registry_fold}
  </main>
  <footer class="dash-foot">
    <p class="refresh-hint">{html.escape(refresh_note)}</p>
    JSON: <a href="/api/status">/api/status</a> ·
    <a href="/api/purchases">/api/purchases</a> ·
    <a href="/api/release_intel">/api/release_intel</a> ·
    <a href="/api/movies">/api/movies</a> ·
    <a href="/healthz">/healthz</a>
  </footer>
{live_script}</body>
</html>
"""
