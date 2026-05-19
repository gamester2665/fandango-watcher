# VPS co-location handoff (second Docker project)

**Purpose:** Everything inferred from the **rose_astrology** monorepo + a live SSH survey of production, so another agent can deploy a **second** Docker stack on the **same** VPS without breaking Rose or the existing mail stack.

**Do not commit secrets.** SSH password lives in gitignored `secrets.vps.md` at monorepo root (or `ROSE_VPS_SSH_PASSWORD`). Rose hook tokens / tunnel tokens live in VPS systemd and Cloudflare — fetch on-server, never paste into git.

---

## 1. Host identity

| Field | Value |
|--------|--------|
| Provider | RackNerd (label `racknerd-03a2b82` in local ops notes) |
| Public IP | `74.48.91.123` |
| OS | Ubuntu, kernel 6.8.x, x86_64 |
| Hostname | `mail.mtom.co` (mail is primary role) |
| SSH | `root@74.48.91.123:22` |
| CPU | 2 vCPU (Intel Xeon E5-2697 v2 @ 2.70GHz, 2 sockets × 1 core) |
| RAM | **2.4 GiB** (swap **~1.2 GiB**, often **~800 MiB** in use) |
| Disk | **43 GiB** root (`/dev/vda2`), typically **~70%** used |
| Uptime | Long-lived (100+ days typical) |

**Capacity reality:** This is a **shared** box (mail + DB + two tunnels + Rose). Plan builds **off-peak**; expect **minutes** of origin unreachability during `docker compose up --build`. Prune Docker build cache regularly (`docker builder prune` — Rose alone had **~14 GiB** reclaimable cache).

---

## 2. What already runs (do not break)

### Mail / web (non-Docker)

| Service | Bind | Notes |
|---------|------|--------|
| Postfix | `0.0.0.0:25`, `0.0.0.0:587` | SMTP |
| Dovecot | (IMAP/POP) | `dovecot.service` |
| OpenDKIM / OpenDMARC / OpenARC | `127.0.0.1:8891`, `8893`, `8895` | |
| PostSRSd | `127.0.0.1:10001`, `10002` | |
| MariaDB | `127.0.0.1:3306` | |
| nginx (PostfixAdmin) | `127.0.0.1:8080` | PHP via `php8.3-fpm-postfixadmin.sock` |

### Cloudflare Tunnel (two daemons)

| Unit | Config | Public hostname(s) |
|------|--------|---------------------|
| `cloudflared-email-mtom-ui.service` | `/etc/cloudflared/config.yml` | `email.mtom.co` → `http://127.0.0.1:8080` |
| `cloudflared.service` | **Token-based** (`tunnel run --token …`) | **Remote-managed** in Cloudflare Zero Trust (Rose: `rose.geobregon.com`) |

Rose ingress (configured in **Cloudflare dashboard**, not in `config.yml`):

```text
rose.geobregon.com/__deploy_hook__  -> http://127.0.0.1:8989
rose.geobregon.com/*                -> http://localhost:7166
```

**HTTP/2 transport:** `/etc/systemd/system/cloudflared.service.d/transport-http2.conf` sets `TUNNEL_TRANSPORT_PROTOCOL=http2` (reduces QUIC/525 flakes). Apply the same pattern if you add another `cloudflared` unit.

### Rose Astrology (existing Docker project)

| Item | Value |
|------|--------|
| Repo on VPS | `/root/rose-astrology` |
| Compose project name | `rose-astrology` (from directory name) |
| Service | `app` → container `rose-astrology-app-1` |
| Published port | **`0.0.0.0:7166`** → container `7166` |
| Deploy hook | `127.0.0.1:8989` — `rose-astrology-hook.service` |
| Public URL | `https://rose.geobregon.com` |
| Git remote | `https://github.com/gamester2665/rose_astrology.git` (use **deploy key**, not PAT in URL) |
| Secrets file | `/root/rose-astrology/.env.production` (mode `600`, gitignored) |
| Deploy log | `/root/rose-astrology/.deploy-hook.last.log` |
| Named volume | `rose_better_auth_data` → `/app/data` (SQLite) |

