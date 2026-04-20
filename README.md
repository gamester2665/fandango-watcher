# fandango-watcher

Playwright + Pydantic watcher for Fandango release drops at **AMC Universal
CityWalk Hollywood**, with:

- a transition-only **Twilio SMS + SMTP email** notifier (only fires on
  `bad → good` per-target),
- a scripted **AMC A-List auto-purchaser** gated by a hard `$0.00`
  invariant in Python (the model is *never* trusted to self-attest the
  total),
- an optional open-source **vision-LLM rescue fallback**
  (`browser-use` driving any OpenAI-compatible vision LLM — Qwen2.5-VL
  via OpenRouter is the default), invoked only on mid-flow scripted
  failures and *still* gated by the Python invariant on the final click,
- a decoupled **X / Twitter advisory poller** (Phase 2.5) that surfaces
  early "tickets soon" hints from official movie / studio handles
  without ever blocking the Fandango watcher.

See [`PLAN.md`](./PLAN.md) for the full architecture, schema design, and
phased checklist.

---

## Status

| Phase | Area                                | Status |
| ----- | ----------------------------------- | ------ |
| 1     | Source validation + Docker skeleton | done — config + Docker image build, A-List $0.00 fixtures captured. |
| 2     | Watcher / classifier (3 schemas)    | done — `crawl_target` + classifier + `state.py` + `once` / `watch` CLI. |
| 2.5   | X / Twitter advisory poller         | done — `social_x.py` + `x-poll` / `movies` CLI + movie ↔ handle registry. |
| 3     | Notifications (Twilio + SMTP)       | done — transition-only, with optional screenshot/video MIME attachments. |
| 4     | Scripted purchaser (dry-run)        | done — `purchaser.py` + `$0.00` invariant + fixture-driven invariant tests. |
| 5     | Full auto-buy                       | wired; defaulted to `notify_only` until calibrated against a live drop. |
| 6     | Agent rescue (browser-use + VLM)    | wired into `run_scripted_purchase` on Complete-button miss; `max_cost_usd` enforced; calibration workflow in `tests/fixtures/rescue/README.md`. |
| 7     | Hardening / VPS readiness           | in progress (this README is part of it). |

427+ unit + integration tests; run `uv run pytest -q`.

---

## VPS / production (manual)

There is no bundled CI. Deploy by building the Docker image (or installing with `uv` on the host), copying `config.yaml` and a populated `.env`, and running `watch` (or `docker compose` as in the repo). Verify the process with `GET http://127.0.0.1:8787/healthz` (or your bound host/port) and optional `GET /metrics` for heartbeat counters. The read-only dashboard on `/` exposes `/api/status`, `/api/purchases` (tail of `state/purchases.jsonl`), and `/api/revision` for live reload.

---

## Quick start (local, no Docker)

```bash
# 1. Install deps + browser
uv sync                                    # core
uv sync --extra agent                      # add browser-use + langchain-openai for Phase 6 rescue
uv run playwright install chromium

# 2. Secrets + config
cp .env.example .env                       # Twilio + SMTP + X bearer + OPENROUTER_API_KEY/OPENAI_API_KEY
cp config.example.yaml config.yaml         # edit targets, formats, seat priority

# 3. Sanity checks (no network side effects beyond what each command says)
uv run fandango-watcher --help
uv run fandango-watcher refs               # list shipped Fandango reference fixtures
uv run fandango-watcher movies             # print movie ↔ X handle registry
uv run fandango-watcher x-poll --check-bearer    # validate X bearer without consuming tweet quota

# 4. Single live crawl (writes screenshot to ./artifacts/screenshots)
uv run fandango-watcher once --config config.yaml --target odyssey-overview
#    Optional: click a format chip before extract (e.g. IMAX 3D), or set
#    format_filter_click_* on the target in config.yaml — see config.example.yaml.
# uv run fandango-watcher once --config config.yaml --target odyssey-overview \
#     --format-filter-label "IMAX 3D"
#    Optional: also persist state/<target>.json like `watch` does:
# uv run fandango-watcher once --config config.yaml --target odyssey-overview --write-state

# 5. Long poll
uv run fandango-watcher watch
```

---

## Live observation workflow

When you want to **watch the bot work in a real Chromium window** (and
keep a recording / time-travel trace), use the per-run flags. They
override `config.yaml` for that one invocation, so production stays
headless.

```bash
# A. One-shot crawl, headed, with video + Playwright trace
uv run fandango-watcher once \
    --target odyssey-overview \
    --headed --video --trace

# B. Watch loop, headed, with video + trace per crawl & per purchase attempt
uv run fandango-watcher watch --headed --video --trace --max-ticks 3

# C. Test the notifier end-to-end (sends a real SMS + email)
uv run fandango-watcher test-notify --subject "smoke" --body "pipe is hot"

# D. Plan-only purchase (NEVER clicks Complete; just classifies + plans)
uv run fandango-watcher test-purchase --target odyssey-overview

# E. Same crawl + plan as D, but click a format chip first (live crawl only)
# uv run fandango-watcher test-purchase --target odyssey-overview --format-filter-label "IMAX 3D"
```

