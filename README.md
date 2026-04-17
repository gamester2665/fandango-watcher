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
| 6     | Agent rescue (browser-use + VLM)    | wired into `run_scripted_purchase` on Complete-button miss; `max_cost_usd` enforcement + real-failure calibration pending. |
| 7     | Hardening / VPS readiness           | in progress (this README is part of it). |

373 unit tests; run `uv run pytest -q`.

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
uv run fandango-watcher once --target odyssey-overview

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
```

Artifacts produced:

| Path                                  | Written by                | What it is |
| ------------------------------------- | ------------------------- | ---------- |
| `artifacts/screenshots/`              | every `crawl_target`      | full-page PNG per crawl, pruned by `screenshots.max_age_days` (default 7). |
| `artifacts/purchase-attempts/<ts>/`   | every purchase attempt    | step-by-step PNGs of the checkout flow (scripted + rescue). |
| `artifacts/videos/`                   | `--video` / `record_video`| `.webm` per browser context (one per crawl, one per purchase). Finalized when the context closes. |
| `artifacts/traces/`                   | `--trace` / `record_trace`| Playwright `.zip` per context. Open with `npx playwright show-trace artifacts/traces/<file>.zip` for a time-travel debugger (DOM snapshots + screenshots + network + console + sources, per action). |
| `state/state.json`                    | `state.py`                | per-target last seen schema + last alert time so restarts don't re-alert. |
| `state/social_x.json`                 | `social_x.py`             | resolved X user_id + last seen tweet id per handle. |

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
| `fandango_state`     | `/app/state`           | `state.json` + `social_x.json`                          |

---

## CLI reference

```text
fandango-watcher <subcommand>

  once             Single crawl + classify + JSON to stdout. Flags:
                   --target / --url / --no-screenshot / --dry-run /
                   --headed / --video / --trace
  watch            Long poll loop (Twilio + SMTP + scripted purchaser).
                   Flags: --no-healthz / --healthz-port / --max-ticks /
                          --headed / --video / --trace
  login            Headed first-run to warm the persistent profile.
  test-notify      Fire one Twilio + SMTP message through the configured channels.
  test-purchase    Crawl + classify + plan + JSON. Never clicks Complete.
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
├── Dockerfile                        # mcr.microsoft.com/playwright/python + uv
├── docker-compose.yml                # watcher / login services + named volumes
├── .env.example                      # Twilio + SMTP + X + OPENROUTER_API_KEY / OPENAI_API_KEY
├── config.example.yaml               # targets, formats, seat priority, social_x, agent_fallback
├── PLAN.md                           # full architecture + phased plan
├── pyproject.toml                    # uv-managed deps; [agent] extra = browser-use + langchain-openai
└── src/fandango_watcher/
    ├── cli.py                        # argparse subcommands (lazy-imported)
    ├── config.py                     # Pydantic Settings + WatcherConfig (incl. BrowserConfig record_video / record_trace)
    ├── models.py                     # ParsedPageData (Schema A / B / C discriminated union)
    ├── reference_pages.py            # canonical Fandango fixtures (Odyssey, Dune Pt 3, Project Hail Mary, Mandalorian + Grogu)
    ├── watcher.py                    # crawl_target — Playwright + screenshot + classify
    ├── detect.py                     # Schema A/B/C classifier
    ├── state.py                      # per-target state machine + transitions
    ├── notify.py                     # Twilio + SMTP, parallel send, screenshot/video MIME attach
    ├── loop.py                       # poll loop (Fandango + decoupled X poller) + healthz
    ├── purchase.py                   # PurchaseAttempt + extract_review_state + validate_invariant ($0.00 gate)
    ├── purchaser.py                  # run_scripted_purchase + agent rescue wiring
    ├── agent_fallback.py             # AgentFallback Protocol + browser-use provider
    └── social_x.py                   # X poller (httpx + Bearer) + match_tweet
```

---

## Tests

```bash
uv run pytest -q                               # full suite (~4s, 373 tests)
uv run pytest tests/test_review_fixtures.py    # auto-discovered $0.00 invariant fixtures
uv run pytest tests/test_agent_fallback_golden.py  # invariant must halt even if agent claims success
uv run pytest tests/test_purchaser_rescue.py   # rescue is invoked + retried correctly
```

Add a real-world Fandango review fixture with:

```bash
uv run fandango-watcher dump-review --url <fandango-review-url> --headed
# then commit the JSON under tests/fixtures/review_pages/
```

---

## License

Personal use.
