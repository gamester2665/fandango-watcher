# Docker Implementation

This document captures the current plan to make Docker Compose the signed-off primary runtime for `fandango_watcher` on the local Windows machine before moving anything to a VPS.

The VPS plan is intentionally parked until local Docker has passed build, cutover, and a 24-hour soak.

## Goal

Replace the daily-driver `uv run fandango-watcher watch --no-open` process with:

```powershell
docker compose up -d watcher
```

The Docker runtime must preserve the same behavior:

- Fandango direct API polling, with browser crawl fallback.
- X/Twitter advisory polling and cursor dedupe.
- Twilio notifications.
- Dashboard on `http://127.0.0.1:8787/`.
- `notify_only` purchase mode unless deliberately changed.
- Persistent browser profile, state, and artifacts.

## What Already Exists

The repo is already structurally Dockerized. This plan is about operator hardening and sign-off, not rebuilding Docker from scratch.

| Area | Already in repo | Remaining gap |
|------|-----------------|---------------|
| Runtime image | `Dockerfile` has Python 3.13, uv, Playwright Chromium, `tini`, healthcheck, app/dev stages | Build and sign off locally |
| Production compose | `docker-compose.yml` has `watcher`, `login`, `once`, named volumes, `.env`, `config.yaml`, loopback `127.0.0.1:8787`, restart policy | Add operator cutover/seed/smoke scripts |
| Dev compose | `docker-compose.dev.yml` bind-mounts `src/` and `tests/` into the development image | No major change needed |
| Profile | `fandango_profile` maps to `/app/browser-profile` | Seed best-effort or re-login in headed container |
| State | `fandango_state` maps to `/app/state` | Seed host `state/` before first Docker X poll |
| Artifacts | `fandango_artifacts` maps to `/app/artifacts` | Optional seed/export/backup |
| Tests | `tests/test_docker_assets.py` already checks Dockerfile, compose services, volumes, loopback bind, tools profile, ignores | Extend for new scripts/workflow |
| Docs | README has Docker quick start; `docs/DOCKER_DEV.md` covers dev overlay | Add operator runbook details here |
| Scripts | Host-native `scripts/smoke.*`, dev compose wrappers, bootstrap/restart scripts exist | Add Docker-specific smoke/cutover/seed/backup |
| CI | Cloudflare Worker deploy workflow exists | Add Docker build/test workflow |

Do not spend implementation time replacing what already exists unless local Docker sign-off reveals a concrete defect.

## Operating Principles

- One poller at a time. Docker cutover starts only after the current host `uv` watcher is stopped.
- State before profile. Seed `state/` first because it prevents X bootstrap replay and preserves target history.
- Profile is best-effort. A Windows Chromium profile may not work inside the Linux container; headed Docker login is the fallback.
- No accidental SMS. Docker smoke never sends `test-notify` unless explicitly requested.
- No secret churn. `.env` remains local, gitignored, out of the image, and out of logs.
- Production compose is the sign-off target. Use `docker-compose.yml` only for parity validation.
- Back up before overwrite. Host state and Docker volumes should be backed up before seeding or force operations.

## Remaining Work

1. ~~Add Docker operator scripts~~ — done (`scripts/docker-*.{sh,ps1}`).
2. ~~Add Docker build/test CI~~ — done (`.github/workflows/docker-build.yml`).
3. ~~Extend Docker static tests~~ — done (`tests/test_docker_assets.py`).
4. ~~Update docs~~ — done (README, `docs/FIRST_RUN.md`, `PLAN.md`; this file is the runbook).
5. **Execute local Docker validation** (operator):
   - build
   - seed
   - headed login if needed
   - cutover
   - 24-hour soak

## Functionality Preservation Contract

Dockerization is an ops/runtime change, not a product behavior change.

These must remain intact:

- Fandango direct API remains primary; browser crawl remains fallback.
- Existing 403 mitigation and poll cadence stay aligned with local config.
- X bootstrap still advances cursors without SMS.
- Shared X handles still dedupe API calls and notifications.
- Dashboard endpoints remain available:
  - `/`
  - `/healthz`
  - `/api/status`
  - `/api/purchases`
  - `/api/revision`
