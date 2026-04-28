# Fandango CityWalk API Notes

Captured on 2026-04-28 from:

```text
https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all
```

## Useful URLs

The theater page loads showtimes from this JSON endpoint:

```text
https://www.fandango.com/napi/theaterMovieShowtimes/AAAWX?chainCode=AMC&startDate=2026-04-28&isdesktop=true&partnerRestrictedTicketing=
```

The date list comes from:

```text
https://www.fandango.com/napi/theaterCalendar/aaawx
```

`AAAWX` is the CityWalk theater id. `AMC` is the chain code. `startDate` is a
`YYYY-MM-DD` date from the theater calendar response.

## Format Behavior

The `format` value in the theater-page URL is a client-side filter, not an
effective `napi/theaterMovieShowtimes` query parameter. I checked variants such
as `&format=IMAX`, `&format=3D`, `&format=all`, `&filter=IMAX`, and
`&filmFormat=IMAX`; the JSON response still returned the same unfiltered 80
showtimes for 2026-04-28.

The page renders chips from `viewModel.formats` and filters in-browser by
matching each showtime's `filmFormat[].filterName`.

## Current CityWalk Format Values

Across the published CityWalk calendar from 2026-04-28 through 2026-12-24, the
format values currently exposed by Fandango are:

```text
all
IMAX
3D
IMAX 70MM
```

Use URL encoding for page links when a value contains a space:

```text
https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=IMAX%2070MM
```

Observed availability by `viewModel.formats` / `filmFormat[].filterName`:

| Format | First date | Last date | Dates seen | Filtered showtimes seen |
| --- | --- | --- | ---: | ---: |
| `3D` | 2026-04-28 | 2026-06-04 | 26 | 125 |
| `IMAX` | 2026-04-28 | 2026-12-20 | 35 | 117 |
| `IMAX 70MM` | 2026-07-17 | 2026-12-20 | 7 | 11 |

`all` is a UI sentinel used by the format chip. It appears as `data-format="all"`
and removes the `format` query parameter when clicked.

## Practical Takeaway

For this theater, poll `napi/theaterMovieShowtimes` by calendar date and read
`viewModel.formats` plus each showtime's `filmFormat[].filterName`. Do not rely
on a `format=` query parameter to filter the JSON response.
