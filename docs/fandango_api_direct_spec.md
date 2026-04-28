# Fandango Direct API Spec

This document is a self-contained implementation spec for using the private
Fandango JSON endpoints observed behind the Universal Cinema AMC at CityWalk
Hollywood theater page. It is written so another coding agent, scraper, or data
pipeline can learn the endpoint contract without reading the original browser
trace or this repository's source code.

These endpoints are not documented or guaranteed by Fandango. Treat every field
as observed behavior, validate responses at runtime, and keep the browser-based
crawler as a fallback.

Observed source page:

```text
https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all
```

Observed theater id:

```text
AAAWX
```

Observed chain code:

```text
AMC
```

## Tool Consumption Summary

If another tool reads only one section, use this one.

Goal:

- Discover Fandango showtimes directly from JSON, without scraping rendered DOM.
- Detect CityWalk showtimes by date, movie, format, and ticket URL.
- Support format-focused polling such as `IMAX`, `IMAX 70MM`, and `3D`.
- In this repo, use the direct API as the primary `once`/`watch` detector and
  keep Playwright as fallback plus checkout/login automation.

Canonical endpoint sequence:

1. `GET /napi/theaterCalendar/{theater_id_lower}` to get valid dates.
2. `GET /napi/theaterMovieShowtimes/{theater_id_upper}?chainCode={chain_code}&startDate={yyyy-mm-dd}&isdesktop=true&partnerRestrictedTicketing=` for each date.
3. Iterate `viewModel.movies[].variants[].amenityGroups[].showtimes[]`.
4. Read each showtime's `filmFormat[].filterName`.
5. If `filmFormat` is empty, classify the showtime as `STANDARD`.
6. Use `ticketingJumpPageURL` only when it exists, `expired` is false, and
   `type` is an accepted buyable state such as `available`.

Important rule:

- Do not pass `format=` to `theaterMovieShowtimes` expecting server filtering.
  It was tested and returned unfiltered JSON.
- Match formats client-side from `filmFormat[].filterName` and normalized
  `FormatTag` values.
- Match movies using configured Fandango `movie_id` when known; otherwise use a
  conservative title substring from `movies[].title` or target overrides.

Machine-readable manifest:

```yaml
fandango_direct_api:
  status: private_observed_contract
  observed_at: "2026-04-28"
  base_url: "https://www.fandango.com"
  citywalk:
    theater_id_upper: "AAAWX"
    theater_id_lower: "aaawx"
    chain_code: "AMC"
    zip_code: "91608"
    source_page: "https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all"
  endpoints:
    calendar: "/napi/theaterCalendar/{theater_id_lower}"
    showtimes: "/napi/theaterMovieShowtimes/{theater_id_upper}?chainCode={chain_code}&startDate={date}&isdesktop=true&partnerRestrictedTicketing="
    nearby_theaters: "/napi/nearbyTheaters?limit={limit}&zipCode={zip_code}"
  primary_json_pointers:
    date_list: "/showtimeDates"
    showtime_payload: "/viewModel"
    date_formats: "/viewModel/formats"
    movies: "/viewModel/movies"
    movie_variants: "/viewModel/movies/*/variants"
    amenity_groups: "/viewModel/movies/*/variants/*/amenityGroups"
    showtimes: "/viewModel/movies/*/variants/*/amenityGroups/*/showtimes"
    showtime_formats: "/viewModel/movies/*/variants/*/amenityGroups/*/showtimes/*/filmFormat"
    showtime_filter_name: "/viewModel/movies/*/variants/*/amenityGroups/*/showtimes/*/filmFormat/*/filterName"
    ticket_url: "/viewModel/movies/*/variants/*/amenityGroups/*/showtimes/*/ticketingJumpPageURL"
  observed_format_values:
    ui_sentinel: ["all"]
    api_filter_names: ["IMAX", "3D", "IMAX 70MM"]
    standard_rule: "filmFormat == [] means STANDARD"
  tested_ineffective_showtimes_query_params:
    - "format=IMAX"
    - "format=3D"
    - "format=all"
    - "filter=IMAX"
    - "filmFormat=IMAX"
  buyable_showtime_rule:
    require_ticketingJumpPageURL: true
    require_expired_false: true
    accepted_type_values: ["available"]
```

