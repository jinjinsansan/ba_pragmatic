"""READ-ONLY Pragmatic multibaccarat card + gameresult collector for the VPS.

Purpose
-------
Capture the per-card / per-result WebSocket frames that flow inside the Pragmatic
``/desktop/multibaccarat`` game client (discovered 2026-07-12 on the bafather
betting Chrome via CDP :9222). The VPS runs Camoufox (Firefox), which has no CDP
:9222, so instead of a raw CDP sniff we use Playwright's ``route_web_socket`` — the
exact same transparent-proxy mechanism ``dual_line_live_executor.py`` already uses
to reach the multibaccarat game WS on this stack.

Safety / non-interference (this process must never break anything else)
----------------------------------------------------------------------
- READ-ONLY: it NEVER sends a bet or any client-originated message of its own. The
  route handler forwards every frame verbatim in BOTH directions; we only *observe*
  the server->client frames. If parsing ever fails we still forward the frame.
- Own dedicated Camoufox profile (``--profile``), seeded from the shared
  ``stake_cookies.json`` (hakudasama, zero-deposit). It never touches the profiles
  used by ``collector_pragmatic.py`` or ``dual_line_pragmatic_bot.py``.
- Separate account from bafather (lselfloveself), so no cross-session kick.
- Additive: a brand-new file + its own systemd unit. No existing file is modified.

Output (same compact format as bafather ``_cdp_card_collector.py``)
    {"k":"c","ts":..,"tb":table,"g":game,"pl":place,"sc":"JC8","v":val,"n":count}
    {"k":"r","ts":..,"tb":table,"g":game,"res":"player","score":"9",
     "nat":1,"pp":0,"bp":0,"s6":0,"t":"14:49:53"}

Usage
    python _vps_card_collector.py --headless --cookies /path/stake_cookies.json \
        --profile /opt/laplace2/auth_state/camoufox_profile_cardfeed \
        --out /opt/laplace2/card_feed.jsonl
    python _vps_card_collector.py --headless --duration 180 --verbose-ws   # short probe
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from camoufox.sync_api import Camoufox  # type: ignore

LOBBY_URL = os.getenv(
    "BACOPY_PRAGMATIC_LOBBY_URL",
    "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat",
)
DEFAULT_OUT = os.getenv("BACOPY_CARD_FEED_OUT", "/opt/laplace2/card_feed.jsonl")
MAX_MB = int(os.getenv("BACOPY_CARD_FEED_MAX_MB", "800"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("card_collector")


def _b(v) -> int:
    return 1 if str(v).lower() == "true" else 0


def _msg_to_text(msg) -> str:
    """route_web_socket message -> str payload (or '' if not text/JSON-ish)."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, (bytes, bytearray)):
        try:
            return bytes(msg).decode("utf-8", "replace")
        except Exception:
            return ""
    # Some Playwright builds wrap the payload on an attribute.
    for attr in ("payload", "text", "data"):
        v = getattr(msg, attr, None)
        if isinstance(v, str):
            return v
        if isinstance(v, (bytes, bytearray)):
            try:
                return bytes(v).decode("utf-8", "replace")
            except Exception:
                return ""
    return ""


