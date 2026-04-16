"""Config loader.

``WatcherConfig`` mirrors ``config.example.yaml`` one-to-one as strict Pydantic
models (``extra='forbid'`` everywhere). ``Settings`` loads env-var secrets via
pydantic-settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
    enabled: bool = True
    model: str = "claude-sonnet-4-5"
    invoke_only_on: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=1)
    max_cost_usd: float = Field(default=2.0, gt=0)


class NotifyConfig(ConfigBase):
    channels: list[Literal["twilio", "smtp"]] = Field(
        default_factory=lambda: ["twilio", "smtp"]
    )
    on_events: list[str] = Field(default_factory=list)


class ScreenshotsConfig(ConfigBase):
    dir: str = "/app/artifacts/screenshots"
    max_age_days: int = Field(default=7, ge=1)
    per_purchase_dir: str = "/app/artifacts/purchase-attempts"
    keep_last_n: int | None = Field(default=None, ge=1)


class StateConfig(ConfigBase):
    """Where the watch loop persists per-target state JSON files."""

    dir: str = "/app/state"


class ViewportConfig(ConfigBase):
    width: int = Field(default=1440, ge=320)
    height: int = Field(default=900, ge=240)


class BrowserConfig(ConfigBase):
    headless: bool = True
    user_data_dir: str = "/app/browser-profile"
    locale: str = "en-US"
    timezone: str = "America/Los_Angeles"
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)


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


def load_config(path: str | Path) -> WatcherConfig:
    """Load and validate a YAML config file."""
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"config root must be a mapping, got {type(data).__name__}")
    return WatcherConfig.model_validate(data)


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

    # Anthropic (CU fallback)
    anthropic_api_key: str = ""
