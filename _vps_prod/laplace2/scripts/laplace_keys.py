"""LAPLACE API Key Management CLI

Administrative CLI for the per-user API key registry. Talks to the
running LAPLACE API over HTTP using the admin master key.

Usage:
    # Issue a new key for a user (returns full secret ONCE)
    python scripts/laplace_keys.py issue alice --name "Alice laptop"
    python scripts/laplace_keys.py issue bob --rate 1800 --ips 203.0.113.5,198.51.100.0/24

    # List all registered keys (masked)
    python scripts/laplace_keys.py list

    # Revoke a key (by prefix)
    python scripts/laplace_keys.py revoke lpk_live_a1b2c3d4

    # Disable/enable a key
    python scripts/laplace_keys.py disable lpk_live_a1b2c3d4
    python scripts/laplace_keys.py enable  lpk_live_a1b2c3d4

    # Reload keys file (if edited manually on disk)
    python scripts/laplace_keys.py reload

Environment:
    LAPLACE_API_URL        e.g. https://api.example.com
    LAPLACE_ADMIN_KEY      admin master key for Bearer auth
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests


def _base() -> tuple[str, dict]:
    url = os.getenv("LAPLACE_API_URL", "http://127.0.0.1:8000").rstrip("/")
    admin = os.getenv("LAPLACE_ADMIN_KEY", "").strip()
    if not admin:
        print("ERROR: LAPLACE_ADMIN_KEY env var is required", file=sys.stderr)
        sys.exit(2)
    return url, {"Authorization": f"Bearer {admin}"}


def _pp(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_issue(args) -> int:
    url, hdr = _base()
    ips = [s.strip() for s in (args.ips or "").split(",") if s.strip()]
    body = {
        "user_id": args.user_id,
        "name": args.name or "",
        "rate_limit_per_hour": args.rate,
        "ip_allowlist": ips,
    }
    r = requests.post(f"{url}/api/admin/keys", headers=hdr, json=body, timeout=10)
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    print("=" * 70)
    print(" NEW API KEY ISSUED -- SAVE THE FULL SECRET NOW")
    print(" It will NEVER be displayed again.")
    print("=" * 70)
    _pp(data)
    print("")
    print("Client env var:")
    print(f"  LAPLACE_API_KEY={data['key']}")
    return 0


def cmd_list(args) -> int:
    url, hdr = _base()
    r = requests.get(f"{url}/api/admin/keys", headers=hdr, timeout=10)
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    keys = data.get("keys", [])
    if not keys:
        print("(no keys registered)")
        return 0
    print(f"{'PREFIX':<24} {'USER':<20} {'RATE/h':<8} {'USED':<6} {'ENABLED':<8} {'NAME'}")
    print("-" * 92)
    for k in keys:
        print(
            f"{k['prefix']:<24} {k['user_id']:<20} "
            f"{k['rate_limit_per_hour']:<8} "
            f"{k['rate_usage_last_hour']:<6} "
            f"{'yes' if k['enabled'] else 'NO':<8} "
            f"{k['name']}"
        )
    return 0


def cmd_revoke(args) -> int:
    url, hdr = _base()
    r = requests.delete(
        f"{url}/api/admin/keys/{args.prefix}", headers=hdr, timeout=10
    )
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    _pp(r.json())
    return 0


def cmd_toggle(args, enabled: bool) -> int:
    url, hdr = _base()
    r = requests.patch(
        f"{url}/api/admin/keys/{args.prefix}",
        headers=hdr,
        params={"enabled": "true" if enabled else "false"},
        timeout=10,
    )
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    _pp(r.json())
    return 0


def cmd_reload(args) -> int:
    url, hdr = _base()
    r = requests.post(f"{url}/api/admin/keys/reload", headers=hdr, timeout=10)
    if r.status_code >= 400:
        print(f"ERROR {r.status_code}: {r.text}", file=sys.stderr)
        return 1
    _pp(r.json())
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="LAPLACE API key management")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("issue", help="Create a new per-user API key")
    pi.add_argument("user_id")
    pi.add_argument("--name", default="")
    pi.add_argument("--rate", type=int, default=3600, help="Requests per hour")
    pi.add_argument("--ips", default="", help="Comma-separated IP or CIDR allowlist")

    sub.add_parser("list", help="List all registered keys (masked)")

    pr = sub.add_parser("revoke", help="Permanently delete a key by prefix")
    pr.add_argument("prefix")

    pd = sub.add_parser("disable", help="Disable (but keep) a key")
    pd.add_argument("prefix")

    pe = sub.add_parser("enable", help="Re-enable a disabled key")
    pe.add_argument("prefix")

    sub.add_parser("reload", help="Reload api_keys.json from disk")

    args = p.parse_args()
    if args.cmd == "issue":
        return cmd_issue(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "revoke":
        return cmd_revoke(args)
    if args.cmd == "disable":
        return cmd_toggle(args, False)
    if args.cmd == "enable":
        return cmd_toggle(args, True)
    if args.cmd == "reload":
        return cmd_reload(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