Artifacts produced:

| Path                                  | Written by                | What it is |
| ------------------------------------- | ------------------------- | ---------- |
| `artifacts/screenshots/`              | every `crawl_target`      | full-page PNG per crawl, pruned by `screenshots.max_age_days` (default 7). |
| `artifacts/purchase-attempts/<ts>/`   | every purchase attempt    | step-by-step PNGs of the checkout flow (scripted + rescue). |
| `artifacts/videos/`                   | `--video` / `record_video`| `.webm` per browser context (one per crawl, one per purchase). Finalized when the context closes. |
| `artifacts/traces/`                   | `--trace` / `record_trace`| Playwright `.zip` per context. Open with `npx playwright show-trace artifacts/traces/<file>.zip` for a time-travel debugger (DOM snapshots + screenshots + network + console + sources, per action). |
| `state/<target-name>.json`            | `state.py`                | per-target last seen schema + last alert time so restarts don't re-alert. |
| `state/social_x.json`                 | `social_x.py`             | resolved X user_id + last seen tweet id per handle. |
| `state/purchases.jsonl`               | `loop.py`                 | append-only JSON lines for purchase outcomes (when purchases run). |

You can also have screenshots and `.webm`s **emailed back** automatically
on transitions / purchase outcomes — set in `config.yaml`:

```yaml
notify:
  attach_screenshots_to_email: true
  email_max_attachments: 4
  email_max_attachment_bytes: 10_000_000
```

Twilio SMS stays text-only (the message includes the Fandango deep link).

---

## Read-only dashboard

When the HTTP server is enabled (default for `watch`), the same process serves a
**read-only HTML dashboard** alongside `/healthz`:

| Route | Purpose |
| ----- | ------- |
| `/` | Dashboard: per-target cards (screenshot, **video**, trace link), X poller, movies registry. With JS enabled, polls `/api/revision` and reloads the same tab when crawl state or artifacts change; `<noscript>` falls back to periodic refresh (`--refresh-seconds` / `watch --dashboard-refresh-seconds`). |
| `/api/status` | JSON snapshot (same data as the HTML page). |
| `/api/revision` | `{"revision": "..."}` fingerprint for live tab reload (cheap poll vs. full `/api/status`). |
| `/api/movies` | JSON list of `movies:` registry entries. |
| `/healthz` | Liveness JSON (Docker healthcheck). |
| `/metrics` | Prometheus text exposition (tick/error counters from the heartbeat; scrape locally if you use an agent). |
| `/artifacts/...` | Static files under your `artifacts/` tree (screenshots, videos, traces) — path-traversal safe. |

Per-target **history** (ticks, classifier schema, `last_success_at`) lives in **`state/<target-name>.json`**. It is updated when **`watch`** runs, or when you **`once --write-state`** (same `transition()` + save as `watch`). Plain **`once`** only prints JSON unless **`--write-state`** is set. The **`dashboard`** subcommand never crawls; it only reads those files. If you run locally, point `state.dir` and `screenshots.dir` at repo-relative paths (e.g. `state`, `artifacts/screenshots`) instead of Docker’s `/app/...` so files land where the dashboard expects them.

Bind address is **`127.0.0.1`** by default (not exposed to LAN). **`--no-healthz`** disables the server and the dashboard together.

```bash
# While watch is running (auto-opens your browser unless --no-open):
uv run fandango-watcher watch --headed --video

# Browse historical state + artifacts without crawling:
uv run fandango-watcher dashboard
# Optional: uv run fandango-watcher dashboard --host 127.0.0.1 --port 8787 --no-open
```

Use **`--no-open`** on `watch` if you only want the dashboard URL in the logs (e.g. SSH session).

Videos from crawls and purchases are renamed to **`{target-name}-{timestamp}.webm`** under `browser.record_video_dir` so each card can pick the latest file for that target.

---

## Quick start (Docker)

```bash
cp .env.example .env                       # Twilio + SMTP + agent + X keys
cp config.example.yaml config.yaml         # edit targets / seat priorities

docker compose build

# One-time headed login to warm the Fandango + AMC Stubs persistent profile.
#   - Linux: `xhost +local:root` first.
#   - macOS: run XQuartz, set DISPLAY=host.docker.internal:0
#   - Windows: run VcXsrv / X410, set DISPLAY=host.docker.internal:0
docker compose --profile tools run --rm login

# Long-running watcher
docker compose up -d
docker compose logs -f watcher
```

Volumes (declared in `docker-compose.yml`):

