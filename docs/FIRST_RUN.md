# First run (clone → smoke)

Ordered steps to go from a fresh clone to a confident local run. The same flow appears in [README.md](../README.md) under **Operator checklist**; this page adds tooling and Windows notes.

## 1. Config and secrets

1. Copy `config.example.yaml` to `config.yaml` at the repo root (or use the bootstrap script below).
2. Copy `.env.example` to `.env` and fill Twilio, SMTP, optional `X_BEARER_TOKEN`, and any model keys you use.
3. Run **`fandango-watcher doctor`** — it loads the resolved config path (including `config.example.yaml` fallback when `config.yaml` is missing), checks YAML validity, compares `notify.channels` to env-backed credentials, and warns on risky settings (`purchase.mode: full_auto`, `social_x.enabled` without a bearer token, empty browser profile).

## 2. Bootstrap scripts (optional)

From the repo root:

- **Windows (PowerShell):** `powershell -File scripts/bootstrap.ps1`
- **Unix:** `bash scripts/bootstrap.sh`

Each script copies `config.example.yaml` → `config.yaml` only if `config.yaml` does not already exist. It does **not** create `.env` (secrets stay local and untracked).

## 3. Browser profile

Run **`fandango-watcher login --headed`** once so Fandango / AMC context lives under `browser.user_data_dir` from config. `doctor` will note if the directory is missing or empty.

## 4. Core smoke

| Step | Command |
|------|---------|
| Single crawl | `fandango-watcher once` (add `--target` / `--url` as needed) |
| Notifications | `fandango-watcher test-notify` |
| Purchaser dry path | `fandango-watcher test-purchase`; add `--stub` for a live scripted run to the review page without completing |
| Long poll + UI | `fandango-watcher watch` and open the dashboard (default health/dashboard port from `WATCHER_HEALTHZ_PORT` / config; see README) |

Stay on **`purchase.mode: notify_only`** until you deliberately escalate after calibration.

## 5. Windows / `uv` notes

If **`uv run`** fails while **`fandango-watcher.exe`** is still running (file lock), stop the process or use **`python -m fandango_watcher`** from the project environment. **`scripts/restart-dashboard.ps1`** (or `.sh`) can help cycle a stuck dashboard. Heavy shells sometimes hit memory limits; closing other apps or increasing the page file can help.

## 6. Before VPS

Do not deploy until local behavior is signed off (see README **VPS / production** and [PLAN.md](../PLAN.md)).
