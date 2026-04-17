"""Evolution lobby watcher -> snapshot_store.

This process is read-only (no betting). It connects to the Stake.com Evolution
lobby WS via BaccaratScraper and publishes per-table snapshots to
`data/latest_snapshots.json`.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any

import os

import requests

from snapshot_store import update_snapshot


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _summarize_raw_history(raw: list) -> dict[str, Any]:
    # raw entries usually dict: {"c": "B"|"R", "s": "score", "ties": n}
    p = b = ties = 0
    last: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            c = entry.get("c")
            t = int(entry.get("ties") or 0)
            if c == "B":
                # internal color mapping in this codebase: B=Player, R=Banker
                p += 1
                last.append("P")
            elif c == "R":
                b += 1
                last.append("B")
            if t > 0:
                ties += t
        elif isinstance(entry, str):
            e = entry.lower()
            if e == "player":
                p += 1
                last.append("P")
            elif e == "banker":
                b += 1
                last.append("B")
            elif e == "tie":
                ties += 1
    last_10 = last[-10:]
    return {
        "p_count": p,
        "b_count": b,
        "tie_count": ties,
        "hands": p + b + ties,
        "last_10": last_10,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)

    api_url = os.getenv("BACOPY_API_URL", "").rstrip("/")
    api_key = os.getenv("BACOPY_API_KEY", "").strip()
    push = os.getenv("BACOPY_PUSH_SNAPSHOTS", "").strip() in ("1", "true", "yes", "on")

    def _push_snapshot(provider: str, table_id: str, snap: dict[str, Any]) -> None:
        if not push or not api_url or not api_key:
            return
        try:
            requests.post(
                f"{api_url}/api/snapshots/update",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"provider": provider, "table_id": table_id, "snapshot": snap},
                timeout=5,
            )
        except Exception:
            return

    try:
        import config as _cfg  # type: ignore
        from scraper import BaccaratScraper  # type: ignore
    except Exception as e:
        sys.stderr.write(
            "Evolution watcher import failed (Camoufox dependencies missing?).\n"
            f"Error: {e}\n"
        )
        return 2

    # Apply headless override for watcher use
    if args.headless:
        _cfg.HEADLESS = True

    scraper = BaccaratScraper()
    scraper.table_name = "all"
    scraper.start()

    try:
        while True:
            configs = scraper.get_all_table_configs() or {}
            players = scraper.get_players_count() or {}
            for tid, cfg in configs.items():
                title = cfg.get("title") or ""
                raw = scraper.get_raw_history(tid) or []
                sm = _summarize_raw_history(raw)
                snap = {
                    "captured_at": _utc_now_iso(),
                    "table_id": tid,
                    "table_name": title,
                    "players": int(players.get(tid, 0) or 0),
                    **sm,
                }
                update_snapshot("evolution", tid, snap)
                _push_snapshot("evolution", tid, snap)
            time.sleep(max(args.interval, 0.2))
    finally:
        try:
            scraper.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