class CardCollector:
    def __init__(self, out_path: str, verbose_ws: bool = False):
        self.out_path = out_path
        self.verbose_ws = verbose_ws
        self.fh = None
        self.stop_flag = False
        self.n_cards = 0
        self.n_results = 0
        self.n_frames = 0            # server->client frames seen (any socket)
        self.n_since_flush = 0
        self.last_event_at = time.time()
        self._seen_ws: set[str] = set()

    # ---- frame parsing (mirrors bafather _cdp_card_collector.py) ----
    def _handle_server_frame(self, payload: str) -> None:
        if not payload or payload[0] != "{":
            return
        line = None
        try:
            if '"card"' in payload[:16]:
                c = json.loads(payload)["card"]
                line = {
                    "k": "c", "ts": round(time.time(), 1),
                    "tb": c.get("table"), "g": c.get("game"),
                    "pl": c.get("place"), "sc": c.get("sc"),
                    "v": c.get("value"), "n": c.get("cardCount"),
                }
                self.n_cards += 1
            elif '"gameresult"' in payload[:24]:
                r = json.loads(payload)["gameresult"]
                if str(r.get("gameType") or "") not in ("baccarat", ""):
                    return
                line = {
                    "k": "r", "ts": round(time.time(), 1),
                    "tb": r.get("table"), "g": r.get("gameId"),
                    "res": r.get("result"), "score": r.get("score"),
                    "nat": _b(r.get("natural")), "pp": _b(r.get("player_pair")),
                    "bp": _b(r.get("banker_pair")), "s6": _b(r.get("super6")),
                    "t": r.get("time"),
                }
                self.n_results += 1
        except Exception:
            line = None
        if line is not None:
            self.last_event_at = time.time()
            self.fh.write(json.dumps(line, separators=(",", ":")) + "\n")
            self.n_since_flush += 1
            if self.n_since_flush >= 50:
                self.fh.flush()
                self.n_since_flush = 0

    # ---- transparent WS proxy (verbatim two-way forward; observe only) ----
    def _make_handler(self):
        def _handle_ws(route):
            url = ""
            try:
                url = str(getattr(route, "url", "") or "")
                if self.verbose_ws and url not in self._seen_ws:
                    self._seen_ws.add(url)
                    logger.info(f"[WS] intercepted: {url[:120]}")
                server = route.connect_to_server()

                def _from_client(msg):
                    # NEVER modify/inspect for action — just forward verbatim.
                    try:
                        server.send(msg)
                    except Exception:
                        pass

                def _from_server(msg):
                    # 1) forward verbatim FIRST (never let observation delay/break it)
                    try:
                        route.send(msg)
                    except Exception:
                        pass
                    # 2) observe only (isolated; any failure here is harmless)
                    try:
                        self.n_frames += 1
                        text = _msg_to_text(msg)
                        if text[:1] == "{" and ('"card"' in text[:16] or '"gameresult"' in text[:24]):
                            self._handle_server_frame(text)
                    except Exception:
                        pass

                route.on_message(_from_client)
                server.on_message(_from_server)
            except Exception as e:
                logger.warning(f"[WS] proxy setup error for {url[:80]}: {e}")
                try:
                    route.continue_()
                except Exception:
                    pass
        return _handle_ws

    def run(self, headless: bool, cookies_file: Path | None, profile_dir: Path,
            duration: int | None) -> int:
        profile_dir.mkdir(parents=True, exist_ok=True)

        def on_signal(signum, frame):
            logger.warning(f"Signal {signum} received, stopping...")
            self.stop_flag = True

        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_signal)

        stale_sec = int(os.getenv("BACOPY_CARD_FEED_STALE_SEC", "600"))  # 10 min no frames -> restart
        start_ts = time.time()
        last_report = start_ts

        self.fh = open(self.out_path, "a", encoding="utf-8")
        self.fh.write(json.dumps({"k": "start", "ts": round(time.time(), 1)}) + "\n")
        self.fh.flush()

        launch_opts = {
            "headless": headless,
            "persistent_context": True,
            "user_data_dir": str(profile_dir),
        }

        with Camoufox(**launch_opts) as ctx:
            # register transparent proxy at the CONTEXT level (catches OOPIF sockets)
            handler = self._make_handler()
            try:
                ctx.route_web_socket("**", handler)
                logger.info("[WS] context.route_web_socket registered")
            except AttributeError:
                logger.error("[WS] context.route_web_socket unavailable in this Playwright build")
                return 3

            if cookies_file and cookies_file.exists():
                try:
                    with open(cookies_file) as cf:
                        cookies = json.load(cf)
                    ctx.add_cookies(cookies)
                    logger.info(f"Restored {len(cookies)} cookies from {cookies_file}")
                except Exception as e:
                    logger.warning(f"Cookie restore failed: {e}")

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            logger.info(f"Navigating to {LOBBY_URL}")
            try:
                page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"goto failed (continuing to observe): {e}")
            page.wait_for_timeout(8000)

            while not self.stop_flag:
                # yields to Playwright event loop so route handlers fire in real time
                page.wait_for_timeout(1000)
                now = time.time()
                if os.path.exists(self.out_path) and os.path.getsize(self.out_path) > MAX_MB * 1024 * 1024:
                    logger.error("size guard reached; stopping")
                    break
                if now - last_report >= 60:
                    logger.info(
                        f"[STATUS] elapsed={int(now-start_ts)}s frames={self.n_frames} "
                        f"cards={self.n_cards} results={self.n_results} ws={len(self._seen_ws)}"
                    )
                    last_report = now
                # watchdog: no card/result events for a long time -> self-restart (systemd)
                if (now - self.last_event_at) >= stale_sec and (now - start_ts) >= stale_sec:
                    logger.error(
                        f"[watchdog] no card/result events for {int(now-self.last_event_at)}s "
                        f"(frames={self.n_frames}); exit(1) for systemd restart"
                    )
                    self._close()
                    os._exit(1)
                if duration and (now - start_ts) >= duration:
                    logger.info(f"Duration {duration}s reached, stopping.")
                    break

        self._close()
        logger.info(f"Final: frames={self.n_frames} cards={self.n_cards} results={self.n_results}")
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
    ap.add_argument("--verbose-ws", action="store_true", help="log every intercepted WS url")
    args = ap.parse_args()

    c = CardCollector(out_path=args.out, verbose_ws=args.verbose_ws)
    return c.run(
        headless=args.headless,
        cookies_file=Path(args.cookies) if args.cookies else None,
        profile_dir=Path(args.profile),
        duration=args.duration,
    )


if __name__ == "__main__":
    raise SystemExit(main())
