"""Pragmatic WS watcher -> snapshot_store.

This process is read-only (no betting). It listens to Pragmatic lobby WS and
publishes per-table snapshots to `data/latest_snapshots.json` so the Master UI
can make decisions without opening Stake.com.

Run:
  BACOPY_API_KEY=... python bacopy_watch_pragmatic.py --headless
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from snapshot_store import update_snapshot


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_snapshot(table_id: str, buf: Any) -> dict[str, Any]:
    # buf is collector_pragmatic.ShoeBuffer
    last_hand = buf.hands[-1] if getattr(buf, "hands", None) else None
    return {
        "captured_at": _utc_now_iso(),
        "table_id": table_id,
        "table_name": getattr(buf, "table_name", "") or "",
        "table_type": getattr(buf, "table_type", None),
        "hands": len(getattr(buf, "hands", []) or []),
        "fresh_start": bool(getattr(buf, "fresh_start", False)),
        "statistics": getattr(buf, "last_statistics", None),
        "shoe_summary": getattr(buf, "last_shoe_summary", None),
        "good_roads_map": getattr(buf, "last_good_roads", None),
        "last_hand": last_hand,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--duration", type=int, default=0, help="seconds (0=forever)")
    ap.add_argument("--profile", type=str, default="", help="Camoufox profile dir (optional)")
    ap.add_argument("--cookies", type=str, default="", help="stake cookies json (optional)")
    args = ap.parse_args()

    # Lazy imports (Camoufox/Playwright may not exist in all dev envs)
    try:
        import collector_pragmatic as cp  # type: ignore
    except Exception as e:
        sys.stderr.write(
            "collector_pragmatic import failed. This watcher requires Camoufox/Playwright.\n"
            f"Error: {e}\n"
        )
        return 2

    class WatchCollector(cp.Collector):  # type: ignore
        def on_ws_frame(self, payload):  # type: ignore[override]
            super().on_ws_frame(payload)
            try:
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
                msg = json.loads(payload)
            except Exception:
                return
            if not isinstance(msg, dict):
                return
            table_id = msg.get("tableId")
            if not table_id:
                return
            buf = self.buffers.get(table_id)
            if not buf:
                return
            snap = _make_snapshot(table_id, buf)
            update_snapshot("pragmatic", table_id, snap)

    c = WatchCollector(headless=args.headless, raw_log=False)
    profile = Path(args.profile) if args.profile else None
    cookies = Path(args.cookies) if args.cookies else None
    t0 = time.time()
    while True:
        # collector_pragmatic has its own reconnect logic via page goto loop,
        # so we just run it once and rely on its internal stability.
        c.run(duration=args.duration or None, profile_dir=profile, cookies_file=cookies)
        if args.duration and args.duration > 0:
            return 0
        if time.time() - t0 > 5:
            time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