The same contract is also available as a standalone file for tools that prefer
YAML over Markdown: [`docs/fandango_api_manifest.yaml`](fandango_api_manifest.yaml).

## Operating Model

Fandango's theater page does not server-filter showtimes by `format=` through
the showtime JSON endpoint. The browser loads all showtime data for a selected
date, renders format chips from the JSON, and filters the already-loaded data in
JavaScript.

For direct polling:

1. Fetch the theater calendar.
2. Pick the dates to inspect.
3. Fetch `theaterMovieShowtimes` for each date.
4. Read `viewModel.formats` to discover available filter values.
5. Read each showtime's `filmFormat[].filterName` to decide which showtimes
   match a format.
6. Use `ticketingJumpPageURL` as the buy/reserve URL.

Mental model for tools:

## Watcher Integration Contract

`fandango_watcher` now treats this direct API as the fast path:

1. `fandango-watcher once --config config.yaml --target <name>` and `watch`
   call the direct API first when `direct_api.enabled: true`.
2. The adapter in `src/fandango_watcher/direct_api_detect.py` converts matching
   buyable `FandangoShowtimeRecord` values into the same `ParsedPageData` union
   used by the Playwright crawler.
3. Matching uses, in order, target overrides, `movies[]` registry fields, then
   top-level format config:
   - `targets[].direct_api_movie_id`
   - `targets[].direct_api_movie_title`
   - `targets[].direct_api_formats`
   - `movies[].fandango_movie_id`
   - `movies[].title`
   - `movies[].preferred_formats`
   - `formats.require + formats.include`
4. The scanner fetches `theaterCalendar`, inspects up to
   `direct_api.max_dates_per_tick` dates, and stops early when
   `direct_api.stop_on_first_match: true` and a matching buyable showtime is
   found.
5. If the direct API raises or validates poorly, `direct_api.fallback_to_browser`
   decides whether to fall back to Playwright or record an error.
6. State files persist direct API status, inspected dates, formats seen, unknown
   formats, matching showtime hashes, fallback count, and drift warning text.
7. Dashboard `/api/status` exposes the same direct API fields under each target
   plus runtime `direct_api` config.
8. Use `fandango-watcher api-drift --max-dates 3` for an opt-in live drift
   report. Default tests stay deterministic and mocked.

```text
theater calendar -> dates
date -> all movies at theater
movie -> variants
variant -> amenity groups
amenity group -> showtimes
showtime -> format names + ticketing URL
```

The API is date-centric, not movie-centric. A watcher that cares about one movie
must fetch date/theater payloads and then filter `movies[]` by `id`, `title`, or
other movie metadata.

## Stable Inputs To Configure

Store these values in config instead of hard-coding them throughout code:

```yaml
theater:
  display_name: "Universal Cinema AMC at CityWalk Hollywood"
  theater_id: "AAAWX"
  theater_id_lower: "aaawx"
  chain_code: "AMC"
  zip_code: "91608"
  source_page_url: "https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all"
formats:
  desired_api_filter_names:
    - "IMAX"
    - "IMAX 70MM"
  standard_sentinel: "STANDARD"
  all_formats_ui_sentinel: "all"
```

Discovery hints:

- Use `nearbyTheaters` to discover `id`, `formattedID`, `chainCode`, and
  `theaterPageUrl`.
- Use the theater page URL only as a browser referer and user-facing link.
- Use `ticketingJumpPageURL` for the purchase/reservation entry point.

## HTTP Requirements

Direct unauthenticated requests worked with normal browser-like headers.

Recommended headers:

```http
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36
Accept: application/json,text/plain,*/*
Referer: https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all
```

Cookies were not required for the observed read-only JSON calls. If Fandango
starts returning empty or blocked responses, use Playwright to fetch from the
page context with `credentials: "include"` and the same theater page as the
referer.

Response assumptions a tool should validate:

- HTTP status is `200`.
- `Content-Type` includes `application/json`.
- Calendar response root is an object with `showtimeDates`.
- Showtimes response root is an object with `viewModel`.
- `viewModel.movies` is a list, even if empty.
- Each consumed showtime is an object; unknown fields should be ignored.

## Endpoint: Theater Calendar

Returns the list of dates Fandango currently publishes for a theater.