**Laptop helper (Rose repo only):** `python run_vps_cmd.py "<cmd>"` | `upload` | `sync-dotenv-production` — defaults `ROSE_VPS_HOST=74.48.91.123`, `ROSE_VPS_SSH_USER=root`.

---

## 3. Port allocation (for the new project)

**Taken — do not use:**

| Port | Listener | Owner |
|------|----------|--------|
| 22 | sshd | system |
| 25, 587 | postfix | mail |
| 3306 | mariadb | mail stack |
| 7166 | docker-proxy | **Rose app** |
| 8080 | nginx | PostfixAdmin |
| 8989 | node | **Rose deploy hook** |
| 8891, 8893, 8895 | opendkim/dmarc/arc | mail |
| 10001, 10002 | postsrsd | mail |
| 20241, 20242 | cloudflared | tunnel metrics |

**Suggested for new app:**

| Use | Suggestion |
|-----|------------|
| App HTTP | **`127.0.0.1:7170`** (or 7180, 3001, etc.) — bind **localhost only**, expose via Tunnel |
| Optional second hook | **`127.0.0.1:8990`** (Rose uses 8989) |
| Public HTTPS | **New hostname** in Cloudflare Tunnel (e.g. `myapp.example.com` → `http://127.0.0.1:7170`) |

**Avoid** publishing Docker ports on `0.0.0.0` unless required; Rose uses `7166` on all interfaces — prefer `127.0.0.1:PORT:PORT` in compose.

**No host nginx on 443:** Traffic is **Tunnel-only** for Rose; no need to open 443 on the VPS firewall for the new app if you use the same pattern.

---

## 4. Recommended layout for project #2

```text
/root/<your-project>/          # clone your git repo here (sibling to rose-astrology)
  docker-compose.yml
  .env.production              # gitignored, mode 600
  .deploy-hook.last.log        # optional, if you copy Rose hook pattern
  scripts/
    vps-pull-and-restart.sh    # optional
    vps-deploy-hook-server.mjs # optional (copy from Rose or reuse generic)
```

**Compose project name** = directory name under `/root/` (e.g. `my-project` → network `my-project_default`).

**Disk budget:** Leave **≥10 GiB** free before heavy builds; prune with:

```bash
docker system df
docker builder prune -f
docker image prune -f
```

**RAM:** If the new image build runs **while** Rose builds, expect swap thrash. Options:

