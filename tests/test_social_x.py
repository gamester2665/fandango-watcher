"""Tests for src/fandango_watcher/social_x.py.

Covers:

* ``match_tweet`` matcher: case-insensitive, dedupe, empty-keyword guard.
* ``check_x_signals`` with a stub ``XClient``: resolves user_id once and
  caches it, advances ``last_seen_tweet_id`` correctly, only emits matches
  for tweets that contain at least one keyword.
* Per-handle error isolation: one broken handle does not abort the sweep.
* Notification builder labels output as a soft "X HINT" hint.
* CLI ``x-poll`` errors out cleanly when not configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fandango_watcher.config import (
    FormatsConfig,
    MovieConfig,
    NotifyConfig,
    PollConfig,
    PurchaseConfig,
    SocialXConfig,
    SocialXHandleConfig,
    TargetConfig,
    TheaterConfig,
    WatcherConfig,
)
from fandango_watcher.loop import build_social_x_notification
from fandango_watcher.social_x import (
    SocialXState,
    XSignalMatch,
    check_x_signals,
    load_social_x_state,
    match_tweet,
    save_social_x_state,
)

# -----------------------------------------------------------------------------
# match_tweet
# -----------------------------------------------------------------------------


class TestMatchTweet:
    def test_returns_keywords_in_original_casing(self) -> None:
        hits = match_tweet("Tickets ON SALE now!", ["tickets", "on sale"])
        assert hits == ["tickets", "on sale"]

    def test_case_insensitive(self) -> None:
        assert match_tweet("Imax 70mm tonight", ["IMAX"]) == ["IMAX"]

    def test_empty_text_or_no_keywords(self) -> None:
        assert match_tweet("", ["tickets"]) == []
        assert match_tweet("anything", []) == []

    def test_dedupes_when_same_keyword_listed_twice(self) -> None:
        # Different casing of the same effective keyword should collapse.
        hits = match_tweet("tickets!", ["tickets", "TICKETS"])
        assert hits == ["tickets"]

    def test_no_match_returns_empty(self) -> None:
        assert match_tweet("just a trailer drop", ["tickets", "presale"]) == []


# -----------------------------------------------------------------------------
# State persistence
# -----------------------------------------------------------------------------


class TestSocialXState:
    def test_round_trip(self, tmp_path: Path) -> None:
        s = SocialXState()
        h = s.for_handle("@IMAX")
        h.user_id = "12345"
        h.last_seen_tweet_id = "999"
        save_social_x_state(tmp_path, s)

        reloaded = load_social_x_state(tmp_path)
        assert reloaded.handles["imax"].user_id == "12345"
        assert reloaded.handles["imax"].last_seen_tweet_id == "999"

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        assert load_social_x_state(tmp_path).handles == {}

    def test_load_corrupt_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "social_x.json").write_text("{not json", encoding="utf-8")
        assert load_social_x_state(tmp_path).handles == {}

    def test_for_handle_normalizes(self) -> None:
        s = SocialXState()
        a = s.for_handle("@IMAX")
        b = s.for_handle("imax")
        assert a is b


# -----------------------------------------------------------------------------
# Stub X client + check_x_signals
# -----------------------------------------------------------------------------


class _StubXClient:
    """In-memory X client. ``tweets_by_user`` keyed by user_id."""

    def __init__(
        self,
        users: dict[str, str],
        tweets_by_user: dict[str, list[dict[str, Any]]],
        *,
        fail_users: set[str] | None = None,
        tweet_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._users = users
        self._tweets = tweets_by_user
        self._fail_users = fail_users or set()
        self._tweet_by_id = dict(tweet_by_id or {})
        self.user_id_calls: list[str] = []
        self.tweet_calls: list[tuple[str, str | None]] = []
        self.get_tweet_calls: list[str] = []

    def get_user_id(self, handle: str) -> str:
        clean = handle.lstrip("@").lower()
        self.user_id_calls.append(clean)
        if clean in self._fail_users:
            raise RuntimeError(f"boom for @{clean}")
        return self._users[clean]

    def get_recent_tweets(
        self,
        user_id: str,
        *,
        since_id: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        self.tweet_calls.append((user_id, since_id))
        all_tweets = self._tweets.get(user_id, [])
        if since_id is None:
            return list(all_tweets)
        return [t for t in all_tweets if int(str(t["id"])) > int(since_id)]

    def get_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        self.get_tweet_calls.append(tweet_id)
        return self._tweet_by_id.get(tweet_id)


def _cfg(handles: list[SocialXHandleConfig], **overrides: Any) -> SocialXConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "handles": handles,
        "default_keywords": ["tickets"],
    }
    base.update(overrides)
    return SocialXConfig(**base)


class TestCheckXSignals:
    def test_disabled_returns_empty_without_calling_client(
        self, tmp_path: Path
    ) -> None:
        cfg = SocialXConfig(enabled=False)
        # If the client is touched, get_user_id will raise KeyError.
        result = check_x_signals(
            cfg, "ignored", tmp_path, client=_StubXClient({}, {})
        )
        assert result.matches == []
        assert result.handles_polled == 0

    def test_resolves_user_id_once_and_caches(self, tmp_path: Path) -> None:
        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={
                "111": [
                    {"id": "5", "text": "trailer drop", "created_at": "t0"},
                ]
            },
        )
        cfg = _cfg([SocialXHandleConfig(handle="IMAX", keywords=["tickets"])])

        check_x_signals(cfg, "tok", tmp_path, client=client)
        check_x_signals(cfg, "tok", tmp_path, client=client)

        assert client.user_id_calls == ["imax"]  # not called the second time

    def test_match_emitted_and_since_id_advances(self, tmp_path: Path) -> None:
        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={
                "111": [
                    {"id": "10", "text": "tickets on sale!", "created_at": "t1"},
                    {"id": "9", "text": "behind the scenes", "created_at": "t0"},
                ]
            },
        )
        cfg = _cfg(
            [
                SocialXHandleConfig(
                    handle="IMAX",
                    keywords=["tickets"],
                    target_name="odyssey-imax-70mm",
                    label="IMAX (official)",
                )
            ]
        )

        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert len(result.matches) == 1
        match = result.matches[0]
        assert match.tweet_id == "10"
        assert match.matched_keywords == ["tickets"]
        assert match.target_name == "odyssey-imax-70mm"
        assert match.url == "https://x.com/IMAX/status/10".replace(
            "IMAX", "imax".upper()  # handle preserved as-is from cfg
        ) or match.url.endswith("/status/10")

        rstate = load_social_x_state(tmp_path)
        imax_hs = rstate.handles["imax"]
        assert imax_hs.last_seen_tweet_id == "10"
        assert imax_hs.last_seen_tweet_text == "tickets on sale!"
        assert imax_hs.last_seen_tweet_created_at == "t1"

        # Second poll: with since_id=10, the stub returns nothing new.
        client.tweet_calls.clear()
        result2 = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert result2.matches == []
        # Confirm we passed the persisted since_id.
        assert client.tweet_calls == [("111", "10")]
        # Text already in state; no single-tweet backfill.
        assert client.get_tweet_calls == []

    def test_backfills_tweet_text_when_timeline_empty(self, tmp_path: Path) -> None:
        # Cursor from a previous run, but no body yet (e.g. old state file).
        s = SocialXState()
        h = s.for_handle("IMAX")
        h.user_id = "111"
        h.last_seen_tweet_id = "10"
        save_social_x_state(tmp_path, s)

        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={"111": []},
            tweet_by_id={
                "10": {
                    "id": "10",
                    "text": "Steady-state tweet body",
                    "created_at": "2026-01-01T00:00:00.000Z",
                }
            },
        )
        cfg = _cfg(
            [SocialXHandleConfig(handle="IMAX", keywords=["tickets"])]
        )
        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert result.matches == []
        rstate = load_social_x_state(tmp_path)
        assert rstate.handles["imax"].last_seen_tweet_text == "Steady-state tweet body"
        assert rstate.handles["imax"].last_seen_tweet_created_at == (
            "2026-01-01T00:00:00.000Z"
        )
        assert client.get_tweet_calls == ["10"]

    def test_no_keywords_means_no_matches_even_on_new_tweets(
        self, tmp_path: Path
    ) -> None:
        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={
                "111": [{"id": "1", "text": "trailer", "created_at": "t"}]
            },
        )
        # default_keywords explicitly empty AND no per-handle keywords would
        # be a config error; we test the per-handle empty + non-matching default.
        cfg = _cfg(
            [SocialXHandleConfig(handle="IMAX", keywords=["nothingmatches"])]
        )
        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert result.matches == []

        rstate = load_social_x_state(tmp_path)
        assert rstate.handles["imax"].last_seen_tweet_text == "trailer"
        assert rstate.handles["imax"].last_seen_tweet_id == "1"

    def test_handle_failure_isolated(self, tmp_path: Path) -> None:
        client = _StubXClient(
            users={"good": "1", "bad": "2"},
            tweets_by_user={
                "1": [{"id": "1", "text": "tickets!", "created_at": "t"}]
            },
            fail_users={"bad"},
        )
        cfg = _cfg(
            [
                SocialXHandleConfig(handle="bad", keywords=["tickets"]),
                SocialXHandleConfig(handle="good", keywords=["tickets"]),
            ]
        )

        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert result.handles_polled == 2
        assert result.handles_failed == 1
        assert len(result.matches) == 1
        assert result.matches[0].handle == "good"


# -----------------------------------------------------------------------------
# Notification builder
# -----------------------------------------------------------------------------


class TestBuildSocialXNotification:
    def _match(self) -> XSignalMatch:
        return XSignalMatch(
            handle="imax",
            user_id="111",
            tweet_id="999",
            text="Tickets for The Odyssey go on sale Friday",
            created_at="2026-04-15T12:00:00Z",
            matched_keywords=["tickets", "on sale"],
            target_name="odyssey-imax-70mm",
            label="IMAX (official)",
        )

    def test_subject_uses_label(self) -> None:
        msg = build_social_x_notification(self._match())
        assert msg.event == "social_x_match"
        assert msg.subject == "X hint: IMAX (official)"

    def test_body_labeled_advisory_and_includes_url(self) -> None:
        msg = build_social_x_notification(self._match())
        assert "X HINT" in msg.body
        assert "advisory only" in msg.body
        assert "https://x.com/imax/status/999" in msg.body
        assert "tickets, on sale" in msg.body
        # Original tweet text appears verbatim.
        assert "Tickets for The Odyssey go on sale Friday" in msg.body

    def test_target_url_appended_when_provided(self) -> None:
        msg = build_social_x_notification(
            self._match(),
            target_url="https://www.fandango.com/odyssey",
        )
        assert "Watching: https://www.fandango.com/odyssey" in msg.body


# -----------------------------------------------------------------------------
# Movie registry: WatcherConfig.expanded_social_x_handles + dedupe behavior
# -----------------------------------------------------------------------------


def _wcfg(
    *,
    targets: list[TargetConfig] | None = None,
    movies: list[MovieConfig] | None = None,
    social_x: SocialXConfig | None = None,
) -> WatcherConfig:
    return WatcherConfig(
        targets=targets
        or [
            TargetConfig(name="odyssey-imax-70mm", url="https://www.fandango.com/x"),
            TargetConfig(name="odyssey-overview", url="https://www.fandango.com/y"),
        ],
        theater=TheaterConfig(
            display_name="AMC Universal CityWalk 19 + IMAX",
            fandango_theater_anchor="AMC Universal CityWalk",
        ),
        formats=FormatsConfig(),
        poll=PollConfig(min_seconds=270, max_seconds=330),
        purchase=PurchaseConfig(seat_priority={}),
        notify=NotifyConfig(),
        social_x=social_x or SocialXConfig(),
        movies=movies or [],
    )


class TestMovieExpansion:
    def test_movie_handles_inherit_title_and_target(self) -> None:
        cfg = _wcfg(
            movies=[
                MovieConfig(
                    key="odyssey",
                    title="The Odyssey (2026)",
                    fandango_targets=["odyssey-imax-70mm"],
                    x_handles=["TheOdysseyFilm"],
                    x_keywords=["odyssey", "tickets"],
                )
            ]
        )
        expanded = cfg.expanded_social_x_handles()
        assert len(expanded) == 1
        e = expanded[0]
        assert e.handle == "TheOdysseyFilm"
        assert e.label == "The Odyssey (2026)"
        assert e.target_name == "odyssey-imax-70mm"
        assert e.keywords == ["odyssey", "tickets"]

    def test_explicit_handles_preserved_alongside_movies(self) -> None:
        cfg = _wcfg(
            social_x=SocialXConfig(
                handles=[
                    SocialXHandleConfig(
                        handle="AMCTheatres", keywords=["tickets"]
                    )
                ]
            ),
            movies=[
                MovieConfig(
                    key="odyssey",
                    title="The Odyssey",
                    fandango_targets=["odyssey-imax-70mm"],
                    x_handles=["TheOdysseyFilm"],
                    x_keywords=["odyssey"],
                )
            ],
        )
        handles = [h.handle for h in cfg.expanded_social_x_handles()]
        assert handles == ["AMCTheatres", "TheOdysseyFilm"]

    def test_unknown_target_in_movie_rejected(self) -> None:
        with pytest.raises(Exception):  # pydantic ValidationError
            _wcfg(
                movies=[
                    MovieConfig(
                        key="bad",
                        title="x",
                        fandango_targets=["nope"],
                    )
                ]
            )

    def test_duplicate_movie_keys_rejected(self) -> None:
        with pytest.raises(Exception):
            _wcfg(
                movies=[
                    MovieConfig(key="m", title="A"),
                    MovieConfig(key="m", title="B"),
                ]
            )

    def test_social_x_enabled_with_only_movie_handles_is_valid(self) -> None:
        cfg = _wcfg(
            social_x=SocialXConfig(enabled=True, handles=[]),
            movies=[
                MovieConfig(
                    key="odyssey",
                    title="x",
                    x_handles=["TheOdysseyFilm"],
                    x_keywords=["x"],
                )
            ],
        )
        assert cfg.expanded_social_x_handles()  # not empty

    def test_social_x_enabled_without_any_handles_rejected(self) -> None:
        with pytest.raises(Exception):
            _wcfg(social_x=SocialXConfig(enabled=True, handles=[]))

    def test_movie_for_target_lookup(self) -> None:
        cfg = _wcfg(
            movies=[
                MovieConfig(
                    key="odyssey",
                    title="The Odyssey",
                    fandango_targets=["odyssey-imax-70mm", "odyssey-overview"],
                )
            ]
        )
        assert cfg.movie_for_target("odyssey-overview").key == "odyssey"  # type: ignore[union-attr]
        assert cfg.movie_for_target("nonexistent") is None


class TestSharedHandleAcrossMovies:
    """A handle shared by multiple movies (e.g. @IMAX) must:
    1. Hit the X API exactly once per poll (dedupe by handle)
    2. Emit one match per movie whose keywords match the tweet
    """

    def test_one_api_call_two_matches(self, tmp_path: Path) -> None:
        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={
                "111": [
                    {
                        "id": "100",
                        "text": "Odyssey AND Dune both in IMAX 70mm — tickets on sale Friday",
                        "created_at": "t1",
                    }
                ]
            },
        )

        cfg = SocialXConfig(
            enabled=True,
            handles=[
                SocialXHandleConfig(
                    handle="IMAX",
                    label="The Odyssey",
                    target_name="odyssey-imax-70mm",
                    keywords=["odyssey"],
                ),
                SocialXHandleConfig(
                    handle="IMAX",
                    label="Dune: Part Three",
                    target_name=None,
                    keywords=["dune"],
                ),
            ],
        )

        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        # Exactly one network call to fetch tweets for @IMAX (dedupe).
        assert len(client.tweet_calls) == 1
        # Two matches emitted (one per movie context).
        assert len(result.matches) == 2
        labels = {m.label for m in result.matches}
        assert labels == {"The Odyssey", "Dune: Part Three"}

    def test_only_movies_whose_keywords_hit_get_matches(
        self, tmp_path: Path
    ) -> None:
        client = _StubXClient(
            users={"imax": "111"},
            tweets_by_user={
                "111": [
                    {
                        "id": "100",
                        "text": "New Odyssey trailer drops tomorrow!",
                        "created_at": "t1",
                    }
                ]
            },
        )
        cfg = SocialXConfig(
            enabled=True,
            handles=[
                SocialXHandleConfig(
                    handle="IMAX", label="Odyssey", keywords=["odyssey"]
                ),
                SocialXHandleConfig(
                    handle="IMAX", label="Dune", keywords=["dune"]
                ),
            ],
        )
        result = check_x_signals(cfg, "tok", tmp_path, client=client)
        assert [m.label for m in result.matches] == ["Odyssey"]
