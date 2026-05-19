# VPS deploy runbook (generic)

Operator steps for any project using the shared **`vps/`** kit on `74.48.91.123`.

Host inventory and port rules: [COLOCATION.md](./COLOCATION.md)

---

## 1. One-time setup (new project)

**On laptop**

1. Copy `vps/` into your repo (or use this repo’s copy).
2. Create `vps/projects/<repo-name>.env` from `vps/project.env.example`.
3. Add `docker-compose.vps.yml` from `vps/templates/docker-compose.vps.yml.example`.
4. Copy `vps/host.env.example` → `vps/host.env` (gitignored).

**On VPS**

```bash
ssh root@74.48.91.123
cd /root
git clone <your-repo-url> <your-project>
cd <your-project>
# Upload secrets from laptop first (step 2 below)
bash vps/scripts/first-time.sh
```

---

## 2. Upload secrets (from laptop)

```bash
export VPS_PROJECT_ENV=$PWD/vps/projects/<name>.env

python vps/run_vps_cmd.py --project <name> --sync-secrets
# or: bash vps/scripts/sync-secrets.sh
# Windows: powershell -File vps/scripts/sync-secrets.ps1 -ProjectName <name>
```

Never commit `.env`, `.env.production`, or `config.yaml`.

---

## 3. Deploy / update

**On VPS** (preferred — no SSH password from laptop):

```bash
cd /root/<your-project>
bash vps/scripts/pull-and-restart.sh
```

**From laptop** (requires SSH):

```bash
bash vps/scripts/deploy-remote.sh
```

Compose command is driven by `VPS_COMPOSE_FILES` and `VPS_COMPOSE_SERVICE` in project env.

---

## 4. Verify

```bash
bash vps/scripts/preflight.sh          # before deploy
bash vps/scripts/verify-neighbors.sh   # after deploy

docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
curl -fsS http://127.0.0.1:<port><health-path>
```

Rose must stay HTTP 200:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z"
```

**From laptop** (port-forward):

```bash
ssh -L <port>:127.0.0.1:<port> root@74.48.91.123
curl -fsS http://127.0.0.1:<port><health-path>
```

---

## 5. Cloudflare Tunnel (public HTTPS)

**For `geobregon.com`:** use the **API** from your laptop (`CLOUDFLARE_API_TOKEN` in `.env`).  
The VPS `cloudflared` origin cert is scoped to **`mtom.co`** — do not use `cloudflared tunnel route dns` for geobregon hostnames.

**Recommended:** add a hostname to an existing tunnel (`--strategy reuse`), e.g. rose-astrology on this VPS.

```bash
export VPS_PROJECT_ENV=$PWD/vps/projects/<name>.env   # sets VPS_PUBLIC_HOSTNAME, VPS_TUNNEL_ID
bash vps/scripts/cloudflare-publish.sh
curl -fsS https://<your-hostname>/healthz
```

Manual dashboard: Zero Trust → Networks → Tunnels → **rose-astrology** → Public Hostname.

Do **not** edit `/etc/cloudflared/config.yml` (email.mtom.co only).

---

## 6. Cutover (avoid duplicate notifications)

- Stop local Docker / `uv run watch` on laptop
- Disable Cloudflare Worker cron if deployed (VPS watcher becomes primary)

---

## 7. Build discipline (2.4 GiB RAM)

- Do not run `docker compose --build` for Rose and your app simultaneously.
- Before deploy: `docker builder prune -f` if disk is tight.
- Prefer CI-built images for heavy stacks.

---

## 8. Rollback

```bash
cd /root/<your-project>
docker compose -f docker-compose.yml -f docker-compose.vps.yml down
# Restore volume backup if needed
```

---

## 9. Ad-hoc SSH

```bash
python vps/run_vps_cmd.py --project <name> "docker ps"
python vps/run_vps_cmd.py --project <name> "bash vps/scripts/preflight.sh"
```

Password: `vps/host.env`, Rose `secrets.vps.md`, or `VPS_SSH_PASSWORD`.