```text
GET https://www.fandango.com/napi/theaterCalendar/{theater_id}
```

CityWalk example:

```text
https://www.fandango.com/napi/theaterCalendar/aaawx
```

Path parameters:

- `theater_id`: Fandango theater id. The page uses lowercase in this URL, but
  `AAAWX` and `aaawx` both identify the same theater in observed calls.

Important response fields:

- `showtimeDates`: list of `YYYY-MM-DD` strings to pass to the showtimes
  endpoint.
- `selectedDate`: page-selected date.
- `firstShowtime`: first date with showtime data.
- `isEmpty`: whether the calendar has no showtime dates.
- `startDateFull` / `endDateFull`: display-oriented date labels.
- `calendar`: HTML/structured calendar payload used by the web UI.

Observed response summary on 2026-04-28:

```json
{
  "showtimeDates": ["2026-04-28", "2026-04-29", "2026-04-30"],
  "selectedDate": "2026-04-28",
  "isEmpty": false
}
```

The observed calendar contained 79 dates, from `2026-04-28` through
`2026-12-24`.

## Endpoint: Theater Movie Showtimes

Returns the theater, movies, variants, amenity groups, and showtimes for one
theater on one date.

```text
GET https://www.fandango.com/napi/theaterMovieShowtimes/{theater_id}?chainCode={chain_code}&startDate={date}&isdesktop=true&partnerRestrictedTicketing=
```

CityWalk example:

```text
https://www.fandango.com/napi/theaterMovieShowtimes/AAAWX?chainCode=AMC&startDate=2026-04-28&isdesktop=true&partnerRestrictedTicketing=
```

Path parameters:

- `theater_id`: Fandango theater id, uppercase in the observed showtimes URL.

Query parameters:

- `chainCode`: observed as `AMC` for CityWalk.
- `startDate`: `YYYY-MM-DD`, ideally from `theaterCalendar`.
- `isdesktop`: observed as `true`; matches the desktop web page.
- `partnerRestrictedTicketing`: observed as an empty query value.

Parameters tested but not effective:

- `format=IMAX`
- `format=3D`
- `format=all`
- `filter=IMAX`
- `filmFormat=IMAX`

All of those returned the same unfiltered JSON for the tested date. Filtering is
client-side.

Root response shape:

```json
{
  "viewModel": {
    "date": "2026-04-28",
    "formats": ["IMAX", "3D"],
    "theater": {},
    "movies": []
  }
}
```

### `viewModel`

Important fields:

- `date`: selected date for this response.
- `formats`: format filter values available on this date. These are the values
  the page uses for format chips, except for the UI-only `all` sentinel.
- `theater`: theater-level metadata.
- `movies`: movie list with showtimes.

### `viewModel.theater`

Important fields:

- `isTicketing`: whether Fandango ticketing is enabled for this theater.
- `details`: full theater metadata.

Important `viewModel.theater.details` fields:

- `id`: theater id, for CityWalk `AAAWX`.
- `formattedID`: lowercase theater id, for CityWalk `aaawx`.
- `chainCode`: for CityWalk `AMC`.
- `name`: display theater name.
- `sluggedName`: URL slug.
- `theaterPageUrl`: relative or absolute theater page URL.
- `address1`, `address2`, `city`, `state`, `zip`, `fullAddress`.
- `geo`: latitude/longitude object.
- `isTicketing`, `hasShowtimes`, `hasReservedSeating`.
- `amenities`: theater-level amenity objects.
- `amenitiesString`: comma-separated theater amenities.
- `theaterMessage`, `agePolicy`, `feeDisclosure`.

### `viewModel.movies[]`

Important movie fields:

- `id`: Fandango movie id.
- `title`: movie title with year.
- `rating`: MPAA or equivalent rating.
- `runtime`: display runtime.
- `releaseDate`: release date string/object as returned by Fandango.
- `genres`: genre list.
- `poster` / `darkPoster`: image metadata.
- `hasMatinee`: whether any showtime is marked matinee.
- `mopURI`: movie overview page URI.
- `variants`: grouped showtime variants.

### `movie.variants[]`

Variants group showtimes by broad presentation bucket.

Important variant fields:

- `filmFormatHeader`: UI header, for example `Standard`, `Premium Format`, or
  `3D`.
