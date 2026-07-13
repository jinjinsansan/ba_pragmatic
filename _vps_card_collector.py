"""READ-ONLY Pragmatic baccarat card/result collector for the VPS (dga lobby WS).

Discovery (2026-07-14): the Pragmatic **dga lobby WebSocket**
(``wss://dga.pragmaticplaylive.net/ws``) already streams, per resolved hand, the
FULL card data inside ``gameResult`` entries:

    {"time":..,"player":4,"banker":3,"winner":"PLAYER_WIN","gameId":"..",
     "playerCards":["9S","5S","JC"],"bankerCards":["6H","4C","3S"]}

That is everything needed for the "wave = naturals 8/9" hypothesis (naturals,
pairs, scores, third-card comebacks are all derivable from the card lists). The
multibaccarat game client (bafather :9222 / _cdp_card_collector.py) is NOT needed,
and neither is Playwright ``route_web_socket`` (which does not intercept on this
Camoufox build — verified: 0 sockets caught, while ``page.on("websocket")`` caught
the dga WS reliably).

Safety / non-interference
-------------------------
- READ-ONLY passive listener. Never sends a bet or any message. Uses only
  ``page.on("websocket")`` + ``ws.on("framereceived")`` (same mechanism as the
  proven ``collector_pragmatic.py``).
- Own dedicated Camoufox profile (``--profile``), seeded from the shared hakudasama
  ``stake_cookies.json``. Never touches other collectors' profiles.
- Additive new file + its own systemd unit; no existing file is modified.

Output: one compact JSON line per resolved hand (deduped in-process by tableId+gameId):
    {"k":"r","ts":..,"tb":tableId,"tn":tableName,"g":gameId,"win":"P|B|T",
     "ps":playerScore,"bs":bankerScore,"pc":[..cards..],"bc":[..cards..],"t":".."}

Usage
    python _vps_card_collector.py --headless \
        --cookies /opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json \
        --profile /opt/laplace2/auth_state/camoufox_profile_cardfeed \
        --out /opt/laplace2/card_feed.jsonl
    python _vps_card_collector.py --headless --duration 120   # short probe
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from collections import deque
from pathlib import Path

from camoufox.sync_api import Camoufox  # type: ignore

LOBBY_URL = os.getenv(
    "BACOPY_PRAGMATIC_LOBBY_URL",
    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
)
DEFAULT_OUT = os.getenv("BACOPY_CARD_FEED_OUT", "/opt/laplace2/card_feed.jsonl")
MAX_MB = int(os.getenv("BACOPY_CARD_FEED_MAX_MB", "800"))
DGA_HOST = "dga.pragmaticplaylive.net"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("card_collector")

_WIN_MAP = {"PLAYER_WIN": "P", "BANKER_WIN": "B", "TIE": "T"}


def _frame_text(f) -> str:
    p = getattr(f, "payload", f)
    if isinstance(p, (bytes, bytearray)):
        try:
            return bytes(p).decode("utf-8", "replace")
        except Exception:
            return ""
    return p if isinstance(p, str) else ""


class CardCollector:
    def __init__(self, out_path: str):
        self.out_path = out_path
        self.fh = None
        self.stop_flag = False
        self.n_hands = 0
        self.n_frames = 0
        self.n_since_flush = 0
        self.last_hand_at = time.time()
        # in-process dedup of (tableId, gameId); bounded so memory stays flat
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=400_000)

    def _seen_add(self, key: str) -> bool:
        if key in self._seen:
            return False
        if len(self._seen_order) >= self._seen_order.maxlen:
            old = self._seen_order[0]
            self._seen.discard(old)
        self._seen_order.append(key)
        self._seen.add(key)
        return True

    def _on_frame(self, payload: str) -> None:
        self.n_frames += 1
        if not payload or "gameResult" not in payload:
            return
        try:
            obj = json.loads(payload)
        except Exception:
            return
        gr = obj.get("gameResult")
        if not isinstance(gr, list) or not gr:
            return
        tb = obj.get("tableId") or ""
        tn = obj.get("tableName") or ""
        for h in gr:
            if not isinstance(h, dict):
                continue
            gid = h.get("gameId")
            if not gid:
                continue
            key = f"{tb}:{gid}"
            if not self._seen_add(key):
                continue
            line = {
                "k": "r", "ts": round(time.time(), 1),
                "tb": tb, "tn": tn, "g": gid,
                "win": _WIN_MAP.get(str(h.get("winner") or ""), str(h.get("winner") or "")),
                "ps": h.get("player"), "bs": h.get("banker"),
                "pc": h.get("playerCards"), "bc": h.get("bankerCards"),
                "t": h.get("time"),
            }
            self.fh.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
            self.n_hands += 1
            self.last_hand_at = time.time()
            self.n_since_flush += 1
            if self.n_since_flush >= 50:
                self.fh.flush()
                self.n_since_flush = 0

    def _on_ws(self, ws) -> None:
        if DGA_HOST not in ws.url:
            return
        logger.info(f"[WS OPEN] {ws.url[:90]}")
        ws.on("framereceived", lambda f: self._safe_frame(f))
        ws.on("close", lambda: logger.warning("[WS CLOSE] dga"))

    def _safe_frame(self, f) -> None:
        try:
            self._on_frame(_frame_text(f))
        except Exception as e:
            logger.debug(f"frame err: {e}")

    def run(self, headless: bool, cookies_file: Path | None, profile_dir: Path,
            duration: int | None) -> int:
        profile_dir.mkdir(parents=True, exist_ok=True)

        def on_signal(signum, frame):
            logger.warning(f"Signal {signum} received, stopping...")
            self.stop_flag = True

        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_signal)

        stale_sec = int(os.getenv("BACOPY_CARD_FEED_STALE_SEC", "300"))  # no hands 5min -> restart
        start_ts = time.time()
        last_report = start_ts

        self.fh = open(self.out_path, "a", encoding="utf-8")
        self.fh.write(json.dumps({"k": "start", "ts": round(time.time(), 1)}) + "\n")
        self.fh.flush()

        launch_opts = {"headless": headless, "persistent_context": True,
                       "user_data_dir": str(profile_dir)}

        with Camoufox(**launch_opts) as ctx:
            if cookies_file and cookies_file.exists():
                try:
                    with open(cookies_file) as cf:
                        cookies = json.load(cf)
                    ctx.add_cookies(cookies)
                    logger.info(f"Restored {len(cookies)} cookies from {cookies_file}")
                except Exception as e:
                    logger.warning(f"Cookie restore failed: {e}")

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("websocket", self._on_ws)
            logger.info(f"Navigating to {LOBBY_URL}")
            try:
                page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"goto failed (continuing to observe): {e}")
            page.wait_for_timeout(8000)

            while not self.stop_flag:
                page.wait_for_timeout(1000)  # yield to Playwright loop so frames fire
                now = time.time()
                if os.path.exists(self.out_path) and os.path.getsize(self.out_path) > MAX_MB * 1024 * 1024:
                    logger.error("size guard reached; stopping")
                    break
                if now - last_report >= 60:
                    logger.info(f"[STATUS] elapsed={int(now-start_ts)}s frames={self.n_frames} "
                                f"hands={self.n_hands} seen={len(self._seen)}")
                    last_report = now
                if (now - self.last_hand_at) >= stale_sec and (now - start_ts) >= stale_sec:
                    logger.error(f"[watchdog] no new hands for {int(now-self.last_hand_at)}s; "
                                 f"exit(1) for systemd restart")
                    self._close()
                    os._exit(1)
                if duration and (now - start_ts) >= duration:
                    logger.info(f"Duration {duration}s reached, stopping.")
                    break

        self._close()
        logger.info(f"Final: frames={self.n_frames} hands={self.n_hands}")
        return 0

    def _close(self):
        try:
            if self.fh:
                self.fh.flush()
                self.fh.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--duration", type=int, help="stop after N seconds (probe/test)")
    ap.add_argument("--profile", type=str,
                    default="/opt/laplace2/auth_state/camoufox_profile_cardfeed")
    ap.add_argument("--cookies", type=str,
                    default="/opt/laplace2/monitor/auth_state_pragmatic_collector/stake_cookies.json")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT)
    args = ap.parse_args()

    c = CardCollector(out_path=args.out)
    return c.run(
        headless=args.headless,
        cookies_file=Path(args.cookies) if args.cookies else None,
        profile_dir=Path(args.profile),
        duration=args.duration,
    )


if __name__ == "__main__":
    raise SystemExit(main())
