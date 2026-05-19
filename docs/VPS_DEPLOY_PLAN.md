# VPS deploy plan (fandango_watcher co-location)

Deploy **fandango_watcher** on `root@74.48.91.123` next to Rose Astrology and the mail stack, using [VPS_COLOCATION_HANDOFF.md](./VPS_COLOCATION_HANDOFF.md) collision rules and the reusable [vps/](../vps/README.md) kit ([VPS_DEPLOY.md](./VPS_DEPLOY.md)).

**Production cutover (done):** [vps/docs/CHECKLIST-fandango-watcher.md](../vps/docs/CHECKLIST-fandango-watcher.md) — includes Cloudflare API publish to `https://fandango.geobregon.com`.

---

## Safety — existing connections (will NOT break)

Deploy scripts are **read-only toward neighbors** until `compose up` in `/root/fandango-watcher` only.

| Existing stack | What we do | What we never do |
|----------------|------------|------------------|
| **Mail** (Postfix, Dovecot, nginx:8080, MariaDB) | `vps-preflight.sh` asserts units stay `active`; re-check after deploy | `systemctl stop/restart` mail; bind ports 25/587/8080/3306 |
| **Rose** (`/root/rose-astrology`, `:7166`, hook `:8989`) | Preflight/postflight `curl` Rose → must stay **200**; abort if not | `cd` Rose dir; `docker compose` there; change `:7166`/`:8989` |
| **Cloudflare Tunnel** (`email.mtom.co`, `rose.geobregon.com`) | Optional **new** hostname → `127.0.0.1:8787` only | Edit `/etc/cloudflared/config.yml`; repoint Rose ingress rules |
| **Docker isolation** | Compose project `fandango-watcher`; volumes `fandango_watcher_fandango_*` | `docker system prune -a`; prune Rose volumes; simultaneous `--build` with Rose |

**Port bind:** `127.0.0.1:8787:8787` only — no new `0.0.0.0` listeners (unlike Rose’s `:7166`).

**Cutover (Phase 6)** affects **fandango only**: disable Cloudflare **Worker** cron (duplicate SMS), stop **local** Docker/`uv watch`. Does **not** stop Rose, mail, or tunnels.

**Rollback:** `cd /root/fandango-watcher && docker compose -f docker-compose.yml -f docker-compose.vps.yml down` — removes fandango container only; Rose/mail untouched.

Automated guards: `scripts/vps-preflight.sh` (before build), `scripts/vps-verify-neighbors.sh` (after healthz).

---

## Resource map

| Stack | Path / port | Do not touch |
|-------|-------------|--------------|
| Mail | `:25`, `:587`, `:8080`, `:3306` | Postfix, Dovecot, nginx, MariaDB |
| Rose | `/root/rose-astrology`, `:7166`, hook `:8989` | `rose.geobregon.com` tunnel routes |
| **fandango_watcher** | `/root/fandango-watcher`, **`127.0.0.1:8787`** | Volumes `fandango_watcher_fandango_*` |

---

## Phase 0 — Local gate (laptop)

- [ ] Docker watcher running: `docker compose up -d watcher`
- [ ] Host `uv run watch` **stopped**
- [ ] Soak check passes: `powershell -File scripts/docker-soak-check.ps1 -MinTicks 3`
- [ ] SMS volume acceptable (watch for 403 → false release transitions)
- [ ] Sign off local Docker as primary runtime

---

## Phase 1 — SSH access

- [ ] Add laptop public key to VPS `/root/.ssh/authorized_keys`, **or** set `ROSE_VPS_SSH_PASSWORD` / `FANDANGO_VPS_SSH_PASSWORD` and use `python scripts/run_vps_cmd.py "hostname"`

---

## Phase 2 — VPS preflight (no build yet)

On VPS:

- [ ] Disk ≥10 GiB free: `df -h /`
- [ ] Prune cache: `docker builder prune -f`
- [ ] Rose healthy: `curl -sS -o /dev/null -w "%{http_code}\n" "https://rose.geobregon.com/api/solar-snapshot?instant=2000-01-01T00:00:00.000Z"` → 200
- [ ] Mail active: `systemctl is-active postfix dovecot nginx mariadb`
- [ ] Port 8787 free: `ss -tlnp | grep 8787` (empty)
- [ ] **Do not** run Rose `docker compose --build` at the same time

---

## Phase 3 — Bootstrap

- [ ] Clone: `git clone git@github.com:gamester2665/fandango-watcher.git /root/fandango-watcher`
- [ ] From laptop: `powershell -File scripts/vps-sync-secrets.ps1`
- [ ] On VPS: `chmod 600 .env.production config.yaml`; `sed -i 's/\r$//' .env.production`
- [ ] Set `purchase.mode: notify_only` in server `config.yaml` for first run
- [ ] Restore volume tarballs from laptop `backups/docker-volumes/` into `fandango_state`, `fandango_profile` (see `scripts/docker-volume-backup.ps1` restore example)

---

## Phase 4 — First deploy

On VPS (off-peak):

```bash
cd /root/fandango-watcher
bash scripts/vps-first-time.sh
```

Or from laptop after secrets sync:

```bash
bash scripts/vps-deploy.sh
```

Compose: `docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build watcher`

---

## Phase 5 — Verify co-location

On VPS:

```bash
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8787/api/status | head -c 400
docker compose logs watcher --tail 50
```

Re-check Rose (200) and mail units. From laptop: `ssh -L 8787:127.0.0.1:8787 root@74.48.91.123` → open dashboard.

---

## Phase 6 — Cutover notifications

- [ ] **Disable Cloudflare Worker cron** (avoid duplicate SMS with VPS watcher)
- [ ] Stop local Docker watcher after VPS confirmed
- [ ] Rollback: VPS `docker compose down`; re-enable Worker or local watch

---

## Phase 7 — Optional follow-ups

- [ ] Cloudflare Tunnel: new hostname → `http://127.0.0.1:8787` + Access policy (do not edit Rose routes)
- [ ] Deploy hook on `127.0.0.1:8990` (Rose pattern, handoff §7)
- [ ] VPS soak 24–48h; headed `login` via VNC only if profile restore fails
- [ ] Fix 403 API fallback treating as release transition (false SMS)

---

## Open decisions

1. **Dashboard hostname** — Tunnel subdomain vs SSH port-forward only?
2. **Build on VPS** vs pre-built image from CI (2.4 GiB RAM constraint)?
3. **Worker disable timing** — before VPS `up`, or brief dual-run in `notify_only`?