| Volume               | Mount                  | Purpose                                                |
| -------------------- | ---------------------- | ------------------------------------------------------ |
| `fandango_profile`   | `/app/browser-profile` | Playwright `user_data_dir` (Fandango / AMC Stubs login) |
| `fandango_artifacts` | `/app/artifacts`       | Screenshots, videos, traces, purchase-attempt steps    |
| `fandango_state`     | `/app/state`           | Per-target `*.json`, `social_x.json`, optional `purchases.jsonl` audit log |

---

## CLI reference

```text
fandango-watcher <subcommand>

  once             Single crawl + classify + JSON to stdout. Flags:
                   --target / --url / --no-screenshot / --dry-run /
                   --headed / --video / --trace / --write-state (config only;
                   updates state/<target>.json; stdout wraps parsed + metadata).
                   Optional format-chip click (YAML or CLI override):
                   --format-filter-selector / --format-filter-label /
                   --format-filter-timeout-ms
  watch            Long poll loop (Twilio + SMTP + scripted purchaser).
                   Serves dashboard + /healthz unless --no-healthz.
                   Flags: --no-healthz / --healthz-port / --max-ticks /
                          --headed / --video / --trace / --no-open
  dashboard        Read-only UI only (no crawl). Flags:
                   --config / --host / --port / --no-open
  login            Headed first-run to warm the persistent profile.
  test-notify      Fire one Twilio + SMTP message through the configured channels.
  test-purchase    Crawl + classify + plan + JSON. Optional ``--stub`` runs
                   the scripted checkout to the review page only (no Complete
                   click). Same optional --format-filter-* as ``once`` when
                   crawling (ignored with --from-fixture).
  x-poll           One-shot poll of configured X handles. Use --check-bearer
                   to validate the bearer token without consuming tweet quota.
  movies           Print the movie ↔ X-handle ↔ Fandango-target registry.
  refs             Print the shipped Fandango reference fixtures.
  dump-review      Capture a Fandango review-page DOM snapshot + screenshot
                   into tests/fixtures/review_pages/ for invariant testing.
```

---

## Safety model

The whole thing is built around the rule **"the bot only commits to a
purchase when Python can prove the total is `$0.00` and the page shows a
recognizable A-List benefit phrase."**

- Watcher emits at most one notification per `bad → good` transition per
  target — see the state machine in [`PLAN.md`](./PLAN.md).
- `purchase.py::validate_invariant` re-extracts the review-page DOM and
  asserts simultaneously: `total == "$0.00"`, an A-List benefit phrase
  is present, and showtime / theater / seat selection match what the
  watcher chose. Any mismatch → halt + SMS the human with the deep link.
- Agent rescue (browser-use + vision LLM) is invoked **only** when the
  scripted Complete click misses *after* the invariant has already
  passed. Its prompt explicitly forbids clicking
  Complete / Place Order / Confirm, entering payment data, or
  substituting alternate seats. After rescue, Python re-reads the DOM
  and re-runs the invariant before retrying the Complete click — the
  model never gets to attest the total.
- Audit fields on every `PurchaseAttempt`: `agent_rescue_attempted`,
  `agent_rescue_outcome`, `agent_rescue_notes`. Plus the
  fixture-driven golden test
  (`tests/test_agent_fallback_golden.py`) proves the invariant halts
  even if the agent reports `SUCCEEDED` on a `$5.99` upcharge fixture.

---

## Layout

```
.
├── Dockerfile                        # python:3.13-slim-bookworm + uv + Playwright Chromium
├── docker-compose.yml                # watcher / login services + named volumes
├── .env.example                      # Twilio + SMTP + X + OPENROUTER_API_KEY / OPENAI_API_KEY
├── config.example.yaml               # targets, formats, seat priority, social_x, agent_fallback
├── PLAN.md                           # full architecture + phased plan
├── pyproject.toml                    # uv-managed deps; [agent] extra = browser-use + langchain-openai
└── src/fandango_watcher/
    ├── cli/                          # package: parser, commands, logging_setup; entry in __init__.py
    ├── artifacts.py                # prune old screenshots / videos / traces under artifacts/
    ├── dashboard.py                  # collect_dashboard_state + render_index_html (read-only UI)
    ├── healthz.py                    # /healthz, /metrics, optional dashboard + /artifacts static
    ├── config.py                     # Pydantic Settings + WatcherConfig (incl. BrowserConfig record_video / record_trace)
    ├── models.py                     # ParsedPageData (Schema A / B / C discriminated union)
    ├── reference_pages.py            # canonical Fandango fixtures (Odyssey, Dune Pt 3, Project Hail Mary, Mandalorian + Grogu)
    ├── watcher.py                    # crawl_target / crawl_targets_in_tick — Playwright + classify
    ├── detect.py                     # Schema A/B/C classifier
    ├── state.py                      # per-target state machine + transitions
    ├── notify.py                     # Twilio + SMTP, parallel send, screenshot/video MIME attach
    ├── loop.py                       # poll loop (Fandango + decoupled X poller) + healthz
    ├── purchase.py                   # PurchaseAttempt + validate_invariant ($0.00 gate)
    ├── purchaser.py                  # run_scripted_purchase + extract_review_state + agent rescue wiring
    ├── agent_fallback.py             # AgentFallback Protocol + browser-use provider
    ├── social_x.py                   # X poller (httpx + Bearer) + match_tweet
    ├── release_intel.py              # optional Grok-backed release intel for dashboard
    ├── playwright_video.py           # finalize .webm filenames when a context closes
    ├── extract_page.js               # bundled page extractor for Fandango crawl
    └── extract_review.js             # bundled review-page hints for purchaser invariant
```