- `filmFormatHeaderClassName`: CSS class used by the page.
- `amenityGroupClassName`: CSS class for child amenity groups.
- `amenityGroups`: groups of showtimes sharing amenities.

Do not use `filmFormatHeader` alone for business logic. For example, IMAX
showtimes can appear under `Premium Format`; the actual filter value is on the
showtime.

### `variant.amenityGroups[]`

Important amenity group fields:

- `movieVariantId`: id used by the page for this movie/presentation group.
- `amenities`: amenity objects, each usually including `name`, `description`,
  and sometimes image metadata.
- `amenityString`: display string such as `Closed caption, Accessibility devices
  available, IMAX with Laser, Reserved seating`.
- `hasReservedSeating`: boolean.
- `isDolby`: boolean.
- `lateNightMsg`: optional display message.
- `showtimes`: actual showtime objects.

Amenities are useful for secondary classification. For primary format filtering,
prefer `showtime.filmFormat[].filterName`.

### `amenityGroup.showtimes[]`

Important showtime fields:

- `date`: human display time, for example `6:30p`.
- `screenReaderTime`: accessible display time, for example `6:30 PM`.
- `ticketingDate`: machine-friendly local datetime, for example
  `2026-04-28+18:30`.
- `type`: availability state, observed as `available` for buyable showtimes.
- `expired`: boolean.
- `showtimeHashCode`: opaque Fandango showtime id/hash.
- `filmFormat`: list of format filter objects.
- `ticketingJumpPageURL`: direct ticketing URL.
- `message`, `matineeMessage`, `hasMatineeMessage`: optional UI messages.

Standard showtimes can have an empty `filmFormat` list. Treat those as
`STANDARD` if no premium format is present.

Observed standard showtime shape:

```json
{
  "date": "10:00p",
  "expired": false,
  "showtimeHashCode": "v2-...",
  "screenReaderTime": "10 o'clock PM",
  "ticketingDate": "2026-04-28+22:00",
  "type": "available",
  "filmFormat": [],
  "ticketingJumpPageURL": "https://tickets.fandango.com/transaction/ticketing/mobile/jump.aspx?sdate=2026-04-28%2B22%3A00&from=mov_det_showtimes&source=desktop&mid=244794&tid=AAAWX&dfam=webbrowser&showtimehashcode=v2-..."
}
```

Observed IMAX showtime shape:

```json
{
  "date": "6:30p",
  "expired": false,
  "showtimeHashCode": "v2-...",
  "screenReaderTime": "6:30 PM",
  "ticketingDate": "2026-04-28+18:30",
  "type": "available",
  "filmFormat": [
    {
      "filterName": "IMAX",
      "order": 2
    }
  ],
  "ticketingJumpPageURL": "https://tickets.fandango.com/transaction/ticketing/mobile/jump.aspx?sdate=2026-04-28%2B18%3A30&from=mov_det_showtimes&source=desktop&mid=244541&tid=AAAWX&dfam=webbrowser&showtimehashcode=v2-..."
}
```

## Raw Data Model For Tools

This is an intentionally partial schema. It includes the fields needed for
watching, alerting, and entering ticketing. Tools should ignore unknown fields
and tolerate missing optional fields.

