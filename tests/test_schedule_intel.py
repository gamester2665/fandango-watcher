"""Tests for CityWalk schedule inventory + pin matching."""

from __future__ import annotations

from fandango_watcher.fandango_api import FandangoShowtimeRecord
from fandango_watcher.models import FormatTag
from fandango_watcher.movie_watch_state import PinnedShowtime
from fandango_watcher.schedule_intel import (
    build_schedule_snapshot,
    classify_pin_against_records,
    format_schedule_notification_body,
    record_matches_pin,
    schedule_fingerprint,
)
from fandango_watcher.config import ScheduleNotifyConfig


def _record(
    *,
    date: str = "2026-07-17",
    time: str = "7:00p",
    buyable: bool = False,
    fmt: FormatTag = FormatTag.IMAX_70MM,
    showtime_hash: str | None = "abc123",
) -> FandangoShowtimeRecord:
    return FandangoShowtimeRecord(
        theater_id="AAAWX",
        chain_code="AMC",
        date=date,
        movie_title="The Odyssey",
        format_names=["IMAX 70MM"],
        normalized_formats=[fmt],
        display_time=time,
        is_buyable=buyable,
        ticket_url="https://fandango.com/buy/x" if buyable else None,
        showtime_hash=showtime_hash,
    )


class TestScheduleIntel:
    def test_build_snapshot_groups_by_date(self) -> None:
        records = [
            _record(date="2026-07-17", time="7:00p"),
            _record(date="2026-07-17", time="10:00p"),
            _record(date="2026-07-18", time="1:00p"),
        ]
        snap = build_schedule_snapshot(
            movie_key="odyssey",
            movie_title="The Odyssey",
            theater_name="CityWalk",
            records=records,
            inspected_dates=["2026-07-17", "2026-07-18"],
            source="test",
        )
        assert len(snap.days) == 2
        assert len(snap.days[0].times) == 2

    def test_schedule_fingerprint_stable(self) -> None:
        records = [_record()]
        snap = build_schedule_snapshot(
            movie_key="odyssey",
            movie_title="The Odyssey",
            theater_name="CityWalk",
            records=records,
            inspected_dates=[],
            source="test",
        )
        assert schedule_fingerprint(snap) == schedule_fingerprint(snap)

    def test_format_notification_body_truncates(self) -> None:
        days = []
        for i in range(20):
            from fandango_watcher.schedule_intel import ScheduleDay

            days.append(ScheduleDay(date=f"2026-07-{i+1:02d}", times=["7:00p"]))
        from fandango_watcher.schedule_intel import MovieScheduleSnapshot

        snap = MovieScheduleSnapshot(
            movie_key="odyssey",
            movie_title="The Odyssey",
            theater_name="CityWalk",
            days=days,
        )
        body = format_schedule_notification_body(
            snap,
            cfg=ScheduleNotifyConfig(max_dates_in_sms=3),
            dashboard_url="http://localhost:8787/#movie-odyssey",
        )
        assert "(+17 more date(s) on dashboard)" in body
        assert "Pick a showtime:" in body

    def test_pin_match_by_hash(self) -> None:
        rec = _record(showtime_hash="hash-1")
        pin = PinnedShowtime(
            date="2026-07-17",
            time_label="7:00p",
            format=FormatTag.IMAX_70MM,
            showtime_hash="hash-1",
        )
        assert record_matches_pin(rec, pin)
        assert classify_pin_against_records(pin, [rec]) == "listed"

    def test_pin_live_when_buyable(self) -> None:
        rec = _record(buyable=True)
        pin = PinnedShowtime(
            date="2026-07-17",
            time_label="7:00p",
            format=FormatTag.IMAX_70MM,
            showtime_hash="abc123",
        )
        assert classify_pin_against_records(pin, [rec]) == "live"
