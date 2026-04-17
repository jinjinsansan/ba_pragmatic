from __future__ import annotations

"""Pragmatic live executor (WS direct) — MVP.

Notes:
  - This opens a single Camoufox session and uses the in-page WebSocket object
    to send the <lpbet ...> command observed in sniff logs.
  - DO NOT run bacopy_watch_pragmatic concurrently with this executor on the
    same Stake account (duplicate sessions can trigger a kick).
  - Safety: defaults to $1 flat. Banker/Tie codes are not enabled until verified.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from decision_logger import append_decision_event
from bacopy_db import init_db, try_lock_bet

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"

# Captures WebSocket objects so python can trigger ws.send(payload) via page.evaluate().
_WS_BRIDGE_INIT = r"""
(() => {
  try {
    if (window.__bacopy_ws_bridge_installed) return;
    window.__bacopy_ws_bridge_installed = true;
    window.__bacopy_sockets = [];

    const OrigWS = window.WebSocket;
    function WrappedWebSocket(url, protocols) {
      const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      try {
        ws.__bacopy_url = url;
        window.__bacopy_sockets.push(ws);
      } catch (e) {}
      return ws;
    }
    WrappedWebSocket.prototype = OrigWS.prototype;
    WrappedWebSocket.OPEN = OrigWS.OPEN;
    WrappedWebSocket.CLOSED = OrigWS.CLOSED;
    WrappedWebSocket.CLOSING = OrigWS.CLOSING;
    WrappedWebSocket.CONNECTING = OrigWS.CONNECTING;
    window.WebSocket = WrappedWebSocket;

    window.__bacopy_ws_send = (match, payload) => {
      const urls = [];
      for (const ws of (window.__bacopy_sockets || [])) {
        try {
          const u = ws.__bacopy_url || ws.url || "";
          if (u) urls.push(u);
          if (ws.readyState === OrigWS.OPEN && u.includes(match)) {
            ws.send(payload);
            return { ok: true, url: u };
          }
        } catch (e) {}
      }
      return { ok: false, error: "ws_not_found", known: urls.slice(0, 50) };
    };
  } catch (e) {}
})();
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_ms() -> int:
    return int(time.time() * 1000)


def _api_url() -> str:
    return os.getenv("BACOPY_API_URL", "http://127.0.0.1:8010").rstrip("/")


def _api_key() -> str:
    key = os.getenv("BACOPY_API_KEY", "").strip()
    if not key:
        raise SystemExit("BACOPY_API_KEY is required")
    return key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


def _redact_jsession(url: str) -> str:
    return re.sub(r"(JSESSIONID=)[^&]+", r"\1<REDACTED>", str(url or ""))


def _post_ack(decision_id: str, ack: dict[str, Any], status: str = "processing") -> None:
    try:
        requests.post(
            f"{_api_url()}/api/decisions/{decision_id}/ack",
            headers=_headers(),
            json={"ack": ack, "status": status},
            timeout=10,
        ).raise_for_status()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] post_ack timeout: {e}", flush=True)
    except Exception as e:
        print(f"[WARN] post_ack error: {e}", flush=True)


def _post_result(decision_id: str, result: dict[str, Any], status: str = "done") -> None:
    try:
        requests.post(
            f"{_api_url()}/api/decisions/{decision_id}/result",
            headers=_headers(),
            json={"result": result, "status": status},
            timeout=10,
        ).raise_for_status()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] post_result timeout: {e}", flush=True)
    except Exception as e:
        print(f"[WARN] post_result error: {e}", flush=True)


def _post_heartbeat(payload: dict[str, Any]) -> None:
    # best-effort
    try:
        requests.post(
            f"{_api_url()}/api/executors/heartbeat",
            headers=_headers(),
            json=payload,
            timeout=5,
        )
    except Exception:
        return