```typescript
type FandangoCalendarResponse = {
  showtimeDates?: string[];
  selectedDate?: string;
  firstShowtime?: string;
  isEmpty?: boolean;
  startDateFull?: string;
  endDateFull?: string;
  calendar?: unknown;
};

type FandangoShowtimesResponse = {
  viewModel?: {
    date?: string;
    formats?: string[];
    theater?: FandangoTheaterBlock;
    movies?: FandangoMovie[];
  };
};

type FandangoTheaterBlock = {
  isTicketing?: boolean;
  details?: {
    id?: string;
    formattedID?: string;
    chainCode?: string;
    name?: string;
    sluggedName?: string;
    theaterPageUrl?: string;
    address1?: string;
    address2?: string;
    city?: string;
    state?: string;
    zip?: string;
    fullAddress?: string;
    geo?: unknown;
    isTicketing?: boolean;
    hasShowtimes?: boolean;
    hasReservedSeating?: boolean;
    amenities?: unknown[];
    amenitiesString?: string;
  };
};

type FandangoMovie = {
  id?: number | string;
  title?: string;
  rating?: string;
  runtime?: string;
  releaseDate?: unknown;
  genres?: unknown[];
  mopURI?: string;
  hasMatinee?: boolean;
  variants?: FandangoVariant[];
};

type FandangoVariant = {
  filmFormatHeader?: string;
  filmFormatHeaderClassName?: string;
  amenityGroupClassName?: string;
  amenityGroups?: FandangoAmenityGroup[];
};

type FandangoAmenityGroup = {
  movieVariantId?: number | string;
  amenities?: Array<{ name?: string; description?: string }>;
  amenityString?: string;
  hasReservedSeating?: boolean;
  isDolby?: boolean;
  lateNightMsg?: string | null;
  showtimes?: FandangoShowtime[];
};

type FandangoShowtime = {
  date?: string;
  expired?: boolean;
  hasMatineeMessage?: boolean;
  showtimeHashCode?: string;
  matineeMessage?: string;
  message?: string | null;
  screenReaderTime?: string;
  ticketingDate?: string;
  type?: string;
  filmFormat?: Array<{ filterName?: string; order?: number }>;
  ticketingJumpPageURL?: string;
};
```

Required fields for a usable normalized showtime:

- `movie.title`
- `showtime.ticketingDate` or `showtime.date`
- `showtime.ticketingJumpPageURL`
- `showtime.expired`
- `showtime.type`

Optional but valuable fields:

- `movie.id`
- `showtime.showtimeHashCode`
- `showtime.filmFormat[].filterName`
- `variant.filmFormatHeader`
- `amenityGroup.amenityString`

## Endpoint: Nearby Theaters

Returns theaters near a zip code. This is useful for discovering theater ids,
chain codes, and theater page URLs.

```text
GET https://www.fandango.com/napi/nearbyTheaters?limit={limit}&zipCode={zip}
```

CityWalk-area example:

```text
https://www.fandango.com/napi/nearbyTheaters?limit=7&zipCode=91608
```

Query parameters:

- `limit`: max number of theaters to return.
- `zipCode`: zip code to search around.

Root response fields:

- `theaters`: list of theater metadata.
- `filteredTheaters`: secondary filtered result list.
- `expandedSearch`: whether Fandango expanded beyond the requested area.
- `concessionsEnabled`: whether concessions are supported in the result set.
- `chainInfo`: optional chain-level metadata.
- `theatersUri`: page URI for theater search results.

Important `theaters[]` fields:

- `id`: theater id, for example `AAAWX`.
- `formattedID`: lowercase theater id, for example `aaawx`.
- `chainCode`: chain code, for example `AMC`.
- `name`, `sluggedName`, `theaterPageUrl`.
- `address1`, `address2`, `city`, `state`, `zip`, `fullAddress`.
- `distance`: distance from the query location.
- `geo`: latitude/longitude object.
- `isTicketing`, `hasShowtimes`, `hasReservedSeating`.
- `amenities`, `amenitiesString`.

## Format Values

For CityWalk, the format values observed across the published calendar on
2026-04-28 were:

```text
all
IMAX
3D
IMAX 70MM
```

`all` is not from `showtime.filmFormat`; it is a UI sentinel used by the format
chip. Clicking it removes `format` from the page URL.

Observed format availability:

- `3D`: seen from 2026-04-28 through 2026-06-04.
- `IMAX`: seen from 2026-04-28 through 2026-12-20.
- `IMAX 70MM`: seen from 2026-07-17 through 2026-12-20.

Use URL encoding for page links:

```text
IMAX 70MM -> IMAX%2070MM
```

For watcher classification, normalize direct API strings like this:

```text
IMAX -> IMAX
IMAX 70MM -> IMAX_70MM
3D -> THREE_D
RealD 3D -> THREE_D
empty filmFormat -> STANDARD
```

## Filtering Algorithm

Given one `theaterMovieShowtimes` response:

```python
def showtime_format_names(showtime: dict) -> list[str]:
    formats = showtime.get("filmFormat") or []
    return [f["filterName"] for f in formats if f.get("filterName")]


def normalized_showtime_formats(showtime: dict) -> list[str]:
    names = showtime_format_names(showtime)
    return names or ["STANDARD"]


def iter_matching_showtimes(payload: dict, wanted_formats: set[str]):
    for movie in payload["viewModel"].get("movies", []):
        for variant in movie.get("variants", []):
            for group in variant.get("amenityGroups", []):
                for showtime in group.get("showtimes", []):
                    formats = set(normalized_showtime_formats(showtime))
                    if formats & wanted_formats:
                        yield {
                            "movie_id": movie.get("id"),
                            "movie_title": movie.get("title"),
                            "formats": sorted(formats),
                            "time": showtime.get("date"),
                            "ticketing_date": showtime.get("ticketingDate"),
                            "type": showtime.get("type"),
                            "expired": showtime.get("expired"),
                            "ticket_url": showtime.get("ticketingJumpPageURL"),
                            "showtime_hash": showtime.get("showtimeHashCode"),
                            "amenities": group.get("amenityString"),
                        }
```

Only treat a showtime as buyable when:

- `ticketingJumpPageURL` is non-empty.
- `expired` is false.
- `type` is `available` or another explicitly accepted buyable state.

Recommended normalized output shape:

```json
{
  "source": "fandango_direct_api",
  "theater_id": "AAAWX",
  "chain_code": "AMC",
  "date": "2026-04-28",
  "movie_id": 244541,
  "movie_title": "Michael (2026)",
  "format_names": ["IMAX"],
  "normalized_formats": ["IMAX"],
  "display_time": "6:30p",
  "screen_reader_time": "6:30 PM",
  "ticketing_date": "2026-04-28+18:30",
  "showtime_hash": "v2-...",
  "availability_type": "available",
  "expired": false,
  "is_buyable": true,
  "ticket_url": "https://tickets.fandango.com/transaction/ticketing/mobile/jump.aspx?sdate=...",
  "variant_header": "Premium Format",
  "amenities": "Closed caption, Accessibility devices available, IMAX with Laser, Reserved seating"
}
```

Recommended validation rules for normalized output:

- `source` must be `fandango_direct_api`.
- `date` must match the request `startDate`.
- `format_names` must be non-empty; use `["STANDARD"]` when raw
  `filmFormat` is empty.
- `normalized_formats` must use this repository's enum names when integrating
  with `FormatTag`.
- `is_buyable` must be computed by code, not copied from Fandango.
- `ticket_url` must start with `https://tickets.fandango.com/` before any
  purchase flow uses it.

## End-To-End Extraction Recipe

Use this as the canonical algorithm for another tool or agent.

```text
INPUT:
  theater_id = "AAAWX"
  chain_code = "AMC"
  wanted_formats = {"IMAX", "IMAX 70MM"}
  date_policy = near_term dates, release window dates, or all calendar dates

STEPS:
  1. Fetch /napi/theaterCalendar/aaawx.
  2. Validate showtimeDates is a list of YYYY-MM-DD strings.
  3. Select dates according to date_policy.
  4. For each date:
       a. Fetch /napi/theaterMovieShowtimes/AAAWX with chainCode=AMC.
       b. Validate response.viewModel exists.
       c. Read response.viewModel.formats for date-level available filters.
       d. Iterate movies -> variants -> amenityGroups -> showtimes.
       e. For each showtime, set raw_format_names to:
            showtime.filmFormat[].filterName values, or ["STANDARD"] if empty.
       f. Normalize raw_format_names to local enum names.
       g. Compute is_buyable:
            ticketingJumpPageURL present
            AND expired is false
            AND type is "available"
       h. Emit normalized showtime records.
  5. Match emitted records against wanted formats, movie ids, movie titles, or
     release windows.

OUTPUT:
  A list of normalized showtime records plus date-level formats observed.

DO NOT:
  - Scrape rendered DOM for showtimes unless direct API validation fails.
  - Assume format= filters the showtimes endpoint.
  - Use filmFormatHeader as the authoritative format.
  - Attempt purchase without ticketingJumpPageURL.
```

## Minimal Python Client

This uses the standard library only.

