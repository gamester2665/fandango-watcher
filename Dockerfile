# syntax=docker/dockerfile:1.7

# =============================================================================
# fandango_watcher — Dockerized Playwright watcher + A-List auto-purchaser.
#
# Base: python:3.13-slim-bookworm (matches project requires-python >=3.13).
# Playwright browsers are installed via `playwright install --with-deps` which
# pulls in the ~50 native libs Chromium needs on Debian.
# =============================================================================

ARG PYTHON_VERSION=3.13

# -----------------------------------------------------------------------------
# Stage 1: base with uv and OS prerequisites used by both dep install and app.
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=America/Los_Angeles

# uv comes from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /uvx /usr/local/bin/

# Minimal apt packages needed for healthcheck + timezone data. Chromium's own
# system deps are installed by `playwright install --with-deps` below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# -----------------------------------------------------------------------------
# Stage 2: resolve Python deps in a cached layer before copying source.
# -----------------------------------------------------------------------------
FROM base AS deps

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Install Chromium + system deps into a known path (PLAYWRIGHT_BROWSERS_PATH).
# `--with-deps` runs apt-get under the hood; we're root during build.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv run playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Stage 3: final image with app source installed on top of the deps layer.
# -----------------------------------------------------------------------------
FROM deps AS app

COPY src/ ./src/
COPY config.example.yaml ./config.example.yaml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Pre-create volume mount points so they exist with sane ownership when users
# mount empty named volumes. These paths match docker-compose.yml.
RUN mkdir -p \
        /app/browser-profile \
        /app/artifacts/screenshots \
        /app/artifacts/purchase-attempts \
        /app/state

EXPOSE 8787

# Same URL as compose `healthcheck`: liveness JSON from the watch loop.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8787/healthz || exit 1

# tini is PID 1 so SIGTERM reaches the Python process cleanly on
# `docker compose down`, which matters for closing the browser profile.
ENTRYPOINT ["/usr/bin/tini", "--", "fandango-watcher"]
CMD ["watch"]