def _fetch_decisions(status: str, limit: int) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            f"{_api_url()}/api/decisions",
            params={"status": status, "limit": int(limit)},
            headers=_headers(),
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("decisions") or []
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] fetch_decisions timeout/connection error: {e}", flush=True)
        return []
    except Exception as e:
        print(f"[WARN] fetch_decisions error: {e}", flush=True)
        return []
    return items if isinstance(items, list) else []


def find_game_frame(page, attempts: int = 30):
    for _ in range(attempts):
        for f in page.frames:
            if "qpidreoxcc.net" in f.url or "pragmaticplaylive" in f.url:
                return f
        page.wait_for_timeout(1000)
    return None


def find_shell_app_frame(page, attempts: int = 60):
    for _ in range(attempts):
        for f in page.frames:
            if "apps/lobby" in f.url or f.name == "shell-app":
                return f
        page.wait_for_timeout(1000)
    return None


@dataclass
class _PragmaticState:
    # table mapping
    operator_table_id: str = ""  # numeric (e.g. "415")
    table_name: str = ""  # e.g. "SPEED_BACCARAT_6"
    table_id: str = ""  # internal string (e.g. "2q57e...")
    user_id: str = ""  # ppc...
    jsession_id: str = ""  # same value used in JSESSIONID

    # game / betting phase
    current_game_id: str = ""  # string id from {"game":{"id":...}}
    bets_open_game_id: str = ""  # from {"betsopen":{"game":...}}
    bets_closed_game_id: str = ""  # from {"betsclosed":{"game":...}}
    last_timer: str = ""  # seconds string

    # ws urls
    game_ws_url: str = ""  # gsXX.../game?JSESSIONID=...&tableId=...&type=json

    # result cache from lobby feed
    winners_by_table_game_id: dict[str, dict[str, str]] = None  # tableId -> (gameId -> winner)
    _seen_table_game: set[tuple[str, str]] = None

    # bet confirms (from game ws)
    last_bet_confirm: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.winners_by_table_game_id is None:
            self.winners_by_table_game_id = {}
        if self._seen_table_game is None:
            self._seen_table_game = set()


def _side_to_bc(side: str) -> Optional[str]:
    s = str(side or "").upper().strip()
    if s == "PLAYER":
        return "0"
    # Not enabled until we confirm via sniff
    if s in ("BANKER", "TIE"):
        return None
    return None


def _parse_timer_sec(v: str) -> Optional[float]:
    try:
        s = str(v or "").strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _build_lpbet_xml(*, table_id: str, game_id: str, user_id: str, bc: str, amount: float) -> str:
    ck = str(_epoch_ms())
    # keep format close to observed payload
    amt = str(int(amount)) if float(amount).is_integer() else str(amount)
    return (
        f'<command channel="table-{table_id}">'
        f'<lpbet gm="baccarat_desktop" gId="{game_id}" uId="{user_id}" ck="{ck}"  >'
        f'<bet amt="{amt}" bc="{bc}" ck="{ck}"/>'
        f"</lpbet></command>"
    )


def _maybe_json(payload: Any) -> Optional[dict[str, Any]]:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if not isinstance(payload, str):
            return None
        if not payload.startswith("{"):
            return None
        obj = json.loads(payload)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _update_from_chat_msg(state: _PragmaticState, msg: dict[str, Any]) -> None:
    # outgoing SUBSCRIBE contains tableId/userId/jSessionId
    if msg.get("action") == "SUBSCRIBE" and msg.get("content") == "SUBSCRIBE":
        state.user_id = str(msg.get("userId") or state.user_id)
        state.table_id = str(msg.get("tableId") or state.table_id)
        state.jsession_id = str(msg.get("jSessionId") or state.jsession_id)
        state.table_name = str(msg.get("tableName") or state.table_name)
        return
    # incoming ALERT_JOINED contains operatorGameTableId (numeric mapping)
    u = msg.get("user") if isinstance(msg.get("user"), dict) else {}
    if isinstance(u, dict) and u.get("operatorGameTableId"):
        state.operator_table_id = str(u.get("operatorGameTableId") or state.operator_table_id)
        state.table_name = str(u.get("tableName") or state.table_name)
        # also contains userId/tableId
        state.user_id = str(u.get("userId") or state.user_id)
        state.table_id = str(u.get("tableId") or state.table_id)


