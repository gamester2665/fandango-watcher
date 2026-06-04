"""Read-only HTML + JSON dashboard over persisted state and artifacts."""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import Settings, WatcherConfig, load_config
from .release_intel import get_release_intel_for_dashboard
from .social_x import load_social_x_state

_PT = ZoneInfo("America/Los_Angeles")
_CITYWALK_THEATER_SLUG = "universal-cinema-amc-at-citywalk-hollywood-aaawx"
DASHBOARD_STATIC_DIR = Path(__file__).resolve().parent / "static"
IMAX_SCREEN_SIZE_CHART_FILENAME = "la-imax-screen-size-comparison.png"
_TWEET_URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)


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
    config_path: Path | None = None
    config_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # :class:`~fandango_watcher.healthz.Heartbeat` (avoid circular import).
    heartbeat: Any | None = None
    # Env for optional xAI (Grok) release-intel summaries on the dashboard.
    settings: Settings | None = None
    # YAML policy snapshot used when overlaying remote D1 watchlist.
    policy_cfg: WatcherConfig | None = None
    config_revision: int | None = None
    config_source: str = "yaml"
    config_cache_age_seconds: int | None = None
    config_writes_enabled: bool = False
    # HTML meta refresh interval; 0 disables auto-reload.
    refresh_seconds: int = 10
    # Set after the HTTP server binds (actual listen address for dashboard URL copy).
    public_host: str | None = None
    public_port: int | None = None
    # ``(revision_hex, raw_fingerprint)`` — skip re-hashing when inputs unchanged.
    _revision_cache: tuple[str, str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SxHandleRender:
    table_row_html: str
    detail_card_html: str


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


def _fmt_timestamp_html(iso: str | None, *, now: datetime) -> str:
    """Pacific absolute time + relative parenthetical; returns safe HTML."""
    if not iso:
        return "—"
    pt = _fmt_pt(iso)
    if pt == str(iso):
        return html.escape(str(iso))
    rel = _relative_ago(iso, now=now)
    pt_esc = html.escape(pt)
    if rel:
        return f'{pt_esc} <span class="rel">({html.escape(rel)})</span>'
    return pt_esc


def _linkify_tweet_text(raw: str) -> str:
    """Escape tweet text and wrap http(s) URLs in anchors."""
    parts: list[str] = []
    last = 0
    for m in _TWEET_URL_RE.finditer(raw):
        parts.append(html.escape(raw[last : m.start()]))
        url = m.group(0).rstrip(".,;:!?)\"']")
        href = html.escape(url, quote=True)
        label = html.escape(url)
        parts.append(
            f'<a class="sx-tweet-link-inline" href="{href}" '
            f'target="_blank" rel="noopener noreferrer">{label}</a>'
        )
        last = m.end()
    parts.append(html.escape(raw[last:]))
    return "".join(parts)


_SX_STATE_NOT_POLLED = "not_polled"
_SX_STATE_ERROR = "error"
_SX_STATE_MISSING_TEXT = "missing_text"
_SX_STATE_EMPTY_TIMELINE = "empty_timeline"
_SX_STATE_OK = "ok"


def _classify_sx_handle_state(hst: dict[str, Any] | None) -> str:
    if not hst or not hst.get("last_polled_at"):
        return _SX_STATE_NOT_POLLED
    if _as_int(hst.get("consecutive_errors")) > 0 or hst.get("last_error_message"):
        return _SX_STATE_ERROR
    tid = hst.get("last_seen_tweet_id")
    text = hst.get("last_seen_tweet_text")
    if tid and not (isinstance(text, str) and text.strip()):
        return _SX_STATE_MISSING_TEXT
    if not tid:
        return _SX_STATE_EMPTY_TIMELINE
    if isinstance(text, str) and text.strip():
        return _SX_STATE_OK
    return _SX_STATE_EMPTY_TIMELINE


def _sx_empty_message(state: str) -> str:
    return {
        _SX_STATE_NOT_POLLED: "Not polled yet — run x-poll or wait for watch.",
        _SX_STATE_ERROR: "Last poll failed — see errors column or Per-handle details.",
        _SX_STATE_MISSING_TEXT: (
            "Tweet text not cached yet — run x-poll or wait for the next watch poll."
        ),
        _SX_STATE_EMPTY_TIMELINE: (
            "No public tweets returned by the X API for this handle."
        ),
    }.get(state, "No tweet text available.")


def _sx_handle_status_badge(state: str) -> str:
    if state == _SX_STATE_OK:
        return ""
    labels: dict[str, tuple[str, tuple[str, ...]]] = {
        _SX_STATE_NOT_POLLED: ("Not polled", ("pill-muted",)),
        _SX_STATE_EMPTY_TIMELINE: ("Empty timeline", ("pill-muted",)),
        _SX_STATE_MISSING_TEXT: ("Text pending", ("pill-warn",)),
        _SX_STATE_ERROR: ("Poll error", ("pill-warn",)),
    }
    spec = labels.get(state)
    if not spec:
        return ""
    label, variants = spec
    return render_status_pill(html.escape(label), variants=variants)


def _summarize_social_x(
    handles: dict[str, Any],
    *,
    enabled: bool,
) -> dict[str, int]:
    out = {"total": 0, "errors": 0, "empty_timeline": 0, "ticket_signals": 0}
    if not isinstance(handles, dict):
        return out
    out["total"] = sum(1 for v in handles.values() if isinstance(v, dict))
    if not enabled:
        return out
    for hst in handles.values():
        if not isinstance(hst, dict):
            continue
        state = _classify_sx_handle_state(hst)
        if state == _SX_STATE_ERROR:
            out["errors"] += 1
        elif state == _SX_STATE_EMPTY_TIMELINE:
            out["empty_timeline"] += 1
        analysis = hst.get("last_seen_ticket_analysis")
        if isinstance(analysis, dict) and analysis.get("announces_tickets"):
            out["ticket_signals"] += 1
    return out


def _fmt_social_x_ops_summary(summary: dict[str, int], *, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    parts: list[str] = []
    if summary["errors"]:
        n = summary["errors"]
        parts.append(f"{n} error{'s' if n != 1 else ''}")
    if summary["empty_timeline"]:
        n = summary["empty_timeline"]
        parts.append(f"{n} empty")
    n = summary["ticket_signals"]
    parts.append(f"{n} ticket signal{'s' if n != 1 else ''}")
    return " · ".join(parts) if parts else "all clear"


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
                "poster_url": st.get("last_poster_url"),
                "release_date_text": st.get("last_release_date_text"),
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
            "config_path": str(data.config_path) if data.config_path else None,
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
            "config_source": data.config_source,
            "config_revision": data.config_revision,
            "config_api_url": (
                data.settings.config_api_url.strip()
                if data.settings is not None
                else ""
            ),
            "config_cache_age_seconds": data.config_cache_age_seconds,
            "config_writes_enabled": data.config_writes_enabled,
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


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _target_filter_tier(
    st: dict[str, Any],
    *,
    now: datetime,
    stale_threshold_sec: int,
) -> str:
    """Filter bucket used by the dashboard's client-side target controls."""
    tier = _triage_tier(st, now=now, stale_threshold_sec=stale_threshold_sec)
    if tier == 2:
        schema = str(st.get("last_release_schema") or "").lower()
        if "disclosed" in schema:
            return "disclosed"
        return "signal"
    return ("error", "stale", "signal", "routine")[min(max(tier, 0), 3)]


def _target_next_action(
    st: dict[str, Any],
    direct_api: dict[str, Any],
    *,
    is_stale: bool,
) -> str | None:
    """Small deterministic operator hint for target states that need attention."""
    cur_l = str(st.get("current_state") or "").lower()
    if cur_l == "error" or _as_int(st.get("consecutive_errors")) > 0:
        return "Inspect latest error and browser/session health."
    if is_stale:
        return "Run a one-off crawl or check whether watch is still ticking."
    if direct_api.get("last_drift_warning") or st.get("direct_api_last_drift_warning"):
        return "Compare direct API drift against browser fallback."
    if "alert" in cur_l or "purchas" in cur_l or "released" in cur_l or "live" in cur_l:
        return "Review Fandango manually before escalating purchase mode."
    return None


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


def _first_nonempty_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _fmt_release_date(value: Any) -> str:
    raw = _first_nonempty_str(value)
    if raw is None:
        return "Release date not set"
    try:
        dt = datetime.fromisoformat(raw)
        return f"{dt:%b} {dt.day}, {dt:%Y}"
    except ValueError:
        return raw


def _movie_key_from_title(title: str) -> str:
    base = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip() or title
    return _html_id_slug(base).lower().replace("-", "_")


def _unique_name(base: str, existing: set[str], *, separator: str = "-") -> str:
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}{separator}{n}"
        n += 1
    existing.add(candidate)
    return candidate


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _insert_yaml_list_item(raw: str, section: str, item_lines: list[str]) -> str:
    lines = raw.splitlines()
    section_idx: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == f"{section}:" and not line.startswith((" ", "\t")):
            section_idx = i
            break
    block = item_lines
    if section_idx is None:
        prefix = [""] if lines and lines[-1].strip() else []
        return "\n".join([*lines, *prefix, f"{section}:", *block]) + "\n"

    insert_at = len(lines)
    for i in range(section_idx + 1, len(lines)):
        line = lines[i]
        if line and not line.startswith((" ", "\t")) and re.match(r"^[A-Za-z_][\w-]*:", line):
            insert_at = i
            break
    before = lines[:insert_at]
    after = lines[insert_at:]
    if before and before[-1].strip():
        before.append("")
    return "\n".join([*before, *block, *after]) + "\n"


def _movie_id_from_url(url: str) -> int | None:
    match = re.search(r"-(\d+)/movie-overview(?:$|[/?#])", url)
    return int(match.group(1)) if match else None


def add_movie_from_fandango_search_result(
    data: DashboardData,
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Append selected Fandango search result to config and refresh dashboard config."""

    active_settings = settings or data.settings
    if active_settings is not None and active_settings.config_api_url.strip():
        from .config_api_client import config_writes_enabled, reload_merged_config, remote_add_movie, watchlist_config_source

        if not config_writes_enabled(active_settings):
            raise ValueError(
                "CONFIG_ADMIN_TOKEN is not set on this server; dashboard writes are disabled"
            )
        with data.config_lock:
            result = remote_add_movie(active_settings, payload)
            policy = data.policy_cfg or data.cfg
            if data.config_path is not None:
                merged, revision, meta = reload_merged_config(
                    data.config_path,
                    active_settings,
                    policy_cfg=policy,
                )
            else:
                from .config_api_client import fetch_watchlist_http
                from .config import merge_watchlist

                remote = fetch_watchlist_http(active_settings.config_api_url)
                merged = merge_watchlist(policy, remote.targets, remote.movies)
                revision = remote.revision
                meta = {
                    "config_source": watchlist_config_source(active_settings),
                    "config_revision": revision,
                }
            data.cfg = merged
            data.paths = DashboardPaths.from_config(data.cfg)
            data.config_revision = revision
            data.config_source = str(
                meta.get("config_source") or watchlist_config_source(active_settings)
            )
            data.config_cache_age_seconds = meta.get("config_cache_age_seconds")
        movie = result.get("movie") or {}
        targets = result.get("targets") or []
        return {
            "movie": movie,
            "targets": targets,
            "config_path": str(data.config_path) if data.config_path else None,
            "restart_watch_required": False,
            "revision": result.get("revision"),
        }

    config_path = data.config_path
    if config_path is None:
        raise ValueError("dashboard was not started with a writable config path")
    title = _first_nonempty_str(payload.get("title"))
    url = _first_nonempty_str(payload.get("url"))
    if not title or not url:
        raise ValueError("title and url are required")
    if not url.startswith("https://www.fandango.com/") or "/movie-overview" not in url:
        raise ValueError("url must be a Fandango movie-overview URL")

    overview_url = url.split("?", 1)[0]
    movie_id = payload.get("movie_id")
    if not isinstance(movie_id, int):
        movie_id = _movie_id_from_url(overview_url)
    include_imax_70mm = bool(payload.get("include_imax_70mm", True))

    with data.config_lock:
        raw = config_path.read_text(encoding="utf-8")
        target_names = {t.name for t in data.cfg.targets}
        movie_keys = {m.key for m in data.cfg.movies}
        key = _unique_name(_movie_key_from_title(title), movie_keys, separator="_")
        prefix = key.replace("_", "-")

        new_targets: list[tuple[str, str]] = []
        overview_name = _unique_name(f"{prefix}-overview", target_names)
        new_targets.append((overview_name, overview_url))
        if include_imax_70mm:
            imax_name = _unique_name(f"{prefix}-imax-70mm", target_names)
            new_targets.append((imax_name, f"{overview_url}?format={quote('IMAX 70MM')}"))

        target_lines: list[str] = []
        for name, target_url in new_targets:
            target_lines.extend(
                [
                    f"  - name: {_yaml_string(name)}",
                    f"    url: {_yaml_string(target_url)}",
                ]
            )

        movie_lines = [
            f"  - key: {_yaml_string(key)}",
            f"    title: {_yaml_string(title)}",
        ]
        if movie_id is not None:
            movie_lines.append(f"    fandango_movie_id: {movie_id}")
        release_date_text = _first_nonempty_str(payload.get("release_date_text"))
        if release_date_text:
            movie_lines.append(f"    release_date: {_yaml_string(release_date_text)}")
        poster_url = _first_nonempty_str(payload.get("poster_url"))
        if poster_url:
            movie_lines.append(f"    poster_url: {_yaml_string(poster_url)}")
        movie_lines.append(
            "    fandango_targets: ["
            + ", ".join(_yaml_string(name) for name, _ in new_targets)
            + "]"
        )
        if include_imax_70mm:
            movie_lines.append("    preferred_formats: [IMAX_70MM, IMAX]")
        else:
            movie_lines.append("    preferred_formats: [IMAX]")
        movie_lines.append("    x_handles: []")

        updated = _insert_yaml_list_item(raw, "targets", target_lines)
        updated = _insert_yaml_list_item(updated, "movies", movie_lines)
        config_path.write_text(updated, encoding="utf-8")
        try:
            data.cfg = load_config(config_path)
            data.paths = DashboardPaths.from_config(data.cfg)
        except Exception:
            config_path.write_text(raw, encoding="utf-8")
            data.cfg = load_config(config_path)
            data.paths = DashboardPaths.from_config(data.cfg)
            raise

    return {
        "movie": {
            "key": key,
            "title": title,
            "fandango_movie_id": movie_id,
            "fandango_targets": [name for name, _ in new_targets],
        },
        "targets": [{"name": name, "url": target_url} for name, target_url in new_targets],
        "config_path": str(config_path),
        "restart_watch_required": True,
    }


def delete_movie_from_watchlist(
    data: DashboardData,
    key: str,
    *,
    settings: Settings | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    active_settings = settings or data.settings
    if active_settings is None or not active_settings.config_api_url.strip():
        raise ValueError("remote watchlist is not configured")
    from .config_api_client import (
        config_writes_enabled,
        reload_merged_config,
        remote_delete_movie,
        watchlist_config_source,
    )

    if not config_writes_enabled(active_settings):
        raise ValueError(
            "CONFIG_ADMIN_TOKEN is not set on this server; dashboard writes are disabled"
        )
    with data.config_lock:
        result = remote_delete_movie(
            active_settings,
            key,
            expected_revision=expected_revision or data.config_revision,
        )
        policy = data.policy_cfg or data.cfg
        if data.config_path is not None:
            merged, revision, meta = reload_merged_config(
                data.config_path,
                active_settings,
                policy_cfg=policy,
            )
        else:
            from .config import merge_watchlist
            from .config_api_client import fetch_watchlist_http

            remote = fetch_watchlist_http(active_settings.config_api_url)
            merged = merge_watchlist(policy, remote.targets, remote.movies)
            revision = remote.revision
            meta = {
                "config_source": watchlist_config_source(active_settings),
                "config_revision": revision,
            }
        data.cfg = merged
        data.paths = DashboardPaths.from_config(data.cfg)
        data.config_revision = revision
        data.config_source = str(meta.get("config_source") or watchlist_config_source(active_settings))
        data.config_cache_age_seconds = meta.get("config_cache_age_seconds")
    return {
        "deleted_key": key,
        "revision": result.get("revision"),
        "restart_watch_required": False,
    }


def patch_movie_in_watchlist(
    data: DashboardData,
    key: str,
    patch: dict[str, Any],
    *,
    settings: Settings | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    active_settings = settings or data.settings
    if active_settings is None or not active_settings.config_api_url.strip():
        raise ValueError("remote watchlist is not configured")
    from .config_api_client import (
        config_writes_enabled,
        reload_merged_config,
        remote_patch_movie,
        watchlist_config_source,
    )

    if not config_writes_enabled(active_settings):
        raise ValueError(
            "CONFIG_ADMIN_TOKEN is not set on this server; dashboard writes are disabled"
        )
    payload = dict(patch)
    if expected_revision is not None:
        payload["expected_revision"] = expected_revision
    elif data.config_revision is not None:
        payload["expected_revision"] = data.config_revision
    with data.config_lock:
        result = remote_patch_movie(active_settings, key, payload)
        policy = data.policy_cfg or data.cfg
        if data.config_path is not None:
            merged, revision, meta = reload_merged_config(
                data.config_path,
                active_settings,
                policy_cfg=policy,
            )
        else:
            from .config import merge_watchlist
            from .config_api_client import fetch_watchlist_http

            remote = fetch_watchlist_http(active_settings.config_api_url)
            merged = merge_watchlist(policy, remote.targets, remote.movies)
            revision = remote.revision
            meta = {
                "config_source": watchlist_config_source(active_settings),
                "config_revision": revision,
            }
        data.cfg = merged
        data.paths = DashboardPaths.from_config(data.cfg)
        data.config_revision = revision
        data.config_source = str(meta.get("config_source") or watchlist_config_source(active_settings))
        data.config_cache_age_seconds = meta.get("config_cache_age_seconds")
    return {
        "movie_key": key,
        "revision": result.get("revision"),
        "restart_watch_required": False,
    }


def _schema_badge_parts(value: Any) -> tuple[str, str, str]:
    schema = str(value or "").strip().lower()
    if schema == "not_on_sale":
        return (
            "not-on-sale",
            "Schema A - not on sale",
            "Baseline watch state: Fandango has not exposed usable showtimes yet.",
        )
    if schema == "showtimes_disclosed":
        return (
            "showtimes-disclosed",
            "Schema D - showtimes disclosed",
            "Showtimes are visible on Fandango but none are buyable yet; watch for the on-sale flip.",
        )
    if schema == "partial_release":
        return (
            "partial-release",
            "Schema B - partial release",
            "Early ticket signal: buyable showtimes are live — verify target format and theater before purchase.",
        )
    if schema == "full_release":
        return (
            "full-release",
            "Schema C - full release",
            "Broad ticket signal: verify the target format/theater before escalating purchase mode.",
        )
    return (
        "unknown",
        "Schema unknown",
        "No successful crawl schema yet; check freshness, errors, and latest artifacts.",
    )


def _showtime_fact_entries(st: dict[str, Any]) -> list[tuple[str, str]]:
    """Optional showtime / buyable counts for target cards."""
    visible = st.get("last_showtime_count")
    buyable = st.get("last_buyable_showtime_count")
    if visible is None and buyable is None:
        return []
    visible_s = html.escape(str(visible if visible is not None else "—"))
    buyable_s = html.escape(str(buyable if buyable is not None else "—"))
    return [
        (html.escape("Showtimes"), visible_s),
        (html.escape("Buyable"), buyable_s),
    ]


def _schema_badge_html(
    value: Any, *, with_hint: bool = False, compact: bool = False
) -> str:
    key, label, hint = _schema_badge_parts(value)
    raw = str(value or "unknown").strip() or "unknown"
    cls = html.escape(f"schema-badge schema-{key}", quote=True)
    label_esc = html.escape(label)
    hint_esc = html.escape(hint)
    raw_esc = html.escape(raw)
    hint_html = f'<span class="schema-hint">{hint_esc}</span>' if with_hint else ""
    code_html = "" if compact else f"<code>{raw_esc}</code>"
    return (
        f'<span class="{cls}" title="{hint_esc}" aria-label="{label_esc}: {hint_esc}">'
        f'<span class="schema-label">{label_esc}</span>'
        f"{code_html}"
        f"</span>{hint_html}"
    )


def _poster_url_for_movie(
    movie: dict[str, Any],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> str | None:
    configured = _first_nonempty_str(movie.get("poster_url"))
    if configured:
        return configured
    ft = movie.get("fandango_targets")
    if isinstance(ft, list):
        for target_name in ft:
            target = target_by_name.get(str(target_name))
            if target:
                state = target.get("state") if isinstance(target.get("state"), dict) else {}
                poster = _first_nonempty_str(
                    target.get("poster_url"),
                    state.get("last_poster_url") if isinstance(state, dict) else None,
                )
                if poster:
                    return poster
    return None


def _movie_aspect_ratio_max(movie: dict[str, Any]) -> float | None:
    raw = movie.get("aspect_ratio_max")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _format_aspect_ratio_label(ratio: float) -> str:
    text = f"{ratio:.2f}".rstrip("0").rstrip(".")
    return f"{text}:1"


def _render_aspect_ratio_chip(movie: dict[str, Any], *, compact: bool = False) -> str:
    """IMAX max expanded aspect ratio from watchlist metadata (D1 / config)."""
    ratio = _movie_aspect_ratio_max(movie)
    if ratio is None:
        return ""
    label = _format_aspect_ratio_label(ratio)
    real = bool(movie.get("is_real_imax"))
    notes = _first_nonempty_str(movie.get("aspect_ratio_notes"))
    cls = "aspect-ratio-chip aspect-ratio-chip--real-imax" if real else "aspect-ratio-chip aspect-ratio-chip--dmr"
    title_attr = f' title="{html.escape(notes, quote=True)}"' if notes else ""
    prefix = "" if compact else "Max "
    real_suffix = " · GT" if real and compact else (" · full IMAX" if real else "")
    return (
        f'<span class="{cls}"{title_attr}>'
        f"{html.escape(prefix)}{html.escape(label)}{html.escape(real_suffix)}"
        f"</span>"
    )


def _render_aspect_ratio_meta(movie: dict[str, Any]) -> str:
    chip = _render_aspect_ratio_chip(movie, compact=False)
    if not chip:
        return ""
    return f' · <span class="movie-aspect-meta">{chip}</span>'


def dashboard_static_dir() -> Path:
    """Bundled dashboard images (served at ``/static/...``)."""
    return DASHBOARD_STATIC_DIR


def _imax_screen_size_chart_path() -> Path | None:
    path = DASHBOARD_STATIC_DIR / IMAX_SCREEN_SIZE_CHART_FILENAME
    return path if path.is_file() else None


def _render_imax_screen_reference_panel() -> str:
    """LA-area IMAX screen size infographic (expandable via artifact lightbox)."""
    if _imax_screen_size_chart_path() is None:
        return ""
    url = f"/static/{IMAX_SCREEN_SIZE_CHART_FILENAME}"
    frame = _media_frame_html(
        variant="screenshot",
        src=url,
        alt="LA Area IMAX screen size comparison chart",
        title="LA Area IMAX Screen Size Comparison",
        caption="Tap to expand",
        css_class="imax-screen-ref-thumb",
        interactive=True,
        artifact_kind="screenshot",
        artifact_title="LA Area IMAX Screen Size Comparison",
        loading="lazy",
    )
    return (
        '<aside class="imax-screen-ref-panel" id="imax-screen-sizes" '
        'aria-label="LA IMAX screen size reference">'
        '<h3 class="imax-screen-ref-heading">LA IMAX screen sizes</h3>'
        '<p class="hint imax-screen-ref-lede">'
        "Physical screen footprints for SoCal IMAX venues — "
        "<strong>Universal CityWalk</strong> is <strong>79×58 ft</strong> "
        "(~1.36:1). Compare to GT Laser halls like Irvine/Ontario (~1.30:1) "
        "and wider screens like TCL Chinese (~2.04:1)."
        "</p>"
        f"{frame}"
        "</aside>"
    )


def _release_date_for_movie(
    movie: dict[str, Any],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> str | None:
    configured = _first_nonempty_str(movie.get("release_date"))
    if configured:
        return configured
    ft = movie.get("fandango_targets")
    if isinstance(ft, list):
        for target_name in ft:
            target = target_by_name.get(str(target_name))
            if target:
                state = target.get("state") if isinstance(target.get("state"), dict) else {}
                release_date = _first_nonempty_str(
                    target.get("release_date_text"),
                    state.get("last_release_date_text") if isinstance(state, dict) else None,
                )
                if release_date:
                    return release_date
    return None


_SCHEMA_RANK: dict[str, int] = {
    "unknown": 0,
    "not_on_sale": 1,
    "showtimes_disclosed": 2,
    "partial_release": 3,
    "full_release": 4,
}


def _schema_rank(value: Any) -> int:
    return _SCHEMA_RANK.get(str(value or "").strip().lower(), 0)


def _schema_filter_key(value: Any) -> str:
    schema = str(value or "").strip().lower() or "unknown"
    if schema in _SCHEMA_RANK:
        return schema
    return "unknown"


def _best_schema_for_targets(
    target_names: Iterable[str],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> str:
    best = "unknown"
    best_rank = -1
    for name in target_names:
        t = target_by_name.get(str(name))
        if not isinstance(t, dict):
            continue
        st = t.get("state") if isinstance(t.get("state"), dict) else {}
        schema = str(st.get("last_release_schema") or "unknown").lower()
        rank = _schema_rank(schema)
        if rank > best_rank:
            best_rank = rank
            best = schema
    return best


def _target_release_schema_and_buyable(
    name: str,
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> tuple[str, int | None]:
    t = target_by_name.get(str(name))
    if not isinstance(t, dict):
        return "unknown", None
    st = t.get("state") if isinstance(t.get("state"), dict) else {}
    schema = str(st.get("last_release_schema") or "unknown").lower()
    raw_buyable = st.get("last_buyable_showtime_count")
    if raw_buyable is None:
        return schema, None
    try:
        return schema, int(raw_buyable)
    except (TypeError, ValueError):
        return schema, None


def _movie_group_release_schema(
    target_names: Iterable[str],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> str:
    """Roll up release schema for a movie group (poster + header).

    Live/partial signals still promote when any sub-target has buyable inventory.
    Showtimes-disclosed (visible but not buyable) only appears on the parent when
    every sub-target is in that state — one disclosed format must not mark the
  whole movie as sold out.
    """
    names = [str(n) for n in target_names]
    if not names:
        return "unknown"

    entries = [
        _target_release_schema_and_buyable(n, target_by_name=target_by_name) for n in names
    ]
    schemas = [schema for schema, _ in entries]

    if any(buyable is not None and buyable > 0 for _, buyable in entries):
        return _best_schema_for_targets(names, target_by_name=target_by_name)

    if any(schema in ("partial_release", "full_release") for schema in schemas):
        return _best_schema_for_targets(names, target_by_name=target_by_name)

    if schemas and all(schema == "showtimes_disclosed" for schema in schemas):
        return "showtimes_disclosed"

    if schemas and all(schema == "not_on_sale" for schema in schemas):
        return "not_on_sale"

    without_disclosed = [schema for schema in schemas if schema != "showtimes_disclosed"]
    if without_disclosed:
        best = "unknown"
        best_rank = -1
        for schema in without_disclosed:
            rank = _schema_rank(schema)
            if rank > best_rank:
                best_rank = rank
                best = schema
        return best

    return "unknown"


def _release_date_sort_value(
    movie: dict[str, Any],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> str:
    raw = _release_date_for_movie(movie, target_by_name=target_by_name)
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except ValueError:
        return raw.strip().lower()


def _movie_showtime_totals(
    target_names: Iterable[str],
    *,
    target_by_name: dict[str, dict[str, Any]],
) -> tuple[int | None, int | None]:
    visible = 0
    buyable = 0
    saw = False
    for name in target_names:
        t = target_by_name.get(str(name))
        if not isinstance(t, dict):
            continue
        st = t.get("state") if isinstance(t.get("state"), dict) else {}
        if (
            st.get("last_showtime_count") is None
            and st.get("last_buyable_showtime_count") is None
        ):
            continue
        saw = True
        visible += int(st.get("last_showtime_count") or 0)
        buyable += int(st.get("last_buyable_showtime_count") or 0)
    if not saw:
        return None, None
    return visible, buyable


_MediaVariant = Literal["poster", "thumb", "screenshot", "video", "lightbox"]


def _media_frame_html(
    *,
    variant: _MediaVariant,
    src: str | None = None,
    alt: str,
    title: str | None = None,
    caption: str | None = None,
    css_class: str = "",
    interactive: bool = False,
    artifact_kind: str | None = None,
    artifact_title: str | None = None,
    loading: str = "lazy",
    video_controls: bool = False,
    video_preload: str = "metadata",
) -> str:
    classes = [f"media-frame media-frame--{variant}"]
    if interactive:
        classes.append("is-clickable")
    frame_cls = html.escape(" ".join(classes), quote=True)

    cap_html = ""
    if caption:
        cap_html = f'<figcaption class="media-caption">{html.escape(caption)}</figcaption>'

    inner = ""
    if variant in ("poster", "thumb") and not src:
        initial = (title or alt or "?").strip()[:1].upper() or "?"
        fb_cls = html.escape(
            " ".join(x for x in (css_class, "poster-fallback") if x).strip(),
            quote=True,
        )
        inner = (
            f'<div class="{fb_cls}" aria-label="No poster available">'
            f"{html.escape(initial)}</div>"
        )
    elif src and variant == "video":
        ctrl = " controls" if video_controls else ""
        inner = (
            f'<video{ctrl} preload="{html.escape(video_preload, quote=True)}" '
            f'playsinline muted src="{html.escape(src, quote=True)}" '
            f'title="{html.escape(alt, quote=True)}"></video>'
        )
    elif src:
        img_cls = html.escape(css_class, quote=True) if css_class else ""
        cls_attr = f' class="{img_cls}"' if img_cls else ""
        inner = (
            f"<img{cls_attr} src=\"{html.escape(src, quote=True)}\" "
            f'alt="{html.escape(alt, quote=True)}" loading="{html.escape(loading, quote=True)}" />'
        )
    else:
        inner = '<div class="poster-fallback" aria-label="No media available">?</div>'

    badge_html = ""
    if variant == "video" and interactive:
        badge_html = '<span class="media-play-badge" aria-hidden="true"></span>'

    btn_html = ""
    if interactive and artifact_kind and (artifact_src := src):
        btn_html = (
            '<button type="button" class="artifact-open media-hit-target" '
            f'data-artifact-kind="{html.escape(artifact_kind, quote=True)}" '
            f'data-artifact-src="{html.escape(artifact_src, quote=True)}" '
            f'data-artifact-title="{html.escape(artifact_title or alt, quote=True)}" '
            f'aria-label="{html.escape(artifact_title or alt, quote=True)}"></button>'
        )

    if caption and variant in ("screenshot", "video") and interactive:
        return f'<figure class="{frame_cls}">{inner}{badge_html}{cap_html}{btn_html}</figure>'
    return f'<figure class="{frame_cls}">{cap_html}{inner}{badge_html}{btn_html}</figure>'


def _render_movie_schedule_panel(movie_key: str) -> str:
    key_attr = html.escape(movie_key, quote=True)
    return (
        f'<div class="movie-schedule-panel" data-movie-schedule-panel '
        f'data-movie-key="{key_attr}">'
        '<p class="movie-schedule-head"><strong>CityWalk schedule</strong> '
        '<span class="hint">Load dates, then pin a showtime to watch.</span></p>'
        '<button type="button" class="btn-ghost" data-load-schedule>Load schedule</button>'
        '<div class="movie-schedule-list" hidden></div>'
        '<p class="hint movie-schedule-pins">Pinned: '
        f'<span data-pin-summary data-movie-key="{key_attr}">none</span></p>'
        "</div>"
    )


def _poster_html(poster_url: str | None, title: str, *, css_class: str) -> str:
    return (
        '<div class="movie-group-poster-stack">'
        + _media_frame_html(
            variant="poster",
            src=poster_url,
            alt=f"Poster for {title}",
            title=title,
            css_class=css_class,
        )
        + "</div>"
    )


def _summarize_targets_status(
    target_names: Iterable[str],
    *,
    target_by_name: dict[str, dict[str, Any]],
    fandango_poll: dict[str, Any],
    now: datetime,
) -> str:
    """Worst crawl-health status across targets for poster-shelf icons."""
    stale_thr = _stale_threshold_seconds(fandango_poll)
    rank = 3
    has_live_signal = False
    for name in target_names:
        t = target_by_name.get(str(name))
        if not isinstance(t, dict):
            continue
        st = t.get("state") if isinstance(t.get("state"), dict) else {}
        tier = _triage_tier(st, now=now, stale_threshold_sec=stale_thr)
        rank = min(rank, min(max(tier, 0), 3))
        if tier == 2:
            cur_l = str(st.get("current_state") or "").lower()
            schema_l = str(st.get("last_release_schema") or "").lower()
            if (
                "partial" in schema_l
                or "full" in schema_l
                or "alert" in cur_l
                or "purchas" in cur_l
            ):
                has_live_signal = True
    status = ("error", "stale", "signal", "routine")[rank]
    if status == "signal" and not has_live_signal:
        return "routine"
    return status


def _poster_shelf_status_icon_html(status_key: str) -> str:
    if status_key == "routine":
        return ""
    glyphs = {
        "error": "!",
        "stale": "⏱",
        "signal": "●",
    }
    labels = {
        "error": "Needs attention",
        "stale": "Stale crawl",
        "signal": "On-sale signal",
    }
    glyph = glyphs.get(status_key)
    if not glyph:
        return ""
    label = html.escape(labels.get(status_key, status_key), quote=True)
    glyph_esc = html.escape(glyph)
    cls = html.escape(f"poster-shelf-status-icon poster-shelf-status-icon--{status_key}", quote=True)
    return (
        f'<span class="{cls}" title="{label}" aria-label="{label}">'
        f'<span class="poster-shelf-status-glyph" aria-hidden="true">{glyph_esc}</span></span>'
    )


def _render_poster_shelf_tile(
    *,
    movie_id: str,
    title: str,
    poster_url: str | None,
    status: str,
    schema: str = "unknown",
    aspect_chip: str = "",
) -> str:
    status_key = status if status in ("error", "stale", "signal", "routine") else "routine"
    schema_key = _schema_filter_key(schema)
    status_labels = {
        "error": "Needs attention",
        "stale": "Stale crawl",
        "signal": "On-sale signal",
        "routine": "Routine watch",
    }
    schema_labels = {
        "not_on_sale": "Not on sale",
        "showtimes_disclosed": "Showtimes disclosed",
        "partial_release": "Partial release",
        "full_release": "Full release",
        "unknown": "Schema unknown",
    }
    poster = _media_frame_html(
        variant="poster",
        src=poster_url,
        alt=f"Poster for {title}",
        title=title,
        css_class="poster-shelf-poster",
    )
    label = html.escape(title)
    mid = html.escape(movie_id, quote=True)
    schema_esc = html.escape(schema_key, quote=True)
    cls = html.escape(
        f"poster-shelf-tile poster-shelf-tile--{status_key} poster-shelf-tile--schema-{schema_key}",
        quote=True,
    )
    hint = html.escape(
        f"{title} · {status_labels[status_key]} · {schema_labels.get(schema_key, schema_key)}",
        quote=True,
    )
    status_icon = _poster_shelf_status_icon_html(status_key)
    aspect_block = (
        f'<span class="poster-shelf-aspect">{aspect_chip}</span>' if aspect_chip else ""
    )
    return (
        f'<a class="{cls}" href="#movie-{mid}" data-movie-jump="{mid}" '
        f'data-poster-schema="{schema_esc}" title="{hint}">'
        f'<span class="poster-shelf-poster-wrap">{poster}{status_icon}</span>'
        f'<span class="poster-shelf-label">{label}</span>'
        f"{aspect_block}</a>"
    )


def _render_poster_shelf(tiles: list[str]) -> str:
    if not tiles:
        return ""
    legend = (
        '<p class="poster-shelf-legend" aria-hidden="true">'
        '<span class="poster-shelf-legend-group poster-shelf-legend-group--status">'
        '<span class="poster-shelf-key poster-shelf-key--status poster-shelf-key--error">'
        '<span class="poster-shelf-status-glyph poster-shelf-status-glyph--error" aria-hidden="true">!</span>'
        "Attention</span>"
        '<span class="poster-shelf-key poster-shelf-key--status poster-shelf-key--stale">'
        '<span class="poster-shelf-status-glyph poster-shelf-status-glyph--stale" aria-hidden="true">⏱</span>'
        "Stale</span>"
        '<span class="poster-shelf-key poster-shelf-key--status poster-shelf-key--signal">'
        '<span class="poster-shelf-status-glyph poster-shelf-status-glyph--signal" aria-hidden="true">●</span>'
        "Signal</span>"
        "</span>"
        '<span class="poster-shelf-legend-sep" aria-hidden="true">·</span>'
        '<span class="poster-shelf-legend-group poster-shelf-legend-group--schema">'
        '<span class="poster-shelf-key poster-shelf-key--schema poster-shelf-key--schema-not-on-sale">Not on sale</span>'
        '<span class="poster-shelf-key poster-shelf-key--schema poster-shelf-key--schema-disclosed">Disclosed</span>'
        '<span class="poster-shelf-key poster-shelf-key--schema poster-shelf-key--schema-live">Live</span>'
        "</span>"
        "</p>"
    )
    return (
        '<nav class="poster-shelf" aria-label="Movie poster overview">'
        f"{legend}<div class=\"poster-shelf-track\">{''.join(tiles)}</div></nav>"
    )


def _render_shelf_view_toggle(*, visible: bool) -> str:
    if not visible:
        return ""
    return """
<div class="shelf-view-toggle" role="group" aria-label="Watchlist layout">
  <button type="button" class="shelf-view-btn is-active" data-movie-view="cards" aria-pressed="true">Cards</button>
  <button type="button" class="shelf-view-btn" data-movie-view="posters" aria-pressed="false">Posters</button>
</div>"""


def _render_watchlist_controls(*, movie_count: int) -> str:
    if movie_count <= 0:
        return ""
    return f"""
<div class="watchlist-controls" data-watchlist-controls>
  <label class="target-search-label">
    <span class="visually-hidden">Search movies</span>
    <input type="search" id="movie-search" placeholder="Search movies, schema, distributor..." autocomplete="off" />
  </label>
  <div class="watchlist-controls-row">
    <label class="watchlist-sort-label">
      <span class="watchlist-sort-text">Sort</span>
      <select id="movie-sort" aria-label="Sort movies">
        <option value="release-asc">Release date (soonest)</option>
        <option value="release-desc">Release date (latest)</option>
        <option value="title-asc">Title (A–Z)</option>
        <option value="title-desc">Title (Z–A)</option>
        <option value="schema-desc">Schema (most live first)</option>
        <option value="schema-asc">Schema (least live first)</option>
      </select>
    </label>
    <div class="target-filter-row" role="group" aria-label="Movie schema filters">
      <button type="button" class="target-filter-btn is-active" data-movie-filter="all">All</button>
      <button type="button" class="target-filter-btn" data-movie-filter="not_on_sale">Not on sale</button>
      <button type="button" class="target-filter-btn" data-movie-filter="showtimes_disclosed">Disclosed</button>
      <button type="button" class="target-filter-btn" data-movie-filter="live">Live</button>
    </div>
  </div>
  <p class="target-filter-count" id="movie-filter-count" aria-live="polite">{html.escape(str(movie_count))} movies shown</p>
</div>
"""


def _render_card_media_preview(
    *,
    name: str,
    name_attr: str,
    screenshot_url: str | None,
    video_url: str | None,
) -> str:
    tiles: list[str] = []
    if screenshot_url:
        tiles.append(
            _media_frame_html(
                variant="screenshot",
                src=screenshot_url,
                alt=f"screenshot {name}",
                caption="Screenshot",
                interactive=True,
                artifact_kind="screenshot",
                artifact_title=f"Screenshot for {name_attr}",
            )
        )
    if video_url:
        tiles.append(
            _media_frame_html(
                variant="video",
                src=video_url,
                alt=f"crawl video {name}",
                caption="Video",
                interactive=True,
                artifact_kind="video",
                artifact_title=f"Video for {name_attr}",
            )
        )
    if not tiles:
        return ""
    return (
        '<div class="card-media-preview media-shelf media-shelf--compact" '
        'aria-label="Latest crawl media">'
        + "".join(tiles)
        + "</div>"
    )


def _social_state_for_handle(
    handles: dict[str, Any],
    handle: str,
) -> dict[str, Any] | None:
    h_norm = handle.lstrip("@").lower()
    for key, value in handles.items():
        if str(key).lstrip("@").lower() != h_norm:
            continue
        return value if isinstance(value, dict) else None
    return None


def _effective_recent_tweets(hst: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cached tweet history, falling back to the legacy single-tweet fields."""
    recent = hst.get("recent_tweets")
    if isinstance(recent, list):
        out: list[dict[str, Any]] = []
        for item in recent:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("tweet_id") or item.get("id") or "").strip()
            text = item.get("text")
            if not tid or not isinstance(text, str) or not text.strip():
                continue
            out.append(item)
        if out:
            return out
    tid = hst.get("last_seen_tweet_id")
    text = hst.get("last_seen_tweet_text")
    if tid and isinstance(text, str) and text.strip():
        return [
            {
                "tweet_id": str(tid),
                "text": text,
                "created_at": hst.get("last_seen_tweet_created_at"),
                "ticket_analysis": hst.get("last_seen_ticket_analysis"),
            }
        ]
    return []


def _tweet_sort_key(tweet: dict[str, Any]) -> tuple[int, str]:
    tid = str(tweet.get("tweet_id") or tweet.get("id") or "0")
    try:
        return (int(tid), tid)
    except ValueError:
        return (0, tid)


def _tweet_announces_tickets(tweet: dict[str, Any]) -> bool:
    analysis = tweet.get("ticket_analysis")
    return isinstance(analysis, dict) and bool(analysis.get("announces_tickets"))


def _render_tweet_filter_controls(*, total: int, ticket_count: int) -> str:
    buttons = "".join(
        f'<button type="button" class="target-filter-btn tweet-filter-btn{" is-active" if key == "all" else ""}" '
        f'data-tweet-filter="{html.escape(key, quote=True)}" aria-pressed="{"true" if key == "all" else "false"}">'
        f"{html.escape(label)}</button>"
        for key, label in (
            ("all", "All"),
            ("ticket", "Ticket related"),
        )
    )
    return f"""
  <div class="tweet-filter-row" role="group" aria-label="Tweet filters">
    {buttons}
  </div>
  <p class="tweet-filter-count" aria-live="polite">{html.escape(str(total))} tweet{"s" if total != 1 else ""} · {html.escape(str(ticket_count))} ticket related</p>
"""


def _render_tweet_timeline_card(
    handle: str,
    tweet: dict[str, Any],
    *,
    handle_state: str,
    handle_badge: str,
    now: datetime,
    polled: object | None = None,
) -> str:
    handle_e = html.escape(handle)
    tid = str(tweet.get("tweet_id") or tweet.get("id") or "")
    text_raw = str(tweet.get("text") or "")
    tw_at = tweet.get("created_at")
    profile_url = f"https://x.com/{handle}"
    tweet_url = f"https://x.com/{handle}/status/{tid}" if tid else profile_url
    card_classes = ["tweet-embed", "tweet-timeline-item"]
    filter_tier = "ticket" if _tweet_announces_tickets(tweet) else "all"
    has_text = bool(text_raw.strip())
    if not has_text:
        card_classes.append("tweet-empty-card")

    analysis = tweet.get("ticket_analysis")
    analysis_html = ""
    if isinstance(analysis, dict):
        status = str(analysis.get("status") or "unknown").replace("_", " ")
        status_raw = str(analysis.get("status") or "unknown")
        announces = bool(analysis.get("announces_tickets"))
        if announces:
            slug = re.sub(r"[^a-z0-9_-]", "", status_raw.lower()) or "unknown"
            card_classes.extend(["sx-ticket-signal", f"sx-status-{slug}"])
        variants = ("pill-ok",) if announces else ("pill-muted",)
        if announces and status_raw == "soon":
            variants = ("pill-warn",)
        analysis_html = (
            '<p class="tweet-analysis">'
            + render_status_pill(html.escape(status), variants=variants)
            + "</p>"
        )

    meta_bits = []
    if tw_at:
        meta_bits.append(
            f'posted <span class="sx-ts">{_fmt_timestamp_html(str(tw_at), now=now)}</span>'
        )
    if polled:
        meta_bits.append(
            f'polled <span class="sx-ts">{_fmt_timestamp_html(str(polled), now=now)}</span>'
        )
    meta = " · ".join(meta_bits) or "cached tweet"
    body = _format_sx_tweet_body_html(
        text_raw if has_text else None,
        empty_message=_sx_empty_message(handle_state),
    ).replace('class="sx-tweet-body"', 'class="tweet-body"', 1)
    card_cls = html.escape(" ".join(card_classes), quote=True)
    tier_attr = html.escape(filter_tier, quote=True)
    return (
        f'<article class="{card_cls}" data-tweet-tier="{tier_attr}">'
        f'<p class="tweet-handle">@{handle_e} {handle_badge}</p>'
        f"{body}"
        f"{analysis_html}"
        f'<p class="tweet-meta">{meta}</p>'
        f'<p class="tweet-actions"><a href="{html.escape(tweet_url, quote=True)}" target="_blank" rel="noopener">Open on X</a></p>'
        "</article>"
    )


def _format_sx_tweet_body_html(
    tw_text: object,
    *,
    empty_message: str,
) -> str:
    """Readable tweet body for dashboard tables and cards."""
    if isinstance(tw_text, str) and tw_text.strip():
        inner = _linkify_tweet_text(tw_text.strip())
        return f'<blockquote class="sx-tweet-body">{inner}</blockquote>'
    return (
        f'<blockquote class="sx-tweet-body">'
        f'<em class="sx-no-text">{html.escape(empty_message)}</em>'
        f"</blockquote>"
    )


def _render_sx_handle_cells(
    hkey: str,
    hst: dict[str, Any],
    *,
    now: datetime,
) -> SxHandleRender:
    h_raw = str(hst.get("handle") or hkey)
    handle_display = html.escape(h_raw)
    path_handle = h_raw.lstrip("@")
    uid = html.escape(str(hst.get("user_id") or "—"))
    state = _classify_sx_handle_state(hst)
    badge = _sx_handle_status_badge(state)

    tw_text = hst.get("last_seen_tweet_text")
    tw_at = hst.get("last_seen_tweet_created_at")
    tid_raw = hst.get("last_seen_tweet_id")
    tid_disp = html.escape(str(tid_raw) if tid_raw else "—")
    ticket_analysis = hst.get("last_seen_ticket_analysis")
    ce = html.escape(str(hst.get("consecutive_errors") or 0))
    err = hst.get("last_error_message")

    body = _format_sx_tweet_body_html(
        tw_text if state == _SX_STATE_OK else None,
        empty_message=_sx_empty_message(state),
    )
    tweet_cell = f'<td class="sx-tweet-read-cell">{body}</td>'

    posted_html = _fmt_timestamp_html(str(tw_at) if tw_at else None, now=now)
    posted_cell = f'<td><span class="sx-ts">{posted_html}</span></td>'

    polled_html = _fmt_timestamp_html(
        str(hst.get("last_polled_at") or "") or None,
        now=now,
    )
    polled_cell = f'<td><span class="sx-ts">{polled_html}</span></td>'

    if tid_raw:
        tweet_href = f"https://x.com/{path_handle}/status/{tid_raw}"
        href_e = html.escape(tweet_href, quote=True)
        tid_row = (
            f'<p class="sx-tweet-idline">'
            f'<a class="sx-tweet-link" href="{href_e}" target="_blank" rel="noopener">Open on X</a>'
            f' · id <code class="tweet-snowflake" title="Snowflake id">{tid_disp}</code>'
            f"</p>"
        )
        open_cell = f'<a href="{href_e}" target="_blank" rel="noopener">Open</a>'
    else:
        tid_row = '<p class="sx-tweet-idline">No saved tweet yet.</p>'
        open_cell = "—"

    le_short = "—" if not err else html.escape(str(err).replace("\n", " ")[:100])
    if err and len(str(err)) > 100:
        le_short += "…"
    err_html = (
        f'<p class="sx-err">Last error: {html.escape(str(err))}</p>' if err else ""
    )

    analysis_cell = "—"
    analysis_card = ""
    announces = False
    status_slug = "unknown"
    if isinstance(ticket_analysis, dict):
        status = str(ticket_analysis.get("status") or "unknown")
        status_slug = re.sub(r"[^a-z0-9_-]", "", status.lower()) or "unknown"
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
        variants: tuple[str, ...] = ("pill-ok",) if announces else ("pill-muted",)
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

    row_classes = ["sx-row"]
    card_classes = ["sx-card"]
    if announces:
        row_classes.append("sx-row-ticket-signal")
        card_classes.append("sx-row-ticket-signal")
        row_classes.append(f"sx-status-{status_slug}")
        card_classes.append(f"sx-status-{status_slug}")
    if state == _SX_STATE_ERROR:
        row_classes.append("sx-row-error")

    handle_cell = (
        f'<td class="sx-handle-cell"><strong>@{handle_display}</strong> {badge}</td>'
    )
    row_cls = html.escape(" ".join(row_classes), quote=True)
    card_cls = html.escape(" ".join(card_classes), quote=True)

    table_row = (
        f'<tr class="{row_cls}">{handle_cell}{tweet_cell}{posted_cell}'
        f"<td>{analysis_cell}</td>{polled_cell}<td>{ce}</td>"
        f"<td>{le_short}</td><td>{open_cell}</td></tr>"
    )
    detail_card = (
        f'<article class="{card_cls}" aria-label="X handle {handle_display}">'
        f'<h4 class="sx-handle">@{handle_display} {badge}</h4>'
        f'<p class="sx-meta">user_id <code>{uid}</code> · posted <span class="sx-ts">'
        f"{posted_html}</span> · polled <span class=\"sx-ts\">{polled_html}</span>"
        f" · err streak {ce}</p>"
        f"{tid_row}{analysis_card}{body}{err_html}</article>"
    )
    return SxHandleRender(table_row_html=table_row, detail_card_html=detail_card)


def _render_movie_tweet_embeds(
    movie: dict[str, Any],
    *,
    social_handles: dict[str, Any],
    now: datetime,
) -> str:
    """Embed-style tweet previews for all configured movie X handles."""
    raw_handles = movie.get("x_handles")
    handles = [str(x).lstrip("@") for x in raw_handles if str(x).strip()] if isinstance(raw_handles, list) else []
    movie_key = str(movie.get("key") or "movie")
    if not handles:
        return """
<div class="movie-twitter-panel">
  <p class="movie-twitter-label">X / Twitter</p>
  <p class="tweet-empty">No X handles configured for this movie.</p>
</div>
"""

    timeline: list[tuple[str, dict[str, Any], dict[str, Any] | None, str, str]] = []
    empty_handle_cards: list[str] = []
    for handle in handles:
        hst = _social_state_for_handle(social_handles, handle)
        handle_e = html.escape(handle)
        profile_url = f"https://x.com/{handle}"
        if not hst:
            badge = _sx_handle_status_badge(_SX_STATE_NOT_POLLED)
            body = _format_sx_tweet_body_html(
                None,
                empty_message=_sx_empty_message(_SX_STATE_NOT_POLLED),
            ).replace('class="sx-tweet-body"', 'class="tweet-body"', 1)
            empty_handle_cards.append(
                '<article class="tweet-embed tweet-empty-card tweet-timeline-item" data-tweet-tier="all">'
                f'<p class="tweet-handle">@{handle_e} {badge}</p>'
                f"{body}"
                f'<p class="tweet-actions"><a href="{html.escape(profile_url, quote=True)}" target="_blank" rel="noopener">Open profile</a></p>'
                "</article>"
            )
            continue

        state = _classify_sx_handle_state(hst)
        badge = _sx_handle_status_badge(state)
        tweets = _effective_recent_tweets(hst)
        if not tweets:
            body = _format_sx_tweet_body_html(
                None,
                empty_message=_sx_empty_message(state),
            ).replace('class="sx-tweet-body"', 'class="tweet-body"', 1)
            empty_handle_cards.append(
                f'<article class="tweet-embed tweet-empty-card tweet-timeline-item" data-tweet-tier="all">'
                f'<p class="tweet-handle">@{handle_e} {badge}</p>'
                f"{body}"
                f'<p class="tweet-actions"><a href="{html.escape(profile_url, quote=True)}" target="_blank" rel="noopener">Open profile</a></p>'
                "</article>"
            )
            continue

        for tweet in tweets:
            timeline.append((handle, tweet, hst, state, badge))

    timeline.sort(key=lambda item: _tweet_sort_key(item[1]), reverse=True)
    cards: list[str] = []
    ticket_count = 0
    for handle, tweet, hst, state, badge in timeline:
        if _tweet_announces_tickets(tweet):
            ticket_count += 1
        cards.append(
            _render_tweet_timeline_card(
                handle,
                tweet,
                handle_state=state,
                handle_badge=badge,
                now=now,
                polled=hst.get("last_polled_at") if hst else None,
            )
        )
    cards.extend(empty_handle_cards)
    total = len(cards)
    filter_controls = _render_tweet_filter_controls(total=total, ticket_count=ticket_count)
    panel_key = html.escape(movie_key, quote=True)
    return (
        f'<div class="movie-twitter-panel" data-tweet-filter-panel data-movie-key="{panel_key}">'
        '<p class="movie-twitter-label">X / Twitter</p>'
        f"{filter_controls}"
        f'<div class="tweet-embed-list">{"".join(cards)}</div>'
        "</div>"
    )


def _render_target_controls(target_count: int) -> str:
    """Progressive-enhancement controls; all cards remain visible without JS."""
    if target_count <= 0:
        return ""
    buttons = "".join(
        f'<button type="button" class="target-filter-btn{" is-active" if key == "all" else ""}" '
        f'data-filter="{html.escape(key, quote=True)}">{html.escape(label)}</button>'
        for key, label in (
            ("all", "All"),
            ("error", "Errors"),
            ("stale", "Stale"),
            ("disclosed", "Disclosed"),
            ("signal", "Live"),
            ("routine", "Routine"),
        )
    )
    return f"""
<div class="target-controls" data-target-controls>
  <label class="target-search-label">
    <span class="visually-hidden">Search targets</span>
    <input type="search" id="target-search" placeholder="Search targets, state, schema..." autocomplete="off" />
  </label>
  <div class="target-filter-row" role="group" aria-label="Target filters">
    {buttons}
    <button type="button" class="target-filter-btn" id="compact-toggle" aria-pressed="false">Compact</button>
  </div>
  <p class="target-filter-count" id="target-filter-count" aria-live="polite">{html.escape(str(target_count))} targets shown</p>
</div>
"""


def _render_movie_add_panel(
    config_path: str | None,
    *,
    runtime: dict[str, Any] | None = None,
) -> str:
    runtime = runtime or {}
    config_api_url = _first_nonempty_str(runtime.get("config_api_url"))
    writes_enabled = bool(runtime.get("config_writes_enabled"))
    config_source = _first_nonempty_str(runtime.get("config_source")) or "yaml"
    config_revision = runtime.get("config_revision")

    if writes_enabled:
        disabled = ""
        disabled_hint = ""
        status_bits = [f"Config source: {config_source}"]
        if config_revision is not None:
            status_bits.append(f"revision {config_revision}")
        status_text = " · ".join(status_bits)
    elif config_api_url:
        disabled = " disabled"
        disabled_hint = (
            '<p class="movie-add-status panel-warn">Watchlist is loaded from D1, but dashboard '
            "writes are disabled because <code>CONFIG_ADMIN_TOKEN</code> is not set on this server.</p>"
        )
        status_text = f"Remote watchlist: {html.escape(config_api_url)}"
    elif config_path:
        disabled = ""
        disabled_hint = ""
        status_text = f"Config: {config_path}"
    else:
        disabled = " disabled"
        disabled_hint = (
            '<p class="movie-add-status">Movie add is unavailable because the dashboard has no '
            "writable config path.</p>"
        )
        status_text = ""

    return f"""
<section class="movie-add-panel" aria-label="Add movie from Fandango">
  <div>
    <h3 class="section-label" style="margin:0">Add movie</h3>
    <p class="panel-tagline">Search Fandango, then add the selected movie and overview/IMAX targets to config. <a href="/{_CITYWALK_THEATER_SLUG}/imax-70mm">View future CityWalk IMAX 70MM</a> or <a href="/{_CITYWALK_THEATER_SLUG}/imax">IMAX</a>.</p>
  </div>
  <form class="movie-add-form" id="movie-add-form">
    <input type="search" id="movie-add-query" placeholder="Search Fandango movies..." autocomplete="off"{disabled} />
    <label><input type="checkbox" id="movie-add-imax" checked{disabled} /> add IMAX 70MM target</label>
    <button type="submit" class="target-filter-btn"{disabled}>Search</button>
  </form>
  <div class="movie-add-results" id="movie-add-results"></div>
  <p class="movie-add-status" id="movie-add-status">{html.escape(status_text)}</p>
  {disabled_hint}
</section>
"""


def render_citywalk_format_html(payload: dict[str, Any]) -> str:
    css = dashboard_css()
    ok = bool(payload.get("ok", True))
    error = _first_nonempty_str(payload.get("error"))
    generated = html.escape(str(payload.get("generated_at") or "—"))
    theater = html.escape(str(payload.get("theater_id") or "AAAWX"))
    theater_name_raw = _first_nonempty_str(payload.get("theater_name")) or "Fandango Theater"
    theater_name = html.escape(theater_name_raw)
    theater_slug = html.escape(str(payload.get("theater_slug") or _CITYWALK_THEATER_SLUG), quote=True)
    format_label_raw = _first_nonempty_str(payload.get("format_label"), payload.get("format")) or "Format"
    format_label = html.escape(format_label_raw)
    format_slug = html.escape(str(payload.get("format_slug") or "format"), quote=True)
    source_note = (
        "All movies returned by Fandango's theater calendar; not filtered by your watchlist."
        if payload.get("watchlist_filtered") is False
        else "Fandango theater calendar results."
    )
    dates = payload.get("dates") if isinstance(payload.get("dates"), list) else []
    if error:
        body = f'<p class="hint panel-warn">{html.escape(error)}</p>'
    elif not dates:
        body = f'<p class="hint">No future CityWalk {format_label} showtimes are visible in Fandango calendar data right now.</p>'
    else:
        sections: list[str] = []
        for day in dates:
            if not isinstance(day, dict):
                continue
            date_text = str(day.get("date") or "Date")
            rows: list[str] = []
            for rec in day.get("showtimes") or []:
                if not isinstance(rec, dict):
                    continue
                movie = html.escape(str(rec.get("movie_title") or "Unknown movie"))
                fmt = html.escape(", ".join(str(x) for x in rec.get("format_names") or []))
                time = html.escape(str(rec.get("screen_reader_time") or rec.get("display_time") or "—"))
                ticket_url = _first_nonempty_str(rec.get("ticket_url"))
                buy = (
                    f'<a href="{html.escape(ticket_url, quote=True)}" target="_blank" rel="noopener">Buy/Open</a>'
                    if ticket_url
                    else "—"
                )
                rows.append(
                    '<tr data-imax-row data-search="'
                    + html.escape(
                        " ".join(
                            str(x)
                            for x in (
                                date_text,
                                rec.get("movie_title") or "",
                                rec.get("screen_reader_time") or rec.get("display_time") or "",
                                " ".join(str(x) for x in rec.get("format_names") or []),
                            )
                        ),
                        quote=True,
                    )
                    + '">'
                    f"<td><strong>{movie}</strong></td>"
                    f"<td>{time}</td>"
                    f"<td><code>{fmt}</code></td>"
                    f"<td>{buy}</td>"
                    "</tr>"
                )
            table = render_data_table(
                thead_row="<th>Movie</th><th>Time</th><th>Format</th><th>Fandango</th>",
                tbody_rows_html="".join(rows),
                table_classes=("data-table",),
                caption=f"CityWalk {format_label_raw} showtimes for {day.get('date')}",
            )
            sections.append(
                f'<section class="panel" data-imax-day data-date="{html.escape(date_text, quote=True)}">'
                f'<h2 class="section-label">{html.escape(date_text)}</h2>'
                f"{table}</section>"
            )
        body = "".join(sections)
    status = "Live Fandango query" if ok else "Fandango query failed"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{theater_name} {format_label}</title>
  <style>{css}</style>
</head>
<body>
  <header class="dash-header">
    <p class="eyebrow">Fandango watcher</p>
    <h1 class="dash-title">{theater_name} {format_label}</h1>
    <p class="dash-subtitle">{html.escape(status)} · theater <code>{theater}</code> · generated {generated}</p>
    <p class="dash-subtitle">{html.escape(source_note)}</p>
    <p class="refresh-hint"><a href="/">Back to dashboard</a> · <a href="/api/{theater_slug}/{format_slug}">JSON</a> · <a href="/{theater_slug}/imax-70mm">IMAX 70MM</a> · <a href="/{theater_slug}/imax">IMAX</a></p>
  </header>
  <main class="dash" id="main">
    <section class="movie-add-panel" aria-label="Filter CityWalk {format_label} results">
      <div>
        <h2 class="section-label" style="margin:0">Filter results</h2>
        <p class="panel-tagline">Type a movie, date, or showtime. All future Fandango results remain loaded.</p>
      </div>
      <form class="movie-add-form" id="imax-filter-form">
        <input type="search" id="imax-filter-query" placeholder="Filter by movie, date, or time..." autocomplete="off" />
        <button type="button" class="target-filter-btn" id="imax-filter-clear">Clear</button>
      </form>
      <p class="movie-add-status" id="imax-filter-count" aria-live="polite"></p>
    </section>
    {body}
  </main>
  <script>
(function () {{
  var input = document.getElementById("imax-filter-query");
  var clear = document.getElementById("imax-filter-clear");
  var count = document.getElementById("imax-filter-count");
  var rows = Array.prototype.slice.call(document.querySelectorAll("[data-imax-row]"));
  var days = Array.prototype.slice.call(document.querySelectorAll("[data-imax-day]"));
  function apply() {{
    var q = (input && input.value || "").toLowerCase().trim();
    var shown = 0;
    rows.forEach(function (row) {{
      var text = (row.getAttribute("data-search") || "").toLowerCase();
      var visible = !q || text.indexOf(q) !== -1;
      row.classList.toggle("is-hidden", !visible);
      if (visible) shown += 1;
    }});
    days.forEach(function (day) {{
      var visibleRows = day.querySelectorAll("[data-imax-row]:not(.is-hidden)").length;
      day.classList.toggle("is-hidden", visibleRows === 0);
    }});
    if (count) count.textContent = shown + " of " + rows.length + " showtimes shown";
  }}
  if (input) input.addEventListener("input", apply);
  if (clear) clear.addEventListener("click", function () {{
    if (input) input.value = "";
    apply();
    if (input) input.focus();
  }});
  apply();
}})();
  </script>
</body>
</html>
"""


def render_citywalk_imax70mm_html(payload: dict[str, Any]) -> str:
    """Backward-compatible wrapper for the original fixed-format route."""

    return render_citywalk_format_html(payload)


def _render_operator_status_strip(
    *,
    targets: list[Any],
    fandango_poll: dict[str, Any],
    purchase_mode: str,
    purchase_enabled: bool,
    last_tick_pt: str,
    now: datetime,
    show_purchase: bool,
    social_x_handles: dict[str, Any] | None = None,
    social_x_enabled: bool = False,
) -> str:
    target_dicts = [x for x in targets if isinstance(x, dict)]
    threshold = _stale_threshold_seconds(fandango_poll)
    counts = {"error": 0, "stale": 0, "disclosed": 0, "signal": 0, "routine": 0}
    for t in target_dicts:
        st = t.get("state") or {}
        if not isinstance(st, dict):
            st = {}
        counts[_target_filter_tier(st, now=now, stale_threshold_sec=threshold)] += 1
    handles = social_x_handles if isinstance(social_x_handles, dict) else {}
    sx_summary = _summarize_social_x(handles, enabled=social_x_enabled)
    sx_line = html.escape(_fmt_social_x_ops_summary(sx_summary, enabled=social_x_enabled))
    attention = counts["error"] + counts["stale"] + sx_summary["errors"]
    purchase_label = purchase_mode if purchase_enabled else f"{purchase_mode} (disabled)"
    purchase_href = "#purchase" if show_purchase else "#runtime"
    return f"""
<section class="ops-strip" aria-label="Operator status">
  <a href="#triage"><strong>Needs attention</strong><span>{html.escape(str(attention))}</span></a>
  <a href="#crawl"><strong>Fandango</strong><span>{html.escape(str(counts['stale']))} stale · {html.escape(str(counts['error']))} errors</span></a>
  <a href="#crawl" data-filter-shortcut="signal"><strong>Signals</strong><span>{html.escape(str(counts['signal']))} targets</span></a>
  <a href="{purchase_href}"><strong>Purchase</strong><span><code>{html.escape(purchase_label)}</code></span></a>
  <a href="#x"><strong>X</strong><span>{sx_line}</span></a>
  <span title="{html.escape(last_tick_pt or "—", quote=True)}"><strong>Last tick</strong><span>{html.escape(last_tick_pt or "—")}</span></span>
</section>
"""


def _dashboard_ui_script() -> str:
    """Client-side sugar only: filters, compact mode, disclosure state, media preview."""
    return """
  <script>
(function () {
  var STORAGE_KEY = "fandangoWatcher.dashboard.ui";
  var cards = Array.prototype.slice.call(document.querySelectorAll("[data-target-card]"));
  var search = document.getElementById("target-search");
  var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-filter]"));
  var compact = document.getElementById("compact-toggle");
  var count = document.getElementById("target-filter-count");
  var viewer = document.getElementById("artifact-viewer");
  var details = Array.prototype.slice.call(document.querySelectorAll("details[data-persist-key]"));
  var state = {};
  try {
    state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch (e) {
    state = {};
  }
  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) {}
  }
  function setButtonState(filter) {
    buttons.forEach(function (btn) {
      var active = btn.getAttribute("data-filter") === filter;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
  function apply() {
    var q = (search && search.value || "").toLowerCase().trim();
    var filter = state.filter || "all";
    var shown = 0;
    cards.forEach(function (card) {
      var haystack = (card.getAttribute("data-search") || "").toLowerCase();
      var tier = card.getAttribute("data-tier") || "routine";
      var matchesText = !q || haystack.indexOf(q) !== -1;
      var matchesFilter = filter === "all" || tier === filter;
      var visible = matchesText && matchesFilter;
      card.classList.toggle("is-hidden", !visible);
      if (visible) shown += 1;
    });
    setButtonState(filter);
    if (count) {
      count.textContent = shown + " of " + cards.length + " targets shown";
    }
  }
  if (search) {
    search.value = state.query || "";
    search.addEventListener("input", function () {
      state.query = search.value;
      save();
      apply();
    });
  }
  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.filter = btn.getAttribute("data-filter") || "all";
      save();
      apply();
      if (search) search.focus();
    });
  });
  Array.prototype.slice.call(document.querySelectorAll("[data-filter-shortcut]")).forEach(function (link) {
    link.addEventListener("click", function () {
      state.filter = link.getAttribute("data-filter-shortcut") || "all";
      save();
      apply();
    });
  });
  if (compact) {
    if (state.compact) {
      document.documentElement.classList.add("compact");
      compact.setAttribute("aria-pressed", "true");
    }
    compact.addEventListener("click", function () {
      state.compact = !state.compact;
      document.documentElement.classList.toggle("compact", !!state.compact);
      compact.setAttribute("aria-pressed", state.compact ? "true" : "false");
      save();
    });
  }
  function setMovieView(mode) {
    var posters = mode === "posters";
    state.movieView = posters ? "posters" : "cards";
    document.documentElement.classList.toggle("movie-view-posters", posters);
    Array.prototype.slice.call(document.querySelectorAll(".shelf-view-toggle [data-movie-view]")).forEach(function (btn) {
      var view = btn.getAttribute("data-movie-view") || "cards";
      var active = view === (posters ? "posters" : "cards");
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    save();
  }
  Array.prototype.slice.call(document.querySelectorAll(".shelf-view-toggle [data-movie-view]")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      setMovieView(btn.getAttribute("data-movie-view") || "cards");
    });
  });
  if (state.movieView === "posters") {
    setMovieView("posters");
  }
  Array.prototype.slice.call(document.querySelectorAll("[data-movie-jump]")).forEach(function (link) {
    link.addEventListener("click", function (e) {
      if (!document.documentElement.classList.contains("movie-view-posters")) return;
      e.preventDefault();
      var id = link.getAttribute("data-movie-jump");
      setMovieView("cards");
      var el = id ? document.getElementById("movie-" + id) : null;
      if (el && el.scrollIntoView) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
  details.forEach(function (el) {
    var key = el.getAttribute("data-persist-key");
    if (!key) return;
    if (state.details && Object.prototype.hasOwnProperty.call(state.details, key)) {
      el.open = !!state.details[key];
    }
    el.addEventListener("toggle", function () {
      state.details = state.details || {};
      state.details[key] = el.open;
      save();
    });
  });
  function closeViewer() {
    if (!viewer) return;
    viewer.hidden = true;
    viewer.innerHTML = "";
  }
  Array.prototype.slice.call(document.querySelectorAll("[data-artifact-src]")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (!viewer) return;
      var src = btn.getAttribute("data-artifact-src") || "";
      var kind = btn.getAttribute("data-artifact-kind") || "screenshot";
      var title = btn.getAttribute("data-artifact-title") || "Artifact preview";
      viewer.innerHTML = "";
      var card = document.createElement("div");
      card.className = "artifact-viewer-card";
      var close = document.createElement("button");
      close.type = "button";
      close.className = "artifact-close artifact-close-icon";
      close.textContent = "\u00D7";
      close.setAttribute("aria-label", "Close preview");
      var heading = document.createElement("h2");
      heading.textContent = title;
      var frame = document.createElement("figure");
      frame.className = "media-frame media-frame--lightbox";
      var media = document.createElement(kind === "video" ? "video" : "img");
      media.setAttribute("src", src);
      if (kind === "video") {
        media.setAttribute("controls", "controls");
        media.setAttribute("autoplay", "autoplay");
      } else {
        media.setAttribute("alt", title);
      }
      frame.appendChild(media);
      var linkP = document.createElement("p");
      var link = document.createElement("a");
      link.href = src;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Open artifact in new tab";
      linkP.appendChild(link);
      card.appendChild(close);
      card.appendChild(heading);
      card.appendChild(frame);
      card.appendChild(linkP);
      viewer.appendChild(card);
      viewer.hidden = false;
      if (close) close.focus();
    });
  });
  if (viewer) {
    viewer.addEventListener("click", function (event) {
      if (event.target === viewer || (event.target && event.target.classList && (event.target.classList.contains("artifact-close") || event.target.classList.contains("artifact-close-icon")))) {
        closeViewer();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !viewer.hidden) closeViewer();
    });
  }
  var addForm = document.getElementById("movie-add-form");
  var addQuery = document.getElementById("movie-add-query");
  var addImax = document.getElementById("movie-add-imax");
  var addResults = document.getElementById("movie-add-results");
  var addStatus = document.getElementById("movie-add-status");
  function setAddStatus(text) {
    if (addStatus) addStatus.textContent = text || "";
  }
  function buildPosterFrame(movie) {
    var fig = document.createElement("figure");
    fig.className = "media-frame media-frame--thumb";
    if (movie.poster_url) {
      var img = document.createElement("img");
      img.alt = movie.title || "Fandango movie poster";
      img.src = movie.poster_url;
      img.loading = "lazy";
      fig.appendChild(img);
    } else {
      var fb = document.createElement("div");
      fb.className = "poster-fallback";
      fb.setAttribute("aria-label", "No poster available");
      var t = (movie.title || "?").trim();
      fb.textContent = t ? t.charAt(0).toUpperCase() : "?";
      fig.appendChild(fb);
    }
    return fig;
  }
  function renderAddResults(results) {
    if (!addResults) return;
    addResults.innerHTML = "";
    if (!results.length) {
      setAddStatus("No Fandango movie results found.");
      return;
    }
    results.slice(0, 8).forEach(function (movie) {
      var row = document.createElement("article");
      row.className = "movie-add-result";
      var body = document.createElement("div");
      var title = document.createElement("p");
      title.className = "movie-add-title";
      title.textContent = movie.title || "Untitled movie";
      var meta = document.createElement("p");
      meta.className = "movie-add-meta";
      meta.textContent = [movie.release_date_text, movie.rating, movie.genres].filter(Boolean).join(" · ");
      var action = document.createElement("button");
      action.type = "button";
      action.className = "target-filter-btn";
      action.textContent = "Add";
      action.addEventListener("click", function () {
        action.disabled = true;
        setAddStatus("Adding " + (movie.title || "movie") + "...");
        fetch("/api/movies/add", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(Object.assign({}, movie, {
            include_imax_70mm: !!(addImax && addImax.checked)
          }))
        }).then(function (resp) {
          return resp.json().then(function (data) {
            if (!resp.ok || data.ok === false) throw new Error(data.error || "Add failed");
            return data;
          });
        }).then(function (data) {
          var names = (data.targets || []).map(function (t) { return t.name; }).join(", ");
          setAddStatus("Added " + data.movie.title + " (" + names + "). " + (data.restart_watch_required ? "Restart watch/dashboard for the poll loop to use new targets." : "Watch loop will reload automatically."));
          setTimeout(function () { window.location.reload(); }, 1200);
        }).catch(function (err) {
          action.disabled = false;
          setAddStatus(err && err.message ? err.message : "Add failed");
        });
      });
      body.appendChild(title);
      body.appendChild(meta);
      row.appendChild(buildPosterFrame(movie));
      row.appendChild(body);
      row.appendChild(action);
      addResults.appendChild(row);
    });
    setAddStatus(results.length + " result(s). Choose the movie to add.");
  }
  if (addForm && addQuery) {
    addForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var q = (addQuery.value || "").trim();
      if (!q) {
        setAddStatus("Enter a movie title first.");
        return;
      }
      setAddStatus("Searching Fandango...");
      if (addResults) addResults.innerHTML = "";
      fetch("/api/fandango/search?q=" + encodeURIComponent(q))
        .then(function (resp) {
          return resp.json().then(function (data) {
            if (!resp.ok || data.ok === false) throw new Error(data.error || "Search failed");
            return data;
          });
        })
        .then(function (data) { renderAddResults(data.results || []); })
        .catch(function (err) {
          setAddStatus(err && err.message ? err.message : "Search failed");
        });
    });
  }
  function renderScheduleList(panel, data) {
    var list = panel.querySelector(".movie-schedule-list");
    var summary = panel.querySelector("[data-pin-summary]");
    if (!list) return;
    list.innerHTML = "";
    var schedule = data.schedule || {};
    var days = schedule.days || [];
    var pins = data.pinned_showtimes || [];
    if (summary) {
      summary.textContent = pins.length
        ? pins.map(function (p) { return p.label || (p.date + " " + p.time_label); }).join("; ")
        : "none";
    }
    if (!days.length) {
      list.hidden = false;
      list.textContent = "No CityWalk showtimes found for this movie.";
      return;
    }
    days.forEach(function (day) {
      var block = document.createElement("div");
      block.className = "movie-schedule-day";
      var title = document.createElement("strong");
      title.textContent = day.date || "Date";
      block.appendChild(title);
      (day.records || []).forEach(function (rec) {
        var row = document.createElement("div");
        row.className = "movie-schedule-row";
        var label = (rec.time_label || "?") + " " + ((rec.format_names || [])[0] || "");
        if (rec.is_buyable) label += " · buyable";
        row.textContent = label;
        var pinBtn = document.createElement("button");
        pinBtn.type = "button";
        pinBtn.className = "btn-ghost btn-pin-showtime";
        pinBtn.textContent = "Pin";
        pinBtn.addEventListener("click", function () {
          var movieKey = panel.getAttribute("data-movie-key");
          var pin = {
            date: rec.date || day.date,
            time_label: rec.time_label,
            format: (rec.normalized_formats || [])[0] || "IMAX_70MM",
            showtime_hash: rec.showtime_hash,
            ticket_url: rec.ticket_url,
            label: (day.date || "") + " " + (rec.time_label || "") + " " + ((rec.format_names || [])[0] || "")
          };
          fetch("/api/movies/" + encodeURIComponent(movieKey) + "/pins", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pinned_showtimes: [pin] })
          })
            .then(function (resp) {
              return resp.json().then(function (body) {
                if (!resp.ok || body.ok === false) throw new Error(body.error || "Pin failed");
                return body;
              });
            })
            .then(function (body) {
              renderScheduleList(panel, { schedule: schedule, pinned_showtimes: body.pinned_showtimes || [pin] });
            })
            .catch(function (err) {
              window.alert(err && err.message ? err.message : "Pin failed");
            });
        });
        row.appendChild(pinBtn);
        block.appendChild(row);
      });
      list.appendChild(block);
    });
    list.hidden = false;
  }
  document.querySelectorAll("[data-movie-schedule-panel]").forEach(function (panel) {
    var loadBtn = panel.querySelector("[data-load-schedule]");
    if (!loadBtn) return;
    loadBtn.addEventListener("click", function () {
      var movieKey = panel.getAttribute("data-movie-key");
      if (!movieKey) return;
      loadBtn.disabled = true;
      loadBtn.textContent = "Loading…";
      fetch("/api/movies/" + encodeURIComponent(movieKey) + "/schedule")
        .then(function (resp) {
          return resp.json().then(function (data) {
            if (!resp.ok || data.ok === false) throw new Error(data.error || "Load failed");
            return data;
          });
        })
        .then(function (data) {
          renderScheduleList(panel, data);
          loadBtn.textContent = "Refresh schedule";
        })
        .catch(function (err) {
          window.alert(err && err.message ? err.message : "Schedule load failed");
          loadBtn.textContent = "Load schedule";
        })
        .finally(function () { loadBtn.disabled = false; });
    });
  });
  document.querySelectorAll(".movie-delete-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var key = btn.getAttribute("data-movie-key");
      if (!key || !window.confirm("Delete movie " + key + " from the watchlist?")) return;
      btn.disabled = true;
      fetch("/api/movies/" + encodeURIComponent(key), { method: "DELETE" })
        .then(function (resp) {
          return resp.json().then(function (data) {
            if (!resp.ok || data.ok === false) throw new Error(data.error || "Delete failed");
            return data;
          });
        })
        .then(function () {
          window.location.reload();
        })
        .catch(function (err) {
          btn.disabled = false;
          window.alert(err && err.message ? err.message : "Delete failed");
        });
    });
  });
  var tweetPanels = Array.prototype.slice.call(document.querySelectorAll("[data-tweet-filter-panel]"));
  function applyTweetFilters() {
    var filter = state.tweetFilter || "all";
    tweetPanels.forEach(function (panel) {
      var items = Array.prototype.slice.call(panel.querySelectorAll(".tweet-timeline-item"));
      var buttons = Array.prototype.slice.call(panel.querySelectorAll("[data-tweet-filter]"));
      var shown = 0;
      items.forEach(function (item) {
        var tier = item.getAttribute("data-tweet-tier") || "all";
        var visible = filter === "all" || tier === "ticket";
        item.classList.toggle("is-hidden", !visible);
        if (visible) shown += 1;
      });
      buttons.forEach(function (btn) {
        var active = btn.getAttribute("data-tweet-filter") === filter;
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
      });
      var count = panel.querySelector(".tweet-filter-count");
      if (count) {
        var total = items.length;
        var ticketCount = items.filter(function (item) {
          return item.getAttribute("data-tweet-tier") === "ticket";
        }).length;
        count.textContent = shown + " of " + total + " tweet" + (total === 1 ? "" : "s") + " shown · " + ticketCount + " ticket related";
      }
    });
  }
  tweetPanels.forEach(function (panel) {
    Array.prototype.slice.call(panel.querySelectorAll("[data-tweet-filter]")).forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.tweetFilter = btn.getAttribute("data-tweet-filter") || "all";
        save();
        applyTweetFilters();
      });
    });
  });
  applyTweetFilters();
  var movieGroups = Array.prototype.slice.call(document.querySelectorAll("[data-movie-group]"));
  var movieSearch = document.getElementById("movie-search");
  var movieSort = document.getElementById("movie-sort");
  var movieFilterButtons = Array.prototype.slice.call(document.querySelectorAll("[data-movie-filter]"));
  var movieFilterCount = document.getElementById("movie-filter-count");
  var movieStack = document.querySelector(".movie-stack");
  var posterTrack = document.querySelector(".poster-shelf-track");
  function movieMatchesFilter(group, filter) {
    if (!filter || filter === "all") return true;
    var schema = (group.getAttribute("data-movie-schema") || "").toLowerCase();
    if (filter === "live") {
      return schema === "partial_release" || schema === "full_release";
    }
    return schema === filter;
  }
  function compareMovieGroups(a, b, sortKey) {
    if (sortKey === "title-asc" || sortKey === "title-desc") {
      var ta = (a.getAttribute("data-movie-title") || "").toLowerCase();
      var tb = (b.getAttribute("data-movie-title") || "").toLowerCase();
      var cmp = ta.localeCompare(tb);
      return sortKey === "title-desc" ? -cmp : cmp;
    }
    if (sortKey === "schema-desc" || sortKey === "schema-asc") {
      var ra = parseInt(a.getAttribute("data-movie-schema-rank") || "0", 10);
      var rb = parseInt(b.getAttribute("data-movie-schema-rank") || "0", 10);
      var sr = rb - ra;
      if (sr !== 0) return sortKey === "schema-asc" ? -sr : sr;
      return (a.getAttribute("data-movie-title") || "").localeCompare(b.getAttribute("data-movie-title") || "");
    }
    var da = a.getAttribute("data-movie-release-sort") || "9999-12-31";
    var db = b.getAttribute("data-movie-release-sort") || "9999-12-31";
    if (da !== db) {
      return sortKey === "release-desc" ? (db < da ? -1 : 1) : (da < db ? -1 : 1);
    }
    return (a.getAttribute("data-movie-title") || "").localeCompare(b.getAttribute("data-movie-title") || "");
  }
  function setMovieFilterButtons(filter) {
    movieFilterButtons.forEach(function (btn) {
      var active = btn.getAttribute("data-movie-filter") === filter;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
  function applyMovies() {
    if (!movieGroups.length) return;
    var q = (movieSearch && movieSearch.value || "").toLowerCase().trim();
    var filter = state.movieFilter || "all";
    var sortKey = state.movieSort || "release-asc";
    var filtered = movieGroups.filter(function (group) {
      var hay = (group.getAttribute("data-movie-search") || "").toLowerCase();
      return (!q || hay.indexOf(q) !== -1) && movieMatchesFilter(group, filter);
    });
    var sorted = filtered.slice().sort(function (a, b) {
      return compareMovieGroups(a, b, sortKey);
    });
    movieGroups.forEach(function (group) {
      group.classList.add("is-hidden");
    });
    sorted.forEach(function (group) {
      group.classList.remove("is-hidden");
    });
    if (movieStack) {
      sorted.forEach(function (group) {
        movieStack.appendChild(group);
      });
    }
    if (posterTrack) {
      var tiles = Array.prototype.slice.call(posterTrack.querySelectorAll("[data-movie-jump]"));
      var tileByJump = {};
      tiles.forEach(function (tile) {
        var jump = tile.getAttribute("data-movie-jump");
        if (jump) tileByJump[jump] = tile;
      });
      sorted.forEach(function (group) {
        var id = (group.id || "").replace(/^movie-/, "");
        var tile = tileByJump[id];
        if (tile) posterTrack.appendChild(tile);
      });
      tiles.forEach(function (tile) {
        var jump = tile.getAttribute("data-movie-jump") || "";
        var visible = sorted.some(function (group) {
          return (group.id || "") === "movie-" + jump;
        });
        tile.classList.toggle("is-hidden", !visible);
      });
    }
    setMovieFilterButtons(filter);
    if (movieFilterCount) {
      movieFilterCount.textContent = sorted.length + " of " + movieGroups.length + " movies shown";
    }
  }
  if (movieSearch) {
    movieSearch.value = state.movieQuery || "";
    movieSearch.addEventListener("input", function () {
      state.movieQuery = movieSearch.value;
      save();
      applyMovies();
    });
  }
  if (movieSort) {
    movieSort.value = state.movieSort || "release-asc";
    movieSort.addEventListener("change", function () {
      state.movieSort = movieSort.value || "release-asc";
      save();
      applyMovies();
    });
  }
  movieFilterButtons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.movieFilter = btn.getAttribute("data-movie-filter") || "all";
      save();
      applyMovies();
      if (movieSearch) movieSearch.focus();
    });
  });
  applyMovies();
  apply();
})();
  </script>
