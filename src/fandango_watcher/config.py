"""Config loader.

``WatcherConfig`` mirrors ``config.example.yaml`` one-to-one as strict Pydantic
models (``extra='forbid'`` everywhere). ``Settings`` loads env-var secrets via
pydantic-settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import FormatTag


class ConfigBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -----------------------------------------------------------------------------
# Leaf config blocks
# -----------------------------------------------------------------------------


class TargetConfig(ConfigBase):
    name: str
    url: str
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = (
        "domcontentloaded"
    )
    timeout_ms: int = Field(default=30000, gt=0)


class TheaterConfig(ConfigBase):
    display_name: str
    fandango_theater_anchor: str


class FormatsConfig(ConfigBase):
    require: list[FormatTag] = Field(default_factory=list)
    include: list[FormatTag] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class PollConfig(ConfigBase):
    min_seconds: int = Field(ge=30)
    max_seconds: int = Field(ge=30)
    error_backoff_multiplier: float = Field(default=2.0, ge=1.0)
    error_backoff_cap_seconds: int = Field(default=1800, ge=60)

    @model_validator(mode="after")
    def _validate_bounds(self) -> "PollConfig":
        if self.min_seconds > self.max_seconds:
            raise ValueError(
                f"poll.min_seconds ({self.min_seconds}) must be <= "
                f"poll.max_seconds ({self.max_seconds})"
            )
        return self


class SignalConfig(ConfigBase):
    page_text_contains_any: list[str] = Field(default_factory=list)
    require_theater_card_for: str | None = None


class InvariantConfig(ConfigBase):
    require_total_equals: str = "$0.00"
    require_benefit_phrase_any: list[str] = Field(default_factory=list)
    require_theater_match: bool = True
    require_showtime_match: bool = True
    require_seat_match: bool = True


class SeatPrefEntry(ConfigBase):
    auditorium: int = Field(ge=1)
    seats: list[str] = Field(min_length=1)


class PurchaseConfig(ConfigBase):
    enabled: bool = True
    mode: Literal["full_auto", "hold_and_confirm", "notify_only"] = "notify_only"
    invariant: InvariantConfig = Field(default_factory=InvariantConfig)
    # Keys are FormatTag names (validated against the enum in a model validator
    # so we get crisp error messages for typos like "IMAX_70mm").
    seat_priority: dict[str, SeatPrefEntry] = Field(default_factory=dict)
    on_preferred_sold_out: Literal["notify_only", "try_next_showtime"] = (
        "notify_only"
    )
    max_quantity: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_seat_priority_keys(self) -> "PurchaseConfig":
        valid = {t.value for t in FormatTag}
        bad = [k for k in self.seat_priority if k not in valid]
        if bad:
            raise ValueError(
                f"purchase.seat_priority contains unknown FormatTag(s): {bad}. "
                f"Valid values: {sorted(valid)}"
            )
        return self


class AgentFallbackConfig(ConfigBase):
    """Provider-agnostic config for the rescue agent.

    Default is the open-source ``browser_use`` provider (browser-use library
    + an OpenAI-compatible model endpoint).
    """

    enabled: bool = True
    # ``browser_use`` -> open-source rescue (default).
    # ``noop`` -> explicitly disable without flipping ``enabled``.
    provider: Literal["browser_use", "noop"] = "browser_use"
    # Defaults to a strong open-weights vision-language model. Any model id
    # your ``base_url`` endpoint understands works; popular picks:
    #   * "qwen2.5-vl-72b-instruct" (Apache-2.0; best OSS GUI grounding)
    #   * "qwen2.5-vl-7b-instruct"  (cheap; runs on a 24GB GPU)
    #   * "gpt-4o" / "gpt-4o-mini"  (if you point base_url at OpenAI)
    model: str = "qwen2.5-vl-72b-instruct"
    # OpenAI-compatible chat-completions endpoint. ``None`` -> default OpenAI.
    # Examples:
    #   * "http://localhost:8000/v1"           (self-hosted vLLM)
    #   * "https://openrouter.ai/api/v1"       (OpenRouter)
    #   * "https://api.together.xyz/v1"        (Together AI)
    #   * "https://api.fireworks.ai/inference/v1"
    base_url: str | None = None
    invoke_only_on: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=1)
    max_cost_usd: float = Field(default=2.0, gt=0)


class NotifyConfig(ConfigBase):
    channels: list[Literal["twilio", "smtp"]] = Field(
        default_factory=lambda: ["twilio", "smtp"]
    )
    on_events: list[str] = Field(default_factory=list)
    # When true, SMTP emails for purchase outcomes (and ticket-live alerts)
    # include PNG/WebM paths as MIME attachments. Twilio SMS stays text-only
    # (MMS needs a public MediaUrl — not implemented here).
    attach_screenshots_to_email: bool = False
    email_max_attachments: int = Field(default=5, ge=1, le=20)
    # Skip individual files larger than this (full-page PNGs can be huge).
    email_max_attachment_bytes: int = Field(default=6_000_000, ge=50_000)


class ScreenshotsConfig(ConfigBase):
    dir: str = "/app/artifacts/screenshots"
    max_age_days: int = Field(default=7, ge=1)
    per_purchase_dir: str = "/app/artifacts/purchase-attempts"
    keep_last_n: int | None = Field(default=None, ge=1)


class StateConfig(ConfigBase):
    """Where the watch loop persists per-target state JSON files."""

    dir: str = "/app/state"


class ReleaseIntelConfig(ConfigBase):
    """Dashboard summaries via xAI (Grok) OpenAI-compatible API."""

    enabled: bool = True
    model: str = "grok-3-mini-latest"
    cache_ttl_seconds: int = Field(default=3600, ge=60)
    timeout_seconds: int = Field(default=90, ge=10, le=300)


# -----------------------------------------------------------------------------
# Social signals: X / Twitter (Phase 2.5 — advisory only)
# -----------------------------------------------------------------------------


class SocialXHandleConfig(ConfigBase):
    """One X account to watch for early "tickets soon" hints.

    ``handle`` is the username without ``@``. ``keywords`` are matched
    case-insensitively as substrings against the tweet text. ``target_name``
    optionally pins the match to a Fandango target so the notification can
    deep-link the right URL.
    """

    handle: str = Field(min_length=1)
    # Empty list falls back to ``SocialXConfig.default_keywords`` at match time.
    keywords: list[str] = Field(default_factory=list)
    target_name: str | None = None
    label: str | None = None  # human-friendly display name (movie title, etc.)


class SocialXConfig(ConfigBase):
    """Phase 2.5 — X / Twitter advisory polling.

    Decoupled from the Fandango poll cadence: X API rate limits are far
    tighter than Fandango's, and a stale or failing X poll must NEVER
    interfere with the Fandango watch loop. Matches fire ``social_x_match``
    events, which are explicitly soft hints; Fandango is still the only
    source of truth for ``release_transition_bad_to_good``.
    """

    enabled: bool = False
    # Default 15 min ± 5 min — well under the v2 Basic-tier read budget.
    min_seconds: int = Field(default=900, ge=60)
    max_seconds: int = Field(default=1200, ge=60)
    handles: list[SocialXHandleConfig] = Field(default_factory=list)
    # If a handle has no keywords list, fall back to these. Useful for a
    # broad "any tickets-related buzz" sweep across many studio accounts.
    default_keywords: list[str] = Field(
        default_factory=lambda: [
            "tickets",
            "on sale",
            "presale",
            "now available",
        ]
    )
    # Cap how many tweets we examine per poll per handle (newest first).
    # X v2 max_results minimum is 5; max is 100. Keep modest to stay safely
    # inside free-ish tiers.
    max_results_per_handle: int = Field(default=10, ge=5, le=100)

    @model_validator(mode="after")
    def _validate_bounds(self) -> "SocialXConfig":
        if self.min_seconds > self.max_seconds:
            raise ValueError(
                f"social_x.min_seconds ({self.min_seconds}) must be <= "
                f"social_x.max_seconds ({self.max_seconds})"
            )
        # Note: ``social_x.enabled=true`` no longer requires ``handles`` here,
        # because handles can be supplied indirectly via the top-level
        # ``movies:`` block. The combined check lives on ``WatcherConfig``.
        return self


# -----------------------------------------------------------------------------
# Movie registry (Phase 2.5 — ties Fandango targets to X handles)
# -----------------------------------------------------------------------------


class MovieConfig(ConfigBase):
    """One movie the watcher cares about.

    Acts as the join table between Fandango targets and X / Twitter
    accounts. When a configured X handle posts a tweet whose text matches
    one of this movie's ``x_keywords``, the resulting ``social_x_match``
    notification is automatically labeled with ``title`` and (when
    ``fandango_targets`` is non-empty) deep-links the first matching
    Fandango target URL so the user gets a one-tap path from "X hint" to
    "actually buy."
    """

    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    fandango_targets: list[str] = Field(default_factory=list)
    preferred_formats: list[FormatTag] = Field(default_factory=list)
    x_handles: list[str] = Field(default_factory=list)
    # Default keyword set for every X handle this movie owns. Per-handle
    # overrides happen by adding an explicit entry to ``social_x.handles``
    # for the same handle (the explicit entry wins for that combination).
    x_keywords: list[str] = Field(default_factory=list)
    reference_page_key: str | None = None

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class ViewportConfig(ConfigBase):
    width: int = Field(default=1440, ge=320)
    height: int = Field(default=900, ge=240)


class BrowserConfig(ConfigBase):
    headless: bool = True
    user_data_dir: str = "/app/browser-profile"
    locale: str = "en-US"
    timezone: str = "America/Los_Angeles"
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    # Playwright writes a ``.webm`` per browser context when true; file appears
    # after ``context.close()`` (watch each crawl / purchase attempt).
    record_video: bool = False
    record_video_dir: str = "./artifacts/videos"
    # Playwright tracing: writes a `.zip` you open with
    # `npx playwright show-trace <file>` for a time-travel debugger
    # (DOM snapshots + screenshots + network + console per action).
    record_trace: bool = False
    record_trace_dir: str = "./artifacts/traces"

    def playwright_video_options(self) -> dict[str, Any]:
        """Extra kwargs for ``new_context`` / ``launch_persistent_context``."""
        if not self.record_video:
            return {}
        vdir = Path(self.record_video_dir)
        vdir.mkdir(parents=True, exist_ok=True)
        return {
            "record_video_dir": str(vdir.resolve()),
            "record_video_size": {
                "width": self.viewport.width,
                "height": self.viewport.height,
            },
        }

    def trace_dir_path(self) -> Path | None:
        """Resolved absolute trace directory (or ``None`` if disabled)."""
        if not self.record_trace:
            return None
        p = Path(self.record_trace_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()


# -----------------------------------------------------------------------------
# Top-level config
# -----------------------------------------------------------------------------


class WatcherConfig(ConfigBase):
    targets: list[TargetConfig] = Field(min_length=1)
    theater: TheaterConfig
    formats: FormatsConfig
    poll: PollConfig
    signal: SignalConfig = Field(default_factory=SignalConfig)
    purchase: PurchaseConfig
    agent_fallback: AgentFallbackConfig = Field(default_factory=AgentFallbackConfig)
    notify: NotifyConfig
    screenshots: ScreenshotsConfig = Field(default_factory=ScreenshotsConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    social_x: SocialXConfig = Field(default_factory=SocialXConfig)
    release_intel: ReleaseIntelConfig = Field(default_factory=ReleaseIntelConfig)
    movies: list[MovieConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_movies_and_social(self) -> "WatcherConfig":
        # Movie keys must be unique so notifications and CLI output can
        # round-trip safely.
        seen: set[str] = set()
        for m in self.movies:
            if m.key in seen:
                raise ValueError(f"duplicate movie key: {m.key!r}")
            seen.add(m.key)
            unknown = [
                t for t in m.fandango_targets if t not in {x.name for x in self.targets}
            ]
            if unknown:
                raise ValueError(
                    f"movie {m.key!r} references unknown target(s): {unknown}. "
                    f"Known target names: {[x.name for x in self.targets]}"
                )

        # social_x.enabled requires SOMETHING to poll: either explicit
        # handles, or at least one movie with x_handles defined.
        if self.social_x.enabled:
            has_movie_handles = any(m.x_handles for m in self.movies)
            if not self.social_x.handles and not has_movie_handles:
                raise ValueError(
                    "social_x.enabled=true but no handles configured (set "
                    "social_x.handles[] or add x_handles[] to a movie under movies:)"
                )
        return self

    def expanded_social_x_handles(self) -> list[SocialXHandleConfig]:
        """Combine ``social_x.handles`` with handles inherited from movies.

        Returns a flat list where each element carries enough context
        (``target_name``, ``label``) to render an actionable notification
        on its own. The same X handle may appear multiple times when more
        than one movie tracks it (e.g. ``@IMAX``); the poller dedupes
        API calls per handle but emits one match per movie context.
        """
        out: list[SocialXHandleConfig] = list(self.social_x.handles)
        for m in self.movies:
            if not m.x_handles:
                continue
            target_name = m.fandango_targets[0] if m.fandango_targets else None
            keywords = list(m.x_keywords)
            for h in m.x_handles:
                out.append(
                    SocialXHandleConfig(
                        handle=h,
                        keywords=keywords,
                        target_name=target_name,
                        label=m.title,
                    )
                )
        return out

    def effective_social_x(self) -> SocialXConfig:
        """``social_x`` with ``handles`` replaced by the expanded list."""
        return self.social_x.model_copy(
            update={"handles": self.expanded_social_x_handles()}
        )

    def movie_for_target(self, target_name: str) -> MovieConfig | None:
        for m in self.movies:
            if target_name in m.fandango_targets:
                return m
        return None


def _resolve_paths_against_config_dir(
    config_dir: Path, cfg: WatcherConfig
) -> WatcherConfig:
    """Resolve relative filesystem paths in ``cfg`` against ``config_dir``.

    YAML often uses repo-relative paths like ``state: dir: state``. Those are
    resolved from the **current working directory** unless we anchor them to
    the config file. Without this, ``watch`` started from one cwd and a
    ``dashboard``/inspector opened elsewhere can read/write different
    ``state/*.json`` files than the operator expects.
    """

    def _abs(s: str) -> str:
        # POSIX ``/app/...`` paths are absolute in Docker/Linux configs. On
        # Windows, :func:`pathlib.Path.is_absolute` is false for ``/app/x``,
        # so we must not anchor those to ``config_dir``.
        if s.startswith("/"):
            return s
        p = Path(s)
        if p.is_absolute():
            return str(p)
        return str((config_dir / p).resolve())

    return cfg.model_copy(
        update={
            "state": cfg.state.model_copy(update={"dir": _abs(cfg.state.dir)}),
            "screenshots": cfg.screenshots.model_copy(
                update={
                    "dir": _abs(cfg.screenshots.dir),
                    "per_purchase_dir": _abs(cfg.screenshots.per_purchase_dir),
                }
            ),
            "browser": cfg.browser.model_copy(
                update={
                    "user_data_dir": _abs(cfg.browser.user_data_dir),
                    "record_video_dir": _abs(cfg.browser.record_video_dir),
                    "record_trace_dir": _abs(cfg.browser.record_trace_dir),
                }
            ),
        }
    )


def load_config(path: str | Path) -> WatcherConfig:
    """Load and validate a YAML config file."""
    config_path = Path(path).resolve()
    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping, got {type(data).__name__}")
    cfg = WatcherConfig.model_validate(data)
    return _resolve_paths_against_config_dir(config_path.parent, cfg)


# -----------------------------------------------------------------------------
# Env-var settings (secrets + runtime mode).
# -----------------------------------------------------------------------------


class Settings(BaseSettings):
    """Environment-variable settings. Populated from ``.env`` or real env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    tz: str = "America/Los_Angeles"
    watcher_mode: Literal["watch", "once", "dry-run"] = "watch"
    watcher_config: str = "config.yaml"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    notify_to_e164: str = ""

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_to_email: str = ""

    # OpenAI-compatible API key for the agent_fallback browser_use provider
    # when ``agent_fallback.base_url`` points at OpenAI proper, Together,
    # Fireworks, a self-hosted vLLM, etc. (anything whose hostname is NOT
    # ``openrouter.ai``).
    openai_api_key: str = ""

    # Dedicated bearer for OpenRouter when ``base_url`` is
    # ``https://openrouter.ai/api/v1``. Lets you keep ``OPENAI_API_KEY`` and
    # ``OPENROUTER_API_KEY`` separate in ``.env``. If this is empty but
    # ``base_url`` is OpenRouter, ``resolve_llm_api_key_for_agent`` in
    # ``agent_fallback.py`` falls back to ``openai_api_key``.
    openrouter_api_key: str = ""

    # xAI (Grok) — dashboard ``release_intel`` summaries (OpenAI-compatible).
    # https://docs.x.ai/docs/api-reference — not interchangeable with OpenAI keys.
    xai_api_key: str = ""
    # Optional alternate env name for the same xAI console key.
    grok_api_key: str = ""
    # Optional env override for ``release_intel.model`` in config YAML.
    xai_model: str = ""

    # X / Twitter Developer API (Phase 2.5 — social signals)
    # Only ``x_bearer_token`` is required for read-only public-tweet polling.
    # Key/secret are kept here for future user-context (OAuth1) flows.
    x_api_key: str = ""
    x_api_key_secret: str = ""
    x_bearer_token: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