---

## Tests

```bash
uv run pytest -q                               # full suite (~10s, 422 tests)
uv run ruff check src tests                      # lint (needs: uv sync --group dev)
uv run mypy src/fandango_watcher                 # typecheck (needs dev group)
uv run pytest tests/test_review_fixtures.py    # auto-discovered $0.00 invariant fixtures
uv run pytest tests/test_agent_fallback_golden.py  # invariant must halt even if agent claims success
uv run pytest tests/test_purchaser_rescue.py   # rescue is invoked + retried correctly
uv run pytest tests/test_rescue_calibration.py # prompt safety vs example failure_reason strings
```

Add a real-world Fandango review fixture with:

```bash
uv run fandango-watcher dump-review --url <fandango-review-url> --headed
# then commit the JSON under tests/fixtures/review_pages/
```

---

## Troubleshooting

**Port already in use (`8787`)** — Another process (or a second `watch` /
`dashboard`) is bound to the dashboard / healthz port. The server now binds
**exclusively** (`allow_reuse_address=False`), so a second start fails fast
with `OSError: [WinError 10048]` instead of silently round-robining requests
between two listeners. To clear it:
- **Windows:** `netstat -ano | findstr :8787` → `taskkill /F /PID <pid>`
  (kill the **`python.exe`** PID directly — `taskkill` on the parent `uv`
  wrapper leaves the Python child still bound to the port).
- **POSIX:** `lsof -nP -iTCP:8787 -sTCP:LISTEN` → `kill <pid>`.
- Or pick a different port: `fandango-watcher dashboard --port 8790`
  (and `--healthz-port` on `watch`), or set **`WATCHER_HEALTHZ_PORT`** in `.env`
  as the default when those flags are omitted.

**Crawl shows `theater_count: 0` / `not_on_sale` but the movie is on sale** —
(1) Fandango may need a **ZIP or location** before it renders theater cards —
use a format-filtered URL, warm the profile (`fandango-watcher login`), or
pick a URL with location context. (2) **Race:** the page can paint showtimes
after `domcontentloaded`; the crawler now waits for showtime DOM and can
re-extract once when a ticketing URL is already visible. (3) After `git pull`,
**restart `watch`** so the running process loads the latest extractor — stale
Python processes keep old behavior and keep writing `not_on_sale` into
`state/*.json`. See **Field notes** in [`PLAN.md`](./PLAN.md).

**Twilio or SMTP skipped / warnings** — Check `.env` matches `.env.example`
(`TWILIO_*`, `SMTP_*`). Run `fandango-watcher test-notify` to verify both
channels.

**X / Twitter `x-poll` errors** — Confirm `X_BEARER_TOKEN` and run
`fandango-watcher x-poll --check-bearer`. Free-tier rate limits may require
longer `social_x.min_seconds`.

**Agent rescue always `FAILED` (“browser-use not installed”)** — Install the
optional stack: `uv sync --extra agent`.

### Re-warming the browser profile (session expired)

The Playwright profile (`browser.user_data_dir`, e.g. `./browser-profile` or
Docker volume `fandango_profile`) holds Fandango + AMC Stubs cookies. After
logout, password change, or long idle:

- **Local:** run `uv run fandango-watcher login --headed` and complete sign-in
  in the Chromium window.
- **Docker:** `docker compose --profile tools run --rm login` with a working
  X11/WSL display (see Docker section above).

Then restart `watch`. Purchases and crawls reuse the same profile.

### Known risks

- **DOM drift** — Fandango can change class names and flows; update selectors
  and capture new `dump-review` fixtures when layouts change.
- **Friction** — CAPTCHAs, fraud checks, or extra verification can block
  automation; agent rescue is instructed to stop with `NEEDS_HUMAN`-style
  outcomes.
- **A-List policy** — Premium formats and special events may not always be
  no-upcharge; the `$0.00` invariant is designed to **halt** on any non-zero
  total rather than complete a bad order.

---

## License

Personal use.
