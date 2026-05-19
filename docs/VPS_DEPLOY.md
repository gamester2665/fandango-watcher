# VPS deploy (fandango_watcher)

Deploy the **same Docker stack** validated locally onto the shared RackNerd VPS (`74.48.91.123`) **without** breaking Rose Astrology or the mail stack.

**Shared VPS kit:** [vps/README.md](../vps/README.md) — scripts, SSH helper, and neighbor safety checks live under `vps/`. This project uses `vps/projects/fandango-watcher.env`; `scripts/vps-*.sh` are thin wrappers.

**Host constraints:** [vps/docs/COLOCATION.md](../vps/docs/COLOCATION.md)

**Generic runbook:** [vps/docs/DEPLOY.md](../vps/docs/DEPLOY.md)

**Step-by-step checklist:** [VPS_DEPLOY_PLAN.md](./VPS_DEPLOY_PLAN.md) · **Completed cutover playbook:** [vps/docs/CHECKLIST-fandango-watcher.md](../vps/docs/CHECKLIST-fandango-watcher.md)

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

**SSH from this machine:** add your public key on the VPS (`~/.ssh/authorized_keys`), or use password auth:

```bash
type $env:USERPROFILE\.ssh\id_ed25519.pub   # PowerShell — paste on VPS
ssh root@74.48.91.123
```

Password helper (Paramiko; reads `vps/host.env` + Rose `secrets.vps.md`):

```bash
python vps/run_vps_cmd.py --project fandango-watcher "docker ps"
python scripts/run_vps_cmd.py --sync-secrets    # wrapper → same thing
python scripts/run_vps_cmd.py "bash vps/scripts/preflight.sh"
```

Or use Rose's helper from its monorepo root (identical host):

```bash
cd G:/_backup/Code/_mom/rose_astrology
python run_vps_cmd.py "docker ps"
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

**Public URL:** `https://fandango.geobregon.com` → `http://127.0.0.1:8787`

**Recommended:** add a hostname to the existing **`rose-astrology`** tunnel (same VPS `cloudflared.service` connector). Do **not** create a second tunnel unless you need isolation.

Add `CLOUDFLARE_API_TOKEN` to `.env` (Account **Cloudflare Tunnel Edit** + Zone **DNS Edit**), then:

```bash
bash scripts/cloudflare-publish-fandango.sh
curl -fsS https://fandango.geobregon.com/healthz
```

Script: `vps/scripts/cloudflare-publish-hostname.py` (`--dry-run` to preview ingress).  
Dedicated tunnel: `--strategy dedicated` (second systemd unit — usually unnecessary).

Manual dashboard path (no API token): Zero Trust → Networks → Tunnels → **rose-astrology** → Public Hostname → `fandango.geobregon.com` → `http://127.0.0.1:8787`.

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
- Before deploy: `docker builder prune -f` if disk is tight (cache only — **not** `docker system prune -a`).

## 7b. Neighbor safety scripts

Before every VPS deploy:

```bash
bash scripts/vps-preflight.sh    # aborts if mail down or Rose ≠ 200
bash scripts/vps-pull-and-restart.sh   # includes preflight + post verify
bash scripts/vps-verify-neighbors.sh   # Rose + mail still OK after fandango up
```

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

- **Shared VPS kit:** [vps/README.md](../vps/README.md)
- Local Docker runbook: [docker_implementation.md](./docker_implementation.md)
- Shared VPS inventory: [vps/docs/COLOCATION.md](../vps/docs/COLOCATION.md)
