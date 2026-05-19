#!/usr/bin/env python3
"""Publish fandango_watcher on geobregon.com via Cloudflare Tunnel API.

Default strategy: add hostname to the existing rose-astrology tunnel (same VPS
cloudflared connector). A dedicated tunnel is optional but needs a second
systemd unit and does not simplify geobregon.com DNS.

Requires CLOUDFLARE_API_TOKEN in environment or repo .env:
  Account → Cloudflare One Connectors (or Cloudflare Tunnel) → Edit
  Zone → DNS → Edit

Usage:
  python vps/scripts/cloudflare-publish-hostname.py
  python vps/scripts/cloudflare-publish-hostname.py --dry-run
  python vps/scripts/cloudflare-publish-hostname.py --strategy dedicated
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_ACCOUNT = "7f3e024b68ea359931e13d4688fde4a6"
ROSE_TUNNEL_ID = "7050a8bf-2e17-4b87-a74d-277ab6b9ffb3"
DEFAULT_HOSTNAME = "fandango.geobregon.com"
DEFAULT_SERVICE = "http://127.0.0.1:8787"
DEFAULT_ZONE = "geobregon.com"


def _load_dotenv(repo_root: Path) -> None:
    for name in (".env", ".env.local"):
        path = repo_root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _token() -> str:
    for key in ("CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    raise SystemExit(
        "Missing CLOUDFLARE_API_TOKEN. Add to .env (see .env.example) with "
        "Account Cloudflare Tunnel Edit + Zone DNS Edit, then re-run."
    )


def _api(method: str, url: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API {method} {url} failed ({exc.code}): {detail}") from exc


def _zone_id(token: str, zone_name: str) -> str:
    q = urllib.parse.urlencode({"name": zone_name})
    payload = _api("GET", f"https://api.cloudflare.com/client/v4/zones?{q}", token)
    for zone in payload.get("result", []):
        if zone.get("name") == zone_name:
            return zone["id"]
    raise SystemExit(f"zone not found in account: {zone_name}")


def _merge_ingress(existing: list[dict], hostname: str, service: str) -> list[dict]:
    filtered = [r for r in existing if r.get("hostname") != hostname]
    catch_all = [r for r in filtered if not r.get("hostname") and not r.get("path")]
    middle = [r for r in filtered if r.get("hostname") or r.get("path")]
    new_rule = {"hostname": hostname, "service": service, "originRequest": {}}
    if catch_all:
        return [*middle, new_rule, catch_all[0]]
    return [*middle, new_rule, {"service": "http_status:404"}]


def _get_ingress(account: str, tunnel_id: str, token: str) -> list[dict]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations"
    payload = _api("GET", url, token)
    ingress = payload.get("result", {}).get("config", {}).get("ingress") or []
    if not ingress:
        raise SystemExit(f"tunnel {tunnel_id} has no ingress configuration")
    return ingress


def _put_ingress(account: str, tunnel_id: str, token: str, ingress: list[dict]) -> None:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations"
    payload = _api("PUT", url, token, {"config": {"ingress": ingress}})
    if not payload.get("success"):
        raise SystemExit(json.dumps(payload, indent=2))


def _ensure_dns_cname(zone_id: str, token: str, hostname: str, tunnel_id: str) -> None:
    target = f"{tunnel_id}.cfargotunnel.com"
    q = urllib.parse.urlencode({"type": "CNAME", "name": hostname})
    existing = _api(
        "GET",
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?{q}",
        token,
    )
    for rec in existing.get("result", []):
        if rec.get("name") == hostname and rec.get("content") == target:
            print(f"DNS OK (exists): {hostname} -> {target}")
            return
        if rec.get("name") == hostname:
            _api(
                "PUT",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                token,
                {
                    "type": "CNAME",
                    "name": hostname,
                    "content": target,
                    "proxied": True,
                    "ttl": 1,
                },
            )
            print(f"DNS updated: {hostname} -> {target}")
            return

    _api(
        "POST",
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
        token,
        {
            "type": "CNAME",
            "name": hostname,
            "content": target,
            "proxied": True,
            "ttl": 1,
        },
    )
    print(f"DNS created: {hostname} -> {target}")


def _create_tunnel(account: str, token: str, name: str) -> tuple[str, str]:
    payload = _api(
        "POST",
        f"https://api.cloudflare.com/client/v4/accounts/{account}/cfd_tunnel",
        token,
        {"name": name, "config_src": "cloudflare"},
    )
    if not payload.get("success"):
        raise SystemExit(json.dumps(payload, indent=2))
    result = payload["result"]
    tunnel_id = result["id"]
    connector_token = result.get("token") or ""
    if not connector_token:
        tok = _api(
            "GET",
            f"https://api.cloudflare.com/client/v4/accounts/{account}/cfd_tunnel/{tunnel_id}/token",
            token,
        )
        connector_token = tok.get("result") or ""
    if not connector_token:
        raise SystemExit("tunnel created but connector token missing from API response")
    return tunnel_id, connector_token


def _write_vps_systemd_snippet(tunnel_name: str) -> Path:
    repo = Path(__file__).resolve().parents[2]
    out = repo / "vps" / "templates" / "cloudflared-fandango.service.example"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"""# Optional — only for --strategy dedicated (second cloudflared on VPS).