def _update_from_game_msg(state: _PragmaticState, msg: dict[str, Any]) -> None:
    if "game" in msg and isinstance(msg["game"], dict):
        gid = str(msg["game"].get("id") or "")
        if gid:
            state.current_game_id = gid
        return
    if "timer" in msg and isinstance(msg["timer"], dict):
        state.last_timer = str(msg["timer"].get("value") or "")
        return
    if "betsopen" in msg and isinstance(msg["betsopen"], dict):
        g = str(msg["betsopen"].get("game") or "")
        if g:
            state.bets_open_game_id = g
        return
    if "betsclosed" in msg and isinstance(msg["betsclosed"], dict):
        g = str(msg["betsclosed"].get("game") or "")
        if g:
            state.bets_closed_game_id = g
        return
    if "bet" in msg and isinstance(msg["bet"], dict):
        # This appears after betsclosed in sniff logs and likely confirms our bet.
        state.last_bet_confirm = msg["bet"]
        return


def _update_from_lobby_msg(state: _PragmaticState, msg: dict[str, Any]) -> None:
    # dga feed: {"tableId":"415","gameResult":[{...winner...gameId...}, ...]}
    table_id = str(msg.get("tableId") or "")
    if not table_id:
        return
    gr = msg.get("gameResult")
    if not isinstance(gr, list) or not gr:
        return
    for h in gr:
        if not isinstance(h, dict):
            continue
        gid = str(h.get("gameId") or "")
        win = str(h.get("winner") or "")
        if not gid or not win:
            continue
        key = (table_id, gid)
        if key in state._seen_table_game:
            continue
        state._seen_table_game.add(key)
        state.winners_by_table_game_id.setdefault(table_id, {})[gid] = win