- Dashboard UI keeps X ops strip, tweet links, PT timestamps, and ticket-signal highlighting.
- Twilio notification behavior stays governed by `.env` and `notify.on_events`.
- `purchase.mode` remains `notify_only` unless deliberately changed.
- Purchaser `$0.00` invariant stays protected by existing tests.
- `state/*.json`, `state/social_x.json`, `state/purchases.jsonl`, release-intel cache, screenshots, and purchase artifacts move cleanly into Docker volumes.
- `.env` stays out of image layers, git, and logs.

Any Docker change that alters crawler logic, notification matching, dashboard rendering, or purchaser behavior is out of scope unless it is explicitly tested and called out.

## Critical Windows Reality

State files are portable. Browser profiles are not reliably portable.

| Migration | Expected outcome |
|-----------|------------------|
| `state/` to `fandango_state` | Should work; JSON files are OS-agnostic |
| `browser-profile/` to `fandango_profile` | Best-effort only; Windows Chromium profile may fail in Linux container |
| `artifacts/` to `fandango_artifacts` | Optional; useful for dashboard history |

Primary path:

1. Seed state.
2. Attempt profile seed.
3. Run container `doctor`.
4. If the profile is missing or invalid, run headed login inside Docker via VcXsrv/X410.

## Sign-Off Checklist

Docker is not considered primary until all of these pass:

- [ ] `docker compose build watcher` succeeds.
- [ ] Docker smoke exits 0.
- [ ] `GET http://127.0.0.1:8787/healthz` returns 200 while the container is running.
- [ ] Dashboard shows the same targets, X handles, and recent state as the pre-cutover baseline.
- [ ] At least one Fandango tick completes via direct API inside the container.
- [ ] At least one X poll cycle completes without bootstrap SMS spam.
- [ ] `/app/state` contains writable JSON after ticks.
- [ ] `docker compose logs watcher` has no startup traceback, repeated profile warning, or sustained 403 streak.
- [ ] No host `uv` watcher runs during the Docker soak.
- [ ] 24-hour soak completes with normal SMS volume and no healthcheck flapping.
- [ ] Rollback path is documented and tested once.

## Baseline Before Cutover

Capture this while the existing host watcher is still healthy:

- `/healthz` response.
- `/api/status` response saved to `artifacts/docker-baseline/status-before.json`.
- Latest modified times for `state/*.json` and `state/social_x.json`.
- Optional dashboard screenshot.
- PID currently listening on `127.0.0.1:8787`.

## Phased Checklist

### Phase 0: Preflight

- [ ] Confirm Docker Desktop is running with WSL2 backend.
- [ ] Confirm `.env` and `config.yaml` exist at repo root.
- [ ] Capture baseline from the current host watcher.
- [ ] Stop host `uv run watch` / `fandango-watcher.exe`.
- [ ] Verify port 8787 is free.

Commands:

```powershell
docker compose version
docker info
netstat -ano | findstr :8787
```

### Phase 1: Build

- [ ] Run `docker compose build watcher`.
- [ ] Confirm image exists as `fandango_watcher:latest`.
- [ ] Confirm Chromium install path appears in build logs.
- [ ] Record rough build time and image size.

Command:

```powershell
docker compose build watcher
```

### Phase 2: Back Up and Seed Volumes

Add backup and seed scripts.

Script contracts:

- Resolve Compose project volume names dynamically.
- Fail fast outside repo root.
- Never print `.env` values.
- Default seed should be state only.
- `--force` requires a backup first.

Seed order:

1. `fandango_state` from host `state/`.
2. `fandango_profile` from host `browser-profile/` as best-effort.
3. `fandango_artifacts` from host `artifacts/` optionally.

Verify:

```powershell
docker compose run --rm --entrypoint "" watcher fandango-watcher doctor
docker compose run --rm --entrypoint "" watcher sh -c "ls -la /app/state | head"
```

### Phase 2b: Headed Login If Needed

If profile seed fails:

- [ ] Start VcXsrv or X410.
- [ ] Set `DISPLAY=host.docker.internal:0`.
- [ ] Run the compose login service.
- [ ] Log into Fandango and AMC Stubs.
- [ ] Re-run container `doctor`.

Commands:

```powershell
$env:DISPLAY = "host.docker.internal:0"
docker compose --profile tools build login
docker compose --profile tools run --rm login
docker compose run --rm --entrypoint "" watcher fandango-watcher doctor
```

