# Developing with Docker

Use this when you want the **same runtime as production** (Playwright Chromium in Linux, volumes for profile/state/artifacts) while iterating on Python under `src/`.

## Compose overlay

Production compose stays unchanged (`docker-compose.yml` builds target **`app`** — slim image).

Development merges **`docker-compose.dev.yml`**:

- Builds target **`development`** (installs the uv **dev** group: pytest, ruff, mypy).
- Bind-mounts **`./src`** → **`/app/src`** and **`./tests`** → **`/app/tests`** so edits apply without rebuilding the image.
- Sets **`PYTHONPATH=/app/src`** so imports prefer your mounted tree over the installed wheel.

### Commands

From the repo root (after `.env` + `config.yaml` exist, same as README):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build watcher
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
docker compose -f docker-compose.yml -f docker-compose.dev.yml logs -f watcher
```

After editing Python under `src/`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml restart watcher
```

One-shot **`login`** / **`once`** (profiles `tools`) use image tag `fandango_watcher:latest`. Build **`watcher`** first so `latest` matches your chosen compose files:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile tools build watcher login once
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile tools run --rm login
```

### Convenience wrappers

- **Unix:** `scripts/docker-compose-dev.sh` — prepends `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml`.
- **Windows (PowerShell):** `scripts/docker-compose-dev.ps1` — same idea.

Example:

```bash
./scripts/docker-compose-dev.sh up -d --build
```

### Tests / lint inside the dev image

Run against the merged compose (replace flags as needed):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm \
  --entrypoint "" watcher \
  uv run pytest -q
```

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm \
  --entrypoint "" watcher \
  uv run ruff check src tests
```

### Headed browser notes

Headed **`login`** still needs your platform’s **DISPLAY** / X11 forwarding (see main README **Docker** section). Nothing here changes that.

### Production vs dev images

| Compose files | Build target | Typical use |
|---------------|--------------|-------------|
| `docker-compose.yml` only | **app** | Smaller image; CI / VPS |
| `docker-compose.yml` + `docker-compose.dev.yml` | **development** | Local iteration + pytest/ruff |
