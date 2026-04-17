from __future__ import annotations

import argparse
import os
import sys
import uuid
from typing import Any

import requests


def _api_url() -> str:
    return os.getenv("BACOPY_API_URL", "http://127.0.0.1:8010").rstrip("/")


def _api_key() -> str:
    key = os.getenv("BACOPY_API_KEY", "").strip()
    if not key:
        raise SystemExit("BACOPY_API_KEY is required")
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def cmd_status() -> int:
    r = requests.get(f"{_api_url()}/api/status", headers=_headers(), timeout=10)
    print(r.status_code, r.text)
    return 0


def cmd_list_snapshots(provider: str) -> int:
    r = requests.get(f"{_api_url()}/api/snapshots", headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json()
    snaps = (data.get("snapshots") or {}).get(provider) or {}
    if not isinstance(snaps, dict):
        print("no snapshots")
        return 0
    # Print a compact list
    items = list(snaps.items())
    print(f"provider={provider} tables={len(items)} updated_at={data.get('updated_at')}")
    for tid, s in items[:50]:
        name = (s or {}).get("table_name") or ""
        hands = (s or {}).get("hands")
        players = (s or {}).get("players")
        print(f"- {tid}  {name}  hands={hands} players={players}")
    return 0


def cmd_decision(action: str, provider: str, table_id: str, table_name: str, game_id: str, side: str, amount: float, note: str) -> int:
    payload: dict[str, Any] = {
        "decision_id": f"dec_{uuid.uuid4().hex[:16]}",
        "provider": provider,
        "table_id": table_id,
        "table_name": table_name,
        "game_id": game_id,
        "snapshot": {},
        "friend_action": {"action": action, "side": side, "amount": amount, "note": note},
    }
    r = requests.post(f"{_api_url()}/api/decisions", headers=_headers(), json=payload, timeout=10)
    print(r.status_code, r.text)
    return 0


def cmd_pending(limit: int) -> int:
    r = requests.get(f"{_api_url()}/api/decisions/pending?limit={int(limit)}", headers=_headers(), timeout=10)
    print(r.status_code, r.text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sp = sub.add_parser("snapshots")
    sp.add_argument("--provider", default="evolution", choices=["evolution", "pragmatic"])

    sp = sub.add_parser("pending")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("look")
    sp.add_argument("--provider", default="evolution", choices=["evolution", "pragmatic"])
    sp.add_argument("--table-id", default="")
    sp.add_argument("--table-name", default="")
    sp.add_argument("--game-id", default="")
    sp.add_argument("--note", default="")

    sp = sub.add_parser("bet")
    sp.add_argument("--provider", default="evolution", choices=["evolution", "pragmatic"])
    sp.add_argument("--table-id", default="")
    sp.add_argument("--table-name", default="")
    sp.add_argument("--game-id", default="")
    sp.add_argument("--side", required=True, choices=["PLAYER", "BANKER", "TIE"])
    sp.add_argument("--amount", type=float, default=0.0)
    sp.add_argument("--note", default="")

    args = ap.parse_args(argv)

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "snapshots":
        return cmd_list_snapshots(args.provider)
    if args.cmd == "pending":
        return cmd_pending(args.limit)
    if args.cmd == "look":
        return cmd_decision("LOOK", args.provider, args.table_id, args.table_name, args.game_id, "", 0.0, args.note)
    if args.cmd == "bet":
        return cmd_decision("BET", args.provider, args.table_id, args.table_name, args.game_id, args.side, float(args.amount or 0.0), args.note)

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