### Phase 3: Docker Smoke and Cutover Scripts

Add:

- `scripts/docker-smoke.*`
- `scripts/docker-cutover.*`

Docker smoke should run inside the production image:

```powershell
docker compose run --rm --entrypoint "" watcher fandango-watcher doctor
docker compose run --rm --entrypoint "" watcher fandango-watcher api-drift --max-dates 3
docker compose run --rm --entrypoint "" watcher fandango-watcher x-poll --check-bearer
docker compose up -d watcher
curl.exe -fsS http://127.0.0.1:8787/healthz
```

SMS smoke must be opt-in:

```powershell
if ($env:SMOKE_NOTIFY -eq "1") {
  docker compose run --rm --entrypoint "" watcher fandango-watcher test-notify `
    --subject "fandango-watcher docker smoke" `
    --body "Docker smoke OK"
}
```

Leave existing host-native `scripts/smoke.ps1` and `scripts/smoke.sh` intact.

### Phase 4: Cutover

Cutover flow:

1. Check Docker and repo root.
2. Capture baseline if current watcher is healthy.
3. Prompt or verify host `uv` watcher is stopped.
4. Back up Docker volumes.
5. Build unless `--skip-build`.
6. Seed state unless `--skip-seed`.
7. Seed profile unless `--no-profile-seed`.
8. Run Docker smoke.
9. Print dashboard and log commands.

Verify:

```powershell
docker compose ps
curl.exe -fsS http://127.0.0.1:8787/healthz
curl.exe -fsS http://127.0.0.1:8787/api/status
docker compose logs -f watcher
```

Rollback flow:

```powershell
docker compose down
uv run fandango-watcher watch --config config.yaml --no-open
```

If host `state/` was changed during experimentation, restore it from the pre-Docker backup first.

### Phase 5: 24-Hour Soak

Watch for:

- [ ] 0-30 minutes: first Fandango tick succeeds.
- [ ] 15-45 minutes: first X poll succeeds without SMS burst.
- [ ] 2-6 hours: no repeated 403s, no profile warning loop.
- [ ] 6-24 hours: container stays healthy.
- [ ] SMS volume matches expected behavior.
- [ ] Dashboard still reads current state after overnight run.

Inspect:

```powershell
docker compose exec watcher ls -la /app/state
docker compose logs watcher
docker volume inspect fandango_watcher_fandango_state
```

### Phase 6: CI Guardrail

Add `.github/workflows/docker-build.yml`.

Keep CI offline and secret-free:

```yaml
name: Docker Build

on:
  pull_request:
    paths:
      - "Dockerfile"
      - "docker-compose*.yml"
      - "src/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/docker-build.yml"
  push:
    branches: [main]
    paths:
      - "Dockerfile"
      - "docker-compose*.yml"
      - "src/**"
      - "tests/**"
      - "pyproject.toml"
      - "uv.lock"
      - ".github/workflows/docker-build.yml"

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build production image
        run: docker compose build watcher
```

Optional dev-image pytest:

```yaml
  test-dev-image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build dev image
        run: docker compose -f docker-compose.yml -f docker-compose.dev.yml build watcher
      - name: Run tests
        run: >
          docker compose -f docker-compose.yml -f docker-compose.dev.yml
          run --rm --entrypoint "" watcher uv run pytest -q
```

### Phase 6b: Tests

Existing tests that must keep passing:

- `tests/test_dashboard.py`
- `tests/test_social_x.py`
- `tests/test_loop.py`
- `tests/test_notify.py`
- `tests/test_fandango_api.py`
- `tests/test_direct_api_detect.py`
- `tests/test_healthz.py`
- `tests/test_state.py`
- `tests/test_config.py`
- `tests/test_config_example.py`
- `tests/test_login.py`
- `tests/test_purchaser.py`
- `tests/test_purchase.py`
- `tests/test_review_fixtures.py`
- `tests/test_docker_assets.py`

Run:

```powershell
uv run pytest -q
```

Extend `tests/test_docker_assets.py` only for new assets:

- [ ] Operator scripts exist.
- [ ] Docker smoke scripts use `docker compose`, not host `uv run`.
- [ ] SMS smoke is opt-in.
- [ ] Seed scripts avoid Docker Desktop internals.
- [ ] Backup scripts write under `backups/docker-volumes`.
- [ ] Script service names are present in compose.
- [ ] Docker CI workflow does not reference Twilio, X, `.env`, live smoke, or `test-notify`.

