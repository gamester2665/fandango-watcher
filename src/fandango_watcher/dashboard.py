"""Read-only HTML + JSON dashboard over persisted state and artifacts."""

from __future__ import annotations

import hashlib
import html
import json
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
        shot = _latest_artifact_for_target(t.name, paths.screenshot_dir, ".png")
        vid = _latest_artifact_for_target(t.name, paths.video_dir, ".webm")
        tr = _latest_artifact_for_target(t.name, paths.trace_dir, ".zip")
        targets_out.append(
            {
                "name": t.name,
                "url": t.url,
                "state": st,
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
        err_html = (
            f'<p class="card-err">{" · ".join(err_bits)}</p>'
        )

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
    if su_dt is not None:
        age = int((now.astimezone(UTC) - su_dt.astimezone(UTC)).total_seconds())
        if age > stale_thr:
            stale_html = (
                f'<p class="card-stale">No successful crawl in ~{_fmt_duration(age)} '
                f"(expected ≤ ~{_fmt_duration(stale_thr)} under normal poll). "
                f"Check errors or whether <code>watch</code> is running.</p>"
            )

    pill_class = "pill"
    cur_l = str(st.get("current_state") or "").lower()
    schema_l = str(st.get("last_release_schema") or "").lower()
    if cur_l == "error" or (st.get("consecutive_errors") or 0) > 0:
        pill_class += " pill-warn"
    elif "alert" in cur_l or "purchas" in cur_l or "released" in cur_l or "live" in cur_l:
        pill_class += " pill-ok"
    elif "partial" in schema_l or "full" in schema_l:
        pill_class += " pill-ok"

    route_lbl = html.escape(_target_route_label(raw_name))

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
    media_block = ""
    if media_inner.strip():
        media_block = (
            f'<details class="card-expand">'
            f'<summary>Media &amp; traces</summary>'
            f'<div class="card-expand-body">{media_inner}</div></details>'
        )

    return f"""
<section class="card" data-target="{name_attr}">
  <p class="card-kind"><span class="pill pill-muted">{route_lbl}</span></p>
  <h2>{name}</h2>
  <p><a href="{url_e}" target="_blank" rel="noopener">{name} on Fandango</a></p>
  <p><span class="{pill_class}">{cur}</span></p>
  <p class="card-stats"><strong>release_schema</strong> {schema} · <strong>ticks</strong> {tticks} · <strong>last OK</strong> {su}{rel_html}</p>
  {err_meta}
  {err_html}
  {stale_html}
  {media_block}
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
        return """
  <div class="triage-priority">
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

    return f"""
  <div class="triage-priority">
    <p class="section-label" style="margin:0.75rem 0 0.35rem 0">Target priority</p>
    <p class="hint meta" style="margin-top:0">Rows are sorted: errors, stale last OK, on-sale/alerted signals, then routine. Use <a href="#crawl">Fandango crawl</a> for full cards.</p>
    <div class="table-wrap triage-table-wrap">
    <table class="data-table triage-table">
      <thead><tr>
        <th scope="col">Priority</th>
        <th scope="col">Target</th>
        <th scope="col">State</th>
        <th scope="col">Schema</th>
        <th scope="col">Last OK</th>
        <th scope="col">CE</th>
        <th scope="col">Fandango</th>
      </tr></thead>
      <tbody>{body}</tbody>
    </table>
    </div>
  </div>
"""


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
        else "<p class=\"hint meta\">No extra attention flags — see per-target cards below.</p>"
    )

    priority_table = _render_triage_priority_table(
        [x for x in targets if isinstance(x, dict)],
        now=now,
        stale_threshold_sec=thr,
    )

    return f"""
<section class="panel triage-panel" id="triage" aria-label="At a glance">
  <h2 class="section-label">At a glance</h2>
  <p class="panel-tagline">Triage: ticket signals, process health, and config hints.</p>
  <div class="triage-grid">
    <div><strong>Targets</strong><span>{n_targets} configured · {n_shots} with screenshot</span></div>
    <div><strong>States</strong><span>alerted/purchasing-like: {alerted} · watching/idle: {watching} · error streak: {errish}</span></div>
    <div><strong>Schema signals</strong><span>{good_schema} with partial/full release schema</span></div>
    <div><strong>Stale crawls</strong><span>{stale_n} past ~{_fmt_duration(thr)} since last OK</span></div>
    <div><strong>Movies registry</strong><span>{n_movies} rows</span></div>
  </div>
  {priority_table}
  <div class="triage-attention">
    <p class="section-label" style="margin-top:0.75rem">What needs attention</p>
    {att_html}
  </div>
</section>
"""


def _render_release_intel_panel(
    movies: list[Any], release_intel: dict[str, Any]
) -> str:
    """HTML for xAI-backed release summaries (one sub-card per movie)."""
    if not release_intel:
        return (
            '<section class="panel intel-panel" id="release-intel">'
            '<h2 class="section-label">Release intel</h2>'
            "<p class=\"panel-tagline\">xAI Grok</p>"
            "<p class=\"hint\">The release-intel payload is empty. If you expected Grok "
            "summaries, check <code>release_intel</code> in <code>config.yaml</code> and "
            "API keys; otherwise this panel may appear while the cache is warming up.</p>"
            "</section>"
        )
    status = release_intel.get("status")
    if status == "disabled":
        return (
            '<section class="panel intel-panel" id="release-intel">'
            "<h2 class=\"section-label\">Release intel</h2>"
            '<p class="panel-tagline">xAI Grok</p>'
            f'<p class="hint">{html.escape(str(release_intel.get("reason") or "disabled"))}</p></section>'
        )
    if status == "unconfigured":
        return (
            '<section class="panel intel-panel" id="release-intel">'
            "<h2 class=\"section-label\">Release intel</h2>"
            '<p class="panel-tagline">xAI Grok · not configured</p>'
            '<p class="hint">Set <code>XAI_API_KEY</code> (or <code>GROK_API_KEY</code>) in '
            "<code>.env</code> with a key from <a href=\"https://console.x.ai\" "
            'target="_blank" rel="noopener">console.x.ai</a> — OpenAI keys do not work '
            "on api.x.ai. Summaries are advisory; Fandango crawl state below is the "
            "source of truth for on-sale detection.</p>"
            "</section>"
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

        blocks.append(
            f"""
<article class="intel-card">
  <h3>{title}</h3>
  <p class="intel-headline">{headline}</p>
  <details class="intel-expand">
    <summary>Summary, ticketing &amp; notes</summary>
    <div class="intel-expand-body">
      <p>{summary}</p>
      <p><strong>Ticketing</strong>: {ticketing}</p>
      {nd_line}
      <p class="qualifier">{qual}</p>
    </div>
  </details>
</article>
"""
        )

    body = "".join(blocks) if blocks else "<p class=\"hint\">No movies in registry.</p>"
    return f"""
<section class="panel intel-panel" id="release-intel">
  <h2 class="section-label">Release intel</h2>
  <p class="panel-tagline">xAI Grok · advisory context (Fandango crawl is authoritative)</p>
  <p class="hint meta">{meta_line}</p>
  {err_html}
  <div class="intel-grid">{body}</div>
</section>
"""


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
        return (
            '<section class="panel panel-secondary" id="purchase">'
            '<details class="panel-fold" open>'
            '<summary><span class="fold-title">Purchase history</span>'
            '<span class="fold-badge">0 lines</span></summary>'
            '<div class="fold-body">'
            f"<p class=\"hint meta\">Purchase tier: <code>{html.escape(purchase_mode)}</code> "
            f"({html.escape(pe)}). "
            "No purchase attempts are logged to <code>state/purchases.jsonl</code> until "
            "the scripted purchaser runs (or a prior run wrote no rows).</p>"
            "<p class=\"hint\">No rows in <code>"
            f"{html.escape(file_path)}</code> yet."
            "</p></div></details></section>"
        )
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
    return f"""
<section class="panel panel-secondary" id="purchase">
  <details class="panel-fold" open>
    <summary><span class="fold-title">Purchase history</span>
    <span class="fold-badge">{n} lines</span></summary>
    <div class="fold-body">
      <p class="hint meta">{ph_meta}</p>
      <div class="table-wrap">
      <table class="data-table">
        <thead><tr><th scope="col">at (UTC)</th><th scope="col">target</th>
        <th scope="col">outcome</th><th scope="col">error</th></tr></thead>
        <tbody>{body}</tbody>
      </table>
      </div>
    </div>
  </details>
</section>
"""


def render_dashboard_not_found_html(*, request_path: str) -> str:
    """Branded HTML for unknown routes when the dashboard server is enabled."""
    esc = html.escape(request_path)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Not found — fandango-watcher</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #080a0f;
      --text: #eef1f7;
      --muted: #8b95ab;
      --border: rgba(120, 140, 180, 0.18);
      --accent: #5eead4;
      --violet: #818cf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Outfit", ui-sans-serif, system-ui, sans-serif;
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
        radial-gradient(ellipse 100% 50% at 50% 0%, rgba(99, 102, 241, 0.18), transparent 52%),
        var(--bg);
    }}
    p.kicker {{
      font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.2em;
      color: var(--muted); margin: 0 0 0.4rem 0;
    }}
    h1 {{
      font-size: clamp(1.35rem, 3.5vw, 1.7rem);
      font-weight: 700; letter-spacing: -0.03em; margin: 0 0 1rem 0; line-height: 1.2;
      background: linear-gradient(125deg, #f8fafc 12%, var(--accent) 45%, var(--violet) 90%);
      -webkit-background-clip: text; background-clip: text; color: transparent;
    }}
    p {{ color: var(--muted); margin: 0.65rem 0; }}
    a {{
      color: #7dd3fc; text-underline-offset: 3px;
      text-decoration-color: rgba(125, 211, 252, 0.45);
    }}
    a:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}
    code {{
      font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.85rem;
      color: #c7d2fe; background: rgba(0, 0, 0, 0.3); padding: 0.15rem 0.4rem;
      border-radius: 6px; border: 1px solid var(--border);
    }}
    .card {{
      margin-top: 1.5rem; padding: 1.1rem 1.15rem 1.2rem; border-radius: 14px;
      border: 1px solid var(--border);
      background: linear-gradient(165deg, rgba(255,255,255,0.04) 0%, #12161f 45%);
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
    }}
    .card p {{ color: var(--text); font-size: 0.9rem; margin: 0; }}
    .card .links {{ display: flex; flex-wrap: wrap; gap: 0.3rem 0.55rem; margin-top: 0.5rem; }}
    .card a:not(:last-child)::after {{
      content: "·"; margin-left: 0.45rem; color: var(--muted); opacity: 0.5; pointer-events: none;
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f0f2f8; --text: #12151c; --muted: #5a6372;
        --border: rgba(60, 70, 100, 0.14);
      }}
      body {{ background: radial-gradient(ellipse 100% 45% at 50% 0%, rgba(99, 102, 241, 0.1), transparent 50%), var(--bg); }}
      h1 {{
        background: linear-gradient(125deg, #0f172a 20%, #0d9488 55%, #4f46e5 95%);
        -webkit-background-clip: text; background-clip: text;
      }}
      code {{ background: rgba(0,0,0,0.05); color: #4338ca; border-color: rgba(0,0,0,0.08); }}
    }}
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
    """Single-page HTML with inline CSS.

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
    purchase_link = (
        '<a href="#purchase">Purchase</a>\n  '
        if show_ph
        else ""
    )
    jump_nav = f"""
<nav class="jump-nav" aria-label="On this page">
  <div class="jump-nav-list">
  <a href="#triage">At a glance</a>
  <a href="#runtime">Runtime</a>
  <a href="#release-intel">Release intel</a>
  {purchase_link}<a href="#crawl">Fandango</a>
  <a href="#x">X / Twitter</a>
  <a href="#registry">Movies</a>
  </div>
</nav>
"""

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
        sx_table_rows.append(
            f"<tr><td><strong>@{handle_display}</strong></td><td><code>{uid}</code></td>"
            f"<td><code>{polled}</code></td><td><code>{tid_disp}</code></td>"
            f'<td class="sx-tweet-preview-cell">{preview_cell}</td>'
            f"<td>{ce}</td><td>{le_short}</td><td>{open_cell}</td></tr>"
        )

        sx_cards.append(
            f'<article class="sx-card" aria-label="X handle {handle_display}">'
            f'<h4 class="sx-handle">@{handle_display}</h4>'
            f'<p class="sx-meta">user_id <code>{uid}</code> · polled <code>{polled}</code> · err streak {ce}</p>'
            f"{tid_row}"
            f"{at_line}"
            f'<blockquote class="sx-tweet-body">{body}</blockquote>'
            f"{err_html}"
            f"</article>"
        )
    sx_table_html = (
        (
            '<div class="table-wrap sx-snapshot" role="region" aria-label="X handles, tweet text snapshot">'
            "<table class=\"data-table\">"
            "<caption class=\"visually-hidden\">Per-handle poller state and last tweet text preview</caption>"
            "<thead><tr><th scope=\"col\">Handle</th><th scope=\"col\">user_id</th>"
            "<th scope=\"col\">last_polled_at</th><th scope=\"col\">last_seen_tweet_id</th>"
            "<th scope=\"col\">tweet text (preview)</th>"
            "<th scope=\"col\">errors</th><th scope=\"col\">last_error</th><th scope=\"col\">open</th>"
            "</tr></thead><tbody>"
            f'{"".join(sx_table_rows)}</tbody></table></div>'
        )
        if sx_table_rows
        else ""
    )
    sx_cards_html = (
        f'<div class="sx-cards">{"".join(sx_cards)}</div>'
        if sx_cards
        else '<p class="hint">No X handles in state.</p>'
    )
    sx_block = f"""
      <p class="hint meta">
        Last global X poll: <code>{sx_last_polled}</code>. Cadence: {social_cadence};
        fetches up to {social_max_results} tweets per handle when needed.
      </p>
      <p class="hint">
        The watcher stores <code>since_id</code> in <code>{social_state_path}</code> and only requests
        tweets newer than the cursor. <strong>Tweet text</strong> in the table and cards is the
        latest body saved from the API (new tweets, or a one-tweet backfill when the timeline is empty).
        Use <code>x-poll</code> or <code>x-poll --reset</code> to refresh.
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
    social_fold = (
        f'<section class="panel panel-secondary" id="x" aria-label="X / Twitter poller">'
        f'<details class="panel-fold" open>'
        f'<summary><span class="fold-title">X / Twitter poller</span>'
        f'<span class="fold-badge">{n_social} handles</span></summary>'
        f'<div class="fold-body">{sx_block}</div></details></section>'
    )
    registry_fold = (
        f'<section class="panel panel-secondary" id="registry" aria-label="Movies registry">'
        f'<details class="panel-fold">'
        f'<summary><span class="fold-title">Movies registry</span>'
        f'<span class="fold-badge">{n_registry} movies</span></summary>'
        f'<div class="fold-body"><div class="table-wrap"><table class="data-table">'
        f"<thead><tr><th scope=\"col\">key</th><th scope=\"col\">title</th>"
        f"<th scope=\"col\">fandango_targets</th>"
        f"<th scope=\"col\">x_handles</th></tr></thead><tbody>"
        f'{"".join(movie_rows)}</tbody></table></div></div></details></section>'
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

    runtime_panel = f"""
<section class="panel runtime-panel" id="runtime">
  <h2 class="section-label">Runtime &amp; cadence</h2>
  <p class="panel-tagline">This snapshot is served at <code>{public_base_e}</code> (read-only; bind address comes from the running process).</p>
  <div class="meta-grid">
    <div><strong>Fandango poll</strong><span>{fandango_cadence} with backoff up to {fandango_backoff}</span></div>
    <div><strong>X / Twitter poll</strong><span>{html.escape(social_enabled)} · {social_cadence} · max {social_max_results} tweets/handle</span></div>
    <div><strong>State lives in</strong><span><code>{runtime_state_dir}</code></span></div>
    <div><strong>Artifacts live in</strong><span><code>{runtime_artifacts_root}</code></span></div>
    <div><strong>Browser profile</strong><span><code>{browser_profile}</code></span></div>
    <div><strong>Purchase / notify</strong><span><code>{purchase_mode}</code> · {notify_line}</span></div>
  </div>
</section>
"""

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
{meta_refresh}{noscript_meta}  <title>fandango-watcher</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&family=IBM+Plex+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet" />
  <style>
    :root {{
      --bg: #080a0f;
      --bg-elevated: #0c0f16;
      --surface: #12161f;
      --surface2: #161c28;
      --border: rgba(120, 140, 180, 0.18);
      --border-bright: rgba(180, 200, 255, 0.12);
      --text: #eef1f7;
      --muted: #8b95ab;
      --accent: #5eead4;
      --accent-dim: rgba(94, 234, 212, 0.12);
      --accent2: #a5b4fc;
      --violet: #818cf8;
      --radius: 14px;
      --shadow: 0 8px 32px rgba(0, 0, 0, 0.45), 0 0 0 1px var(--border-bright);
      --shadow-sm: 0 2px 12px rgba(0, 0, 0, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Outfit", ui-sans-serif, system-ui, sans-serif;
      color: var(--text);
      margin: 0;
      padding: 0 1.25rem 2.5rem;
      max-width: 1200px;
      margin-left: auto;
      margin-right: auto;
      line-height: 1.5;
      font-size: 0.95rem;
      min-height: 100vh;
      background:
        radial-gradient(ellipse 100% 60% at 50% -15%, rgba(99, 102, 241, 0.22), transparent 55%),
        radial-gradient(ellipse 60% 40% at 100% 20%, rgba(45, 212, 191, 0.08), transparent 45%),
        radial-gradient(ellipse 50% 35% at 0% 60%, rgba(129, 140, 248, 0.07), transparent 40%),
        var(--bg);
    }}
    main.dash {{ display: flex; flex-direction: column; gap: 1.35rem; }}
    header.dash-header {{
      padding: 1.75rem 0 1.5rem;
      margin-bottom: 0.15rem;
      border-bottom: 1px solid var(--border);
      position: relative;
    }}
    header.dash-header::after {{
      content: "";
      position: absolute;
      left: 0;
      bottom: -1px;
      width: 100%;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(94, 234, 212, 0.35), rgba(129, 140, 248, 0.25), transparent);
      opacity: 0.9;
    }}
    .dash-kicker {{
      font-size: 0.68rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.2em;
      color: var(--muted);
      margin: 0 0 0.4rem 0;
    }}
    h1.dash-title {{
      font-size: clamp(1.45rem, 4vw, 1.85rem);
      font-weight: 700;
      letter-spacing: -0.03em;
      margin: 0 0 0.6rem 0;
      line-height: 1.15;
      background: linear-gradient(125deg, #f8fafc 12%, var(--accent) 42%, var(--violet) 88%);
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }}
    header.dash-header p {{ margin: 0.28rem 0; font-size: 0.86rem; color: var(--muted); }}
    header .hb-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem 0.75rem;
      margin-top: 0.65rem;
      align-items: center;
    }}
    .hb-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.78rem;
      font-weight: 500;
      padding: 0.28rem 0.65rem;
      border-radius: 999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      box-shadow: var(--shadow-sm);
    }}
    .hb-pill .dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 10px var(--accent);
    }}
    .section-head {{
      margin: 0;
      padding: 0.35rem 0 0.15rem 0;
    }}
    .section-label {{
      font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.12em; color: var(--muted); margin: 0 0 0.2rem 0;
    }}
    .panel-tagline {{
      font-size: 0.82rem; color: var(--muted); margin: 0 0 0.5rem 0;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
      gap: 1rem;
    }}
    .grid .card {{
      background: linear-gradient(165deg, rgba(255,255,255,0.04) 0%, var(--surface) 40%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      box-shadow: var(--shadow-sm);
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.2s ease;
    }}
    .grid .card:hover {{
      transform: translateY(-3px);
      box-shadow: var(--shadow);
      border-color: rgba(94, 234, 212, 0.22);
    }}
    .card {{
      border-radius: var(--radius);
    }}
    .card h2 {{ margin: 0; font-size: 1.05rem; font-weight: 600; letter-spacing: -0.02em; }}
    .card-stats {{ font-size: 0.82rem; color: var(--muted); margin: 0.15rem 0 0 0; }}
    .pill {{
      display: inline-block; padding: 0.2rem 0.6rem; border-radius: 999px;
      background: rgba(255,255,255,0.06); font-size: 0.8rem; font-weight: 600;
      border: 1px solid var(--border);
    }}
    .pill-ok {{ background: rgba(16, 185, 129, 0.18); color: #6ee7b7; border-color: rgba(16,185,129,0.35); }}
    .pill-warn {{ background: rgba(245, 158, 11, 0.15); color: #fcd34d; border-color: rgba(245,158,11,0.3); }}
    a {{ color: #7dd3fc; text-underline-offset: 3px; text-decoration-color: rgba(125, 211, 252, 0.45); transition: color 0.15s; }}
    a:hover {{ color: var(--accent); text-decoration-color: var(--accent); }}
    .thumb img {{ max-width: 100%; height: auto; border-radius: 10px; border: 1px solid var(--border);
      box-shadow: 0 4px 20px rgba(0,0,0,0.35); }}
    video {{ max-width: 100%; border-radius: 10px; background: #000; border: 1px solid var(--border); }}
    details {{ color: var(--text); }}
    summary {{
      cursor: pointer; list-style: none; user-select: none;
      font-size: 0.85rem; font-weight: 500; color: var(--accent2);
      padding: 0.35rem 0;
    }}
    summary::-webkit-details-marker {{ display: none; }}
    summary::before {{
      content: "▸"; display: inline-block; margin-right: 0.4rem;
      transition: transform 0.15s ease; opacity: 0.75; font-size: 0.75rem;
    }}
    details[open] > summary::before {{ transform: rotate(90deg); }}
    .card-expand, .intel-expand {{ margin-top: 0.25rem; }}
    .card-expand-body, .intel-expand-body {{
      padding: 0.5rem 0 0 0.85rem; border-left: 2px solid var(--border);
      margin-top: 0.35rem; font-size: 0.88rem;
    }}
    .runtime-panel {{
      background: linear-gradient(155deg, rgba(129, 140, 248, 0.07) 0%, var(--surface) 55%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.15rem 1.2rem 1.25rem;
      box-shadow: var(--shadow-sm);
    }}
    .runtime-panel .panel-tagline {{ margin-bottom: 0.75rem; }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr));
      gap: 0.65rem;
    }}
    .meta-grid > div {{
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.75rem 0.8rem;
      min-width: 0;
      transition: border-color 0.2s;
    }}
    .meta-grid > div:hover {{ border-color: rgba(94, 234, 212, 0.2); }}
    .meta-grid strong {{
      display: block; font-size: 0.74rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.25rem;
    }}
    .meta-grid span {{
      display: block; font-size: 0.86rem; color: var(--text);
      overflow-wrap: anywhere;
    }}
    .intel-panel {{
      background: linear-gradient(165deg, rgba(94, 234, 212, 0.06) 0%, var(--surface) 50%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.15rem 1.2rem 1.25rem;
      box-shadow: var(--shadow-sm);
    }}
    .intel-panel .section-label {{ margin-top: 0; }}
    .intel-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr));
      gap: 0.75rem; margin-top: 0.65rem;
    }}
    .intel-card {{
      background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.85rem 1rem;
    }}
    .intel-card h3 {{
      font-size: 0.95rem; margin: 0 0 0.25rem 0; color: #d4daf0;
      font-weight: 600;
    }}
    .intel-headline {{ font-weight: 600; color: var(--accent2); margin: 0 0 0.4rem 0; font-size: 0.9rem; }}
    p.qualifier {{ font-size: 0.78rem; opacity: 0.8; margin: 0.5rem 0 0 0; font-style: italic; color: var(--muted); }}
    .pill-warn-inline {{
      background: #3d2a1e; color: #f0d4a8; padding: 0.35rem 0.55rem;
      border-radius: 6px; display: inline-block; font-size: 0.82rem;
    }}
    section.panel {{ margin: 0; }}
    .panel-secondary {{ margin-top: 0.25rem; }}
    .panel-fold {{
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface);
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }}
    .panel-fold > summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.75rem 1.05rem;
      background: linear-gradient(90deg, rgba(99, 102, 241, 0.08), var(--surface2));
      border-bottom: 1px solid transparent;
      font-size: 0.92rem;
    }}
    .panel-fold[open] > summary {{
      border-bottom-color: var(--border);
    }}
    .fold-title {{ font-weight: 600; color: var(--text); }}
    .fold-badge {{
      font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--muted); background: var(--bg); padding: 0.2rem 0.5rem;
      border-radius: 999px; border: 1px solid var(--border);
    }}
    .fold-body {{ padding: 0.65rem 1rem 0.85rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    th, td {{ border: 1px solid var(--border); padding: 0.4rem 0.55rem; text-align: left; }}
    th {{ background: var(--surface2); color: var(--muted); font-weight: 600; font-size: 0.75rem;
      text-transform: uppercase; letter-spacing: 0.04em; }}
    tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
    .purchase-err {{ font-size: 0.78rem; color: #e8c4a8; }}
    code {{
      font-family: "IBM Plex Mono", ui-monospace, monospace;
      font-size: 0.74rem;
      word-break: break-all;
      color: #c7d2fe;
      background: rgba(0, 0, 0, 0.25);
      padding: 0.1rem 0.35rem;
      border-radius: 5px;
      border: 1px solid rgba(255,255,255,0.06);
    }}
    p.hint {{ font-size: 0.88rem; opacity: 0.9; margin: 0.5rem 0 0 0; color: var(--muted); }}
    p.hint.meta {{ font-size: 0.78rem; opacity: 0.85; margin-bottom: 0.65rem; }}
    footer.dash-foot {{
      margin-top: 2.25rem;
      padding: 1.15rem 1.15rem 1.25rem;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--surface);
      font-size: 0.82rem;
      color: var(--muted);
      box-shadow: var(--shadow-sm);
    }}
    p.refresh-hint {{ margin: 0 0 0.65rem 0; font-size: 0.78rem; opacity: 0.92; }}
    .card-stats .rel {{ font-size: 0.78rem; opacity: 0.88; font-weight: 450; }}
    .skip-link {{
      position: absolute; left: -9999px; z-index: 100;
      padding: 0.55rem 1rem;
      background: linear-gradient(135deg, var(--accent), #818cf8);
      color: #0a0c10;
      font-weight: 700;
      border-radius: 8px;
      box-shadow: var(--shadow-sm);
    }}
    .skip-link:focus {{ left: 1rem; top: 1rem; outline: 2px solid var(--violet); outline-offset: 2px; }}
    a:focus-visible, summary:focus-visible, .skip-link:focus {{
      outline: 2px solid var(--accent); outline-offset: 2px;
    }}
    .jump-nav {{
      position: sticky;
      top: 0.5rem;
      z-index: 20;
      padding: 0.55rem 1rem;
      background: rgba(8, 10, 15, 0.72);
      backdrop-filter: blur(14px) saturate(1.3);
      -webkit-backdrop-filter: blur(14px) saturate(1.3);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 0.8rem;
      font-weight: 500;
      color: var(--muted);
      box-shadow: 0 4px 24px rgba(0,0,0,0.4);
    }}
    .jump-nav-list {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: center;
      row-gap: 0.4rem;
      column-gap: 0.2rem;
    }}
    .jump-nav-list a {{
      white-space: nowrap;
      opacity: 0.92;
    }}
    .jump-nav-list a:not(:last-child)::after {{
      content: "·";
      display: inline-block;
      margin-left: 0.4rem;
      color: var(--muted);
      opacity: 0.5;
      font-weight: 400;
      pointer-events: none;
      user-select: none;
    }}
    .jump-nav a:hover {{ opacity: 1; }}
    .triage-panel {{
      background: linear-gradient(160deg, rgba(99, 102, 241, 0.1) 0%, var(--surface) 45%);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.15rem 1.2rem 1.25rem;
      box-shadow: var(--shadow-sm);
    }}
    .triage-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 200px), 1fr));
      gap: 0.65rem;
    }}
    .triage-grid > div {{
      background: var(--bg); border: 1px solid var(--border);
      border-radius: 8px; padding: 0.55rem 0.65rem; min-width: 0;
    }}
    .triage-grid strong {{
      display: block; font-size: 0.72rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.2rem;
    }}
    .triage-grid span {{ font-size: 0.84rem; overflow-wrap: anywhere; }}
    .triage-priority .hint.meta {{ margin-top: 0.25rem; }}
    .triage-table-wrap {{ margin-top: 0.35rem; }}
    table.triage-table {{ min-width: 640px; font-size: 0.8rem; }}
    .triage-table td {{ vertical-align: top; }}
    .triage-pill {{
      display: inline-block; font-size: 0.65rem; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.05em; padding: 0.12rem 0.4rem; border-radius: 999px; white-space: nowrap;
    }}
    tr.triage-tier-0 td {{ background: rgba(200, 80, 80, 0.12); }}
    tr.triage-tier-1 td {{ background: rgba(200, 160, 80, 0.1); }}
    tr.triage-tier-2 td {{ background: rgba(80, 160, 120, 0.1); }}
    .triage-pill-0 {{ background: #4a2a2a; color: #f0a8a8; }}
    .triage-pill-1 {{ background: #3d3520; color: #e8c4a8; }}
    .triage-pill-2 {{ background: #1e3d2e; color: #a8f0c0; }}
    .triage-pill-3 {{ background: #2a3140; color: var(--muted); }}
    ul.attention-list {{ margin: 0.35rem 0 0 1.1rem; padding: 0; color: var(--text); font-size: 0.86rem; }}
    ul.attention-list li {{ margin: 0.25rem 0; }}
    .pill-muted {{ background: #252a36; color: var(--muted); font-size: 0.72rem; }}
    .card-kind {{ margin: 0 0 0.15rem 0; }}
    .card-err {{ font-size: 0.78rem; color: #f0d4a8; margin: 0.25rem 0 0 0; }}
    .card-stale {{ font-size: 0.78rem; color: #e8c4a8; margin: 0.35rem 0 0 0; }}
    .card-media-meta {{ font-size: 0.76rem; color: var(--muted); margin: 0 0 0.35rem 0; }}
    .movie-group {{ margin: 0; }}
    .movie-group-title {{
      font-size: 0.95rem; font-weight: 600; margin: 0 0 0.5rem 0; color: #d4daf0;
    }}
    .panel-warn {{ border-left: 3px solid #c9a227; padding-left: 0.65rem; }}
    .conn-line {{ font-size: 0.8rem; margin: 0.5rem 0 0 0; }}
    .conn-label {{ color: var(--muted); }}
    .conn-ok {{ color: #a8f0c0; }}
    .conn-bad {{ color: #f0a8a8; }}
    .conn-static {{ color: var(--muted); }}
    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    .visually-hidden {{
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
    }}
    .sx-snapshot {{ margin: 0.35rem 0 0.85rem; }}
    .sx-tweet-preview-cell {{
      max-width: 36ch; font-size: 0.82rem; color: var(--text);
      line-height: 1.45; vertical-align: top; word-break: break-word;
    }}
    .sx-preview-missing {{ color: var(--muted); }}
    .sx-cards {{ display: flex; flex-direction: column; gap: 1rem; margin-top: 0.5rem; }}
    .sx-card {{
      background: linear-gradient(145deg, rgba(29, 155, 240, 0.06), var(--bg-elevated));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1rem 1.1rem;
      box-shadow: var(--shadow-sm);
    }}
    .sx-handle {{ margin: 0 0 0.35rem 0; font-size: 1.05rem; font-weight: 700; color: #e0e7ff; letter-spacing: -0.02em; }}
    .sx-meta, .sx-tweet-idline, .sx-tweet-when {{ font-size: 0.78rem; color: var(--muted); margin: 0.2rem 0; }}
    code.tweet-snowflake {{ font-size: 0.85rem; letter-spacing: 0.02em; word-break: break-all; }}
    .sx-tweet-body {{
      margin: 0.65rem 0 0 0;
      padding: 0.85rem 1rem;
      border-left: 3px solid #22d3ee;
      background: linear-gradient(105deg, rgba(34, 211, 238, 0.1), rgba(129, 140, 248, 0.06));
      border-radius: 0 12px 12px 0;
      font-size: 0.9rem;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .sx-tweet-body em.sx-no-text {{ color: var(--muted); font-style: italic; }}
    .sx-err {{ font-size: 0.78rem; color: #f0c4a8; margin: 0.5rem 0 0 0; }}
    table.data-table {{ min-width: 520px; }}
    @media (max-width: 700px) {{
      table.data-table {{ font-size: 0.78rem; }}
      .jump-nav {{
        font-size: 0.76rem; line-height: 1.5;
        border-radius: 14px;
        padding: 0.5rem 0.7rem;
      }}
    }}
    @media (prefers-color-scheme: light) {{
      :root {{
        --bg: #f0f2f8;
        --bg-elevated: #ffffff;
        --surface: #ffffff;
        --surface2: #e8ecf4;
        --border: rgba(60, 70, 100, 0.14);
        --border-bright: rgba(255, 255, 255, 0.9);
        --text: #12151c;
        --muted: #5a6372;
        --accent: #0d9488;
        --accent-dim: rgba(13, 148, 136, 0.12);
        --accent2: #4f46e5;
        --violet: #6366f1;
        --shadow: 0 8px 28px rgba(30, 40, 80, 0.1), 0 0 0 1px rgba(0,0,0,0.04);
        --shadow-sm: 0 2px 10px rgba(30, 40, 80, 0.07);
      }}
      body {{
        background:
          radial-gradient(ellipse 100% 50% at 50% 0%, rgba(99, 102, 241, 0.12), transparent 50%),
          var(--bg);
      }}
      h1.dash-title {{
        background: linear-gradient(125deg, #0f172a 20%, #0d9488 55%, #4f46e5 95%);
        -webkit-background-clip: text;
        background-clip: text;
      }}
      .jump-nav {{
        background: rgba(255, 255, 255, 0.82);
        border-color: rgba(0,0,0,0.08);
      }}
      .pill {{ background: rgba(0,0,0,0.04); }}
      .pill-ok {{ background: rgba(16, 185, 129, 0.15); color: #047857; }}
      .pill-warn {{ background: rgba(245, 158, 11, 0.15); color: #b45309; }}
      code {{ background: rgba(0,0,0,0.05); color: #4338ca; border-color: rgba(0,0,0,0.06); }}
      tr:nth-child(even) td {{ background: rgba(0,0,0,0.02); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      summary::before {{ transition: none !important; }}
      .grid .card {{
        transition: none !important;
      }}
      .grid .card:hover {{ transform: none !important; }}
    }}
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
  {runtime_panel}
  {intel_panel}
  {purchases_panel}
  <section class="section-head" id="crawl" aria-label="Fandango crawl">
    <h2 class="section-label">Fandango crawl</h2>
    <p class="panel-tagline">Per-target state · expand <strong>Media &amp; traces</strong> for screenshots / video / Playwright trace. Grouped by <strong>movies</strong> registry when possible.</p>
  </section>
  {crawl_body}
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
