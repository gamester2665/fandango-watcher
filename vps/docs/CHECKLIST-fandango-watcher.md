# fandango_watcher VPS + public URL cutover checklist

Exact playbook used for production (May 2026). Copy `vps/` to other projects and adapt project env.

---

## Prerequisites

- [ ] Local Docker soak passed (or accept risk)
- [ ] `vps/host.env` from `host.env.example` (Rose `secrets.vps.md` path for SSH)
- [ ] `vps/projects/fandango-watcher.env` committed (no secrets)
- [ ] `docker-compose.vps.yml` — `127.0.0.1:8787`, `.env.production`
- [ ] `.env` + `config.yaml` on laptop (never commit)
- [ ] `CLOUDFLARE_API_TOKEN` in `.env` — Account **Cloudflare Tunnel Edit** + Zone **DNS Edit**

---

## 1. VPS deploy (Docker)

**From laptop:**

```bash
python vps/run_vps_cmd.py --project fandango-watcher --sync-secrets
python vps/run_vps_cmd.py --project fandango-watcher "bash vps/scripts/preflight.sh"
```

**On VPS** (first time):

```bash
cd /root/fandango-watcher   # or: bash vps/scripts/first-time.sh
bash vps/scripts/pull-and-restart.sh
```

**Or from laptop:**

```bash
bash vps/scripts/deploy-remote.sh   # needs VPS_PROJECT_ENV set, or run from repo with wrappers
# fandango: bash scripts/vps-deploy.sh
```

**Verify on VPS:**

```bash
curl -fsS http://127.0.0.1:8787/healthz
bash vps/scripts/verify-neighbors.sh
```

---

## 2. Public HTTPS (Cloudflare)

**Recommended:** reuse **`rose-astrology`** tunnel (same VPS `cloudflared.service`).  
Do **not** use `cloudflared tunnel route dns` from VPS cert for `geobregon.com` — cert is scoped to `mtom.co`.

**From laptop:**

```bash
bash vps/scripts/cloudflare-publish.sh
# or: bash scripts/cloudflare-publish-fandango.sh
# or: python vps/scripts/cloudflare-publish-hostname.py --hostname fandango.geobregon.com --service http://127.0.0.1:8787
```

**Verify:**

```bash
curl -fsS https://fandango.geobregon.com/healthz
```

---

## 3. Cutover (stop duplicates)

- [ ] **Stop local Docker:** `docker compose down` (laptop)
- [ ] **Stop local `uv run watch`** if running
- [ ] **Disable Cloudflare Worker cron** (`wrangler.toml` `*/5` job) if Worker is deployed — otherwise duplicate SMS with VPS watcher

After cutover, **laptop can be off** — VPS sends SMS via Twilio in `.env.production`.

---

## 4. Ongoing ops

| Task | Command |
|------|---------|
| Update app | On VPS: `bash vps/scripts/pull-and-restart.sh` |
| Sync secrets | `python vps/run_vps_cmd.py -p fandango-watcher --sync-secrets` |
| Dashboard | https://fandango.geobregon.com/ |
| SSH health | `python vps/run_vps_cmd.py -p fandango-watcher "curl -fsS http://127.0.0.1:8787/healthz"` |
| Preflight | `bash vps/scripts/preflight.sh` |

---

## 5. What we did *not* do (on purpose)

- **New dedicated tunnel** — unnecessary; rose-astrology connector already on VPS
- **Edit `/etc/cloudflared/config.yml`** — email.mtom.co only
- **Publish port on `0.0.0.0`** — localhost bind + Tunnel only

---

## Reference values (fandango_watcher)

| Item | Value |
|------|--------|
| VPS path | `/root/fandango-watcher` |
| Port | `127.0.0.1:8787` |
| Public URL | `https://fandango.geobregon.com` |
| Tunnel | `rose-astrology` (`7050a8bf-2e17-4b87-a74d-277ab6b9ffb3`) |
| Account | `7f3e024b68ea359931e13d4688fde4a6` |