- Serialize builds (don't trigger both hooks at once)
- Build on laptop/CI, `docker save` / registry pull on VPS
- Upsize VPS to **4 GiB** (operator decision)

---

## 5. Docker patterns (copy from Rose)

### 5.1 Minimal `docker-compose.yml` template

```yaml
services:
  app:
    build: .
    ports:
      - "127.0.0.1:7170:7170"   # pick unused port
    restart: always
    env_file:
      - path: .env.production
        required: false
    environment:
      - NODE_ENV=production
      - PORT=7170
```

Use a **unique** compose project directory name so networks/volumes do not collide with `rose-astrology_*`.

### 5.2 Next.js `NEXT_PUBLIC_*` at build time

Rose lesson: `env_file:` is **runtime only**. For Next (or any build-arg inlining), export public vars before compose:

- Rose: `scripts/docker-compose-with-production-env.sh` — exports `NEXT_PUBLIC_*` from `.env.production`, then `exec docker compose "$@"`
- Your project: same wrapper or explicit `export` + `docker compose build`

### 5.3 `.env.production` hygiene

- LF line endings only (`sed -i 's/\r$//' .env.production` if edited on Windows)
- `chmod 600 .env.production`
- Never commit; document keys in `.env.production.example`

### 5.4 CRLF / Windows ops

- Git Bash SFTP: remote absolute paths as `//root/my-project/file` (MSYS strips single `/root/...`)
- SSH during heavy build: increase banner timeout (Rose `run_vps_cmd.py` uses 90s TCP / 180s banner)

---

## 6. Cloudflare Tunnel — add the new app

Rose uses a **token-managed** tunnel (`cloudflared.service`). To add a hostname:

1. Cloudflare Zero Trust → **Networks → Tunnels** → select the tunnel used by `cloudflared.service` (or create a **second** tunnel + systemd unit if you want isolation).
2. **Public hostname** → service `http://127.0.0.1:<YOUR_PORT>`.
3. Do **not** edit `/etc/cloudflared/config.yml` for Rose/email — that file is only for `email.mtom.co` → 8080.

**Optional deploy hook path** (same hostname):

```text
myapp.example.com/__deploy_hook__  -> http://127.0.0.1:8990
myapp.example.com/*                -> http://127.0.0.1:7170
```

Hook server only checks `POST` + `Authorization: Bearer <token>`, not path.

---

## 7. Auto-deploy hook (optional copy of Rose)

Rose files (monorepo):

- `scripts/vps-deploy-hook-server.mjs` — listens `127.0.0.1`, port from `DEPLOY_HOOK_PORT` (default **8989**)
- `scripts/vps-pull-and-restart.sh` — `git fetch`, checkout branch, `merge --ff-only`, `docker compose up --build -d`

**New project systemd sketch** (`/etc/systemd/system/my-project-hook.service`):

```ini
[Unit]
Description=My Project Deploy Hook
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/my-project
Environment=DEPLOY_HOOK_TOKEN=<generate-new-secret>
Environment=DEPLOY_REPO_ROOT=/root/my-project
Environment=DEPLOY_HOOK_PORT=8990
ExecStart=/usr/bin/node /root/my-project/scripts/vps-deploy-hook-server.mjs
Restart=always

[Install]
WantedBy=multi-user.target
```

Then: `systemctl daemon-reload && systemctl enable --now my-project-hook.service`

**GitHub → Cloudflare Worker:** Rose uses `cloudflare/deploy-hook-worker` with secrets `VPS_HOOK_URL` + `VPS_HOOK_TOKEN`. For project #2, either:

- A **second Worker** + webhook, or
- Extend the Worker to dispatch multiple hooks (not in repo today)

**Dry-run smoke:**

```bash
export DEPLOY_HOOK_DRY_RUN=1
curl -sS -X POST -H "Authorization: Bearer $DEPLOY_HOOK_TOKEN" http://127.0.0.1:8990/
# expect 202 + dryRun:true
```

---

## 8. Manual deploy commands (on VPS)

```bash
cd /root/my-project
git fetch origin main
git checkout main
git merge --ff-only origin/main
docker compose up -d --build
# or with Next public build args:
# bash scripts/docker-compose-with-production-env.sh up -d --build
```

Verify:

```bash
docker compose ps
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7170/health   # your health route
ss -tlnp | grep 7170
```

---

## 9. SSH from Windows (any project)

```bash
# From rose_astrology monorepo (reusable for ad-hoc commands):
export ROSE_VPS_SSH_PASSWORD='…'   # or use secrets.vps.md
python run_vps_cmd.py "docker ps"

# Override host for a different box later:
export ROSE_VPS_HOST=74.48.91.123
export ROSE_VPS_SSH_USER=root
```

For the **new** repo, copy the Paramiko pattern or use plain `ssh root@74.48.91.123`.

---

## 10. Rose-specific constraints (collision checklist)

| Risk | Mitigation |
|------|------------|
| Port **7166** / **8989** | Use other ports |
| Compose project name `rose-astrology` | Use distinct directory under `/root/` |
| Volume name `rose_better_auth_data` | Use your own volume names |
| Tunnel hostname `rose.geobregon.com` | Add **new** hostname; don't repoint Rose rules |
| `git pull` during Rose build | Stagger deploys |
| Disk full | Prune Docker; Rose images ~19 GiB + cache ~14 GiB |
| Memory | Don't run two `--build` at once on 2.4 GiB |

---

## 11. Verification after co-locating

```bash
# Rose still healthy
curl -sS -o /dev/null -w "%{http_code}\n" "https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z"
cd /root/rose-astrology && git rev-parse HEAD && git rev-parse origin/main

# New app
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7170/
docker compose -f /root/my-project/docker-compose.yml ps

# Mail untouched
systemctl is-active postfix dovecot nginx mariadb
```

From laptop (Rose repo): `ROSE_PROD_URL=https://rose.geobregon.com bash scripts/deploy-verify.sh`

---

## 12. Canonical Rose references

| Doc / file | Content |
|------------|---------|
| `docs/DEPLOY_AND_TRACK.md` | Push → Worker → Tunnel → hook → compose; 525/QUIC; drift checks |
| `_next/rose_astrology_nextjs/docs/DEPLOYMENT.md` | Docker, Tunnel ingress, hook setup |
| `cloudflare/deploy-hook-worker/README.md` | Worker secrets, webhook |
| `docker-compose.yml` | Rose service + volume |
| `Dockerfile` | Multi-stage Node 20, ephemeris download, port 7166 |
| `run_vps_cmd.py` | SSH/SFTP/sync-dotenv-production |
| `scripts/vps-deploy-hook-server.mjs` | Hook listener |
| `scripts/vps-pull-and-restart.sh` | Pull + compose |
| `scripts/docker-compose-with-production-env.sh` | NEXT_PUBLIC build-arg export |
| `scripts/vps-cloudflared-http2-transport.sh` | QUIC → HTTP/2 drop-in |

---

## 13. Prompt block for the other LLM

Copy from here down into the other project's chat:

---

You are deploying **a second Docker application** on an existing Ubuntu VPS.

**Host:** `root@74.48.91.123` (RackNerd, 2 vCPU, 2.4 GiB RAM, 43 GiB disk ~70% full). Hostname `mail.mtom.co`. **Shared with production mail stack (Postfix/Dovecot/MariaDB/nginx:8080) and Rose Astrology.**

**Existing Rose stack (do not break):**

- Path: `/root/rose-astrology`
- App: Docker `rose-astrology-app-1`, **`127.0.0.1` not required — currently `0.0.0.0:7166`**
- Deploy hook: `127.0.0.1:8989`, systemd `rose-astrology-hook.service`
- Public: `https://rose.geobregon.com` via Cloudflare Tunnel (token-managed `cloudflared.service`)
- Tunnel routes: `/__deploy_hook__` → 8989, `/*` → 7166

**Your tasks:**

1. Clone our repo to `/root/<project>/` (sibling to `rose-astrology`).
2. `docker-compose.yml`: bind app to **`127.0.0.1:<NEW_PORT>`** (pick unused; e.g. **7170**). Do not use 7166, 8989, 8080, 3306, 25, 587.
3. `.env.production` on server only, mode 600.
4. Add Cloudflare Tunnel public hostname → `http://127.0.0.1:<NEW_PORT>` (dashboard or new tunnel unit).
5. Optional: copy Rose hook pattern on port **8990** + systemd unit + GitHub webhook Worker.
6. Before/after: `docker builder prune` if disk tight; never run simultaneous `docker compose --build` with Rose.
7. Verify Rose: `curl https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z` → 200.

**SSH credentials:** operator provides via `secrets.vps.md` or `ROSE_VPS_SSH_PASSWORD` (not in git).

**Full detail:** see `rose_astrology` monorepo `docs/VPS_COLOCATION_HANDOFF.md`.

---

## 14. fandango_watcher (this repo)

| Item | Value |
|------|--------|
| Path on VPS | `/root/fandango-watcher` |
| Port | **`127.0.0.1:8787`** (healthz + dashboard) |
| Compose | `docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d` |
| Secrets | `.env.production` |
| Deploy script | `scripts/vps-pull-and-restart.sh` (on server) / `scripts/vps-deploy.sh` (from laptop) |
| Runbook | `docs/VPS_DEPLOY.md` |

**Gate:** 24h local Docker soak before VPS production cutover. Disable Cloudflare Worker cron when VPS is primary to avoid duplicate SMS.

**Profile migration:** Seed `fandango_profile` volume from local `browser-profile/` (see `scripts/docker-seed-volumes.*`) or run headed `login` on VPS via VNC.

---
