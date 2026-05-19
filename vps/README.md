# Shared VPS deploy kit

Reusable tooling for Docker apps on the **RackNerd VPS** (`74.48.91.123`) next to Rose Astrology and the mail stack — without breaking neighbors.

**Copy this entire `vps/` folder** into any project repo (or submodule it). Each project adds one file under `vps/projects/<name>.env`.

---

## Quick start (new project)

1. **Copy the kit**
   ```bash
   cp -r /path/to/fandango_watcher/vps ./vps
   ```

2. **Host config (once per machine)**
   ```bash
   cp vps/host.env.example vps/host.env
   # Edit ROSE_SECRETS_VPS_MD path / VPS_HOST if needed
   ```
   `vps/host.env` is gitignored.

3. **Project config**
   ```bash
   cp vps/project.env.example vps/projects/my-app.env
   ```
   Set `VPS_REMOTE_DIR`, `VPS_HEALTHZ_PORT`, `VPS_COMPOSE_FILES`, `VPS_SECRET_FILES`, etc.

4. **Compose overlay** — add `docker-compose.vps.yml` (see `vps/templates/docker-compose.vps.yml.example`):
   - Bind **`127.0.0.1:<port>`** only
   - Use `.env.production` on the server

5. **Deploy**
   ```bash
   export VPS_PROJECT_ENV=$PWD/vps/projects/my-app.env   # or rely on auto-detect by repo name

   python vps/run_vps_cmd.py --project my-app --sync-secrets
   python vps/run_vps_cmd.py --project my-app "bash vps/scripts/preflight.sh"
   # On VPS after clone:
   bash vps/scripts/first-time.sh
   # Updates:
   bash vps/scripts/pull-and-restart.sh
   # Public HTTPS (geobregon.com zones — use API, not VPS cloudflared cert):
   bash vps/scripts/cloudflare-publish.sh
   ```

---

## Layout

```text
vps/
  README.md                 ← this file
  host.env.example          ← shared host (copy → host.env, gitignored)
  project.env.example       ← template for new projects
  run_vps_cmd.py            ← SSH/SFTP from Windows or Linux
  projects/
    fandango-watcher.env    ← example project config (no secrets)
  scripts/
    lib.sh                  ← env loading + compose helpers
    preflight.sh            ← abort if mail/Rose unhealthy
    verify-neighbors.sh     ← post-deploy Rose + mail check
    sync-secrets.sh         ← laptop → VPS secret upload
    sync-secrets.ps1
    pull-and-restart.sh     ← on VPS: git pull + compose up
    first-time.sh           ← on VPS: clone + first deploy
    deploy-remote.sh        ← from laptop: SSH + pull-and-restart
    cloudflare-publish.sh   ← public hostname via API (needs CLOUDFLARE_API_TOKEN in .env)
    cloudflare-publish-hostname.py
  templates/
    docker-compose.vps.yml.example
  docs/
    COLOCATION.md           ← host inventory, ports, safety
    DEPLOY.md               ← generic operator runbook
    CHECKLIST-fandango-watcher.md  ← end-to-end cutover we used
```

---

## Environment variables

### Host (`vps/host.env`)

| Variable | Purpose |
|----------|---------|
| `VPS_HOST` | SSH target (default `74.48.91.123`) |
| `VPS_SSH_USER` | SSH user (default `root`) |
| `ROSE_SECRETS_VPS_MD` | Path to Rose `secrets.vps.md` for password auth |
| `ROSE_PROD_URL` | Rose health URL (must stay HTTP 200) |
| `VPS_RESERVED_PORTS` | Ports owned by mail/Rose — do not bind |
| `VPS_MAIL_UNITS` | systemd units that must stay active |

### Project (`vps/projects/<name>.env`)

| Variable | Purpose |
|----------|---------|
| `VPS_PROJECT_NAME` | Label for logs |
| `VPS_REMOTE_DIR` | Path on VPS, e.g. `/root/my-app` |
| `VPS_REPO_URL` | Git clone URL |
| `VPS_HEALTHZ_PORT` | Localhost port on VPS |
| `VPS_HEALTHZ_PATH` | Health path, e.g. `/healthz` |
| `VPS_COMPOSE_FILES` | Space-separated compose files |
| `VPS_COMPOSE_SERVICE` | Service name for `up -d --build` |
| `VPS_SECRET_FILES` | `local:remote` pairs, e.g. `.env:.env.production config.yaml:config.yaml` |
| `VPS_PUBLIC_HOSTNAME` | Optional — e.g. `fandango.geobregon.com` for `cloudflare-publish.sh` |
| `VPS_TUNNEL_STRATEGY` | `reuse` (default) or `dedicated` |
| `VPS_TUNNEL_ID` | Tunnel UUID when reusing an existing connector |

Legacy names still work: `FANDANGO_VPS_*`, `ROSE_VPS_*`.

---

## SSH helper

Requires **Paramiko** (`pip install paramiko` or project venv).

```bash
# Auto-loads vps/projects/fandango-watcher.env with --project
python vps/run_vps_cmd.py --project fandango-watcher "docker ps"
python vps/run_vps_cmd.py -p fandango-watcher --sync-secrets
python vps/run_vps_cmd.py --upload ./local.txt /root/my-app/local.txt
```

Password resolution order: `VPS_SSH_PASSWORD` → repo `secrets.vps.md` → `ROSE_SECRETS_VPS_MD` → prompt.

---

## Safety rules (shared VPS)

- Only touch **`/root/<your-project>/`** and your compose project name
- Bind new apps to **`127.0.0.1:<port>`** — expose via Cloudflare Tunnel if public
- **Do not** use ports `7166`, `8989`, `8080`, `3306`, `25`, `587`
- **Do not** edit `/etc/cloudflared/config.yml` for Rose/email routes
- Run **`preflight.sh`** before and **`verify-neighbors.sh`** after every deploy
- On **2.4 GiB RAM**: do not `docker compose --build` Rose and your app at the same time

Full host inventory: [docs/COLOCATION.md](./docs/COLOCATION.md)

---

## fandango_watcher (this repo)

Project config: `vps/projects/fandango-watcher.env`

**Full cutover checklist:** [docs/CHECKLIST-fandango-watcher.md](./docs/CHECKLIST-fandango-watcher.md)

Thin wrappers in `scripts/vps-*.sh`, `scripts/run_vps_cmd.py`, and `scripts/cloudflare-publish-fandango.sh` delegate to `vps/`.

Project runbook: [docs/VPS_DEPLOY.md](../docs/VPS_DEPLOY.md)
