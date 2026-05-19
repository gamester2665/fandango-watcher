# mypy: disable-error-code="unused-ignore,attr-defined"
"""Static tests for Dockerfile, docker-compose.yml, .dockerignore, .gitignore.

We can't (and shouldn't) run `docker build` inside pytest, but we can assert
structural properties of these files so typos and drift don't ship silently:

* Dockerfile pins a Python base matching pyproject's `requires-python`.
* docker-compose.yml declares the named volumes referenced by the watcher.
* config paths inside the container match what config.example.yaml expects.
* .dockerignore excludes secrets (`.env`) and runtime volumes.
* .gitignore excludes secrets + runtime state.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
DOCKER_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
COMPOSE_VPS_PATH = REPO_ROOT / "docker-compose.vps.yml"

DOCKER_OPERATOR_SCRIPTS = (
    "scripts/docker-smoke.sh",
    "scripts/docker-smoke.ps1",
    "scripts/docker-cutover.sh",
    "scripts/docker-cutover.ps1",
    "scripts/docker-seed-volumes.sh",
    "scripts/docker-seed-volumes.ps1",
    "scripts/docker-volume-backup.sh",
    "scripts/docker-volume-backup.ps1",
    "scripts/docker-common.sh",
    "scripts/docker-common.ps1",
    "scripts/vps-deploy.sh",
    "scripts/vps-pull-and-restart.sh",
    "scripts/vps-sync-secrets.sh",
    "scripts/vps-sync-secrets.ps1",
    "scripts/vps-first-time.sh",
    "scripts/vps-preflight.sh",
    "scripts/vps-verify-neighbors.sh",
    "scripts/docker-soak-check.sh",
    "scripts/docker-soak-check.ps1",
)


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


class TestDockerfile:
    def test_uses_python_313_base(self, dockerfile: str) -> None:
        assert "PYTHON_VERSION=3.13" in dockerfile
        assert "python:${PYTHON_VERSION}-slim-bookworm" in dockerfile

    def test_installs_uv_from_official_image(self, dockerfile: str) -> None:
        assert "ghcr.io/astral-sh/uv" in dockerfile

    def test_installs_chromium_with_system_deps(self, dockerfile: str) -> None:
        assert "playwright install --with-deps chromium" in dockerfile

    def test_copies_pyproject_before_source_for_layer_caching(
        self, dockerfile: str
    ) -> None:
        # pyproject/uv.lock copy must appear before `COPY src/`.
        pyproject_idx = dockerfile.index("COPY pyproject.toml uv.lock")
        source_idx = dockerfile.index("COPY src/")
        assert pyproject_idx < source_idx

    def test_precreates_volume_mount_points(self, dockerfile: str) -> None:
        for path in (
            "/app/browser-profile",
            "/app/artifacts/screenshots",
            "/app/artifacts/purchase-attempts",
            "/app/state",
        ):
            assert path in dockerfile, f"{path} must be pre-created in the image"

    def test_healthcheck_hits_healthz(self, dockerfile: str) -> None:
        assert "HEALTHCHECK" in dockerfile
        assert "/healthz" in dockerfile
        assert "8787" in dockerfile

    def test_tini_is_pid1(self, dockerfile: str) -> None:
        assert "tini" in dockerfile
        assert 'ENTRYPOINT ["/usr/bin/tini"' in dockerfile

    def test_default_cmd_is_watch(self, dockerfile: str) -> None:
        assert 'CMD ["watch"]' in dockerfile

    def test_requires_python_alignment_with_pyproject(self) -> None:
        pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
        assert 'requires-python = ">=3.13"' in pyproject


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, object]:
    raw = COMPOSE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    return data


class TestCompose:
    def test_declares_expected_services(self, compose: dict[str, object]) -> None:
        services = compose["services"]  # type: ignore[index]
        assert {"watcher", "login", "once"}.issubset(services.keys())  # type: ignore[union-attr]

    def test_declares_expected_named_volumes(
        self, compose: dict[str, object]
    ) -> None:
        volumes = compose["volumes"]  # type: ignore[index]
        assert {"fandango_profile", "fandango_artifacts", "fandango_state"}.issubset(
            volumes.keys()  # type: ignore[union-attr]
        )

    def test_watcher_restart_policy(self, compose: dict[str, object]) -> None:
        watcher = compose["services"]["watcher"]  # type: ignore[index]
        assert watcher["restart"] == "unless-stopped"

    def test_watcher_mounts_all_volumes(self, compose: dict[str, object]) -> None:
        watcher = compose["services"]["watcher"]  # type: ignore[index]
        mounts = " ".join(watcher["volumes"])
        assert "fandango_profile:/app/browser-profile" in mounts
        assert "fandango_artifacts:/app/artifacts" in mounts
        assert "fandango_state:/app/state" in mounts
        assert "./config.yaml:/app/config.yaml" in mounts

    def test_healthz_only_bound_to_loopback(
        self, compose: dict[str, object]
    ) -> None:
        watcher = compose["services"]["watcher"]  # type: ignore[index]
        ports = watcher["ports"]
        assert any(p.startswith("127.0.0.1:8787:") for p in ports), (
            "healthz port should be bound to 127.0.0.1 only, not 0.0.0.0"
        )

    def test_login_and_once_are_behind_tools_profile(
        self, compose: dict[str, object]
    ) -> None:
        for name in ("login", "once"):
            svc = compose["services"][name]  # type: ignore[index]
            assert "tools" in svc["profiles"], (
                f"{name} service should be gated behind --profile tools"
            )

    def test_login_service_forwards_display(
        self, compose: dict[str, object]
    ) -> None:
        login = compose["services"]["login"]  # type: ignore[index]
        env = login.get("environment", [])
        # Env can be a list of "KEY=value" or a dict; handle both.
        if isinstance(env, list):
            joined = " ".join(env)
        else:
            joined = " ".join(f"{k}={v}" for k, v in env.items())
        assert "DISPLAY" in joined


@pytest.fixture(scope="module")
def compose_vps() -> dict[str, object]:
    raw = COMPOSE_VPS_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    return data


class TestComposeVps:
    def test_vps_overlay_binds_loopback_8787(
        self, compose_vps: dict[str, object]
    ) -> None:
        watcher = compose_vps["services"]["watcher"]  # type: ignore[index]
        ports = watcher["ports"]
        assert any(p.startswith("127.0.0.1:8787:") for p in ports)

    def test_vps_overlay_uses_production_env_file(
        self, compose_vps: dict[str, object]
    ) -> None:
        watcher = compose_vps["services"]["watcher"]  # type: ignore[index]
        env_files = watcher["env_file"]
        assert any(
            (isinstance(e, dict) and e.get("path") == ".env.production")
            or e == ".env.production"
            for e in env_files
        )

    def test_vps_watch_command_no_open(self, compose_vps: dict[str, object]) -> None:
        watcher = compose_vps["services"]["watcher"]  # type: ignore[index]
        assert watcher["command"] == ["watch", "--no-open"]

    def test_vps_compose_project_name_isolated(self, compose_vps: dict[str, object]) -> None:
        assert compose_vps.get("name") == "fandango-watcher"


# ---------------------------------------------------------------------------
# .dockerignore and .gitignore
# ---------------------------------------------------------------------------


class TestIgnoreFiles:
    def test_dockerignore_excludes_secrets_and_volumes(self) -> None:
        content = DOCKERIGNORE_PATH.read_text(encoding="utf-8")
        for pattern in (".env", "artifacts/", "state/", "browser-profile/", ".venv/"):
            assert pattern in content, f".dockerignore missing {pattern!r}"

    def test_gitignore_excludes_secrets_and_volumes(self) -> None:
        content = GITIGNORE_PATH.read_text(encoding="utf-8")
        for pattern in (
            ".env",
            "config.yaml",
            "artifacts/",
            "state/",
            "browser-profile/",
            ".venv/",
        ):
            assert pattern in content, f".gitignore missing {pattern!r}"


# ---------------------------------------------------------------------------
# Docker operator scripts and CI
# ---------------------------------------------------------------------------


class TestDockerOperatorScripts:
    def test_operator_scripts_exist(self) -> None:
        for path in DOCKER_OPERATOR_SCRIPTS:
            assert (REPO_ROOT / path).exists(), f"missing {path}"

    def test_docker_smoke_uses_compose_not_host_uv(self) -> None:
        for path in ("scripts/docker-smoke.sh", "scripts/docker-smoke.ps1"):
            content = (REPO_ROOT / path).read_text(encoding="utf-8")
            assert "docker compose" in content
            assert "uv run fandango-watcher" not in content

    def test_docker_smoke_sms_is_opt_in(self) -> None:
        for path in ("scripts/docker-smoke.sh", "scripts/docker-smoke.ps1"):
            content = (REPO_ROOT / path).read_text(encoding="utf-8")
            assert "test-notify" in content
            assert "SMOKE_NOTIFY" in content or "NotifySmoke" in content

    def test_seed_and_backup_scripts_reference_named_volumes(self) -> None:
        seed = (REPO_ROOT / "scripts/docker-seed-volumes.sh").read_text(encoding="utf-8")
        backup = (REPO_ROOT / "scripts/docker-volume-backup.sh").read_text(
            encoding="utf-8"
        )
        assert "fandango_state" in seed
        assert "fandango_profile" in seed
        assert "fandango_state" in backup
        assert "backups/docker-volumes" in backup

    def test_docker_ci_workflow_exists_and_is_secret_free(self) -> None:
        assert DOCKER_CI_WORKFLOW.exists(), "missing .github/workflows/docker-build.yml"
        content = DOCKER_CI_WORKFLOW.read_text(encoding="utf-8")
        assert "docker compose build watcher" in content
        forbidden = ("TWILIO_", "X_BEARER_TOKEN", ".env", "test-notify", "api-drift")
        for token in forbidden:
            assert token not in content, f"docker-build.yml must not reference {token!r}"
