"""IMAX aspect-ratio research for watchlist movies (max expanded ratio + notes)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

SCHEMA_MIGRATION_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE movies ADD COLUMN aspect_ratio_max REAL",
    "ALTER TABLE movies ADD COLUMN is_real_imax INTEGER",
    "ALTER TABLE movies ADD COLUMN aspect_ratio_notes TEXT",
    "ALTER TABLE movies ADD COLUMN aspect_ratio_source TEXT",
    "ALTER TABLE movies ADD COLUMN aspect_ratio_updated_at TEXT",
)

ASPECT_INTEL_SOURCE = "manual-research-2026-05-27"


@dataclass(frozen=True, slots=True)
class MovieAspectIntel:
    aspect_ratio_max: float
    is_real_imax: bool
    aspect_ratio_notes: str


MOVIE_ASPECT_INTEL: dict[str, MovieAspectIntel] = {
    "odyssey": MovieAspectIntel(
        aspect_ratio_max=1.43,
        is_real_imax=True,
        aspect_ratio_notes=(
            "First Hollywood feature shot 100% on 15/70 IMAX film; full 1.43 runtime. "
            "Select venues (including CityWalk) may show 70mm film prints."
        ),
    ),
    "dune_part_three": MovieAspectIntel(
        aspect_ratio_max=1.43,
        is_real_imax=True,
        aspect_ratio_notes=(
            "Native IMAX 70mm sequences (IMDb: 1.43 dual laser/70mm, 1.90 digital, 2.20 70mm). "
            "CityWalk GT Laser can show 1.43 portions."
        ),
    ),
    "mandalorian_and_grogu": MovieAspectIntel(
        aspect_ratio_max=1.43,
        is_real_imax=True,
        aspect_ratio_notes=(
            "Filmed for IMAX digital; ~53 min of shifting expanded ratios. "
            "1.43 on GT/dual laser; 1.90 on standard digital IMAX."
        ),
    ),
    "disclosure_day": MovieAspectIntel(
        aspect_ratio_max=1.43,
        is_real_imax=True,
        aspect_ratio_notes=(
            "Spielberg shot with IMAX cameras; trade sources report 1.43 on GT/70mm "
            "(IMDb still lists 2.39 only as of research date)."
        ),
    ),
    "supergirl": MovieAspectIntel(
        aspect_ratio_max=1.90,
        is_real_imax=False,
        aspect_ratio_notes=(
            "~70 minutes of IMAX digital footage with shifting ratios; "
            "expect 1.90 max at CityWalk (no confirmed 1.43 listing yet)."
        ),
    ),
    "toy_story_5": MovieAspectIntel(
        aspect_ratio_max=1.85,
        is_real_imax=False,
        aspect_ratio_notes="Flat 1.85 only (IMDb); IMAX release is marketing/upcharge without expanded frame.",
    ),
    "spider_man_brand_new_day": MovieAspectIntel(
        aspect_ratio_max=1.90,
        is_real_imax=False,
        aspect_ratio_notes=(
            "Typical MCU 2.39 theatrical / 1.90 IMAX DMR pattern (like No Way Home). "
            "May skip US IMAX at launch due to The Odyssey window."
        ),
    ),
    "digger": MovieAspectIntel(
        aspect_ratio_max=2.39,
        is_real_imax=False,
        aspect_ratio_notes="Shot on 35mm VistaVision; IMAX distribution without expanded-ratio composition.",
    ),
    "focker_in_law": MovieAspectIntel(
        aspect_ratio_max=2.39,
        is_real_imax=False,
        aspect_ratio_notes="Standard comedy; no published expanded IMAX aspect ratio or large-format capture.",
    ),
    "avengers_doomsday": MovieAspectIntel(
        aspect_ratio_max=1.90,
        is_real_imax=False,
        aspect_ratio_notes=(
            "IMDb: 2.39 theatrical / 1.90 IMAX version (DMR bump, not native 1.43). "
            "Limited US IMAX vs Dune 3 exclusivity."
        ),
    ),
}


def aspect_intel_updated_at() -> str:
    return datetime.now(UTC).isoformat()


def aspect_intel_seed_sql(*, updated_at: str | None = None) -> list[str]:
    """Return SQL statements to populate aspect intel on existing movie rows."""
    ts = updated_at or aspect_intel_updated_at()
    statements: list[str] = []
    for key, intel in MOVIE_ASPECT_INTEL.items():
        notes = intel.aspect_ratio_notes.replace("'", "''")
        statements.append(
            "UPDATE movies SET "
            f"aspect_ratio_max = {intel.aspect_ratio_max}, "
            f"is_real_imax = {1 if intel.is_real_imax else 0}, "
            f"aspect_ratio_notes = '{notes}', "
            f"aspect_ratio_source = '{ASPECT_INTEL_SOURCE}', "
            f"aspect_ratio_updated_at = '{ts}' "
            f"WHERE key = '{key}';"
        )
    return statements
