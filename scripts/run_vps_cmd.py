#!/usr/bin/env python3
"""SSH/SFTP helper for VPS ops (password or key). See docs/VPS_DEPLOY.md."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import paramiko

DEFAULT_HOST = "74.48.91.123"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/root/fandango-watcher"
CONNECT_TIMEOUT = 90
BANNER_TIMEOUT = 180

# Rose monorepo secrets (same VPS as mail + Rose). Override with ROSE_SECRETS_VPS_MD.
_ROSE_SECRETS_CANDIDATES = (
    Path(os.environ.get("ROSE_SECRETS_VPS_MD", "")),
    Path(r"G:/_backup/Code/_mom/rose_astrology/secrets.vps.md"),
    Path.home() / "rose_astrology" / "secrets.vps.md",
)


def _password_from_secrets_file(path: Path) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "SSH password" in line and ":" in line:
            part = line.split(":", 1)[1].strip().strip("`").strip()
            if part:
                return part
    return ""


def _resolve_password(host: str, user: str) -> str:
    for key in ("FANDANGO_VPS_SSH_PASSWORD", "ROSE_VPS_SSH_PASSWORD"):
        value = os.environ.get(key, "").strip()
        if value:
            return value

    repo_root = Path(__file__).resolve().parent.parent
    local_secrets = repo_root / "secrets.vps.md"
    found = _password_from_secrets_file(local_secrets)
    if found:
        return found

    for candidate in _ROSE_SECRETS_CANDIDATES:
        if not candidate or str(candidate) == ".":
            continue
        found = _password_from_secrets_file(candidate)
        if found:
            return found

    return getpass.getpass(f"SSH password for {user}@{host}: ")


def _connect() -> paramiko.SSHClient:
    host = os.environ.get("FANDANGO_VPS_HOST") or os.environ.get("ROSE_VPS_HOST") or DEFAULT_HOST
    user = os.environ.get("FANDANGO_VPS_SSH_USER") or os.environ.get("ROSE_VPS_SSH_USER") or DEFAULT_USER
    password = _resolve_password(host, user)

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


def _run_remote(command: str) -> int:
    client = _connect()
    try:
        _stdin, stdout, stderr = client.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if out:
            sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
        return stdout.channel.recv_exit_status()
    finally:
        client.close()


def _upload(local: Path, remote: str) -> None:
    client = _connect()
    try:
        sftp = client.open_sftp()
        sftp.put(str(local), remote)
        sftp.close()
        print(f"uploaded {local} -> {remote}")
    finally:
        client.close()


def _sync_secrets() -> int:
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    config_file = root / "config.yaml"
    remote_dir = os.environ.get("FANDANGO_VPS_DIR", DEFAULT_REMOTE_DIR)
    if not env_file.is_file():
        print("missing .env", file=sys.stderr)
        return 1
    if not config_file.is_file():
        print("missing config.yaml", file=sys.stderr)
        return 1
    _upload(env_file, f"{remote_dir}/.env.production")
    _upload(config_file, f"{remote_dir}/config.yaml")
    return _run_remote(
        f"chmod 600 {remote_dir}/.env.production {remote_dir}/config.yaml && "
        f"sed -i 's/\\r$//' {remote_dir}/.env.production 2>/dev/null || true"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command on the VPS via SSH.")
    parser.add_argument("command", nargs="?", help="Remote shell command (default: uname -a)")
    parser.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"), help="SFTP upload")
    parser.add_argument(
        "--sync-secrets",
        action="store_true",
        help="Upload .env -> .env.production and config.yaml to FANDANGO_VPS_DIR",
    )
    args = parser.parse_args()

    if args.sync_secrets:
        return _sync_secrets()
    if args.upload:
        _upload(Path(args.upload[0]), args.upload[1])
        return 0
    cmd = args.command or "uname -a"
    return _run_remote(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