def _wait_for(predicate, *, timeout_sec: float, tick_ms: int, page=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if predicate():
            return True
        if page is not None:
            page.wait_for_timeout(int(tick_ms))
        else:
            time.sleep(tick_ms / 1000.0)
    return False


def _dismiss_stake_loader(page) -> None:
    """Stake.com の siteLoader オーバーレイを強制除去。
    このオーバーレイが pointer events を遮断してクリックを妨げる。"""
    try:
        page.evaluate("""() => {
            // siteLoader overlay
            const loader = document.getElementById('siteLoader');
            if (loader) { loader.style.display = 'none'; loader.style.pointerEvents = 'none'; }
            // Any other blocking overlays
            document.querySelectorAll('[class*="loading"][data-nosnippet]').forEach(el => {
                el.style.display = 'none'; el.style.pointerEvents = 'none';
            });
        }""")
    except Exception:
        pass


def _join_table(page, *, table_substr: str, auto_click_wait_sec: int) -> None:
    print("[Stage 1] goto stake pragmatic lobby ...", flush=True)
    page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(10_000)

    # Stake loader overlay を除去 (クリック遮断防止)
    _dismiss_stake_loader(page)

    print("[Stage 2] wait pragmatic shell ...", flush=True)
    gf = find_game_frame(page)
    if not gf:
        raise RuntimeError("pragmatic shell not found")

    page.wait_for_timeout(5_000)
    _dismiss_stake_loader(page)

    print("[Stage 3] find internal lobby (shell-app) ...", flush=True)
    shell = find_shell_app_frame(page)
    if not shell:
        raise RuntimeError("shell-app not found")

    clicked = False
    table_substr = (table_substr or "").strip()

    if table_substr:
        print(f"[Stage 4] wait (<= {auto_click_wait_sec}s) for '{table_substr}' then click ...", flush=True)
        deadline = time.time() + float(max(auto_click_wait_sec, 1))
        while time.time() < deadline and not clicked:
            # 毎回ローダー除去を試みる (再出現するため)
            _dismiss_stake_loader(page)
            try:
                locator = shell.get_by_text(re.compile(re.escape(table_substr), re.I))
                if locator.count() > 0:
                    first = locator.first
                    first.scroll_into_view_if_needed(timeout=3000)
                    # The matched node may be a <span>. Try to click a nearby clickable element first.
                    try:
                        shell.locator(f"[role='button']:has-text('{table_substr}')").first.click(timeout=3000, force=True)
                    except Exception:
                        try:
                            shell.locator(f"button:has-text('{table_substr}')").first.click(timeout=3000, force=True)
                        except Exception:
                            try:
                                shell.locator(f"a:has-text('{table_substr}')").first.click(timeout=3000, force=True)
                            except Exception:
                                first.click(timeout=3000, force=True)
                    clicked = True
                    print(f"[Stage 4] clicked '{table_substr}' via text match", flush=True)
                    break
            except Exception:
                pass

            # Fallback: only click elements that *contain the target text* (never click a random first button)
            if not clicked:
                for sel in [
                    f"[role='button']:has-text('{table_substr}')",
                    f"button:has-text('{table_substr}')",
                    f"a:has-text('{table_substr}')",
                    f"div:has-text('{table_substr}')",
                ]:
                    try:
                        loc = shell.locator(sel)
                        cnt = loc.count()
                        if cnt > 0:
                            loc.first.scroll_into_view_if_needed(timeout=3000)
                            loc.first.click(timeout=3000, force=True)
                            clicked = True
                            print(f"[Stage 4] clicked via filtered fallback '{sel}' (count={cnt})", flush=True)
                            break
                    except Exception:
                        continue

            if not clicked:
                # shell frame の再取得を試みる (DOM 更新時)
                new_shell = find_shell_app_frame(page, attempts=2)
                if new_shell:
                    shell = new_shell
                page.wait_for_timeout(2000)

    if not clicked:
        print("[WARN] auto-click did not succeed. Please click the table manually.", flush=True)
        print("       After entry, wait for betting phase then you can start sending decisions.", flush=True)
    else:
        page.wait_for_timeout(12_000)
        print("[Stage 4] table entry waiting...", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile-dir", default=str(Path(__file__).parent / "auth_state" / "camoufox_profile"))
    ap.add_argument("--cookies-file", default="")
    ap.add_argument("--table-name-substr", default=os.getenv("BACOPY_TABLE_SUBSTR", ""))
    ap.add_argument("--auto-click-wait-sec", type=int, default=120)

    ap.add_argument("--poll-sec", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--flat-amount", type=float, default=1.0)
    ap.add_argument("--only-table-id", default=os.getenv("BACOPY_ONLY_TABLE_ID", ""), help="operator tableId (numeric) to accept")
    ap.add_argument("--bet-timeout-sec", type=int, default=20)
    ap.add_argument("--min-timer-sec", type=float, default=2.0, help="Refuse bets if timer is below this (when available)")
    ap.add_argument("--result-timeout-sec", type=int, default=90)
    ap.add_argument("--allow-switch-table", action="store_true", help="Allow SWITCH_TABLE action to navigate/click table")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    try:
        from camoufox.sync_api import Camoufox  # type: ignore
    except ModuleNotFoundError as e:
        raise SystemExit(
            "camoufox is required to run this executor. "
            "Install it in the environment that runs the browser automation (likely Windows), "
            "or run this script from the same environment where ba/ GUI automation works."
        ) from e

    print(f"[executor-live] api={_api_url()} poll={args.poll_sec}s limit={args.limit}", flush=True)
    if args.only_table_id:
        print(f"[executor-live] only_table_id={args.only_table_id}", flush=True)
    print("[executor-live] DO NOT run pragmatic watcher concurrently on same account.", flush=True)

    state = _PragmaticState()
    init_db()
    processed_keys: set[tuple[str, str]] = set()  # (operator_table_id, game_id)
    consecutive_hard_errors = 0
    executor_id = os.getenv("BACOPY_EXECUTOR_ID", "").strip() or f"exec_{uuid.uuid4().hex[:8]}"
    executor_label = os.getenv("BACOPY_EXECUTOR_LABEL", "").strip()
    executor_username = os.getenv("BACOPY_EXECUTOR_USERNAME", "").strip()
    last_error = ""
    last_hb = 0.0

    def heartbeat(status: str) -> None:
        nonlocal last_hb
        now = time.time()
        if now - last_hb < 5.0:
            return
        last_hb = now
        _post_heartbeat(
            {
                "executor_id": executor_id,
                "label": executor_label,
                "username": executor_username,
                "provider": "pragmatic",
                "table_id": state.operator_table_id,
                "table_name": state.table_name,
                "balance": None,
                "seq": {},
                "status": status,
                "error": last_error,
            }
        )

    def on_ws(ws):
        url = str(ws.url or "")
        if "pragmaticplaylive.net/game" in url:
            state.game_ws_url = url

        def on_recv(frame_data):
            p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
            obj = _maybe_json(p)
            if not obj:
                return
            if "betsopen" in obj or "betsclosed" in obj or "game" in obj or "timer" in obj or "bet" in obj:
                _update_from_game_msg(state, obj)
                return

        def on_send(frame_data):
            # we only need chat SUBSCRIBE content (it's sent)
            p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
            obj = _maybe_json(p)
            if not obj:
                return
            if "chat.pragmaticplaylive.net" in url:
                _update_from_chat_msg(state, obj)

        ws.on("framereceived", on_recv)
        ws.on("framesent", on_send)

    def on_any_ws_frame(ws, frame_data, direction: str):
        # Fallback listener for lobby feed messages that carry gameResult winners
        url = str(getattr(ws, "url", "") or "")
        if "dga.pragmaticplaylive.net/ws" not in url:
            return
        if direction != "recv":
            return
        p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
        obj = _maybe_json(p)
        if obj:
            _update_from_lobby_msg(state, obj)

    with Camoufox(
        headless=bool(args.headless),
        persistent_context=True,
        user_data_dir=str(Path(args.profile_dir)),
    ) as ctx:
        page = ctx.new_page()
        try:
            page.add_init_script(_WS_BRIDGE_INIT)
        except Exception:
            pass
        try:
            page.evaluate(_WS_BRIDGE_INIT)
        except Exception:
            pass

        if args.cookies_file:
            try:
                cookies = json.loads(Path(args.cookies_file).read_text(encoding="utf-8"))
                if isinstance(cookies, list) and cookies:
                    ctx.add_cookies(cookies)
                    print(f"[executor-live] restored cookies: {len(cookies)} from {args.cookies_file}", flush=True)
            except Exception as e:
                print(f"[executor-live] cookie restore failed: {e}", flush=True)

        # Attach WS listeners
        page.on("websocket", on_ws)

        def _attach_ws_frame_spy(ws):
            ws.on("framereceived", lambda fd: on_any_ws_frame(ws, fd, "recv"))
            ws.on("framesent", lambda fd: on_any_ws_frame(ws, fd, "send"))

        # Playwright doesn't expose all ws through page.on("websocket") callbacks consistently in some envs,
        # so we also attach when detected.
        page.on("websocket", _attach_ws_frame_spy)

        # Enter lobby and (attempt to) join table
        _join_table(page, table_substr=str(args.table_name_substr or ""), auto_click_wait_sec=int(args.auto_click_wait_sec))

        # Ensure WS bridge exists in the pragmatic iframe context (send must be evaluated in-frame).
        game_frame = find_game_frame(page, attempts=60)
        if game_frame:
            try:
                game_frame.evaluate(_WS_BRIDGE_INIT)
            except Exception:
                pass

        # Wait until we have game ws + chat mapping (user_id/table_id/jsession/operator_table_id)
        print("[Stage 5] waiting for Pragmatic session identifiers ...", flush=True)
        _wait_for(
            lambda: bool(state.game_ws_url and state.table_id and state.user_id and state.jsession_id),
            timeout_sec=180,
            tick_ms=500,
            page=page,
        )
        if state.game_ws_url:
            print(f"[session] game_ws={_redact_jsession(state.game_ws_url)}", flush=True)
        if state.operator_table_id:
            print(f"[session] operator_table_id={state.operator_table_id} table_name={state.table_name}", flush=True)
        print(f"[executor-live] executor_id={executor_id}", flush=True)

        def send_bet_xml(xml_payload: str, match: str) -> dict[str, Any]:
            target_frames = []
            if game_frame:
                target_frames.append(game_frame)
            # fallback: try all frames in case pragmatic moved
            target_frames.extend([f for f in page.frames if f not in target_frames])

            last_err = None
            for fr in target_frames:
                try:
                    res = fr.evaluate(
                        "(args) => window.__bacopy_ws_send(args.match, args.payload)",
                        {"match": match, "payload": xml_payload},
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return res
                    last_err = res
                except Exception as e:
                    last_err = {"ok": False, "error": f"evaluate_failed: {e}"}
            return last_err or {"ok": False, "error": "evaluate_failed"}

        def wait_bets_open(timeout_sec: float) -> Optional[str]:
            ok = _wait_for(lambda: bool(state.bets_open_game_id), timeout_sec=timeout_sec, tick_ms=200, page=page)
            return state.bets_open_game_id if ok else None

        def wait_bet_confirm(timeout_sec: float) -> Optional[dict[str, Any]]:
            state.last_bet_confirm = None
            _wait_for(lambda: state.last_bet_confirm is not None, timeout_sec=timeout_sec, tick_ms=200, page=page)
            return state.last_bet_confirm

        def wait_result(game_id: str, operator_table_id: str, timeout_sec: float) -> Optional[str]:
            # dga feed uses winner strings: PLAYER_WIN/BANKER_WIN/TIE
            def _winner() -> Optional[str]:
                w = (state.winners_by_table_game_id.get(str(operator_table_id)) or {}).get(str(game_id))
                if not w:
                    return None
                if w == "PLAYER_WIN":
                    return "player"
                if w == "BANKER_WIN":
                    return "banker"
                if w == "TIE":
                    return "tie"
                return None

            ok = _wait_for(lambda: _winner() is not None, timeout_sec=timeout_sec, tick_ms=250, page=page)
            return _winner() if ok else None

        while True:
            heartbeat("running")

            # Resume processing first, then pending (crash-safe).
            items = _fetch_decisions("processing", limit=int(args.limit))
            if not items:
                items = _fetch_decisions("pending", limit=int(args.limit))
            if not items:
                if args.once:
                    break
                page.wait_for_timeout(int(max(args.poll_sec, 0.2) * 1000))
                continue

            for d in items:
                did = str(d.get("decision_id") or "")
                provider = str(d.get("provider") or "")
                if provider != "pragmatic" or not did:
                    continue

                fa = d.get("friend_action") or {}
                action = str((fa.get("action") or "")).upper()
                side = str((fa.get("side") or "")).upper()
                decision_table_id = str(d.get("table_id") or "")
                decision_table_name = str(d.get("table_name") or "")
                decision_snapshot = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else {}
                target_executor_id = str(d.get("target_executor_id") or "")

                if target_executor_id and target_executor_id != executor_id:
                    continue

                if args.only_table_id and decision_table_id and str(args.only_table_id) != decision_table_id:
                    # refuse silently to avoid acting on wrong table if multiple masters exist
                    continue

                ack = {
                    "mode": "live_ws",
                    "acked_at": _utc_now_iso(),
                    "provider": provider,
                    "decision_table_id": decision_table_id,
                    "decision_table_name": decision_table_name,
                    "session": {
                        "operator_table_id": state.operator_table_id,
                        "table_name": state.table_name,
                        "table_id": state.table_id,
                        "game_ws": _redact_jsession(state.game_ws_url),
                    },
                    "friend_action": fa,
                    "snapshot_before": decision_snapshot,
                }
                _post_ack(did, ack, status="processing")

                if action == "SWITCH_TABLE":
                    if not args.allow_switch_table:
                        _post_result(did, {"error": "switch_table disabled (start executor with --allow-switch-table)"}, status="error")
                        continue
                    target = decision_table_name or (decision_snapshot.get("table_name") if isinstance(decision_snapshot, dict) else "") or ""
                    if not target:
                        _post_result(did, {"error": "table_name required for SWITCH_TABLE"}, status="error")
                        continue

                    # Clear identifiers so we can wait for new mapping.
                    state.operator_table_id = ""
                    state.table_name = ""
                    state.table_id = ""
                    state.user_id = ""
                    state.jsession_id = ""
                    state.game_ws_url = ""

                    try:
                        _join_table(page, table_substr=str(target), auto_click_wait_sec=int(args.auto_click_wait_sec))
                        game_frame = find_game_frame(page, attempts=60)
                        if game_frame:
                            try:
                                game_frame.evaluate(_WS_BRIDGE_INIT)
                            except Exception:
                                pass
                        ok = _wait_for(
                            lambda: bool(state.game_ws_url and state.table_id and state.user_id and state.jsession_id),
                            timeout_sec=180,
                            tick_ms=500,
                            page=page,
                        )
                        if not ok:
                            raise RuntimeError("session identifiers not populated (table/user/ws missing)")
                        # If numeric table_id provided, wait for operator table id match too (best-effort).
                        if decision_table_id:
                            _wait_for(
                                lambda: bool(state.operator_table_id and state.operator_table_id == decision_table_id),
                                timeout_sec=30,
                                tick_ms=500,
                                page=page,
                            )
                        res = {
                            "mode": "live_ws",
                            "observed_at": _utc_now_iso(),
                            "executor_id": executor_id,
                            "switched_to": {
                                "operator_table_id": state.operator_table_id,
                                "table_name": state.table_name,
                                "table_id": state.table_id,
                                "game_ws": _redact_jsession(state.game_ws_url),
                            },
                        }
                        _post_result(did, res, status="done")
                    except Exception as e:
                        last_error = f"switch_table failed: {e}"
                        heartbeat("error")
                        _post_result(did, {"error": last_error}, status="error")
                    continue

                if action == "LOOK":
                    res = {"mode": "live_ws", "observed_at": _utc_now_iso(), "note": "LOOK no-op (live)"}
                    _post_result(did, res, status="done")
                    continue

                if action != "BET":
                    _post_result(did, {"error": f"unsupported action: {action}"}, status="error")
                    continue

                bc = _side_to_bc(side)
                if bc is None:
                    _post_result(did, {"error": f"unsupported side (needs sniff): {side}"}, status="error")
                    continue

                if not (state.table_id and state.user_id and state.game_ws_url):
                    _post_result(did, {"error": "pragmatic session not ready (table/user/ws missing)"}, status="error")
                    continue

                if decision_table_id and state.operator_table_id and decision_table_id != state.operator_table_id:
                    _post_result(
                        did,
                        {
                            "error": "executor is in a different table",
                            "executor_operator_table_id": state.operator_table_id,
                            "decision_table_id": decision_table_id,
                        },
                        status="error",
                    )
                    continue

                # Wait for betting open & current game id
                state.bets_open_game_id = ""
                state.current_game_id = ""
                state.last_timer = ""
                game_id = wait_bets_open(timeout_sec=float(args.bet_timeout_sec))
                if not game_id:
                    _post_result(
                        did,
                        {"error": "betsopen timeout", "timeout_sec": args.bet_timeout_sec},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = "betsopen timeout"
                    heartbeat("error")
                    continue

                op_tid = state.operator_table_id or decision_table_id or str(args.only_table_id or "")
                if not op_tid:
                    _post_result(did, {"error": "operator_table_id unknown (set --only-table-id or ensure chat mapping)"}, status="error")
                    consecutive_hard_errors += 1
                    last_error = "operator_table_id unknown"
                    heartbeat("error")
                    continue

                # Timer gating (best-effort)
                tsec = _parse_timer_sec(state.last_timer)
                if tsec is not None and tsec < float(args.min_timer_sec):
                    _post_result(
                        did,
                        {"error": "bet_window_too_late", "timer_sec": tsec, "min_timer_sec": args.min_timer_sec, "game_id": game_id},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = f"bet_window_too_late timer={tsec}"
                    heartbeat("error")
                    continue

                # Idempotency guard: at most 1 bet per (table, game) across restarts.
                key = (str(op_tid), str(game_id))
                if key in processed_keys or not try_lock_bet(provider=provider, table_id=str(op_tid), game_id=str(game_id), decision_id=did):
                    _post_result(
                        did,
                        {"error": "duplicate_bet_guard", "operator_table_id": op_tid, "game_id": game_id},
                        status="error",
                    )
                    continue
                processed_keys.add(key)

                amt = float(args.flat_amount or 1.0)
                xml = _build_lpbet_xml(table_id=state.table_id, game_id=game_id, user_id=state.user_id, bc=bc, amount=amt)
                match = f"tableId={state.table_id}&type=json"
                send_res = send_bet_xml(xml, match=match)

                if not send_res.get("ok"):
                    _post_result(
                        did,
                        {"error": "ws_send failed", "detail": send_res, "match": match},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = "ws_send failed"
                    heartbeat("error")
                    continue

                confirm = wait_bet_confirm(timeout_sec=5.0)
                if confirm is None:
                    # At this point we might have placed a bet but lost confirmation.
                    # Stop to avoid accidental duplicate bets.
                    _post_result(
                        did,
                        {"error": "bet_confirm_timeout", "game_id": game_id, "operator_table_id": op_tid},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = "bet_confirm_timeout"
                    heartbeat("error")
                    if consecutive_hard_errors >= 3:
                        raise SystemExit("panic_stop: repeated critical errors (missing bet confirmation)")
                    continue
                consecutive_hard_errors = 0
                last_error = ""

                # Resolve by dga feed winner
                outcome = wait_result(game_id, operator_table_id=op_tid, timeout_sec=float(args.result_timeout_sec))
                if outcome is None:
                    _post_result(
                        did,
                        {"error": "result timeout", "game_id": game_id, "operator_table_id": op_tid, "timeout_sec": args.result_timeout_sec},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = "result timeout"
                    heartbeat("error")
                    continue

                result_payload = {
                    "mode": "live_ws",
                    "observed_at": _utc_now_iso(),
                    "provider": provider,
                    "executor_id": executor_id,
                    "operator_table_id": state.operator_table_id,
                    "table_name": state.table_name,
                    "table_id": state.table_id,
                    "game_id": game_id,
                    "friend_action": fa,
                    "bet": {"amount": amt, "side": side, "bc": bc, "sent_to": send_res.get("url", "")[:120]},
                    "bet_confirm": confirm or {},
                    "outcome": outcome,
                }
                _post_result(did, result_payload, status="done")

                try:
                    append_decision_event(
                        {
                            "schema_version": 1,
                            "event_type": "decision_resolved",
                            "decision_id": did,
                            "captured_at": ack.get("acked_at"),
                            "provider": provider,
                            "table_id": decision_table_id,
                            "table_name": decision_table_name,
                            "snapshot": decision_snapshot,
                            "friend_action": fa,
                            "ack": ack,
                            "result": outcome,
                            "execution": {"mode": "live_ws", "game_id": game_id, "bet_confirm": confirm or {}},
                            "resolved_at": result_payload.get("observed_at"),
                        }
                    )
                except Exception:
                    pass

                print(f"[done] {did} {state.table_name} game={game_id} side={side} -> {outcome}", flush=True)

            if args.once:
                break
            page.wait_for_timeout(int(max(args.poll_sec, 0.2) * 1000))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
