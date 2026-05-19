# VPS deploy (fandango_watcher)

Deploy the **same Docker stack** validated locally onto the shared RackNerd VPS (`74.48.91.123`) **without** breaking Rose Astrology or the mail stack.

**Gate:** Complete [docker_implementation.md](./docker_implementation.md) Phase 5 (24h local soak) before production cutover. This doc is prep work and the operator runbook.

**Host constraints:** See [VPS_COLOCATION_HANDOFF.md](./VPS_COLOCATION_HANDOFF.md) for ports, RAM (2.4 GiB), disk, and Tunnel patterns.

**Step-by-step checklist:** [VPS_DEPLOY_PLAN.md](./VPS_DEPLOY_PLAN.md)

---

## fandango_watcher on this VPS

| Item | Value |
|------|--------|
| Suggested path | `/root/fandango-watcher` |
| Compose project | `fandango-watcher` (from directory name) |
| Service | `watcher` → container `fandango_watcher` |
| Healthz / dashboard | **`127.0.0.1:8787`** (localhost only on VPS) |
| Public access | Optional Cloudflare Tunnel → `http://127.0.0.1:8787` |
| Secrets file | `.env.production` (mode `600`, never commit) |
| Config | `config.yaml` on server (copy from laptop) |
| Volumes | `fandango_profile`, `fandango_state`, `fandango_artifacts` |

**Do not use:** 7166 (Rose), 8989 (Rose hook), 8080, 3306, 25, 587.

---

## 1. One-time server setup

```bash
ssh root@74.48.91.123

cd /root
git clone git@github.com:gamester2665/fandango-watcher.git fandango-watcher
cd fandango-watcher

# Secrets + config (from laptop, SFTP, or Rose-style upload helper)
chmod 600 .env.production config.yaml
sed -i 's/\r$//' .env.production   # if edited on Windows

# Optional: restore volume tarballs from local backups/docker-volumes/
# docker run --rm -v fandango_watcher_fandango_state:/v -v "$PWD/backups/docker-volumes:/backup:ro" \
#   alpine sh -c 'tar -xzf /backup/fandango_state_*.tar.gz -C /v'
```

---

## 2. Deploy / update

On VPS:

```bash
cd /root/fandango-watcher
bash scripts/vps-pull-and-restart.sh
```

From laptop (SSH only; does not upload secrets):

```bash
export FANDANGO_VPS_HOST=74.48.91.123
bash scripts/vps-deploy.sh
```

Upload secrets first (requires SSH key or password auth):

```bash
bash scripts/vps-sync-secrets.sh
# or: powershell -File scripts/vps-sync-secrets.ps1
```

**SSH from this machine:** add your public key on the VPS (`~/.ssh/authorized_keys`), or use password auth once:

```bash
type $env:USERPROFILE\.ssh\id_ed25519.pub   # PowerShell — paste on VPS
ssh root@74.48.91.123
```

First deploy on VPS (after clone + secrets):

```bash
bash scripts/vps-first-time.sh
```

Compose files used:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build watcher
```

The VPS overlay sets `--no-open`, `.env.production`, and `127.0.0.1:8787:8787`.

---

## 3. Verify

On VPS:

```bash
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8787/api/status | head -c 400
docker compose logs watcher --tail 50
ss -tlnp | grep 8787
```

Rose still healthy:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z"
```

From laptop via port-forward:

```bash
ssh -L 8787:127.0.0.1:8787 root@74.48.91.123
curl -fsS http://127.0.0.1:8787/healthz
```

---

## 4. Cloudflare Tunnel (optional dashboard)

If you want HTTPS access without opening VPS :443:

1. Zero Trust → Tunnels → token-managed tunnel (`cloudflared.service`) or a **second** tunnel unit.
2. Public hostname, e.g. `watcher.example.com` → `http://127.0.0.1:8787`.
3. Restrict access (Cloudflare Access) — dashboard is read-only but exposes state.

Do **not** repoint `rose.geobregon.com` rules.

---

## 5. Headed login on VPS

Data-center IP may trigger Fandango friction. Plan a **one-time** headed session:

- VNC/noVNC into the VPS, or
- Warm profile locally, tarball `fandango_profile` volume, restore on VPS (same as local cutover seed).

```bash
# On VPS with DISPLAY/VNC configured:
docker compose -f docker-compose.yml -f docker-compose.vps.yml \
  --profile tools run --rm login
```

---

## 6. Disable duplicate Cloudflare Worker

While VPS watcher is primary, **disable or stop** the existing Cloudflare Worker cron that also polls/notifies — otherwise you get duplicate SMS.

Document the Worker name and disable step in your operator notes before VPS cutover.

---

## 7. Build discipline (2.4 GiB RAM)

- Do **not** run `docker compose --build` for Rose and fandango_watcher at the same time.
- Prefer CI/laptop `docker build` + registry, or build off-peak.
- Before deploy: `docker builder prune -f` if disk is tight.

---

## 8. Rollback

```bash
cd /root/fandango-watcher
docker compose -f docker-compose.yml -f docker-compose.vps.yml down
# Restore volume backup if needed (see scripts/docker-volume-backup.sh)
```

Re-enable local `uv run watch` or Cloudflare Worker only after intentional cutover.

---

## Related

- Local Docker runbook: [docker_implementation.md](./docker_implementation.md)
- Shared VPS inventory: [VPS_COLOCATION_HANDOFF.md](./VPS_COLOCATION_HANDOFF.md)
- Rose SSH helper (reuse pattern): `run_vps_cmd.py` in `rose_astrology` monorepo
