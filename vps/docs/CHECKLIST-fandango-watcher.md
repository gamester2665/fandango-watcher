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

### D1 watchlist CRUD (after deploy)

1. Set matching secrets in `.env.production` and Wrangler:
   - `CONFIG_API_URL=https://fandango-watcher.<account>.workers.dev`
   - `CONFIG_ADMIN_TOKEN=<same random token>`
   - `CONFIG_POLL_SECONDS=60`
   - `CONFIG_CACHE_PATH=/app/state/watchlist-cache.json`
2. Deploy Worker config API: `bash scripts/deploy-worker.sh`
3. Seed D1 from YAML: `uv run fandango-watcher config-seed --from config.yaml --apply --force`
4. Sync secrets to VPS and restart watcher container.
5. Verify:
   - `curl -fsS https://fandango.geobregon.com/api/status | jq '.runtime | {config_source, config_revision, config_writes_enabled}'`
   - Add/delete a movie from the dashboard; confirm `docker logs fandango_watcher | rg "config reload"`

Debug:

```bash
curl -sS "$CONFIG_API_URL/api/watchlist/revision"
curl -sS -X POST "$CONFIG_API_URL/api/movies" \
  -H "Authorization: Bearer $CONFIG_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Test","url":"https://www.fandango.com/test-2027-999999/movie-overview"}'
npx wrangler d1 execute fandango_watcher_db --remote --command \
  "SELECT key, value FROM config_meta; SELECT COUNT(*) AS n FROM movies;"
```

Rollback: unset `CONFIG_API_URL` on VPS, restart container (YAML-only mode).

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