"""


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
      --media-radius: 16px;
      --media-radius-sm: 12px;
      --media-shadow: 0 10px 30px rgba(0, 0, 0, 0.14);
      --media-letterbox: #0a0a0c;
      --shelf-bg: transparent;
      --shelf-gap: 0.85rem;
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
        --media-letterbox: #000000;
        --media-shadow: 0 14px 40px rgba(0, 0, 0, 0.55);
        --shelf-bg: #111114;
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
    .ops-strip {
      position: sticky;
      top: 0.65rem;
      z-index: 25;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 150px), 1fr));
      gap: 0.4rem;
      padding: 0.45rem;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.78);
      box-shadow: var(--shadow-sm);
      backdrop-filter: blur(16px) saturate(1.18);
      -webkit-backdrop-filter: blur(16px) saturate(1.18);
    }
    .ops-strip a,
    .ops-strip > span {
      min-width: 0;
      padding: 0.55rem 0.65rem;
      border-radius: 13px;
      background: var(--surface2);
      border: 1px solid transparent;
      color: var(--text);
      text-decoration: none;
    }
    .ops-strip a:hover { border-color: rgba(0, 122, 255, 0.25); }
    .ops-strip strong {
      display: block;
      color: var(--muted);
      font-size: 0.65rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }
    .ops-strip span span,
    .ops-strip a span {
      display: block;
      margin-top: 0.1rem;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 0.84rem;
      font-weight: 650;
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
    .section-head--shelf { padding-bottom: 0.15rem; }
    .shelf-head-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 1rem;
      flex-wrap: wrap;
      margin: 0 0 0.35rem;
    }
    .shelf-head-actions {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-wrap: wrap;
    }
    .shelf-view-toggle {
      display: inline-flex;
      gap: 0.28rem;
      padding: 0.18rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface2);
    }
    .shelf-view-btn {
      margin: 0;
      padding: 0.28rem 0.62rem;
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 650;
      cursor: pointer;
    }
    .shelf-view-btn.is-active,
    .shelf-view-btn[aria-pressed="true"] {
      background: var(--surface);
      color: var(--text);
      box-shadow: var(--shadow-sm);
    }
    .watchlist-view { display: flex; flex-direction: column; gap: 0.72rem; }
    .watchlist-controls {
      display: grid;
      gap: 0.65rem;
      padding: 0.75rem;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow-sm);
    }
    .watchlist-controls-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 0.65rem;
    }
    .watchlist-sort-label {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 650;
    }
    .watchlist-sort-label select {
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface2);
      color: var(--text);
      font: inherit;
      font-size: 0.76rem;
      padding: 0.36rem 0.62rem;
    }
    .movie-group.is-hidden { display: none; }
    html:not(.movie-view-posters) .poster-shelf { display: none; }
    html.movie-view-posters .movie-stack { display: none; }
    .poster-shelf-track {
      display: flex;
      gap: 0.72rem;
      overflow-x: auto;
      padding: 0.2rem 0.1rem 0.55rem;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
    }
    .poster-shelf-track::-webkit-scrollbar { height: 5px; }
    .poster-shelf-track::-webkit-scrollbar-thumb {
      background: rgba(127, 127, 127, 0.35);
      border-radius: 999px;
    }
    .poster-shelf-legend {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem 0.65rem;
      margin: 0 0 0.45rem;
      font-size: 0.68rem;
      color: var(--muted);
    }
    .poster-shelf-legend-group {
      display: inline-flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem 0.65rem;
    }
    .poster-shelf-legend-sep {
      color: rgba(127, 127, 127, 0.55);
      font-weight: 700;
      user-select: none;
    }
    .poster-shelf-key {
      display: inline-flex;
      align-items: center;
      gap: 0.32rem;
    }
    .poster-shelf-key--schema::before {
      content: "";
      width: 0.62rem;
      height: 0.62rem;
      border-radius: 999px;
      border: 2px solid currentColor;
      background: transparent;
    }
    .poster-shelf-key--status { color: var(--text); }
    .poster-shelf-key--error .poster-shelf-status-glyph { background: var(--bad); }
    .poster-shelf-key--stale .poster-shelf-status-glyph { background: var(--warn); color: #1d1d1f; }
    .poster-shelf-key--signal .poster-shelf-status-glyph { background: var(--ok); }
    .poster-shelf-key--schema-not-on-sale { color: var(--muted); }
    .poster-shelf-key--schema-disclosed { color: #b8860b; }
    .poster-shelf-key--schema-live { color: var(--ok); }
    .poster-shelf-status-glyph {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 0.95rem;
      height: 0.95rem;
      border-radius: 999px;
      color: #fff;
      font-size: 0.58rem;
      font-weight: 800;
      line-height: 1;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18);
    }
    .poster-shelf-status-glyph--signal { font-size: 0.42rem; }
    .poster-shelf-tile {
      flex: 0 0 92px;
      scroll-snap-align: start;
      display: flex;
      flex-direction: column;
      gap: 0.32rem;
      min-width: 0;
      text-decoration: none;
      color: inherit;
    }
    .poster-shelf-poster-wrap {
      position: relative;
      width: 92px;
      align-self: center;
    }
    .poster-shelf-tile .media-frame--poster {
      width: 92px;
      border-radius: 12px;
      box-shadow: var(--media-shadow);
      outline: 3px solid transparent;
      outline-offset: 2px;
      transition: outline-color 0.18s ease, transform 0.18s ease;
    }
    .poster-shelf-tile:hover .media-frame--poster { transform: translateY(-1px); }
    .poster-shelf-tile--schema-not_on_sale .media-frame--poster,
    .poster-shelf-tile--schema-unknown .media-frame--poster { outline-color: rgba(142, 142, 147, 0.55); }
    .poster-shelf-tile--schema-showtimes_disclosed .media-frame--poster { outline-color: rgba(255, 204, 0, 0.85); }
    .poster-shelf-tile--schema-partial_release .media-frame--poster,
    .poster-shelf-tile--schema-full_release .media-frame--poster { outline-color: var(--ok); }
    .poster-shelf-status-icon {
      position: absolute;
      top: -0.28rem;
      right: -0.28rem;
      z-index: 2;
      pointer-events: none;
    }
    .poster-shelf-status-icon .poster-shelf-status-glyph {
      width: 1.05rem;
      height: 1.05rem;
      font-size: 0.62rem;
      border: 2px solid var(--bg-elevated);
    }
    .poster-shelf-status-icon--signal .poster-shelf-status-glyph { font-size: 0.46rem; }
    .poster-shelf-status-icon--error .poster-shelf-status-glyph { background: var(--bad); }
    .poster-shelf-status-icon--stale .poster-shelf-status-glyph { background: var(--warn); color: #1d1d1f; }
    .poster-shelf-status-icon--signal .poster-shelf-status-glyph { background: var(--ok); }
    .poster-shelf-tile.is-hidden { display: none; }
    .poster-shelf-label {
      display: block;
      color: var(--text);
      font-size: 0.68rem;
      font-weight: 650;
      line-height: 1.25;
      text-align: center;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .poster-shelf-aspect {
      display: flex;
      justify-content: center;
      min-height: 1.1rem;
    }
    .poster-shelf-aspect .aspect-ratio-chip {
      font-size: 0.58rem;
      padding: 0.1rem 0.32rem;
    }
    .aspect-ratio-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.15rem;
      padding: 0.12rem 0.42rem;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 700;
      letter-spacing: 0.01em;
      white-space: nowrap;
      border: 1px solid var(--border);
      background: var(--surface2);
      color: var(--text);
      cursor: help;
    }
    .aspect-ratio-chip--real-imax {
      border-color: rgba(36, 138, 61, 0.35);
      background: rgba(36, 138, 61, 0.1);
      color: var(--ok);
    }
    .aspect-ratio-chip--dmr {
      border-color: rgba(88, 86, 214, 0.28);
      background: rgba(88, 86, 214, 0.08);
      color: var(--accent2);
    }
    .movie-aspect-meta {
      display: inline;
      white-space: nowrap;
    }
    .imax-screen-ref-panel {
      margin: 0.35rem 0 0.55rem;
      padding: 0.72rem 0.85rem;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--bg-elevated);
      box-shadow: var(--shadow-sm);
    }
    .imax-screen-ref-heading {
      margin: 0 0 0.35rem;
      font-size: 0.92rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .imax-screen-ref-lede {
      margin: 0 0 0.55rem;
      max-width: 72ch;
    }
    .imax-screen-ref-panel .media-frame--screenshot {
      max-width: min(520px, 100%);
      margin: 0;
      cursor: zoom-in;
    }
    .imax-screen-ref-panel .imax-screen-ref-thumb {
      width: 100%;
      height: auto;
      border-radius: 12px;
    }
    .imax-screen-ref-panel .media-caption {
      font-size: 0.72rem;
      color: var(--muted);
    }
    .shelf-title {
      margin: 0;
      font-size: clamp(1.35rem, 3vw, 1.85rem);
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--text);
    }
    .shelf-meta {
      margin: 0;
      color: var(--muted);
      font-size: 0.82rem;
      white-space: nowrap;
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
    .grid .card,
    .showings-rail > .card {
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
    .grid .card,
    .showings-rail > .card {
      padding: 1rem;
      display: flex;
      flex-direction: column;
      gap: 0.72rem;
      transition: border-color 0.18s ease, background-color 0.18s ease;
    }
    .grid .card:hover,
    .showings-rail > .card:hover {
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
    .card-topline .schema-badge {
      max-width: min(100%, 18rem);
    }
    .card-link a {
      font-size: 0.86rem;
      font-weight: 600;
    }
    .next-action {
      padding: 0.58rem 0.7rem;
      border-radius: 12px;
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.08);
      color: var(--text);
      font-size: 0.82rem;
    }
    .next-action strong { color: var(--warn); }
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
    .schema-badge {
      display: inline-flex;
      max-width: 100%;
      align-items: center;
      gap: 0.38rem;
      padding: 0.25rem 0.55rem;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface2);
      color: var(--muted);
      font-size: 0.72rem;
      font-weight: 750;
      line-height: 1.2;
      vertical-align: middle;
    }
    .schema-badge code {
      padding: 0;
      background: transparent;
      color: inherit;
      font-size: 0.68rem;
      font-weight: 700;
    }
    .schema-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .schema-hint {
      display: block;
      margin-top: 0.32rem;
      color: var(--muted);
      font-size: 0.76rem;
      line-height: 1.35;
    }
    .card-facts .schema-label {
      white-space: normal;
      overflow: visible;
      text-overflow: unset;
    }
    .card-facts .schema-badge {
      flex-wrap: wrap;
      row-gap: 0.15rem;
    }
    .schema-not-on-sale {
      background: rgba(142, 142, 147, 0.14);
      color: var(--muted);
      border-color: rgba(142, 142, 147, 0.2);
    }
    .schema-showtimes-disclosed {
      background: rgba(255, 204, 0, 0.2);
      color: #b8860b;
      border-color: rgba(255, 204, 0, 0.38);
    }
    .schema-partial-release {
      background: rgba(255, 159, 10, 0.18);
      color: var(--warn);
      border-color: rgba(255, 159, 10, 0.32);
    }
    .schema-full-release {
      background: rgba(52, 199, 89, 0.16);
      color: var(--ok);
      border-color: rgba(52, 199, 89, 0.28);
    }
    .schema-unknown {
      background: rgba(255, 69, 58, 0.12);
      color: var(--bad);
      border-color: rgba(255, 69, 58, 0.22);
    }
    .card-kind { margin: 0 0 0.15rem 0; }
    .target-controls {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) auto;
      align-items: center;
      gap: 0.65rem;
      padding: 0.75rem;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow-sm);
    }
    .target-search-label input {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.55rem 0.8rem;
      background: var(--surface2);
      color: var(--text);
      font: inherit;
      outline: none;
    }
    .target-search-label input:focus {
      border-color: rgba(0, 122, 255, 0.45);
      box-shadow: 0 0 0 3px var(--accent-dim);
    }
    .target-filter-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 0.35rem;
    }
    .target-filter-btn,
    .artifact-open,
    .artifact-close {
      appearance: none;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--surface2);
      color: var(--text);
      cursor: pointer;
      font: inherit;
      font-size: 0.76rem;
      font-weight: 650;
      padding: 0.36rem 0.62rem;
    }
    .target-filter-btn:hover,
    .artifact-open:hover,
    .artifact-close:hover { border-color: rgba(0, 122, 255, 0.35); }
    .target-filter-btn.is-active,
    #compact-toggle[aria-pressed="true"] {
      color: var(--accent);
      border-color: rgba(0, 122, 255, 0.35);
      background: var(--accent-dim);
    }
    .target-filter-count {
      grid-column: 1 / -1;
      margin: -0.25rem 0 0;
      color: var(--muted);
      font-size: 0.76rem;
    }
    .movie-add-panel {
      display: grid;
      gap: 0.65rem;
      padding: 0.85rem;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow-sm);
    }
    .movie-add-form {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: center;
    }
    .movie-add-form input[type="search"] {
      flex: 1 1 240px;
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.56rem 0.82rem;
      background: var(--surface2);
      color: var(--text);
      font: inherit;
      outline: none;
    }
    .movie-add-form input[type="search"]:focus {
      border-color: rgba(0, 122, 255, 0.45);
      box-shadow: 0 0 0 3px var(--accent-dim);
    }
    .movie-add-form label {
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 650;
    }
    .movie-add-results {
      display: grid;
      gap: 0.5rem;
    }
    .movie-add-result {
      display: grid;
      grid-template-columns: 60px minmax(0, 1fr) auto;
      gap: 0.65rem;
      align-items: center;
      padding: 0.55rem;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: var(--surface2);
      transition: border-color 0.15s, background 0.15s;
    }
    .movie-add-result:hover {
      border-color: rgba(0, 122, 255, 0.32);
      background: var(--surface);
    }
    .movie-add-result img {
      width: 52px;
      aspect-ratio: 2 / 3;
      object-fit: cover;
      border-radius: 9px;
      border: 1px solid var(--border);
      background: var(--surface);
    }
    .movie-add-title {
      margin: 0;
      color: var(--text);
      font-weight: 750;
      font-size: 0.95rem;
      letter-spacing: -0.02em;
    }
    .movie-add-meta {
      margin: 0.15rem 0 0;
      color: var(--muted);
      font-size: 0.77rem;
    }
    .movie-add-status {
      margin: 0;
      color: var(--muted);
      font-size: 0.8rem;
    }
    .is-hidden { display: none !important; }
    .artifact-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      margin: 0 0 0.5rem;
    }
    a {
      color: var(--accent);
      text-underline-offset: 3px;
      text-decoration-color: rgba(0, 122, 255, 0.35);
      transition: color 0.15s;
    }
    a:hover { color: var(--accent); text-decoration-color: var(--accent); }
    .media-frame {
      position: relative;
      margin: 0;
      overflow: hidden;
      border-radius: var(--media-radius-sm);
      background: var(--surface2);
      box-shadow: var(--media-shadow);
      border: 1px solid var(--border);
    }
    .media-frame img,
    .media-frame video,
    .media-frame .poster-fallback {
      display: block;
      width: 100%;
      height: 100%;
    }
    .media-frame--poster {
      border-radius: var(--media-radius);
      aspect-ratio: 2 / 3;
    }
    .media-frame--poster img,
    .media-frame--poster .poster-fallback {
      object-fit: cover;
      aspect-ratio: 2 / 3;
    }
    .media-frame--thumb {
      width: 60px;
      aspect-ratio: 2 / 3;
      flex-shrink: 0;
    }
    .media-frame--thumb img,
    .media-frame--thumb .poster-fallback { object-fit: cover; }
    .media-frame--screenshot,
    .media-frame--video {
      max-width: 160px;
      aspect-ratio: 16 / 9;
      background: var(--media-letterbox);
      border-color: rgba(127, 127, 127, 0.22);
    }
    .media-frame--screenshot img { object-fit: contain; }
    .media-frame--video video { object-fit: cover; }
    .media-frame--lightbox {
      border: 0;
      box-shadow: none;
      background: #000;
      border-radius: 12px;
      max-height: 72vh;
    }
    .media-frame--lightbox img,
    .media-frame--lightbox video {
      max-height: 72vh;
      object-fit: contain;
      margin-inline: auto;
    }
    .media-caption {
      margin: 0;
      padding: 0.32rem 0.45rem 0;
      color: var(--muted);
      font-size: 0.64rem;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }
    .card-media-preview .media-caption {
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      z-index: 2;
      padding: 0.35rem 0.45rem;
      background: linear-gradient(transparent, rgba(0, 0, 0, 0.72));
      color: #f5f5f7;
      font-size: 0.62rem;
      pointer-events: none;
    }
    .media-frame.is-clickable { cursor: zoom-in; }
    .media-frame.is-clickable:hover {
      border-color: rgba(0, 122, 255, 0.35);
      box-shadow: 0 0 0 3px var(--accent-dim), var(--media-shadow);
    }
    .media-frame.is-clickable:focus-within {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .media-frame.is-clickable img,
    .media-frame.is-clickable video {
      pointer-events: none;
    }
    .media-hit-target {
      position: absolute;
      inset: 0;
      margin: 0;
      padding: 0;
      border: 0;
      background: transparent;
      cursor: inherit;
      color: transparent;
      font-size: 0;
      line-height: 0;
      z-index: 3;
    }
    .media-play-badge::before {
      content: "";
      position: absolute;
      inset: 0;
      background: rgba(0, 0, 0, 0.22);
      pointer-events: none;
    }
    .media-play-badge::after {
      content: "";
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-40%, -50%);
      width: 0;
      height: 0;
      border-style: solid;
      border-width: 0.55rem 0 0.55rem 0.9rem;
      border-color: transparent transparent transparent rgba(255, 255, 255, 0.92);
      pointer-events: none;
    }
    .card-media-full-wrap .media-frame {
      max-width: 100%;
      aspect-ratio: auto;
    }
    .card-media-full-wrap .media-frame--screenshot img { object-fit: contain; }
    .card-media-preview {
      display: flex;
      flex-wrap: wrap;
      gap: 0.55rem;
      margin-top: 0.15rem;
    }
    .card:has(.card-media-preview) .card-expand .artifact-actions { display: none; }
    .media-shelf {
      display: flex;
      gap: var(--shelf-gap);
      overflow-x: auto;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      background: var(--shelf-bg);
      border-radius: var(--radius);
    }
    .media-shelf--compact {
      padding: 0;
      background: transparent;
      scroll-snap-type: none;
      overflow: visible;
    }
    .media-shelf > .movie-group,
    .media-shelf > .media-frame { scroll-snap-align: start; flex-shrink: 0; }
    .movie-stack {
      display: flex;
      flex-direction: column;
      gap: 0.72rem;
    }
    .movie-group {
      width: 100%;
      margin: 0;
      padding: 0.72rem;
      border: 1px solid var(--border);
      border-radius: 16px;
      background: var(--surface);
      box-shadow: var(--shadow-sm);
    }
    @media (prefers-color-scheme: dark) {
      .movie-group {
        background: #161618;
        border-color: rgba(255, 255, 255, 0.06);
        box-shadow: none;
      }
    }
    .movie-group-head {
      display: flex;
      align-items: center;
      gap: 0.65rem;
      min-width: 0;
      margin-bottom: 0.55rem;
    }
    .movie-group-head .movie-group-poster-stack,
    .movie-group-head .media-frame--poster {
      width: 52px;
      flex-shrink: 0;
    }
    .movie-group-head .media-frame--poster img,
    .movie-group-head .media-frame--poster .poster-fallback {
      aspect-ratio: 2 / 3;
      object-fit: cover;
    }
    .movie-group-meta { min-width: 0; }
    .movie-group-title-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem;
    }
    .movie-group-title-row .schema-badge { font-size: 0.68rem; }
    .movie-group--schema-not_on_sale { border-left: 3px solid rgba(142, 142, 147, 0.45); }
    .movie-group--schema-showtimes_disclosed { border-left: 3px solid rgba(255, 204, 0, 0.85); }
    .movie-group--schema-partial_release { border-left: 3px solid rgba(255, 159, 10, 0.75); }
    .movie-group--schema-full_release { border-left: 3px solid rgba(52, 199, 89, 0.75); }
    .movie-group-counts { color: var(--text); font-weight: 650; }
    .movie-schedule-panel {
      margin: 0.75rem 0 0;
      padding: 0.75rem 1rem;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .movie-schedule-day { margin-top: 0.5rem; }
    .movie-schedule-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      font-size: 0.9rem;
      margin-top: 0.25rem;
    }
    .btn-pin-showtime { font-size: 0.8rem; padding: 0.15rem 0.5rem; }
    .movie-group-title {
      margin: 0;
      color: var(--text);
      font-size: 1.02rem;
      font-weight: 700;
      letter-spacing: -0.03em;
      line-height: 1.15;
    }
    .movie-group-eyebrow {
      margin: 0.14rem 0 0;
      color: var(--muted);
      font-size: 0.74rem;
      line-height: 1.35;
    }
    .movie-group-eyebrow .movie-release-date {
      display: inline;
      margin: 0;
      color: var(--accent);
      font-size: inherit;
      font-weight: 700;
    }
    .showings-rail {
      display: flex;
      gap: 0.62rem;
      overflow-x: auto;
      padding: 0.1rem 0.05rem 0.45rem;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
    }
    .showings-rail::-webkit-scrollbar { height: 5px; }
    .showings-rail::-webkit-scrollbar-thumb {
      background: rgba(127, 127, 127, 0.35);
      border-radius: 999px;
    }
    .showings-rail > .card {
      flex: 0 0 min(72vw, 210px);
      scroll-snap-align: start;
      padding: 0.72rem;
      gap: 0.52rem;
    }
    .card--mini {
      gap: 0.42rem;
      padding: 0.62rem;
    }
    .card--mini .card-head-mini {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 0.45rem;
      min-width: 0;
    }
    .card--mini h2 {
      margin: 0;
      font-size: 0.9rem;
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .card--mini .card-status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.28rem;
      align-items: center;
    }
    .card--mini .card-status-row .schema-badge {
      max-width: 100%;
    }
    .card-status-stats {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.32rem;
      margin: 0;
    }
    .card-status-stats div {
      min-width: 0;
      padding: 0.38rem 0.42rem;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface2);
    }
    .card-status-stats dt {
      margin: 0 0 0.08rem;
      color: var(--muted);
      font-size: 0.58rem;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .card-status-stats dd {
      margin: 0;
      min-width: 0;
      color: var(--text);
      font-size: 0.72rem;
      font-weight: 650;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .card-status-stats dd code {
      font-size: 0.68rem;
      background: transparent;
      padding: 0;
    }
    .card-alert {
      margin: 0;
      padding: 0.38rem 0.48rem;
      border-radius: 10px;
      font-size: 0.72rem;
      line-height: 1.35;
    }
    .card-alert--warn {
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.1);
      color: var(--text);
    }
    .card-alert--error {
      border: 1px solid rgba(255, 69, 58, 0.28);
      background: rgba(255, 69, 58, 0.1);
      color: var(--text);
    }
    .card-quick-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.32rem;
      margin: 0;
      align-items: center;
    }
    .card-open-link {
      font-size: 0.72rem;
      font-weight: 650;
      text-decoration: none;
    }
    .card-quick-btn {
      margin: 0;
      padding: 0.22rem 0.48rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface2);
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 650;
      cursor: pointer;
    }
    .card-quick-btn:hover {
      border-color: rgba(0, 122, 255, 0.35);
      color: var(--accent);
    }
    .card--mini .card-expand summary {
      font-size: 0.72rem;
      color: var(--muted);
    }
    .showings-rail .card h2 {
      font-size: 0.94rem;
      line-height: 1.2;
    }
    .showings-rail .card-topline { gap: 0.28rem; }
    .showings-rail .card-topline .schema-badge { max-width: 100%; }
    .showings-rail .card-link a { font-size: 0.78rem; }
    .showings-rail .next-action {
      padding: 0.45rem 0.55rem;
      font-size: 0.74rem;
      border-radius: 10px;
    }
    .showings-rail .card-facts {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 0.35rem;
    }
    .showings-rail .card-facts div { padding: 0.42rem; }
    .showings-rail .card-facts dt { font-size: 0.62rem; }
    .showings-rail .card-facts dd { font-size: 0.74rem; }
    .showings-rail .card-media-preview { gap: 0.4rem; margin-top: 0; }
    .showings-rail .card-media-preview .media-frame {
      max-width: 108px;
    }
    .showings-rail .card-media-preview .media-caption {
      font-size: 0.58rem;
      padding: 0.28rem 0.35rem;
    }
    @media (prefers-color-scheme: dark) {
      .media-frame--poster { border-color: transparent; }
    }
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
    .purchase-timeline {
      list-style: none;
      margin: 0.65rem 0 0.8rem 0;
      padding: 0;
      display: grid;
      gap: 0.55rem;
    }
    .purchase-event {
      padding: 0.75rem 0.82rem;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: var(--surface2);
    }
    .purchase-event-top {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      margin-bottom: 0.18rem;
    }
    .purchase-chip {
      display: inline-block;
      padding: 0.18rem 0.52rem;
      border-radius: 999px;
      background: var(--surface);
      color: var(--muted);
      border: 1px solid var(--border);
      font-size: 0.68rem;
      font-weight: 750;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .purchase-chip-ok { color: var(--ok); background: rgba(52, 199, 89, 0.14); border-color: transparent; }
    .purchase-chip-warn { color: var(--warn); background: rgba(255, 159, 10, 0.16); border-color: transparent; }
    .purchase-chip-bad { color: var(--bad); background: rgba(255, 69, 58, 0.14); border-color: transparent; }
    .purchase-event-meta,
    .purchase-event-note {
      color: var(--muted);
      font-size: 0.8rem;
      margin: 0.18rem 0 0;
    }
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
    p.refresh-hint { margin: 0 0 0.65rem 0; font-size: 0.78rem; opacity: 0.92; overflow-wrap: anywhere; }
    p.refresh-hint code { white-space: nowrap; }
    .artifact-viewer[hidden] { display: none; }
    .artifact-viewer {
      position: fixed;
      inset: 0;
      z-index: 100;
      display: grid;
      place-items: center;
      padding: 1rem;
      background: rgba(0, 0, 0, 0.72);
      backdrop-filter: blur(28px) saturate(1.5);
      -webkit-backdrop-filter: blur(28px) saturate(1.5);
    }
    .artifact-viewer-card {
      width: min(980px, 100%);
      max-height: 92vh;
      overflow: auto;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: var(--radius);
      background: #0b0b0f;
      color: var(--text);
      box-shadow: var(--shadow);
      padding: 0.65rem 0.75rem 0.85rem;
      position: relative;
    }
    .artifact-viewer-card h2 {
      margin: 0.2rem 0 0.75rem;
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--muted);
    }
    .artifact-close-icon {
      position: absolute;
      top: 0.65rem;
      right: 0.65rem;
      z-index: 2;
      width: 2rem;
      height: 2rem;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.12);
      color: #f5f5f7;
      border: 0;
      font-size: 1.35rem;
      line-height: 1;
      cursor: pointer;
    }
    .artifact-viewer-card img,
    .artifact-viewer-card video {
      display: block;
      max-width: 100%;
      max-height: 72vh;
      margin-inline: auto;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: #000;
    }
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
    .advanced-fold {
      margin-top: 0.25rem;
    }
    .advanced-fold > summary {
      justify-content: flex-start;
    }
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
    .movie-carousel {
      display: flex;
      gap: 1rem;
      overflow-x: auto;
      padding: 0.5rem 0.1rem 0.85rem;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
    }
    .movie-carousel.media-shelf { scroll-snap-type: x mandatory; }
    .movie-carousel::-webkit-scrollbar { height: 6px; }
    .movie-carousel::-webkit-scrollbar-thumb {
      background: rgba(127, 127, 127, 0.35);
      border-radius: 999px;
    }
    /* Legacy alias — watchlist now uses .movie-stack + .showings-rail */
    .movie-group-poster,
    .target-poster {
      border: 0;
      box-shadow: none;
      background: transparent;
    }
    .poster-fallback {
      display: grid;
      place-items: center;
      font-size: clamp(1.8rem, 5vw, 2.4rem);
      font-weight: 750;
      background: linear-gradient(160deg, #2a2a2e 0%, #141416 55%, rgba(10, 132, 255, 0.18) 100%);
    }
    .movie-group-body { min-width: 0; }
    .movie-twitter-panel {
      margin-top: 0.65rem;
      padding-top: 0.65rem;
      border-top: 1px solid var(--border);
    }
    .movie-twitter-label {
      margin: 0 0 0.45rem;
      color: var(--muted);
      font-size: 0.68rem;
      font-weight: 750;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .tweet-embed-list {
      display: grid;
      gap: 0.55rem;
    }
    .tweet-filter-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin: 0 0 0.45rem;
    }
    .tweet-filter-count {
      margin: 0 0 0.55rem;
      color: var(--muted);
      font-size: 0.76rem;
    }
    .tweet-timeline-item.is-hidden { display: none; }
    .tweet-embed {
      padding: 0.72rem 0.78rem;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: var(--surface2);
    }
    .tweet-handle {
      margin: 0 0 0.35rem;
      color: var(--text);
      font-size: 0.86rem;
      font-weight: 750;
    }
    .tweet-body {
      margin: 0;
      color: var(--text);
      font-size: 0.86rem;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .tweet-meta,
    .tweet-actions,
    .tweet-empty,
    .tweet-more {
      margin: 0.42rem 0 0;
      color: var(--muted);
      font-size: 0.76rem;
    }
    .tweet-analysis { margin: 0.45rem 0 0; }
    .tweet-actions a { font-weight: 700; }
    .panel-warn {
      border-radius: 12px;
      border-left: 3px solid rgba(255, 159, 10, 0.45);
      padding: 0.72rem 0.85rem;
      border: 1px solid rgba(255, 159, 10, 0.28);
      background: rgba(255, 159, 10, 0.08);
    }
    .conn-line { font-size: 0.8rem; margin: 0.5rem 0 0 0; }
    .conn-line code { white-space: nowrap; }
    .conn-label { color: var(--muted); }
    .conn-ok { color: var(--ok); }
    .conn-bad { color: var(--bad); }
    .conn-static { color: var(--muted); overflow-x: auto; white-space: nowrap; }
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
    .sx-tweet-preview-cell,
    .sx-tweet-read-cell {
      min-width: 18rem;
      max-width: 42rem;
      font-size: 0.86rem;
      color: var(--text);
      line-height: 1.45;
      vertical-align: top;
      word-break: break-word;
    }
    .sx-tweet-read-cell .sx-tweet-body {
      margin: 0;
      font-size: 0.86rem;
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
    .sx-tweet-body a.sx-tweet-link-inline,
    .tweet-body a.sx-tweet-link-inline {
      color: var(--accent);
      text-decoration: underline;
      text-decoration-color: rgba(0, 122, 255, 0.35);
      word-break: break-all;
    }
    .sx-ts { font-size: 0.82rem; color: var(--text); }
    .sx-ts .rel { color: var(--muted); font-size: 0.78rem; font-weight: 450; }
    .sx-handle-cell {
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      align-items: center;
      vertical-align: top;
    }
    tr.sx-row-ticket-signal {
      background: rgba(52, 199, 89, 0.06);
    }
    tr.sx-row-ticket-signal.sx-status-soon {
      background: rgba(255, 159, 10, 0.08);
    }
    tr.sx-row-error {
      background: rgba(215, 0, 21, 0.04);
    }
    article.sx-card.sx-row-ticket-signal,
    article.tweet-embed.sx-ticket-signal {
      border-color: rgba(52, 199, 89, 0.35);
      box-shadow: inset 3px 0 0 var(--ok);
    }
    article.sx-card.sx-status-soon,
    article.tweet-embed.sx-status-soon {
      border-color: rgba(255, 159, 10, 0.35);
      box-shadow: inset 3px 0 0 var(--warn);
    }
    .sx-err { margin: 0.5rem 0 0 0; }
    .inline-fold {
      margin-top: 0.65rem;
      border-top: 1px solid var(--border);
      padding-top: 0.55rem;
    }
    .compact body { font-size: 0.88rem; }
    .compact main.dash { gap: 0.72rem; }
    .compact .grid { gap: 0.55rem; }
    .compact .grid .card { gap: 0.45rem; padding: 0.75rem; }
    .compact .card-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .compact .panel-tagline,
    .compact .card-media-meta { display: none; }
    .compact .card-media-preview .media-caption { display: none; }
    .compact .card-media-preview .media-frame { max-width: 112px; }
    .compact .showings-rail > .card { padding: 0.62rem; gap: 0.42rem; }
    .compact .card--mini { padding: 0.52rem; gap: 0.35rem; }
    .compact .card-status-stats dd { font-size: 0.68rem; }
    .compact .showings-rail .card-media-preview .media-frame { max-width: 96px; }
    @media (prefers-color-scheme: dark) {
      .jump-nav,
      .ops-strip { background: rgba(28, 28, 30, 0.72); }
    }
    @media (max-width: 700px) {
      table.data-table { font-size: 0.78rem; }
      body { padding-inline: 0.85rem; }
      header.dash-header { padding-top: 1.45rem; }
      h1.dash-title { font-size: 2.15rem; }
      .ops-strip {
        position: static;
        grid-template-columns: 1fr 1fr;
      }
      .target-controls { grid-template-columns: 1fr; }
      .target-filter-row { justify-content: flex-start; }
      .movie-stack { gap: 0.62rem; }
      .movie-group { padding: 0.65rem; border-radius: 14px; }
      .movie-group-head .movie-group-poster-stack,
      .movie-group-head .media-frame--poster { width: 46px; }
      .showings-rail {
        margin-inline: -0.35rem;
        padding-inline: 0.35rem;
        scroll-padding-inline: 0.35rem;
      }
      .showings-rail > .card { flex-basis: min(84vw, 240px); }
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
    persist_key: str | None = None,
) -> str:
    open_attr = " open" if open_ else ""
    ce = html.escape(css_class.strip(), quote=True)
    persist_attr = (
        f' data-persist-key="{html.escape(persist_key, quote=True)}"'
        if persist_key
        else ""
    )
    return (
        f'<details class="{ce}"{persist_attr}{open_attr}>'
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


def _render_card_status_stats(
    *,
    last_ok: str,
    ticks: str,
    api_status: str,
    last_ok_title: str | None = None,
) -> str:
    title_attr = (
        f' title="{html.escape(last_ok_title, quote=True)}"' if last_ok_title else ""
    )
    return (
        '<dl class="card-status-stats">'
        f'<div><dt>Last OK</dt><dd{title_attr}>{last_ok}</dd></div>'
        f"<div><dt>Ticks</dt><dd>{ticks}</dd></div>"
        f'<div><dt>API</dt><dd><code>{api_status}</code></dd></div>'
        "</dl>"
    )


def _render_card_alert_line(
    *,
    err_msg: str | None,
    is_stale: bool,
    stale_rel: str | None,
    next_action: str | None,
    tier: str,
) -> str:
    if err_msg:
        em = err_msg.replace("\n", " ").strip()
        if len(em) > 120:
            em = em[:117] + "…"
        return f'<p class="card-alert card-alert--error">{html.escape(em)}</p>'
    if is_stale and stale_rel:
        return (
            f'<p class="card-alert card-alert--warn">'
            f"Stale crawl · last OK {html.escape(stale_rel)} ago</p>"
        )
    if next_action and tier in ("error", "stale", "signal"):
        return f'<p class="card-alert card-alert--warn">{html.escape(next_action)}</p>'
    return ""


def _render_card_quick_actions(
    *,
    url_e: str,
    name_attr: str,
    screenshot_url: str | None,
    video_url: str | None,
) -> str:
    parts: list[str] = [
        f'<a class="card-open-link" href="{url_e}" target="_blank" rel="noopener">Open</a>'
    ]
    if screenshot_url:
        parts.append(
            '<button type="button" class="artifact-open card-quick-btn" '
            'data-artifact-kind="screenshot" '
            f'data-artifact-src="{html.escape(screenshot_url, quote=True)}" '
            f'data-artifact-title="Screenshot for {name_attr}">Screenshot</button>'
        )
    if video_url:
        parts.append(
            '<button type="button" class="artifact-open card-quick-btn" '
            'data-artifact-kind="video" '
            f'data-artifact-src="{html.escape(video_url, quote=True)}" '
            f'data-artifact-title="Video for {name_attr}">Video</button>'
        )
    return f'<p class="card-quick-actions">{"".join(parts)}</p>'


def _render_target_card(
    t: dict[str, Any],
    *,
    fandango_poll: dict[str, Any],
    now: datetime,
    layout: Literal["full", "mini"] = "mini",
) -> str:
    raw_name = str(t.get("name", ""))
    name = html.escape(raw_name)
    name_attr = html.escape(raw_name, quote=True)
    url = str(t.get("url") or "")
    url_e = html.escape(url, quote=True)
    st = t.get("state") or {}
    if not isinstance(st, dict):
        st = {}
    schema_raw = st.get("last_release_schema")
    schema_badge = _schema_badge_html(schema_raw, compact=True)
    schema_fact = _schema_badge_html(schema_raw, compact=True)
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
    is_stale = False
    if su_dt is not None:
        age = int((now.astimezone(UTC) - su_dt.astimezone(UTC)).total_seconds())
        if age > stale_thr:
            is_stale = True
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
    if cur_l == "error" or _as_int(st.get("consecutive_errors")) > 0:
        pill_variants = ("pill-warn",)
    elif "alert" in cur_l or "purchas" in cur_l or "released" in cur_l or "live" in cur_l:
        pill_variants = ("pill-ok",)
    elif "partial" in schema_l or "full" in schema_l:
        pill_variants = ("pill-ok",)
    elif "disclosed" in schema_l:
        pill_variants = ("pill-warn",)
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
            '<div class="card-media-full-wrap">'
            + _media_frame_html(
                variant="screenshot",
                src=str(su_url),
                alt=f"screenshot {name}",
                css_class="card-media-full",
            )
            + "</div>"
        )

    vid_html = ""
    vu = t.get("latest_video_url")
    if vu:
        vid_html = (
            '<div class="card-media-full-wrap">'
            + _media_frame_html(
                variant="video",
                src=str(vu),
                alt=f"crawl video {name}",
                video_controls=True,
                video_preload="metadata",
            )
            + "</div>"
        )

    trace_html = ""
    tz = t.get("latest_trace_url")
    if tz:
        trace_html = f'<p><a href="{html.escape(tz)}">latest trace (.zip)</a></p>'

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
    evidence_raw = st.get("last_schema_evidence") or []
    evidence_list = evidence_raw if isinstance(evidence_raw, list) else []
    evidence_html = ""
    if evidence_list:
        snippet = ", ".join(str(item) for item in evidence_list[-8:])
        evidence_html = (
            f'<p class="card-evidence-meta"><strong>Schema evidence</strong> '
            f"<code>{html.escape(snippet)}</code></p>"
        )
    artifact_actions: list[str] = []
    has_card_preview = bool(su_url or vu)
    if not has_card_preview:
        if su_url:
            artifact_actions.append(
                '<button type="button" class="artifact-open" data-artifact-kind="screenshot" '
                f'data-artifact-src="{html.escape(str(su_url), quote=True)}" '
                f'data-artifact-title="Screenshot for {name_attr}">Preview screenshot</button>'
            )
        if vu:
            artifact_actions.append(
                '<button type="button" class="artifact-open" data-artifact-kind="video" '
                f'data-artifact-src="{html.escape(str(vu), quote=True)}" '
                f'data-artifact-title="Video for {name_attr}">Preview video</button>'
            )
    artifact_actions_html = (
        f'<p class="artifact-actions">{"".join(artifact_actions)}</p>'
        if artifact_actions
        else ""
    )
    media_inner = f"{media_meta_p}{artifact_actions_html}{img_html}{vid_html}{trace_html}"
    next_action = _target_next_action(st, direct_api, is_stale=is_stale)
    next_action_html = (
        f'<p class="next-action"><strong>Next:</strong> {html.escape(next_action)}</p>'
        if next_action
        else ""
    )
    details_inner = f"{err_meta}{err_html}{stale_html}{api_html}{evidence_html}{media_inner}"
    details_block = ""
    if details_inner.strip():
        details_block = render_inline_disclosure(
            css_class="card-expand",
            summary_html="Diagnostics &amp; media",
            inner_html=f'<div class="card-expand-body">{details_inner}</div>',
            persist_key=f"target:{_html_id_slug(raw_name)}:diagnostics",
        )

    facts = render_fact_grid(
        [
            (html.escape("Schema"), schema_fact),
            *_showtime_fact_entries(st),
            (html.escape("Last OK"), f"{su}{rel_html}"),
            (html.escape("Ticks"), tticks),
            (html.escape("Direct API"), f"<code>{html.escape(api_status)}</code>"),
        ]
    )
    tier = _target_filter_tier(st, now=now, stale_threshold_sec=stale_thr)
    search_blob = " ".join(
        str(x)
        for x in (
            raw_name,
            url,
            st.get("current_state") or "",
            st.get("last_release_schema") or "",
            api_status,
            _target_route_label(raw_name),
        )
    )
    data_search = html.escape(search_blob, quote=True)
    state_attr = html.escape(cur_l or "unknown", quote=True)
    tier_attr = html.escape(tier, quote=True)
    schema_attr = html.escape(schema_l or "unknown", quote=True)
    has_media_attr = "true" if (su_url or vu) else "false"

    media_preview_html = _render_card_media_preview(
        name=name,
        name_attr=name_attr,
        screenshot_url=str(su_url) if su_url else None,
        video_url=str(vu) if vu else None,
    )
    media_pill = (
        render_status_pill(html.escape("Media"), variants=("pill-muted",))
        if (su_url or vu)
        else ""
    )

    if layout == "mini":
        last_ok_display = html.escape(rel or su or "—")
        last_ok_title = str(su_raw) if su_raw else None
        status_stats = _render_card_status_stats(
            last_ok=last_ok_display,
            ticks=tticks,
            api_status=html.escape(api_status),
            last_ok_title=last_ok_title,
        )
        alert_html = _render_card_alert_line(
            err_msg=str(err_msg) if err_msg else None,
            is_stale=is_stale,
            stale_rel=rel,
            next_action=next_action,
            tier=tier,
        )
        quick_actions = _render_card_quick_actions(
            url_e=url_e,
            name_attr=name_attr,
            screenshot_url=str(su_url) if su_url else None,
            video_url=str(vu) if vu else None,
        )
        details_inner = (
            f"{facts}{next_action_html}{media_preview_html}"
            f"{err_meta}{err_html}{stale_html}{api_html}{media_inner}"
        )
        details_block = ""
        if details_inner.strip():
            details_block = render_inline_disclosure(
                css_class="card-expand",
                summary_html="Details",
                inner_html=f'<div class="card-expand-body">{details_inner}</div>',
                persist_key=f"target:{_html_id_slug(raw_name)}:diagnostics",
            )
        return f"""
