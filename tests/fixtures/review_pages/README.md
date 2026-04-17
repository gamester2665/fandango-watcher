# Review-page fixtures

Real Fandango review-page DOM snapshots used by `test_review_fixtures.py`
to lock the `$0.00` invariant against actual Fandango copy rather than
synthetic strings.

## How to grow the corpus

When you reach a `$0.00` review page in a manual checkout (best done
during a real release window, not on a stale URL):

```bash
uv run fandango-watcher dump-review \
  --url "https://www.fandango.com/checkout/..." \
  --name "odyssey_imax_70mm_alist_2026" \
  --headed
```

This writes:

- `<name>.json` — DOM snapshot (`title` + `bodyText`) plus an `expected`
  block you must edit by hand to describe the plan + invariant rules
  the fixture should satisfy.
- `<name>.png` — full-page screenshot (audit trail).

Then edit the `expected` block in the JSON:

```jsonc
{
  "expected": {
    "should_pass_invariant": true,   // or false for "$5.99" / no-A-List captures
    "plan": {
      "target_name": "odyssey-imax-70mm",
      "theater_name": "AMC Universal CityWalk",
      "showtime_label": "7:00p",
      "showtime_url": "https://www.fandango.com/...",
      "format_tag": "IMAX_70MM",
      "auditorium": 19,
      "seat_priority": ["N12", "N13", "N14"],
      "quantity": 1
    },
    "invariant": {
      "require_total_equals": "$0.00",
      "require_benefit_phrase_any": ["AMC A-List", "A-List Reservation"],
      "require_theater_match": true,
      "require_showtime_match": true,
      "require_seat_match": false
    }
  }
}
```

`pytest` will auto-discover the fixture and assert that
`extract_review_state(plan, snapshot)` + `validate_invariant(...)`
matches `should_pass_invariant`.

## Negative fixtures (very valuable!)

Capture broken pages too — a $5.99 upcharge, a missing-A-List benefit
page, a wrong-theater rendering. Set `should_pass_invariant: false` and
the test will assert the invariant correctly **halts**. These prove the
kill-switch works on real Fandango DOM.

## .gitignored?

No — fixtures are committed (small, text). Screenshots may be large; add
to `.gitignore` if size becomes an issue.
