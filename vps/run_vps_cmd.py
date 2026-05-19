#!/usr/bin/env python3
"""SSH/SFTP helper for shared VPS ops (password or key). See vps/README.md."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

import paramiko

KIT_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "74.48.91.123"
DEFAULT_USER = "root"
CONNECT_TIMEOUT = 90
BANNER_TIMEOUT = 180

_ROSE_SECRETS_CANDIDATES = (
    Path(os.environ.get("ROSE_SECRETS_VPS_MD", "")),
    Path(r"G:/_backup/Code/_mom/rose_astrology/secrets.vps.md"),
    Path.home() / "rose_astrology" / "secrets.vps.md",
)


def _parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _load_kit_env() -> dict[str, str]:
    env: dict[str, str] = {}
    host_env = KIT_ROOT / "host.env"
    if host_env.is_file():
        env.update(_parse_env_file(host_env))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("VPS_", "ROSE_", "FANDANGO_"))})
    return env


def _resolve_project_env(project: str | None) -> Path | None:
    if os.environ.get("VPS_PROJECT_ENV"):
        path = Path(os.environ["VPS_PROJECT_ENV"])
        return path if path.is_file() else None
    if project:
        path = KIT_ROOT / "projects" / f"{project}.env"
        return path if path.is_file() else None
    name = os.environ.get("VPS_PROJECT_NAME")
    if name:
        path = KIT_ROOT / "projects" / f"{name}.env"
        return path if path.is_file() else None
    try:
        git_root = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        repo_name = git_root.name
    except (OSError, subprocess.CalledProcessError):
        repo_name = Path.cwd().name
    for variant in (repo_name, repo_name.replace("_", "-"), repo_name.replace("-", "_")):
        path = KIT_ROOT / "projects" / f"{variant}.env"
        if path.is_file():
            return path
    return None


def _project_settings(project: str | None) -> dict[str, str]:
    settings = _load_kit_env()
    project_env = _resolve_project_env(project)
    if project_env:
        settings.update(_parse_env_file(project_env))
        settings["VPS_PROJECT_ENV"] = str(project_env)
    return settings


def _password_from_secrets_file(path: Path) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "SSH password" in line and ":" in line:
            part = line.split(":", 1)[1].strip().strip("`").strip()
            if part:
                return part
    return ""


def _resolve_password(host: str, user: str, settings: dict[str, str]) -> str:
    for key in ("VPS_SSH_PASSWORD", "FANDANGO_VPS_SSH_PASSWORD", "ROSE_VPS_SSH_PASSWORD"):
        value = settings.get(key) or os.environ.get(key, "")
        if str(value).strip():
            return str(value).strip()

    for candidate in (
        Path(settings.get("ROSE_SECRETS_VPS_MD", "")),
        *_ROSE_SECRETS_CANDIDATES,
        KIT_ROOT.parent / "secrets.vps.md",
    ):
        if not candidate or str(candidate) == ".":
            continue
        found = _password_from_secrets_file(candidate)
        if found:
            return found

    return getpass.getpass(f"SSH password for {user}@{host}: ")


def _connect(settings: dict[str, str]) -> paramiko.SSHClient:
    host = (
        settings.get("VPS_HOST")
        or os.environ.get("FANDANGO_VPS_HOST")
        or os.environ.get("ROSE_VPS_HOST")
        or DEFAULT_HOST
    )
    user = (
        settings.get("VPS_SSH_USER")
        or os.environ.get("FANDANGO_VPS_SSH_USER")
        or os.environ.get("ROSE_VPS_SSH_USER")
        or DEFAULT_USER
    )
    password = _resolve_password(host, user, settings)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password or None,
        look_for_keys=True,
        allow_agent=True,
        timeout=CONNECT_TIMEOUT,
        banner_timeout=BANNER_TIMEOUT,
    )
    return client


def _emit(text: str, stream: object) -> None:
    buf = getattr(stream, "buffer", None)
    if buf is not None:
        buf.write(text.encode("utf-8", errors="replace"))
    else:
        stream.write(text)  # type: ignore[union-attr]


def _run_remote(command: str, settings: dict[str, str]) -> int:
    client = _connect(settings)
    try:
        _stdin, stdout, stderr = client.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            _emit(out, sys.stdout)
        if err:
            _emit(err, sys.stderr)
        return stdout.channel.recv_exit_status()
    finally:
        client.close()


def _upload(local: Path, remote: str, settings: dict[str, str]) -> None:
    client = _connect(settings)
    try:
        sftp = client.open_sftp()
        sftp.put(str(local), remote)
        sftp.close()
        print(f"uploaded {local} -> {remote}")
    finally:
        client.close()


def _secret_pairs(settings: dict[str, str]) -> list[tuple[str, str]]:
    raw = settings.get("VPS_SECRET_FILES", ".env:.env.production")
    pairs: list[tuple[str, str]] = []
    for item in raw.split():
        if ":" not in item:
            continue
        local, remote = item.split(":", 1)
        pairs.append((local.strip(), remote.strip()))
    return pairs


def _sync_secrets(settings: dict[str, str]) -> int:
    repo_root = Path(settings.get("VPS_REPO_ROOT", KIT_ROOT.parent))
    remote_dir = (
        settings.get("VPS_REMOTE_DIR")
        or os.environ.get("FANDANGO_VPS_DIR")
        or ""
    )
    if not remote_dir:
        print("VPS_REMOTE_DIR not set — add vps/projects/<name>.env or pass --project", file=sys.stderr)
        return 1

    pairs = _secret_pairs(settings)
    if not pairs:
        print("VPS_SECRET_FILES is empty", file=sys.stderr)
        return 1

    remote_cmds: list[str] = []
    for local, remote in pairs:
        local_path = repo_root / local
        if not local_path.is_file():
            print(f"missing {local_path}", file=sys.stderr)
            return 1
        _upload(local_path, f"{remote_dir}/{remote}", settings)
        remote_cmds.append(f"chmod 600 {remote_dir}/{remote}")

    if any(remote == ".env.production" for _, remote in pairs):
        remote_cmds.append(f"ln -sf .env.production {remote_dir}/.env")
        remote_cmds.append(
            f"sed -i 's/\\r$//' {remote_dir}/.env.production 2>/dev/null || true"
        )

    return _run_remote(" && ".join(remote_cmds), settings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command on the VPS via SSH.")
    parser.add_argument("command", nargs="?", help="Remote shell command (default: uname -a)")
    parser.add_argument("--project", "-p", help="Project name (loads vps/projects/<name>.env)")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"), help="SFTP upload")
    parser.add_argument(
        "--sync-secrets",
        action="store_true",
        help="Upload secret files per VPS_SECRET_FILES in project env",
    )
    args = parser.parse_args()

    settings = _project_settings(args.project)
    if args.project and not _resolve_project_env(args.project):
        print(f"missing vps/projects/{args.project}.env", file=sys.stderr)
        return 1

    if args.sync_secrets:
        return _sync_secrets(settings)
    if args.upload:
        _upload(Path(args.upload[0]), args.upload[1], settings)
        return 0
    cmd = args.command or "uname -a"
    return _run_remote(cmd, settings)


if __name__ == "__main__":
    raise SystemExit(main())
