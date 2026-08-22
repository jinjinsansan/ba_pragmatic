#!/usr/bin/env python3
"""_dga_raw_capture.py — one-shot raw dump of ALL dga WS frames (both directions).

Purpose (2026-07-18): field inventory of the dga stream the card collector discards —
looking for (a) shoe/round-number fields (shoe boundaries), (b) bet-volume fields.
Read-only probe; separate profile so the running collector is untouched.
"""
import json, time, argparse, signal
from pathlib import Path
from camoufox.sync_api import Camoufox

DGA_HOST = "dga.pragmaticplaylive.net"
LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"

def frame_text(f):
    p = getattr(f, "payload", f)
    if isinstance(p, (bytes, bytearray)):
        try: return bytes(p).decode("utf-8", "replace")
        except Exception: return ""
    return p if isinstance(p, str) else ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=150)
    ap.add_argument("--out", default="/tmp/dga_raw_capture.jsonl")
    ap.add_argument("--profile", default="/opt/laplace2/auth_state/camoufox_profile_rawprobe")
    ap.add_argument("--cookies", default="/opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json")
    args = ap.parse_args()

    stop = {"f": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(f=True))
    signal.signal(signal.SIGTERM, lambda *a: stop.update(f=True))

    fh = open(args.out, "w", encoding="utf-8")
    stats = {"rx": 0, "tx": 0}

    def dump(direction, payload):
        stats[direction] += 1
        fh.write(json.dumps({"d": direction, "ts": round(time.time(), 2), "p": payload},
                            ensure_ascii=False) + "\n")

    def on_ws(ws):
        if DGA_HOST not in ws.url:
            return
        print(f"[WS OPEN] {ws.url[:100]}", flush=True)
        ws.on("framereceived", lambda f: dump("rx", frame_text(f)))
        ws.on("framesent", lambda f: dump("tx", frame_text(f)))

    Path(args.profile).mkdir(parents=True, exist_ok=True)
    with Camoufox(headless=True, persistent_context=True, user_data_dir=args.profile) as ctx:
        cf = Path(args.cookies)
        if cf.exists():
            try:
                ctx.add_cookies(json.load(open(cf)))
                print("[cookies restored]", flush=True)
            except Exception as e:
                print(f"[cookie restore failed: {e}]", flush=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("websocket", on_ws)
        try:
            page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[goto warn: {e}]", flush=True)
        t0 = time.time()
        while not stop["f"] and time.time() - t0 < args.duration:
            page.wait_for_timeout(1000)
    fh.close()
    print(f"[done] rx={stats['rx']} tx={stats['tx']} -> {args.out}", flush=True)

if __name__ == "__main__":
    main()