# Copy to /etc/systemd/system/cloudflared-fandango.service on the VPS.
# Set ExecStart token from: cloudflared tunnel token {tunnel_name}
# (run on laptop after API create, or fetch from Zero Trust dashboard).

[Unit]
Description=cloudflared fandango-watcher tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
Environment=TUNNEL_TRANSPORT_PROTOCOL=http2
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token <CONNECTOR_TOKEN>
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
""",
        encoding="utf-8",
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish hostname on Cloudflare Tunnel")
    parser.add_argument("--account-id", default=os.environ.get("CLOUDFLARE_ACCOUNT_ID", DEFAULT_ACCOUNT))
    parser.add_argument("--hostname", default=DEFAULT_HOSTNAME)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument(
        "--strategy",
        choices=("reuse", "dedicated"),
        default="reuse",
        help="reuse rose-astrology tunnel (recommended) or create dedicated tunnel",
    )
    parser.add_argument("--tunnel-id", default=ROSE_TUNNEL_ID, help="for reuse strategy")
    parser.add_argument("--tunnel-name", default="fandango-watcher", help="for dedicated strategy")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    _load_dotenv(repo_root)
    token = _token()

    zone_id = _zone_id(token, args.zone)
    tunnel_id = args.tunnel_id
    connector_token = ""

    if args.strategy == "dedicated":
        if args.dry_run:
            print(json.dumps({"action": "create_tunnel", "name": args.tunnel_name}, indent=2))
        else:
            tunnel_id, connector_token = _create_tunnel(args.account_id, token, args.tunnel_name)
            print(f"Created tunnel {args.tunnel_name} ({tunnel_id})")

    ingress = _get_ingress(args.account_id, tunnel_id, token)
    merged = _merge_ingress(ingress, args.hostname, args.service)

    if args.dry_run:
        print(json.dumps({"tunnel_id": tunnel_id, "ingress": merged}, indent=2))
        return 0

    _put_ingress(args.account_id, tunnel_id, token, merged)
    print(f"Ingress OK: {args.hostname} -> {args.service} on tunnel {tunnel_id}")

    _ensure_dns_cname(zone_id, token, args.hostname, tunnel_id)

    if args.strategy == "dedicated" and connector_token:
        unit = _write_vps_systemd_snippet(args.tunnel_name)
        print(f"Wrote systemd template: {unit}")
        print("Fetch connector token: GET .../cfd_tunnel/{id}/token — install on VPS only.")
        print("Recommended: use --strategy reuse (no second cloudflared process).")

    print(f"Verify: curl -fsS https://{args.hostname}/healthz")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
