"""Read-only HTML + JSON dashboard over persisted state and artifacts."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from .config import WatcherConfig
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
    except (ValueError, OSError):
        return str(iso)


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

    return {
        "healthz": healthz,
        "targets": targets_out,
        "social_x": social_x,
        "movies": movies,
        "paths": {
            "state_dir": str(paths.state_dir),
            "social_x_state_path": str(paths.social_x_state_path),
            "artifacts_root": str(paths.artifacts_root),
        },
    }


def render_index_html(snapshot: dict[str, Any]) -> str:
    """Single-page HTML with inline CSS; auto-refresh every 10 seconds."""
    healthz = snapshot.get("healthz") or {}
    targets = snapshot.get("targets") or []
    social_x = snapshot.get("social_x") or {}
    movies = snapshot.get("movies") or []

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
        su = html.escape(str(st.get("last_success_at") or "—"))

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

        cards.append(
            f"""
<section class="card">
  <h2>{name}</h2>
  <p><a href="{url_e}" target="_blank" rel="noopener">{name} on Fandango</a></p>
  <p><span class="{pill_class}">{cur}</span></p>
  <p><strong>release_schema</strong>: {schema}</p>
  <p><strong>total_ticks</strong>: {tticks} · <strong>last_success_at</strong>: {su}</p>
  {img_html}
  {vid_html}
  {trace_html}
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="10" />
  <title>fandango-watcher</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #12141a; color: #e8e8ec;
      margin: 0; padding: 1rem 1.5rem; max-width: 1200px; margin-left: auto;
      margin-right: auto; }}
    header {{ border-bottom: 1px solid #2a2f3a; padding-bottom: 1rem; margin-bottom: 1rem; }}
    h1 {{ font-size: 1.35rem; margin: 0 0 0.5rem 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 1rem; }}
    .card {{ background: #1a1d26; border: 1px solid #2a2f3a; border-radius: 8px;
      padding: 1rem; }}
    .card h2 {{ margin: 0 0 0.5rem 0; font-size: 1.05rem; }}
    .pill {{ display: inline-block; padding: 0.15rem 0.5rem; border-radius: 999px;
      background: #2a3140; font-size: 0.85rem; }}
    .pill-ok {{ background: #1e3d2e; color: #a8f0c0; }}
    .pill-warn {{ background: #3d2a1e; color: #f0d4a8; }}
    .thumb img {{ max-width: 100%; height: auto; border-radius: 4px; border: 1px solid #2a2f3a; }}
    video {{ max-width: 100%; border-radius: 4px; background: #000; }}
    a {{ color: #7eb8ff; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th, td {{ border: 1px solid #2a2f3a; padding: 0.35rem 0.5rem; text-align: left; }}
    code {{ font-size: 0.75rem; word-break: break-all; }}
    section.panel {{ margin-top: 1.5rem; }}
    p.hint {{ font-size: 0.9rem; opacity: 0.85; margin: 0.5rem 0 0 0; }}
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
  <div class="grid">
    {"".join(cards)}
  </div>
  <section class="panel">
    <h2>X / Twitter poller state</h2>
    {sx_block}
  </section>
  <section class="panel">
    <h2>Movies registry</h2>
    <table>
      <thead><tr><th>key</th><th>title</th><th>fandango_targets</th><th>x_handles</th></tr></thead>
      <tbody>{"".join(movie_rows)}</tbody>
    </table>
  </section>
  <p style="margin-top:2rem;font-size:0.85rem;opacity:0.7;">
    JSON: <a href="/api/status">/api/status</a> ·
    <a href="/api/movies">/api/movies</a> ·
    <a href="/healthz">/healthz</a>
  </p>
</body>
</html>
"""