```python
from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.fandango.com"
THEATER_ID = "AAAWX"
CHAIN_CODE = "AMC"
REFERER = (
    "https://www.fandango.com/"
    "universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": REFERER,
}


def get_json(url: str) -> dict:
    request = Request(url, headers=HEADERS)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def theater_calendar(theater_id: str = THEATER_ID) -> list[str]:
    url = f"{BASE}/napi/theaterCalendar/{theater_id.lower()}"
    payload = get_json(url)
    return payload.get("showtimeDates") or []


def theater_showtimes(date: str, theater_id: str = THEATER_ID) -> dict:
    query = urlencode(
        {
            "chainCode": CHAIN_CODE,
            "startDate": date,
            "isdesktop": "true",
            "partnerRestrictedTicketing": "",
        }
    )
    url = f"{BASE}/napi/theaterMovieShowtimes/{theater_id}?{query}"
    return get_json(url)


def iter_showtimes(payload: dict):
    for movie in payload["viewModel"].get("movies", []):
        for variant in movie.get("variants", []):
            for group in variant.get("amenityGroups", []):
                for showtime in group.get("showtimes", []):
                    format_names = [
                        f["filterName"]
                        for f in showtime.get("filmFormat", [])
                        if f.get("filterName")
                    ] or ["STANDARD"]
                    yield {
                        "movie": movie.get("title"),
                        "formats": format_names,
                        "time": showtime.get("date"),
                        "ticketing_date": showtime.get("ticketingDate"),
                        "ticket_url": showtime.get("ticketingJumpPageURL"),
                    }


if __name__ == "__main__":
    first_date = theater_calendar()[0]
    payload = theater_showtimes(first_date)
    print("date:", first_date)
    print("available filters:", payload["viewModel"].get("formats", []))
    for item in iter_showtimes(payload):
        if "IMAX" in item["formats"] or "IMAX 70MM" in item["formats"]:
            print(item)
```

## Minimal JavaScript Fetch

Use this from a browser context, Playwright page, or Node runtime with `fetch`.

```javascript
const theaterId = "AAAWX";
const chainCode = "AMC";
const date = "2026-04-28";

const url =
  `https://www.fandango.com/napi/theaterMovieShowtimes/${theaterId}` +
  `?chainCode=${encodeURIComponent(chainCode)}` +
  `&startDate=${encodeURIComponent(date)}` +
  `&isdesktop=true&partnerRestrictedTicketing=`;

const response = await fetch(url, {
  headers: {
    Accept: "application/json,text/plain,*/*",
  },
  credentials: "include",
});

if (!response.ok) {
  throw new Error(`Fandango request failed: ${response.status}`);
}

const payload = await response.json();
const formats = payload.viewModel.formats;
const movies = payload.viewModel.movies;
```

## cURL Smoke Checks

Calendar:

```bash
curl -sS \
  -H "Accept: application/json,text/plain,*/*" \
  -H "User-Agent: Mozilla/5.0" \
  "https://www.fandango.com/napi/theaterCalendar/aaawx"
```

Showtimes:

```bash
curl -sS \
  -H "Accept: application/json,text/plain,*/*" \
  -H "User-Agent: Mozilla/5.0" \
  -H "Referer: https://www.fandango.com/universal-cinema-amc-at-citywalk-hollywood-aaawx/theater-page?format=all" \
  "https://www.fandango.com/napi/theaterMovieShowtimes/AAAWX?chainCode=AMC&startDate=2026-04-28&isdesktop=true&partnerRestrictedTicketing="
```

Nearby theaters:

```bash
curl -sS \
  -H "Accept: application/json,text/plain,*/*" \
  -H "User-Agent: Mozilla/5.0" \
  "https://www.fandango.com/napi/nearbyTheaters?limit=7&zipCode=91608"