<section class="card card--mini" data-target-card data-target="{name_attr}" data-state="{state_attr}" data-tier="{tier_attr}" data-search="{data_search}" data-has-media="{has_media_attr}">
  <div class="card-head-mini"><h2>{name}</h2></div>
  <div class="card-status-row">{state_pill}{schema_badge}{stale_chip}</div>
  {status_stats}
  {alert_html}
  {quick_actions}
  {details_block}
</section>
"""

    return f"""
<section class="card" data-target-card data-target="{name_attr}" data-state="{state_attr}" data-tier="{tier_attr}" data-schema="{schema_attr}" data-search="{data_search}" data-has-media="{has_media_attr}">
  <div class="card-topline">
    {route_pill}
    {state_pill}
    {schema_badge}
    {stale_chip}
    {media_pill}
  </div>
  <h2>{name}</h2>
  <p class="card-link"><a href="{url_e}" target="_blank" rel="noopener">Open on Fandango</a></p>
  {next_action_html}
  {facts}
  {media_preview_html}
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
    if "disclosed" in sch:
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
        schema_badge = _schema_badge_html(st.get("last_release_schema"), compact=True)
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
            f"<td>{schema_badge}</td>"
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
    social_x_handles: dict[str, Any] | None = None,
    social_x_enabled: bool = False,
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
        if cur_l == "error" or _as_int(st.get("consecutive_errors")) > 0:
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
            f"<strong>{stale_n}</strong> target(s) have a stale <code>last_success_at</code> vs expected poll cadence. "
            'Next: run a one-off crawl or check whether <code>watch</code> is still ticking.'
        )
    if errish > 0:
        attention.append(
            f"<strong>{errish}</strong> target(s) show error state or a consecutive error streak. "
            "Next: inspect latest errors, browser login/session health, and direct API fallback state."
        )

    sx_handles = social_x_handles if isinstance(social_x_handles, dict) else {}
    sx_summary = _summarize_social_x(sx_handles, enabled=social_x_enabled)
    if social_x_enabled and sx_summary["ticket_signals"] > 0:
        n = sx_summary["ticket_signals"]
        attention.append(
            f"<strong>{n}</strong> X handle(s) report ticket availability language — "
            '<a href="#x">review latest tweets</a> and confirm on Fandango before acting.'
        )
    if social_x_enabled and sx_summary["errors"] > 0:
        n = sx_summary["errors"]
        attention.append(
            f"<strong>{n}</strong> X handle(s) have poll errors — "
            '<a href="#x">inspect the X poller panel</a>.'
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

    sx_enabled_label = "enabled" if social_x_enabled else "disabled"
    sx_metrics = (
        "<div><strong>X poller</strong>"
        f"<span>{html.escape(sx_enabled_label)} · "
        f"{sx_summary['ticket_signals']} ticket signal(s) · "
        f"{sx_summary['errors']} error(s)</span></div>"
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
            sx_metrics,
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
    timeline_items: list[str] = []
    for row in reversed(rows):
        at = html.escape(str(row.get("at") or "—"))
        tgt = html.escape(str(row.get("target") or "—"))
        att = row.get("attempt")
        oc_raw = "—"
        err = ""
        if isinstance(att, dict):
            oc_raw = str(att.get("outcome") or "—")
            e_raw = att.get("error")
            if e_raw:
                es = str(e_raw).replace("\n", " ").strip()
                if len(es) > 120:
                    es = es[:117] + "…"
                err = html.escape(es)
        oc = html.escape(oc_raw)
        oc_l = oc_raw.lower()
        chip_cls = "purchase-chip"
        if any(x in oc_l for x in ("ok", "success", "complete", "purchased")):
            chip_cls += " purchase-chip-ok"
            note = "Successful outcome recorded; verify receipt/details if this was a live run."
        elif any(x in oc_l for x in ("fail", "error", "halt")) or err:
            chip_cls += " purchase-chip-bad"
            note = "Review the error and latest purchase artifacts before retrying."
        elif "skip" in oc_l:
            chip_cls += " purchase-chip-warn"
            note = "Skipped before checkout; no purchase was submitted."
        else:
            note = "Attempt recorded; open raw rows for exact fields."
        outcome_slug = html.escape(_html_id_slug(oc_l), quote=True)
        err_cell = f'<span class="purchase-err">{err}</span>' if err else "—"
        pr_rows.append(
            f"<tr><td>{at}</td><td>{tgt}</td><td>{oc}</td><td>{err_cell}</td></tr>"
        )
        timeline_items.append(
            f'<li class="purchase-event purchase-event-{outcome_slug}">'
            '<div class="purchase-event-top">'
            f'<span class="{html.escape(chip_cls, quote=True)}">{oc}</span>'
            f"<strong>{tgt}</strong>"
            "</div>"
            f'<p class="purchase-event-meta">{at}</p>'
            f'<p class="purchase-event-note">{html.escape(note)}</p>'
            "</li>"
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
    timeline = f'<ol class="purchase-timeline">{"".join(timeline_items)}</ol>'
    raw_rows = render_inline_disclosure(
        css_class="inline-fold purchase-raw-fold",
        summary_html="Raw purchase rows",
        inner_html=tbl,
        persist_key="panel:purchase:raw",
    )
    inner = f'<p class="hint meta">{ph_meta}</p>{timeline}{raw_rows}'
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
    config_path = _first_nonempty_str(dash_meta.get("config_path"))
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
    sx_handles = social_x.get("handles") or {}
    if not isinstance(sx_handles, dict):
        sx_handles = {}
    social_x_enabled = bool(social_poll.get("enabled"))

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
        social_x_handles=sx_handles,
        social_x_enabled=social_x_enabled,
    )
    target_count = sum(1 for x in targets if isinstance(x, dict))
    movie_count = sum(
        1
        for m in movies
        if isinstance(m, dict) and isinstance(m.get("fandango_targets"), list)
    )
    shelf_meta = html.escape(f"{movie_count} movie(s) · {target_count} target(s)")
    operator_status = _render_operator_status_strip(
        targets=targets,
        fandango_poll=fandango_poll,
        purchase_mode=purchase_mode_raw,
        purchase_enabled=purchase_enabled,
        last_tick_pt=str(healthz.get("last_tick_at_pt") or "—"),
        now=now,
        show_purchase=show_ph,
        social_x_handles=sx_handles,
        social_x_enabled=social_x_enabled,
    )
    target_controls = _render_target_controls(target_count)
    movie_add_panel = _render_movie_add_panel(
        config_path,
        runtime=runtime if isinstance(runtime, dict) else {},
    )

    assigned: set[str] = set()
    movie_groups: list[dict[str, Any]] = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        ft = m.get("fandango_targets")
        if not isinstance(ft, list):
            continue
        mtitle_raw = str(m.get("title") or m.get("key") or "Movie")
        mkey = str(m.get("key") or "movie")
        mkey_slug = _html_id_slug(mkey)
        movie_target_names: list[str] = []
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
                movie_target_names.append(tname)
        if not subcards:
            continue
        movie_schema = _movie_group_release_schema(
            movie_target_names,
            target_by_name=target_by_name,
        )
        release_sort = _release_date_sort_value(m, target_by_name=target_by_name)
        visible_total, buyable_total = _movie_showtime_totals(
            movie_target_names,
            target_by_name=target_by_name,
        )
        movie_groups.append(
            {
                "movie": m,
                "title_raw": mtitle_raw,
                "key": mkey,
                "key_slug": mkey_slug,
                "subcards": subcards,
                "target_names": movie_target_names,
                "schema": movie_schema,
                "schema_rank": _schema_rank(movie_schema),
                "release_sort": release_sort,
                "title_sort": mtitle_raw.lower(),
                "visible_total": visible_total,
                "buyable_total": buyable_total,
            }
        )

    movie_groups.sort(
        key=lambda row: (
            row["release_sort"] or "9999-12-31",
            row["title_sort"],
        )
    )

    crawl_blocks: list[str] = []
    poster_tiles: list[str] = []
    for group in movie_groups:
        m = group["movie"]
        mtitle_raw = group["title_raw"]
        mtitle = html.escape(mtitle_raw)
        mkey_slug = group["key_slug"]
        movie_schema = group["schema"]
        poster = _poster_url_for_movie(m, target_by_name=target_by_name)
        poster_html = _poster_html(poster, mtitle_raw, css_class="movie-group-poster")
        release_date = html.escape(
            _fmt_release_date(_release_date_for_movie(m, target_by_name=target_by_name))
        )
        distributor = html.escape(_first_nonempty_str(m.get("distributor")) or "Distributor not set")
        tweet_embeds = _render_movie_tweet_embeds(m, social_handles=sx_handles, now=now)
        movie_status = _summarize_targets_status(
            group["target_names"],
            target_by_name=target_by_name,
            fandango_poll=fandango_poll,
            now=now,
        )
        schema_badge = _schema_badge_html(movie_schema, compact=True)
        counts_bits: list[str] = []
        if group["visible_total"] is not None:
            counts_bits.append(f"{group['visible_total']} showtime(s)")
        if group["buyable_total"] is not None:
            counts_bits.append(f"{group['buyable_total']} buyable")
        counts_line = html.escape(" · ".join(counts_bits)) if counts_bits else ""
        counts_html = (
            f'<span class="movie-group-counts">{counts_line}</span> · '
            if counts_line
            else ""
        )
        aspect_chip = _render_aspect_ratio_chip(m, compact=True)
        poster_tiles.append(
            _render_poster_shelf_tile(
                movie_id=mkey_slug,
                title=mtitle_raw,
                poster_url=poster,
                status=movie_status,
                schema=movie_schema,
                aspect_chip=aspect_chip,
            )
        )
        showing_label = html.escape(f"Showings for {mtitle_raw}")
        schema_key = _schema_filter_key(movie_schema)
        aspect_label = (
            _format_aspect_ratio_label(_movie_aspect_ratio_max(m))
            if _movie_aspect_ratio_max(m) is not None
            else ""
        )
        search_blob = " ".join(
            str(x)
            for x in (
                mtitle_raw,
                group["key"],
                movie_schema,
                distributor,
                release_date,
                counts_line,
                aspect_label,
                "imax" if m.get("is_real_imax") else "",
            )
        )
        crawl_blocks.append(
            f'<section class="movie-group movie-group--schema-{html.escape(schema_key, quote=True)}" '
            f'id="movie-{html.escape(mkey_slug, quote=True)}" data-movie-group '
            f'data-movie-title="{html.escape(mtitle_raw, quote=True)}" '
            f'data-movie-schema="{html.escape(schema_key, quote=True)}" '
            f'data-movie-schema-rank="{group["schema_rank"]}" '
            f'data-movie-release-sort="{html.escape(group["release_sort"], quote=True)}" '
            f'data-movie-search="{html.escape(search_blob, quote=True)}">'
            '<div class="movie-group-head">'
            f"{poster_html}"
            '<div class="movie-group-meta">'
            f'<div class="movie-group-title-row"><h3 class="movie-group-title">{mtitle}</h3>'
            f'{schema_badge}</div>'
            f'<p class="movie-group-eyebrow"><span class="movie-release-date">{release_date}</span>'
            f" · {distributor}"
            f"{_render_aspect_ratio_meta(m)}"
            f" · {counts_html}{len(group['subcards'])} showing(s)</p>"
            "</div></div>"
            f'<div class="showings-rail" aria-label="{showing_label}">{"".join(group["subcards"])}</div>'
            f"{_render_movie_schedule_panel(group['key'])}"
            f"{tweet_embeds}"
            "</section>"
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
        rest_tweets = _render_movie_tweet_embeds({"x_handles": []}, social_handles=sx_handles, now=now)
        rest_status = _summarize_targets_status(
            [str(t.get("name", "")) for t in rest],
            target_by_name=target_by_name,
            fandango_poll=fandango_poll,
            now=now,
        )
        poster_tiles.append(
            _render_poster_shelf_tile(
                movie_id="ungrouped",
                title="Other targets",
                poster_url=None,
                status=rest_status,
            )
        )
        crawl_blocks.append(
            f'<section class="movie-group" id="crawl-ungrouped">'
            '<div class="movie-group-head">'
            f'{_poster_html(None, "Other targets", css_class="movie-group-poster")}'
            '<div class="movie-group-meta">'
            '<h3 class="movie-group-title">Other targets</h3>'
            f'<p class="movie-group-eyebrow">Release date not set · Distributor not set · {len(rest)} showing(s)</p>'
            "</div></div>"
            f'<div class="showings-rail" aria-label="Other targets">{rest_html}</div>{rest_tweets}</section>'
        )
    if not targets:
        crawl_blocks.append(
            '<p class="hint">No Fandango targets in config — add <code>targets:</code> '
            "in <code>config.yaml</code> and restart <code>watch</code> or use "
            "<code>once</code> with <code>--write-state</code> when you add URLs.</p>"
        )
    elif not crawl_blocks:
        crawl_blocks.append(
            '<div class="showings-rail" aria-label="All targets">'
            + "".join(
                _render_target_card(t, fandango_poll=fandango_poll, now=now)
                for t in targets
                if isinstance(t, dict)
            )
            + "</div>"
        )

    crawl_body_inner = "\n".join(crawl_blocks)
    poster_shelf = _render_poster_shelf(poster_tiles)
    shelf_view_toggle = _render_shelf_view_toggle(visible=bool(poster_tiles))
    watchlist_controls = _render_watchlist_controls(movie_count=len(movie_groups))
    if targets:
        crawl_body = (
            '<div class="watchlist-view" data-watchlist-view>'
            f"{watchlist_controls}"
            f"{poster_shelf}"
            f'<div class="movie-stack" aria-label="Movie watchlist">{crawl_body_inner}</div>'
            "</div>"
        )
    else:
        crawl_body = crawl_body_inner
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

    sx_last_polled = html.escape(str(social_x.get("last_polled_at") or "—"))
    sx_cards: list[str] = []
    sx_table_rows: list[str] = []
    n_social = 0
    for hkey, hst in sorted(sx_handles.items(), key=lambda x: str(x[0]).lower()):
        if not isinstance(hst, dict):
            continue
        n_social += 1
        rendered = _render_sx_handle_cells(hkey, hst, now=now)
        sx_table_rows.append(rendered.table_row_html)
        sx_cards.append(rendered.detail_card_html)
    thead = "".join(
        f'<th scope="col">{html.escape(col)}</th>'
        for col in (
            "Handle",
            "Latest tweet",
            "Posted",
            "ticket analysis",
            "last_polled_at",
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
            caption="Latest tweet text per monitored X handle",
            caption_class="visually-hidden",
            wrapper_class="table-wrap sx-snapshot",
            outer_prefix='<div role="region" aria-label="X handles and latest tweet text">',
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
        Latest tweet bodies are shown below (full text, not just ids). Technical ids stay in
        <strong>Per-handle details</strong> and on the Open on X link.
        Use <code>x-poll</code> or wait for <code>watch</code> to refresh
        <code>{social_state_path}</code>.
      </p>
      {sx_table_html}
      {sx_cards_html}
"""

    config_writes_enabled = bool(runtime.get("config_writes_enabled")) if isinstance(runtime, dict) else False
    movie_rows: list[str] = []
    for m in movies:
        if not isinstance(m, dict):
            continue
        title = html.escape(str(m.get("title") or m.get("key") or ""))
        key = html.escape(str(m.get("key") or ""))
        key_raw = str(m.get("key") or "")
        ftargets = html.escape(json.dumps(m.get("fandango_targets") or []))
        xh = html.escape(json.dumps(m.get("x_handles") or []))
        actions = "—"
        if config_writes_enabled and key_raw:
            actions = (
                f'<button type="button" class="target-filter-btn movie-delete-btn" '
                f'data-movie-key="{html.escape(key_raw, quote=True)}">Delete</button>'
            )
        movie_rows.append(
            f"<tr><td>{key}</td><td>{title}</td><td><code>{ftargets}</code></td>"
            f"<td><code>{xh}</code></td><td>{actions}</td></tr>"
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
        for col in ("key", "title", "fandango_targets", "x_handles", "actions")
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
    advanced_details = render_fold_panel(
        triage_panel
        + jump_nav
        + runtime_panel
        + intel_panel
        + purchases_panel
        + social_fold
        + registry_fold,
        fold_id="advanced",
        summary_html=(
            '<span class="fold-title">Advanced details</span>'
            '<span class="fold-badge">health · runtime · X · registry</span>'
        ),
        open_=False,
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
            "served with live refresh from <code>watch / dashboard</code>."
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
    imax_screen_ref_panel = _render_imax_screen_reference_panel()
    ui_script = _dashboard_ui_script()

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
  {operator_status}
  <section class="section-head section-head--shelf" id="crawl" aria-label="Fandango crawl">
    <p class="section-label">Watchlist</p>
    <div class="shelf-head-row">
      <h2 class="shelf-title">Movies on your list</h2>
      <div class="shelf-head-actions">
        <p class="shelf-meta">{shelf_meta}</p>
        {shelf_view_toggle}
      </div>
    </div>
    <p class="panel-tagline">Use Posters for an at-a-glance row with status outlines and max IMAX aspect ratio, or Cards for per-target showings. Hover a ratio chip for research notes.</p>
    {imax_screen_ref_panel}
    {movie_add_panel}
    {target_controls}
  </section>
  {crawl_body}
  {advanced_details}
  </main>
  <footer class="dash-foot">
    <p class="refresh-hint">{html.escape(refresh_note)}</p>
    JSON: <a href="/api/status">/api/status</a> ·
    <a href="/api/purchases">/api/purchases</a> ·
    <a href="/api/release_intel">/api/release_intel</a> ·
    <a href="/api/movies">/api/movies</a> ·
    <a href="/healthz">/healthz</a>
  </footer>
  <div class="artifact-viewer" id="artifact-viewer" hidden aria-live="polite"></div>
{ui_script}{live_script}</body>
</html>
"""