Suggested static test:

```python
def test_docker_operator_scripts_exist() -> None:
    for path in (
        "scripts/docker-smoke.sh",
        "scripts/docker-smoke.ps1",
        "scripts/docker-cutover.sh",
        "scripts/docker-cutover.ps1",
        "scripts/docker-seed-volumes.sh",
        "scripts/docker-seed-volumes.ps1",
        "scripts/docker-volume-backup.sh",
        "scripts/docker-volume-backup.ps1",
    ):
        assert (REPO_ROOT / path).exists(), f"missing {path}"
```

Optional final Docker pytest:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --entrypoint "" watcher uv run pytest -q
```

### Phase 7: Documentation

Update:

- README Docker quick start.
- `docs/FIRST_RUN.md`.
- `PLAN.md` VPS gate wording.

Keep `docs/DOCKER_DEV.md` focused on development overlay usage.

## Behavior Regression Matrix

Compare before cutover, after cutover, and after the 24-hour soak:

- [ ] Number of targets in `/api/status`.
- [ ] Latest `last_success_at` per target.
- [ ] Latest `state/social_x.json` cursors.
- [ ] Dashboard X ops strip count and error count.
- [ ] SMS count during first X poll after cutover.
- [ ] `purchase.mode` remains `notify_only`.
- [ ] `/api/purchases` tails `state/purchases.jsonl` if present.
- [ ] Screenshot and artifact paths resolve from dashboard.

## Downside Evaluation

### Operational

- First build is slow and large because of Playwright Chromium and Debian dependencies.
- Docker Desktop becomes a runtime dependency.
- Compose, named volumes, X server, Docker networking, and image builds add moving parts.
- Port `8787` conflicts remain possible.
- Logs move from terminal output to `docker compose logs`.

Mitigation: scripts centralize commands, sign-off includes rollback, and host `uv` remains available until Docker is proven.

### Data and State

- Named volumes hide files from normal Explorer view.
- Running host `uv` after Docker cutover can fork host state from Docker volume state.
- Windows browser profile may not work inside Linux container.
- Force-seeding can overwrite useful volume state.

Mitigation: state seed first, backups before overwrite, Docker volume becomes source of truth after cutover, headed login is the profile fallback.

### Notifications

- Duplicate SMS if host `uv` and Docker both poll.
- X bootstrap replay if `social_x.json` is not seeded.
- SMS smoke can cost money or annoy.

Mitigation: one-poller rule, required state seed, opt-in `SMOKE_NOTIFY`.

### Browser and Purchase

- Headed login on Windows is clunkier with VcXsrv/X410.
- Local Docker does not prove VPS IP behavior.
- Docker does not reduce auto-purchase risk.

Mitigation: keep `notify_only`, run headed login if needed, treat VPS as separate later soak.

### Security

- Secrets are available inside the container runtime.
- Dashboard exposes operational data.
- Volume backups may contain cookies/session data.

Mitigation: `.env` stays out of image/git/logs, dashboard remains loopback-only, backups stay gitignored and private.

### CI and Maintenance

- Docker CI is slower than unit tests.
- CI cannot validate secrets or login.
- Full pytest inside Docker doubles runtime.
- Scripts and docs can drift.

Mitigation: path-filter CI, keep live smoke local only, add static tests for script/workflow assumptions.

## Stop Conditions

Do not mark Docker primary and do not proceed to VPS if:

- Docker sends duplicate or bootstrapped X SMS.
- Docker dashboard shows stale state while logs show fresh ticks.
- Docker cannot keep a valid browser profile after headed login.
- Fandango direct API fails in Docker while host `uv` works with the same config.
- Any purchaser test fails.
- `purchase.mode` changes away from `notify_only` unintentionally.
- Docker healthcheck flaps during the 24-hour soak.

## VPS Gate

Unpark the VPS plan only when:

- Docker is the primary local runtime for at least 24 hours.
- State continuity is verified inside Docker volumes.
- Login/profile is valid inside the Linux container.
- Worker duplicate risk is understood and scheduled for disablement during VPS migration.
- Rollback from Docker to host `uv` has been documented and tested once.