```

Manual drift check script:

```bash
./.venv/Scripts/python.exe scripts/fandango_api_drift_check.py --max-dates 1
```

This script performs live network requests and prints a compact JSON report. It
is intentionally not part of the default unit test suite.

## Polling Guidance

Use the calendar endpoint as the source of truth for dates. For each watched
movie or format, only poll the date range you care about.

Recommended conservative behavior:

- Cache the calendar for at least several minutes.
- Poll showtimes for near-term dates more often than far-future dates.
- Add jitter to avoid fixed request patterns.
- Back off on HTTP 403, 429, 5xx, empty `movies`, or malformed JSON.
- Keep the browser-based crawler as a fallback if the direct API changes.

## Failure Modes

Handle these cases explicitly:

- Calendar has no `showtimeDates`: treat as no published dates.
- Showtimes response has no `viewModel.movies`: treat as no showtimes for that
  date, not necessarily a hard error.
- `viewModel.formats` omits a desired format: no matching format on that date.
- A showtime has no `ticketingJumpPageURL`: do not attempt purchase.
- A showtime has `expired: true`: ignore it for alerts and purchases.
- Unknown `filmFormat[].filterName`: log it and classify as `OTHER` until
  mapped.

## Integration Notes For This Repo

The existing Playwright crawler can keep working as a DOM fallback, but direct
API polling can be faster and less brittle for release detection.

Suggested direct API state for each target:

```json
{
  "theater_id": "AAAWX",
  "chain_code": "AMC",
  "date": "2026-04-28",
  "formats": ["IMAX", "3D"],
  "matching_showtimes": [
    {
      "movie_id": 244541,
      "movie_title": "Michael (2026)",
      "format": "IMAX",
      "ticketing_date": "2026-04-28+18:30",
      "ticket_url": "https://tickets.fandango.com/transaction/ticketing/mobile/jump.aspx?sdate=..."
    }
  ]
}
```

Suggested repo enum mapping:

```text
Fandango API       Repo FormatTag
IMAX               IMAX
IMAX 70MM          IMAX_70MM
3D / RealD 3D      THREE_D
70MM               SEVENTY_MM
Dolby / DOLBY      DOLBY
Laser at AMC       LASER_RECLINER when used as an amenity, not a filterName
empty filmFormat   STANDARD
unknown            OTHER
```

Keep the original page URL in notifications because it is user-friendly, but use
`ticketingJumpPageURL` for purchase flow entry.

## Agent Learning Checklist

An agent or tool has understood this file if it can answer these questions:

- Which endpoint lists valid dates? Answer:
  `/napi/theaterCalendar/{theater_id_lower}`.
- Which endpoint returns showtimes? Answer:
  `/napi/theaterMovieShowtimes/{theater_id_upper}` with `chainCode`,
  `startDate`, `isdesktop=true`, and empty `partnerRestrictedTicketing`.
- Where are format filter values listed for a date? Answer:
  `viewModel.formats`.
- Where is the authoritative per-showtime format? Answer:
  `showtime.filmFormat[].filterName`.
- What means standard format? Answer:
  `showtime.filmFormat` is empty.
- Does `format=IMAX` filter the showtimes endpoint? Answer:
  no, not in observed behavior.
- Which field starts ticketing? Answer:
  `ticketingJumpPageURL`.
- What should block a purchase attempt? Answer:
  missing `ticketingJumpPageURL`, `expired: true`, unknown/non-buyable `type`,
  malformed JSON, or a ticket URL outside `https://tickets.fandango.com/`.

## Minimal Test Fixtures To Build

If another tool turns this spec into code, create fixtures with these cases:

- Calendar response with multiple `showtimeDates`.
- Calendar response with empty or missing `showtimeDates`.
- Showtimes response with `viewModel.formats` containing `IMAX`.
- Showtimes response where an IMAX showtime has
  `filmFormat: [{"filterName": "IMAX"}]`.
- Showtimes response where a standard showtime has `filmFormat: []`.
- Showtimes response with `IMAX 70MM`.
- Showtimes response with an unknown `filterName`, which should normalize to
  `OTHER`.
- Showtimes response with `expired: true`, which should not be buyable.
- Showtimes response with missing `ticketingJumpPageURL`, which should not be
  buyable.

## One-Prompt Summary For Another Agent

Use Fandango's private JSON API instead of scraping the rendered theater page.
First call `https://www.fandango.com/napi/theaterCalendar/aaawx` for date
strings. Then call
`https://www.fandango.com/napi/theaterMovieShowtimes/AAAWX?chainCode=AMC&startDate={date}&isdesktop=true&partnerRestrictedTicketing=`
for each selected date. Iterate
`viewModel.movies[].variants[].amenityGroups[].showtimes[]`. The authoritative
format values are in `showtime.filmFormat[].filterName`; if that list is empty,
the showtime is `STANDARD`. `viewModel.formats` lists the date-level filter
values. Do not expect a `format=` query parameter to filter the JSON response.
Use `ticketingJumpPageURL` for ticketing only when it exists, `expired` is false,
and `type` is `available`.
