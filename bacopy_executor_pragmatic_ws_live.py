from __future__ import annotations

"""Pragmatic live executor (WS direct) — MVP.

Notes:
  - This opens a single Camoufox session and uses the in-page WebSocket object
    to send the <lpbet ...> command observed in sniff logs.
  - DO NOT run bacopy_watch_pragmatic concurrently with this executor on the
    same Stake account (duplicate sessions can trigger a kick).
  - Safety: defaults to $1 flat. Banker/Tie codes are not enabled until verified.
"""

import atexit
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
    window.__bacopy_ws_events = window.__bacopy_ws_events || [];

    const pushEvent = (ev) => {
      try {
        window.__bacopy_ws_events.push(ev);
        if (window.__bacopy_ws_events.length > 2000) {
          window.__bacopy_ws_events.splice(0, window.__bacopy_ws_events.length - 1000);
        }
      } catch (e) {}
    };

    window.__bacopy_ws_drain = (maxItems) => {
      try {
        const n = (typeof maxItems === 'number' && maxItems > 0) ? maxItems : 300;
        const out = window.__bacopy_ws_events.slice(0, n);
        window.__bacopy_ws_events.splice(0, out.length);
        return out;
      } catch (e) {
        return [];
      }
    };

    const OrigWS = window.WebSocket;

    const attachSpy = (ws) => {
      try {
        if (ws.__bacopy_spy_attached) return;
        ws.__bacopy_spy_attached = true;
        const u = ws.__bacopy_url || ws.url || "";
        ws.addEventListener('message', (evt) => {
          try {
            const data = (evt && evt.data !== undefined) ? evt.data : "";
            pushEvent({ts: Date.now(), dir: "recv", url: u, data});
          } catch (e) {}
        });
        const origSend = ws.send;
        ws.send = function(payload) {
          try { pushEvent({ts: Date.now(), dir: "send", url: u, data: payload}); } catch (e) {}
          return origSend.call(ws, payload);
        };
      } catch (e) {}
    };

    // Ensure we always have at least one controllable WS to the game url, even if
    // Pragmatic moved its internal WS into a Worker and our wrapper can't capture it.
    window.__bacopy_ws_open = (url) => {
      try {
        const u = String(url || "");
        if (!u) return { ok: false, error: "url_required" };
        for (const ws of (window.__bacopy_sockets || [])) {
          try {
            const wu = ws.__bacopy_url || ws.url || "";
            if (wu === u && ws.readyState !== OrigWS.CLOSED) {
              return { ok: true, existing: true, url: wu, readyState: ws.readyState };
            }
          } catch (e) {}
        }
        const ws = new OrigWS(u);
        ws.__bacopy_url = u;
        window.__bacopy_sockets.push(ws);
        attachSpy(ws);
        return { ok: true, existing: false, url: u, readyState: ws.readyState };
      } catch (e) {
        return { ok: false, error: "open_failed", detail: String(e) };
      }
    };

    window.__bacopy_ws_open_send = async (url, payload, timeoutMs) => {
      try {
        const u = String(url || "");
        if (!u) return { ok: false, error: "url_required" };

        // Reuse an existing open socket if possible.
        for (const ws of (window.__bacopy_sockets || [])) {
          try {
            const wu = ws.__bacopy_url || ws.url || "";
            if (wu === u && ws.readyState === OrigWS.OPEN) {
              ws.send(payload);
              return { ok: true, url: wu, reused: true };
            }
          } catch (e) {}
        }

        // Otherwise, open then send.
        const res = window.__bacopy_ws_open(u);
        if (!res || !res.ok) return res;

        const tmo = (typeof timeoutMs === "number" && timeoutMs > 0) ? timeoutMs : 5000;
        const startedAt = Date.now();

        while (Date.now() - startedAt < tmo) {
          for (const ws of (window.__bacopy_sockets || [])) {
            try {
              const wu = ws.__bacopy_url || ws.url || "";
              if (wu === u && ws.readyState === OrigWS.OPEN) {
                ws.send(payload);
                return { ok: true, url: wu, reused: false };
              }
            } catch (e) {}
          }
          await new Promise(r => setTimeout(r, 50));
        }
        return { ok: false, error: "open_timeout", url: u };
      } catch (e) {
        return { ok: false, error: "open_send_failed", detail: String(e) };
      }
    };

    function WrappedWebSocket(url, protocols) {
      const ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      try {
        ws.__bacopy_url = url;
        window.__bacopy_sockets.push(ws);
        attachSpy(ws);
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


_HTTP = requests.Session()


def _api_read_timeout_sec() -> float:
    try:
        return float(os.getenv("BACOPY_API_TIMEOUT_SEC", "30").strip() or "30")
    except Exception:
        return 30.0


def _api_connect_timeout_sec() -> float:
    try:
        return float(os.getenv("BACOPY_API_CONNECT_TIMEOUT_SEC", "5").strip() or "5")
    except Exception:
        return 5.0


def _api_retries() -> int:
    try:
        return max(1, int(os.getenv("BACOPY_API_RETRIES", "3").strip() or "3"))
    except Exception:
        return 3


def _http_request(method: str, url: str, *, timeout: tuple[float, float], retries: int, **kwargs):
    last_e: Optional[Exception] = None
    for i in range(max(1, int(retries))):
        try:
            return _HTTP.request(method, url, timeout=timeout, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_e = e
            if i >= retries - 1:
                raise
            backoff = min(8.0, 0.5 * (2**i))
            print(f"[WARN] http {method} timeout/connection error (retry {i+1}/{retries}, sleep={backoff}s): {e}", flush=True)
            time.sleep(backoff)
    raise last_e or RuntimeError("http_request failed")


def _redact_jsession(url: str) -> str:
    return re.sub(r"(JSESSIONID=)[^&]+", r"\1<REDACTED>", str(url or ""))

_PROFILE_LOCK_FH: Optional[Any] = None


def _acquire_profile_lock(profile_dir: str, *, lock_name: str = ".bacopy_executor.lock") -> Path:
    global _PROFILE_LOCK_FH
    pdir = Path(profile_dir)
    pdir.mkdir(parents=True, exist_ok=True)
    lock_path = pdir / lock_name
    this_pid = os.getpid()

    try:
        fh = open(lock_path, "r+", encoding="utf-8")
    except FileNotFoundError:
        fh = open(lock_path, "w+", encoding="utf-8")
    except Exception as e:
        raise SystemExit(f"failed to open profile lock: {lock_path} ({e})")

    # OS-level lock (auto-released on crash). This prevents the 2nd instance from even reaching Camoufox,
    # avoiding Stake session conflicts caused by a brief double-start.
    try:
        fh.seek(0)
        if os.name == "nt":
            import msvcrt  # type: ignore

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        raise SystemExit(f"another executor is already using this profile_dir: {pdir}")

    try:
        fh.seek(0)
        fh.write(json.dumps({"pid": this_pid, "created_at": _utc_now_iso()}))
        fh.truncate()
        fh.flush()
    except Exception:
        pass

    _PROFILE_LOCK_FH = fh

    def _release() -> None:
        global _PROFILE_LOCK_FH
        if _PROFILE_LOCK_FH is None:
            return
        try:
            try:
                if os.name == "nt":
                    import msvcrt  # type: ignore

                    _PROFILE_LOCK_FH.seek(0)
                    msvcrt.locking(_PROFILE_LOCK_FH.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl  # type: ignore

                    fcntl.flock(_PROFILE_LOCK_FH.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                _PROFILE_LOCK_FH.close()
            except Exception:
                pass
        finally:
            _PROFILE_LOCK_FH = None
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass

    atexit.register(_release)
    return lock_path


def _post_ack(decision_id: str, ack: dict[str, Any], status: str = "processing") -> None:
    try:
        _http_request(
            "POST",
            f"{_api_url()}/api/decisions/{decision_id}/ack",
            headers=_headers(),
            json={"ack": ack, "status": status},
            timeout=(_api_connect_timeout_sec(), _api_read_timeout_sec()),
            retries=min(_api_retries(), 2),
        ).raise_for_status()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] post_ack timeout: {e}", flush=True)
    except Exception as e:
        print(f"[WARN] post_ack error: {e}", flush=True)


def _post_result(decision_id: str, result: dict[str, Any], status: str = "done") -> None:
    try:
        _http_request(
            "POST",
            f"{_api_url()}/api/decisions/{decision_id}/result",
            headers=_headers(),
            json={"result": result, "status": status},
            timeout=(_api_connect_timeout_sec(), _api_read_timeout_sec()),
            retries=min(_api_retries(), 2),
        ).raise_for_status()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] post_result timeout: {e}", flush=True)
    except Exception as e:
        print(f"[WARN] post_result error: {e}", flush=True)


def _post_heartbeat(payload: dict[str, Any]) -> None:
    # best-effort
    try:
        _http_request(
            "POST",
            f"{_api_url()}/api/executors/heartbeat",
            headers=_headers(),
            json=payload,
            timeout=(_api_connect_timeout_sec(), 5.0),
            retries=1,
        )
    except Exception:
        return


def _fetch_decisions(status: str, limit: int) -> list[dict[str, Any]]:
    try:
        r = _http_request(
            "GET",
            f"{_api_url()}/api/decisions",
            params={"status": status, "limit": int(limit)},
            headers=_headers(),
            timeout=(_api_connect_timeout_sec(), _api_read_timeout_sec()),
            retries=_api_retries(),
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
            if "apps/lobby" in f.url or f.name == "shell-app" or "desktop/lobby" in f.url:
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
    last_bets_open_at: float = 0.0
    last_bets_closed_at: float = 0.0

    # ws urls
    game_ws_url: str = ""  # gsXX.../game?JSESSIONID=...&tableId=...&type=json

    # result cache from lobby feed
    winners_by_table_game_id: dict[str, dict[str, str]] = None  # tableId -> (gameId -> winner)
    _seen_table_game: set[tuple[str, str]] = None

    # bet confirms (from game ws)
    last_bet_confirm: dict[str, Any] | None = None
    expected_bet_ck: str = ""

    # Stake (GraphQL WS) balance cache (used as alternative bet confirmation signal)
    stake_balance_by_currency: dict[str, float] = None
    last_stake_balance_at: float = 0.0

    def __post_init__(self) -> None:
        if self.winners_by_table_game_id is None:
            self.winners_by_table_game_id = {}
        if self._seen_table_game is None:
            self._seen_table_game = set()
        if self.stake_balance_by_currency is None:
            self.stake_balance_by_currency = {}


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
        payload = payload.lstrip()
        if not payload.startswith("{"):
            return None
        obj = json.loads(payload)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _extract_ck_from_lpbet_xml(xml_payload: str) -> str:
    try:
        m = re.search(r'\bck="(\d{8,})"', str(xml_payload or ""))
        return m.group(1) if m else ""
    except Exception:
        return ""


def _update_from_game_xml(state: _PragmaticState, xml_payload: str) -> None:
    p = str(xml_payload or "")
    if not p:
        return
    ck = state.expected_bet_ck
    if ck and ck in p and ("lpbet" in p or "<bet" in p):
        state.last_bet_confirm = {"type": "xml", "ck": ck, "snippet": p[:500]}
        return
    # If server responds with an error for our bet, also treat as "confirm" so we can surface it.
    if ck and ck in p and "<error" in p:
        state.last_bet_confirm = {"type": "xml_error", "ck": ck, "snippet": p[:800]}
        return


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
            state.last_bets_open_at = time.time()
        return
    if "betsclosed" in msg and isinstance(msg["betsclosed"], dict):
        g = str(msg["betsclosed"].get("game") or "")
        if g:
            state.bets_closed_game_id = g
            state.last_bets_closed_at = time.time()
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


def _update_from_stake_ws_msg(state: _PragmaticState, msg: dict[str, Any]) -> None:
    # GraphQL WS protocol: {"type":"next","payload":{"data":{...}}} etc
    payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not data:
        return
    ab = data.get("availableBalances")
    if isinstance(ab, list):
        for it in ab:
            if not isinstance(it, dict):
                continue
            bal = it.get("balance") if isinstance(it.get("balance"), dict) else {}
            cur = str(bal.get("currency") or "")
            amt = bal.get("amount")
            if not cur:
                continue
            try:
                state.stake_balance_by_currency[cur] = float(amt)
                state.last_stake_balance_at = time.time()
            except Exception:
                continue


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


def _drain_ws_events(page_or_frame, *, max_items: int = 300) -> list[dict[str, Any]]:
    try:
        evs = page_or_frame.evaluate("(n) => (window.__bacopy_ws_drain ? window.__bacopy_ws_drain(n) : [])", int(max_items))
        return evs if isinstance(evs, list) else []
    except Exception:
        return []


def _pump_ws_events(page, game_frame, state: _PragmaticState) -> None:
    frames = []
    if game_frame:
        frames.append(game_frame)
    frames.append(page)

    for fr in frames:
        for ev in _drain_ws_events(fr, max_items=400):
            if not isinstance(ev, dict):
                continue
            url = str(ev.get("url") or "")
            data = ev.get("data")
            obj = _maybe_json(data)
            if not obj and isinstance(data, str) and "<" in data:
                if "pragmaticplaylive.net/game" in url:
                    _update_from_game_xml(state, data)
                continue
            if not obj:
                continue
            if "chat.pragmaticplaylive.net" in url:
                if str(ev.get("dir") or "") == "send":
                    _update_from_chat_msg(state, obj)
            if "pragmaticplaylive.net/game" in url:
                _update_from_game_msg(state, obj)
            if "dga.pragmaticplaylive.net/ws" in url:
                _update_from_lobby_msg(state, obj)
            if "stake.com/_api/websockets" in url:
                _update_from_stake_ws_msg(state, obj)


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

    # SPA 描画待ち: [role="button"] が出現するまで最大30秒待機
    print("[Stage 3b] waiting for SPA render (role=button elements) ...", flush=True)
    for _w in range(30):
        try:
            if shell.locator('[role="button"]').count() > 0:
                print(f"[Stage 3b] SPA rendered ({shell.locator('[role=\"button\"]').count()} buttons) after {_w}s", flush=True)
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)

    clicked = False
    table_substr = (table_substr or "").strip()

    if table_substr:
        print(f"[Stage 4] wait (<= {auto_click_wait_sec}s) for '{table_substr}' then click ...", flush=True)
        deadline = time.time() + float(max(auto_click_wait_sec, 1))
        while time.time() < deadline and not clicked:
            _dismiss_stake_loader(page)
            # テキスト一致（日本語/英語両対応）
            try:
                locator = shell.get_by_text(re.compile(re.escape(table_substr), re.I))
                if locator.count() > 0:
                    first = locator.first
                    first.scroll_into_view_if_needed(timeout=3000)
                    try:
                        shell.locator(f"[role='button']:has-text('{table_substr}')").first.click(timeout=3000, force=True)
                    except Exception:
                        try:
                            shell.locator(f"button:has-text('{table_substr}')").first.click(timeout=3000, force=True)
                        except Exception:
                            first.click(timeout=3000, force=True)
                    clicked = True
                    print(f"[Stage 4] clicked '{table_substr}' via text match", flush=True)
                    break
            except Exception:
                pass

            # フォールバック: テキスト付きセレクタ
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

            # 最終フォールバック: テーブルカード [role="button"] (60秒経過後のみ)
            if not clicked and (time.time() - deadline + float(auto_click_wait_sec)) > 60:
                try:
                    btns = shell.locator('[role="button"]')
                    cnt = btns.count()
                    if cnt >= 10:
                        # Skip first (Multi-Baccarat), click 2nd or later
                        btns.nth(1).click(timeout=3000, force=True)
                        clicked = True
                        print(f"[Stage 4] clicked via [role=button] nth(1) (total={cnt})", flush=True)
                except Exception:
                    pass

            if not clicked:
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

    lock_path = _acquire_profile_lock(args.profile_dir)
    print(f"[executor-live] profile_lock={lock_path}", flush=True)

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
        if state.game_ws_url and game_frame:
            try:
                game_frame.evaluate(_WS_BRIDGE_INIT)
            except Exception:
                pass
            try:
                game_frame.evaluate("(u) => (window.__bacopy_ws_open ? window.__bacopy_ws_open(u) : null)", state.game_ws_url)
            except Exception:
                pass

        def send_bet_xml(xml_payload: str, match: str) -> dict[str, Any]:
            target_frames = []
            if game_frame:
                target_frames.append(game_frame)
            # fallback: try all frames in case pragmatic moved
            target_frames.extend([f for f in page.frames if f not in target_frames])

            last_err = None
            for fr in target_frames:
                try:
                    # Re-inject WS bridge before every send (iframe may have reloaded)
                    try:
                        fr.evaluate(_WS_BRIDGE_INIT)
                    except Exception:
                        pass
                    # Keep state fresh even if Playwright websocket events miss cross-origin frames.
                    _pump_ws_events(page, game_frame, state)
                    res = fr.evaluate(
                        "(args) => window.__bacopy_ws_send(args.match, args.payload)",
                        {"match": match, "payload": xml_payload},
                    )
                    if isinstance(res, dict) and res.get("ok"):
                        return res
                    # If the bridge couldn't capture Pragmatic's internal WS (e.g. moved to Worker),
                    # open our own WS to the game url and send through it.
                    if state.game_ws_url:
                        res2 = fr.evaluate(
                            "(args) => window.__bacopy_ws_open_send(args.url, args.payload, args.timeoutMs)",
                            {"url": state.game_ws_url, "payload": xml_payload, "timeoutMs": 5000},
                        )
                        if isinstance(res2, dict) and res2.get("ok"):
                            return {**res2, "fallback": "open_send"}
                    last_err = res
                except Exception as e:
                    last_err = {"ok": False, "error": f"evaluate_failed: {e}"}
            return last_err or {"ok": False, "error": "evaluate_failed"}

        def wait_bets_open(timeout_sec: float) -> Optional[str]:
            start = time.time()
            prev_id = state.bets_open_game_id
            prev_at = state.last_bets_open_at

            def _pred() -> bool:
                _pump_ws_events(page, game_frame, state)
                if not state.bets_open_game_id:
                    return False
                if state.bets_closed_game_id == state.bets_open_game_id:
                    return False
                if state.bets_open_game_id != prev_id:
                    return True
                if state.last_bets_open_at > max(prev_at, start - 0.5):
                    return True
                # If betsopen arrived slightly earlier but is likely still open, accept it.
                return (time.time() - state.last_bets_open_at) < 20.0

            ok = _wait_for(_pred, timeout_sec=timeout_sec, tick_ms=200, page=page)
            return state.bets_open_game_id if ok else None

        def wait_bet_confirm(
            timeout_sec: float,
            *,
            currency: str,
            before_balance: Optional[float],
            bet_amount: float,
        ) -> Optional[dict[str, Any]]:
            state.last_bet_confirm = None
            start = time.time()

            def _pred() -> bool:
                _pump_ws_events(page, game_frame, state)
                if state.last_bet_confirm is not None:
                    return True
                if before_balance is None:
                    return False
                if state.last_stake_balance_at < start - 0.5:
                    return False
                after = state.stake_balance_by_currency.get(currency)
                if after is None:
                    return False
                # If available balance decreased by at least the bet amount, treat it as accepted.
                if (before_balance - after) >= max(0.0, float(bet_amount) * 0.9):
                    state.last_bet_confirm = {
                        "type": "stake_balance",
                        "currency": currency,
                        "before": before_balance,
                        "after": after,
                    }
                    return True
                return False

            _wait_for(_pred, timeout_sec=timeout_sec, tick_ms=200, page=page)
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

            ok = _wait_for(
                lambda: (_pump_ws_events(page, game_frame, state) or True) and _winner() is not None,
                timeout_sec=timeout_sec,
                tick_ms=250,
                page=page,
            )
            return _winner() if ok else None

        while True:
            heartbeat("running")
            try:
                _pump_ws_events(page, game_frame, state)
            except Exception:
                pass

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
                # Do not reset existing state here; betsopen/timer may have already arrived.
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
                bet_currency = (os.getenv("BACOPY_BET_CURRENCY", "USD") or "USD").strip().upper()
                before_balance = state.stake_balance_by_currency.get(bet_currency)
                xml = _build_lpbet_xml(table_id=state.table_id, game_id=game_id, user_id=state.user_id, bc=bc, amount=amt)
                state.expected_bet_ck = _extract_ck_from_lpbet_xml(xml)
                state.last_bet_confirm = None
                match = f"tableId={state.table_id}"
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

                confirm = wait_bet_confirm(
                    timeout_sec=10.0,
                    currency=bet_currency,
                    before_balance=before_balance,
                    bet_amount=amt,
                )
                if confirm is None:
                    # At this point we might have placed a bet but lost confirmation.
                    # Stop to avoid accidental duplicate bets.
                    _post_result(
                        did,
                        {"error": "bet_confirm_timeout", "game_id": game_id, "operator_table_id": op_tid, "bet_ck": state.expected_bet_ck},
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
