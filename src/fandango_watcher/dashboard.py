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

    return {
        "healthz": healthz,
        "targets": targets_out,
        "social_x": social_x,
        "movies": movies,
        "release_intel": release_intel,
        "paths": {
            "state_dir": str(paths.state_dir),
            "social_x_state_path": str(paths.social_x_state_path),
            "artifacts_root": str(paths.artifacts_root),
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
    raw = "|".join(parts)
    if data._revision_cache is not None:
        prev_rev, prev_raw = data._revision_cache
        if prev_raw == raw:
            return prev_rev
    rev = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    data._revision_cache = (rev, raw)
    return rev


def _render_release_intel_panel(
    movies: list[Any], release_intel: dict[str, Any]
) -> str:
    """HTML for xAI-backed release summaries (one sub-card per movie)."""
    if not release_intel:
        return (
            '<section class="panel intel-panel"><h2 class="section-label">Release intel</h2>'
            '<p class="panel-tagline">xAI Grok</p>'
            '<p class="hint">No release intel payload (internal).</p></section>'
        )
    status = release_intel.get("status")
    if status == "disabled":
        return (
            '<section class="panel intel-panel"><h2 class="section-label">Release intel</h2>'
            '<p class="panel-tagline">xAI Grok</p>'
            f'<p class="hint">{html.escape(str(release_intel.get("reason") or "disabled"))}</p></section>'
        )
    if status == "unconfigured":
        return (
            '<section class="panel intel-panel"><h2 class="section-label">Release intel</h2>'
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
<section class="panel intel-panel">
  <h2 class="section-label">Release intel</h2>
  <p class="panel-tagline">xAI Grok · advisory context (Fandango crawl is authoritative)</p>
  <p class="hint meta">{meta_line}</p>
  {err_html}
  <div class="intel-grid">{body}</div>
</section>
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

    ticks = healthz.get("total_ticks", "—")
    errs = healthz.get("total_errors", "—")
    started = html.escape(str(healthz.get("started_at") or "—"))
    last_utc = html.escape(str(healthz.get("last_tick_at") or "—"))
    last_pt = html.escape(str(healthz.get("last_tick_at_pt") or "—"))

    no_target_history = False
    if targets:
        no_target_history = all(
            not (isinstance(t.get("state"), dict) and t.get("state"))
            for t in targets
        )

    cards: list[str] = []
    for t in targets:
        name = html.escape(str(t.get("name", "")))
        url = str(t.get("url") or "")
        url_e = html.escape(url, quote=True)
        st = t.get("state") or {}
        schema = html.escape(str(st.get("last_release_schema") or "—"))
        cur = html.escape(str(st.get("current_state") or "—"))
        tticks = html.escape(str(st.get("total_ticks", "—")))
        su_raw = st.get("last_success_at")
        su = html.escape(str(su_raw or "—"))
        rel = _relative_ago(str(su_raw) if su_raw is not None else None)
        rel_html = (
            f' <span class="rel">({html.escape(rel)})</span>' if rel else ""
        )

        pill_class = "pill"
        cur_l = str(st.get("current_state") or "").lower()
        if cur_l == "error" or (st.get("consecutive_errors") or 0) > 0:
            pill_class += " pill-warn"
        elif "released" in cur_l or "live" in cur_l:
            pill_class += " pill-ok"

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
                f'<p class="vid"><video controls preload="metadata" '
                f'src="{html.escape(vu)}"></video></p>'
            )

        trace_html = ""
        tz = t.get("latest_trace_url")
        if tz:
            trace_html = (
                f'<p><a href="{html.escape(tz)}">latest trace (.zip)</a></p>'
            )

        media_inner = f"{img_html}{vid_html}{trace_html}"
        media_block = ""
        if media_inner.strip():
            media_block = (
                f'<details class="card-expand">'
                f'<summary>Media &amp; traces</summary>'
                f'<div class="card-expand-body">{media_inner}</div></details>'
            )

        cards.append(
            f"""
<section class="card">
  <h2>{name}</h2>
  <p><a href="{url_e}" target="_blank" rel="noopener">{name} on Fandango</a></p>
  <p><span class="{pill_class}">{cur}</span></p>
  <p class="card-stats"><strong>release_schema</strong> {schema} · <strong>ticks</strong> {tticks} · <strong>last OK</strong> {su}{rel_html}</p>
  {media_block}
</section>
"""
        )

    sx_handles = social_x.get("handles") or {}
    sx_lines: list[str] = []
    for hkey, hst in sx_handles.items():
        if not isinstance(hst, dict):
            continue
        sx_lines.append(
            "<tr>"
            f"<td>{html.escape(str(hkey))}</td>"
            f"<td>{html.escape(str(hst.get('user_id') or '—'))}</td>"
            f"<td>{html.escape(str(hst.get('last_polled_at') or '—'))}</td>"
            f"<td>{html.escape(str(hst.get('last_seen_tweet_id') or '—'))}</td>"
            "</tr>"
        )
    n_social = len(sx_lines)
    sx_block = (
        "<table><thead><tr><th>handle</th><th>user_id</th>"
        "<th>last_polled_at</th><th>last_seen_tweet_id</th></tr></thead><tbody>"
        + "".join(sx_lines)
        + "</tbody></table>"
    )

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
        f'<details class="panel-fold">'
        f'<summary><span class="fold-title">X / Twitter poller</span>'
        f'<span class="fold-badge">{n_social} handles</span></summary>'
        f'<div class="fold-body">{sx_block}</div></details>'
    )
    registry_fold = (
        f'<details class="panel-fold">'
        f'<summary><span class="fold-title">Movies registry</span>'
        f'<span class="fold-badge">{n_registry} movies</span></summary>'
        f'<div class="fold-body"><table>'
        f"<thead><tr><th>key</th><th>title</th><th>fandango_targets</th>"
        f"<th>x_handles</th></tr></thead><tbody>"
        f'{"".join(movie_rows)}</tbody></table></div></details>'
    )

    intel_panel = _render_release_intel_panel(movies, release_intel)

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
    live_script = ""
    if use_live:
        live_script = f"""
  <script>
(function () {{
  var rev = {rev_json};
  var ms = {poll_ms};
  function poll() {{
    fetch("/api/revision", {{ cache: "no-store" }})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d && d.revision && d.revision !== rev) location.reload();
      }})
      .catch(function () {{}});
  }}
  setInterval(poll, ms);
}})();
  </script>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{meta_refresh}{noscript_meta}  <title>fandango-watcher</title>
  <style>
    :root {{
      --bg: #0f1117;
      --surface: #161a22;
      --surface2: #1a1f2a;
      --border: #2a3142;
      --text: #e8eaef;
      --muted: #9aa3b2;
      --accent: #7eb8ff;
      --accent2: #9ec5ff;
      --radius: 10px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--text);
      margin: 0; padding: 0 1.25rem 2rem;
      max-width: 1280px; margin-left: auto; margin-right: auto;
      line-height: 1.45; font-size: 0.95rem;
    }}
    main.dash {{ display: flex; flex-direction: column; gap: 1.25rem; }}
    header {{
      border-bottom: 1px solid var(--border);
      padding: 1.25rem 0 1rem;
      margin-bottom: 0.25rem;
    }}
    header h1 {{ font-size: 1.4rem; font-weight: 650; margin: 0 0 0.35rem 0; letter-spacing: -0.02em; }}
    header p {{ margin: 0.25rem 0; font-size: 0.88rem; color: var(--muted); }}
    .section-head {{ margin: 0; padding: 0; }}
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
      gap: 0.85rem;
    }}
    .card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 0.9rem 1rem;
      display: flex; flex-direction: column; gap: 0.35rem;
    }}
    .card h2 {{ margin: 0; font-size: 1.02rem; font-weight: 600; }}
    .card-stats {{ font-size: 0.82rem; color: var(--muted); margin: 0.15rem 0 0 0; }}
    .pill {{
      display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px;
      background: #2a3140; font-size: 0.8rem; font-weight: 500;
    }}
    .pill-ok {{ background: #1e3d2e; color: #a8f0c0; }}
    .pill-warn {{ background: #3d2a1e; color: #f0d4a8; }}
    a {{ color: var(--accent); text-underline-offset: 2px; }}
    a:hover {{ color: #a8d4ff; }}
    .thumb img {{ max-width: 100%; height: auto; border-radius: 6px; border: 1px solid var(--border); }}
    video {{ max-width: 100%; border-radius: 6px; background: #000; }}
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
    .intel-panel {{
      background: linear-gradient(180deg, var(--surface2) 0%, var(--surface) 100%);
      border: 1px solid var(--border); border-radius: var(--radius);
      padding: 1rem 1.1rem 1.1rem;
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
      border: 1px solid var(--border); border-radius: var(--radius);
      background: var(--surface); overflow: hidden;
    }}
    .panel-fold > summary {{
      display: flex; align-items: center; justify-content: space-between;
      gap: 0.75rem; padding: 0.65rem 1rem;
      background: var(--surface2); border-bottom: 1px solid transparent;
      font-size: 0.9rem;
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
    code {{ font-size: 0.72rem; word-break: break-all; color: #c5cce0; }}
    p.hint {{ font-size: 0.88rem; opacity: 0.9; margin: 0.5rem 0 0 0; color: var(--muted); }}
    p.hint.meta {{ font-size: 0.78rem; opacity: 0.85; margin-bottom: 0.65rem; }}
    footer.dash-foot {{
      margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border);
      font-size: 0.82rem; color: var(--muted);
    }}
    p.refresh-hint {{ margin: 0 0 0.65rem 0; font-size: 0.78rem; opacity: 0.92; }}
    .card-stats .rel {{ font-size: 0.78rem; opacity: 0.88; font-weight: 450; }}
    @media (prefers-reduced-motion: reduce) {{
      summary::before {{ transition: none !important; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>fandango-watcher</h1>
    <p>Heartbeat · ticks: {html.escape(str(ticks))} · errors: {html.escape(str(errs))}</p>
    <p>Started (UTC): {started} · Last tick (UTC): {last_utc}</p>
    <p>Last tick (Pacific): {last_pt}</p>
    {
        '<p class="hint">No per-target crawl history yet — the dashboard only '
        '<strong>reads</strong> <code>state/&lt;target&gt;.json</code>. Run '
        '<code>fandango-watcher watch</code> (or <code>once</code>) so ticks, '
        "schema, and screenshots populate. <code>dashboard</code> alone does "
        "not crawl.</p>"
        if no_target_history
        else ""
    }
  </header>
  <main class="dash">
  {intel_panel}
  <section class="section-head">
    <h2 class="section-label">Fandango crawl</h2>
    <p class="panel-tagline">Per-target state · expand <strong>Media &amp; traces</strong> for screenshots / video / Playwright trace</p>
  </section>
  <div class="grid">
    {"".join(cards)}
  </div>
  <section class="panel panel-secondary">
    {social_fold}
  </section>
  <section class="panel panel-secondary">
    {registry_fold}
  </section>
  </main>
  <footer class="dash-foot">
    <p class="refresh-hint">{html.escape(refresh_note)}</p>
    JSON: <a href="/api/status">/api/status</a> ·
    <a href="/api/release_intel">/api/release_intel</a> ·
    <a href="/api/movies">/api/movies</a> ·
    <a href="/healthz">/healthz</a>
  </footer>
{live_script}</body>
</html>
"""
