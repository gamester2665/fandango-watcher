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

If **`uv run`** or **`uv sync`** fails with **“fandango-watcher.exe in use”**,
the Windows shim under `.venv\Scripts\` is still locked by a previous
`watch` / `dashboard` / CLI process (or a stray `fandango-watcher.exe`).

1. Stop the app if it is still running, or free the port (see README **Troubleshooting → Port already in use**).
2. Force-kill the shim so `uv` can replace it:
   ```bat
   taskkill /F /IM fandango-watcher.exe
   ```
   From Git Bash: `cmd.exe //c "taskkill /F /IM fandango-watcher.exe"`.
3. Retry your command (e.g. `uv run pytest -q`).

**Alternative:** run without touching the shim:  
`.venv\Scripts\python.exe -m pytest -q` or `python -m fandango_watcher …`.

**Dashboard stuck:** **`scripts/restart-dashboard.ps1`** (or `.sh`) can cycle the listener on the healthz port.

Heavy shells sometimes hit memory limits; closing other apps or increasing the page file can help.

## 6. Before VPS

Do not deploy until local behavior is signed off (see README **VPS / production** and [PLAN.md](../PLAN.md)).
