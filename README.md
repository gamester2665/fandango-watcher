# fandango-watcher

Dockerized Playwright watcher for Fandango release drops at **AMC Universal
CityWalk Hollywood**, with a scripted A-List auto-purchaser gated by a
strict `$0.00` invariant and an optional Claude Computer Use fallback.

See [`PLAN.md`](./PLAN.md) for the full architecture and phased plan.

## Status

Currently at the end of **Phase 1** (scaffolding). The watcher, detector,
notifier, purchaser, and CU fallback are not implemented yet — the Docker
image builds and container starts, but `fandango-watcher watch` is a stub.

Implementation order: Phase 2 watcher → Phase 3 notify → Phase 4 scripted
purchaser (dry-run) → Phase 5 full auto-buy → Phase 6 CU fallback →
Phase 7 hardening.

## Quick start (target workflow)

```bash
# 1. Secrets + config
cp .env.example .env                      # fill in Twilio + SMTP (+ Anthropic if using CU)
cp config.example.yaml config.yaml        # edit targets / seat priorities

# 2. Build the image
docker compose build

# 3. One-time headed login to warm the Fandango + AMC Stubs session.
#    - Linux: `xhost +local:root` first, then the command below.
#    - macOS: run XQuartz and set DISPLAY=host.docker.internal:0
#    - Windows: run VcXsrv / X410, set DISPLAY=host.docker.internal:0
docker compose --profile tools run --rm login

# 4. Start the watcher + purchaser in the background
docker compose up -d

# 5. Tail logs
docker compose logs -f watcher
```

## Local development (without Docker)

```bash
uv sync
uv run playwright install chromium
uv run fandango-watcher --help
```

## Layout

```
.
├── Dockerfile                  # Python 3.13 + uv + Chromium via playwright install
├── docker-compose.yml          # watcher / login / once services + named volumes
├── .env.example                # Twilio + SMTP + ANTHROPIC_API_KEY template
├── config.example.yaml         # targets, format policy, seat priority, $0.00 invariant
├── PLAN.md                     # full architecture + phased plan
├── pyproject.toml              # uv-managed deps
└── src/fandango_watcher/
    ├── models.py               # Pydantic schemas for ParsedPageData (A/B/C)
    └── ...                     # Phase 2+ modules land here
```

## Volumes

| Volume              | Mount                             | Purpose                                 |
| ------------------- | --------------------------------- | --------------------------------------- |
| `fandango_profile`  | `/app/browser-profile`            | Playwright `user_data_dir` (A-List session) |
| `fandango_artifacts`| `/app/artifacts`                  | Screenshots + purchase-attempt traces   |
| `fandango_state`    | `/app/state`                      | `state.json` / SQLite                   |

## Safety model

- Watcher never sends a notification on every poll — only on `bad → good`
  transition per the state machine in [`PLAN.md`](./PLAN.md).
- Purchaser never clicks "Complete Reservation" unless the review-page DOM
  simultaneously shows total `$0.00`, a recognizable A-List benefit phrase,
  and matching showtime/theater/seats. Any mismatch halts and SMSs the
  human with the deep link.
- CU fallback is invoked only mid-flow on scripted failure and is subject
  to hard `max_steps` + `max_cost_usd` caps. The Python invariant still
  gates every final click — the model is not trusted to self-attest.

## License

Personal use.
