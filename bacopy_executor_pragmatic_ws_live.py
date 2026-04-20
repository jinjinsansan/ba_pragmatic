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
import ipaddress
import json
import os
import queue
import re
import sqlite3
import ssl
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

# NOTE: ML 契約 JSONL への書き込みは Master 側 (POST /api/decisions, /ack, /result)
# が担当する。executor から直接 append するとホスト分散/event_type 不整合の原因になる。
# 旧コードで append_decision_event をここで呼んでいたが削除済 (Master 側で完結).
from bacopy_db import init_db, try_lock_bet, try_mark_decision_executed
from marubatsu_strategy import MaruBatsuTracker, SEQ_COUNTER, SetData

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"

# ======== GUI IPC (stdout JSON) ========

# デバッグ目的: executor のすべての IPC メッセージをファイルに残す.
# 実装に問題があっても後から grep で追えるため, 自動復旧や診断が楽になる.
_DEBUG_LOG_PATH = Path(__file__).parent / "executor_debug.log"

def _append_debug_log(line: str) -> None:
    try:
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line.rstrip() + "\n")
    except Exception:
        pass


def send_msg(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    _append_debug_log(line)
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            try:
                buf.write(line.encode("utf-8", errors="replace"))
                buf.flush()
                return
            except Exception:
                pass
        try:
            ascii_line = json.dumps(msg, ensure_ascii=True) + "\n"
            sys.stdout.write(ascii_line)
            sys.stdout.flush()
        except Exception:
            pass
    except Exception:
        pass


def send_log(text: str) -> None:
    send_msg({"type": "log", "message": text})


def send_action(text: str) -> None:
    send_msg({"type": "action", "message": text})


_LAST_PHASE = [""]


def send_phase(name: str, detail: str = "") -> None:
    key = f"{name}|{detail}"
    if _LAST_PHASE[0] == key:
        return
    _LAST_PHASE[0] = key
    send_msg({"type": "phase", "name": name, "detail": detail, "ts": time.time()})


def _jst_date_str(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    # NOTE: Don't rely on the OS timezone in packaged builds; force JST (+09:00).
    jst = timezone(timedelta(hours=9))
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(jst)
    return dt.date().isoformat()


class Seq7Session:
    def __init__(
        self,
        *,
        chip_base: float,
        profit_stop_chips: int,
        loss_cut_chips: int,
        state_path: Path,
        profit_session_limit: int = 0,
    ) -> None:
        self.chip_base = float(chip_base)
        self.profit_stop = int(profit_stop_chips)
        self.loss_cut = int(loss_cut_chips)
        self.profit_session_limit = int(profit_session_limit or 0)

        self.tracker = MaruBatsuTracker(chip_base=self.chip_base, seq=SEQ_COUNTER, set_size=7)
        self.session_count = 0
        self.profit_sessions = 0

        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_ties = 0

        self.session_open_balance: float | None = None
        self.daily_open_balance: float | None = None
        self.daily_open_date: str | None = None  # JST date
        self.current_balance: float | None = None
        self.state_path = state_path

        self._load_state()

        # GUI settings must win over saved state
        self.tracker.chip_base = self.chip_base
        self.profit_stop = max(1, self.profit_stop)
        self.loss_cut = max(1, self.loss_cut)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            return

        try:
            self.session_count = int(data.get("session_count", 0) or 0)
            self.profit_sessions = int(data.get("profit_sessions", 0) or 0)
            self.total_bets = int(data.get("total_bets", 0) or 0)
            self.total_wins = int(data.get("total_wins", 0) or 0)
            self.total_losses = int(data.get("total_losses", 0) or 0)
            self.total_ties = int(data.get("total_ties", 0) or 0)
            self.session_open_balance = data.get("session_open_balance")
            self.daily_open_balance = data.get("daily_open_balance")
            self.daily_open_date = data.get("daily_open_date")
            self.current_balance = data.get("current_balance")

            sets = data.get("sets") or []
            self.tracker.sets.clear()
            for sd in sets:
                if not isinstance(sd, dict):
                    continue
                self.tracker.sets.append(SetData(**sd))

            turns = data.get("current_turns") or []
            self.tracker.current_turns = list(turns)
            self.tracker.total_o = int(data.get("total_o", 0) or 0)
            self.tracker.total_x = int(data.get("total_x", 0) or 0)
        except Exception:
            pass

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "chip_base": self.chip_base,
                "profit_stop": self.profit_stop,
                "loss_cut": self.loss_cut,
                "profit_session_limit": self.profit_session_limit,
                "session_count": self.session_count,
                "profit_sessions": self.profit_sessions,
                "total_bets": self.total_bets,
                "total_wins": self.total_wins,
                "total_losses": self.total_losses,
                "total_ties": self.total_ties,
                "session_open_balance": self.session_open_balance,
                "daily_open_balance": self.daily_open_balance,
                "daily_open_date": self.daily_open_date,
                "current_balance": self.current_balance,
                "sets": [s.__dict__ for s in self.tracker.sets[-200:]],
                "current_turns": list(self.tracker.current_turns),
                "total_o": self.tracker.total_o,
                "total_x": self.tracker.total_x,
                "saved_at": time.time(),
            }
            self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    def update_balance(self, balance: float | None) -> None:
        if balance is None:
            return
        self.current_balance = float(balance)
        if self.session_open_balance is None:
            self.session_open_balance = float(balance)
        jst_date = _jst_date_str()
        if self.daily_open_date != jst_date:
            self.daily_open_date = jst_date
            self.daily_open_balance = float(balance)
        self._save_state()

    def bet_unit(self) -> int:
        idx = self.tracker.current_unit_idx
        unit = SEQ_COUNTER[min(idx, len(SEQ_COUNTER) - 1)]
        return int(unit)

    def bet_amount(self) -> float:
        return float(self.bet_unit()) * float(self.chip_base)

    def effective_profit_chips(self) -> int:
        cp = int(self.tracker.cumulative_profit)
        turns = self.tracker.current_turns
        if turns:
            wins = turns.count("O")
            losses = turns.count("X")
            unit = SEQ_COUNTER[min(self.tracker.current_unit_idx, len(SEQ_COUNTER) - 1)]
            cp += (wins - losses) * int(unit)
        return cp

    def should_reset(self) -> bool:
        cp = self.effective_profit_chips()
        if cp >= self.profit_stop:
            return True
        if cp <= -self.loss_cut:
            return True
        return False

    def reset_session(self, reason: str) -> dict:
        old_open = self.session_open_balance
        balance = self.current_balance
        self.session_count += 1
        money = self.effective_profit_chips() * float(self.chip_base)
        money_actual = (balance - old_open) if (balance is not None and old_open is not None) else money

        self.tracker.sets.clear()
        self.tracker.current_turns.clear()
        if balance is not None:
            self.session_open_balance = float(balance)
        is_profit = reason in ("profit", "target")
        if is_profit:
            self.profit_sessions += 1
        self._save_state()

        return {
            "type": "session_reset",
            "reason": reason,
            "session_count": self.session_count,
            "profit_sessions": self.profit_sessions,
            "is_profit": bool(is_profit),
            "amount": float(money),
            "amount_actual": float(money_actual),
            "balance": float(balance) if balance is not None else None,
        }

    def apply_round(self, outcome: str, won: bool | None, *, bet_side: str = "") -> dict:
        # outcome: player|banker|tie (winner)
        # bet_side: player|banker|tie (our bet)
        bs = str(bet_side or "").strip().lower()
        if outcome == "tie" and bs != "tie":
            # PLAYER/BANKER bet push
            self.total_bets += 1
            self.total_ties += 1
            self._save_state()
            return {"completed_set": None, "pre_turn_count": len(self.tracker.current_turns), "pre_wins": None, "pre_losses": None}

        if won is None:
            return {"completed_set": None, "pre_turn_count": len(self.tracker.current_turns), "pre_wins": None, "pre_losses": None}

        self.total_bets += 1
        if won:
            self.total_wins += 1
        else:
            self.total_losses += 1

        pre_turns = list(self.tracker.current_turns) + ["O" if won else "X"]
        pre_turn_count = len(pre_turns)
        pre_wins = sum(1 for t in pre_turns if t == "O")
        pre_losses = pre_turn_count - pre_wins

        completed_set = self.tracker.add_result("player" if won else "banker")
        self._save_state()
        return {
            "completed_set": completed_set,
            "pre_turn_count": pre_turn_count,
            "pre_wins": pre_wins,
            "pre_losses": pre_losses,
        }

    def status_payload(self) -> dict:
        bal = self.current_balance
        spnl = (bal - self.session_open_balance) if (bal is not None and self.session_open_balance is not None) else None
        dpnl = (bal - self.daily_open_balance) if (bal is not None and self.daily_open_balance is not None) else None
        return {
            "type": "status",
            "chip_base": self.chip_base,
            "session_count": self.session_count,
            "profit_sessions": self.profit_sessions,
            "wins": self.total_wins,
            "losses": self.total_losses,
            "ties": self.total_ties,
            "total_bets": self.total_bets,
            "balance": bal,
            "session_open_balance": self.session_open_balance,
            "daily_open_balance": self.daily_open_balance,
            "daily_open_date": self.daily_open_date,
            "session_pnl": spnl,
            "daily_pnl": dpnl,
            "overshoot": getattr(self.tracker, "prev_overshoot", 0),
            "current_turn": len(self.tracker.current_turns) + 1,
            "turns_display": "".join(self.tracker.current_turns),
            "bet_unit": self.bet_unit(),
            "bet_amount": self.bet_amount(),
            "profit_stop_chips": self.profit_stop,
            "loss_cut_chips": self.loss_cut,
            "should_reset": self.should_reset(),
        }

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
_IP_HTTP_LOCK = threading.Lock()
_IP_HTTP: dict[tuple[str, str], requests.Session] = {}  # (ip, sni_host) -> session


def _api_read_timeout_sec() -> float:
    try:
        v = float(os.getenv("BACOPY_API_TIMEOUT_SEC", "30").strip() or "30")
        return max(15.0, v)
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


class _SNIAdapter(HTTPAdapter):
    def __init__(self, server_hostname: str, **kwargs):
        self.server_hostname = server_hostname
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        ctx = ssl.create_default_context()
        pool_kwargs["ssl_context"] = ctx
        pool_kwargs["server_hostname"] = self.server_hostname
        pool_kwargs["assert_hostname"] = self.server_hostname
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs)


def _api_fallback_ips() -> list[str]:
    raw = (os.getenv("BACOPY_API_FALLBACK_IPS", "") or os.getenv("BACOPY_API_FALLBACK_IP", "") or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
    out: list[str] = []
    for p in parts:
        try:
            ipaddress.ip_address(p)
            out.append(p)
        except Exception:
            continue
    return out


def _is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except Exception:
        return False


def _get_ip_session(ip: str, *, sni_host: str) -> requests.Session:
    key = (ip, sni_host)
    with _IP_HTTP_LOCK:
        s = _IP_HTTP.get(key)
        if s is not None:
            return s
        s = requests.Session()
        s.mount(f"https://{ip}", _SNIAdapter(sni_host))
        _IP_HTTP[key] = s
        return s


def _rewrite_url_to_ip(url: str, *, ip: str) -> tuple[str, str]:
    """Return (ip_url, original_host). Keeps path/query; swaps netloc host->ip."""
    p = urlsplit(url)
    host = str(p.hostname or "")
    port = p.port
    scheme = p.scheme or "http"
    netloc = ip
    if port is not None:
        netloc = f"{ip}:{port}"
    return urlunsplit((scheme, netloc, p.path, p.query, p.fragment)), host


def _maybe_ip_fallback_request(method: str, url: str, *, timeout: tuple[float, float], **kwargs):
    p = urlsplit(url)
    scheme = (p.scheme or "").lower()
    host = str(p.hostname or "")
    if not host or _is_ip_host(host):
        return None
    ips = _api_fallback_ips()
    if not ips:
        return None

    # Preserve original host for nginx routing + TLS SNI.
    hdrs = dict((kwargs.get("headers") or {}).items())
    if not any(k.lower() == "host" for k in hdrs.keys()):
        hdrs["Host"] = host
    kwargs["headers"] = hdrs

    last_e: Optional[Exception] = None
    for ip in ips:
        try:
            ip_url, sni_host = _rewrite_url_to_ip(url, ip=ip)
            if scheme == "https":
                sess = _get_ip_session(ip, sni_host=sni_host)
                return sess.request(method, ip_url, timeout=timeout, **kwargs)
            # http fallback
            return _HTTP.request(method, ip_url, timeout=timeout, **kwargs)
        except Exception as e:
            last_e = e
            continue
    if last_e is not None:
        raise last_e
    return None


def _http_request(method: str, url: str, *, timeout: tuple[float, float], retries: int, **kwargs):
    last_e: Optional[Exception] = None
    for i in range(max(1, int(retries))):
        try:
            prefer_ip_env = (os.getenv("BACOPY_API_PREFER_IP", "") or "").strip().lower()
            prefer_ip = prefer_ip_env in ("1", "true", "yes", "on") or (prefer_ip_env == "" and bool(_api_fallback_ips()))
            if prefer_ip:
                try:
                    r = _maybe_ip_fallback_request(method, url, timeout=timeout, **kwargs)
                    if r is not None:
                        return r
                except Exception:
                    # Fallback IP may be stale; keep going with the normal hostname path.
                    pass
            return _HTTP.request(method, url, timeout=timeout, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_e = e
            # DNS flaps: retry via fallback IP (hosts file equivalent) if configured.
            try:
                r = _maybe_ip_fallback_request(method, url, timeout=timeout, **kwargs)
                if r is not None:
                    return r
            except Exception as e2:
                last_e = e2
            if i >= retries - 1:
                raise
            jitter = 0.15 * (1.0 + (time.time() % 1.0))
            backoff = min(8.0, 0.5 * (2**i) + jitter)
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


def _post_heartbeat(payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        r = _http_request(
            "POST",
            f"{_api_url()}/api/executors/heartbeat",
            headers=_headers(),
            json=payload,
            timeout=(_api_connect_timeout_sec(), _api_read_timeout_sec()),
            retries=1,
        )
        r.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


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


def _wait_decisions(
    *,
    status: str,
    limit: int,
    provider: str,
    executor_id: str,
    wait_sec: float,
) -> list[dict[str, Any]]:
    """Long-poll pending decisions to reduce polling + DNS lookups."""
    try:
        r = _http_request(
            "GET",
            f"{_api_url()}/api/decisions/wait",
            params={
                "status": status,
                "limit": int(limit),
                "provider": provider,
                "executor_id": executor_id,
                "wait_sec": float(wait_sec),
            },
            headers=_headers(),
            timeout=(_api_connect_timeout_sec(), max(_api_read_timeout_sec(), float(wait_sec) + 5.0)),
            retries=_api_retries(),
        )
        r.raise_for_status()
        items = r.json().get("decisions") or []
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        print(f"[WARN] wait_decisions timeout/connection error: {e}", flush=True)
        return []
    except Exception as e:
        print(f"[WARN] wait_decisions error: {e}", flush=True)
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


def _is_lobby_frame_url(url: str) -> bool:
    u = str(url or "")
    return (
        "pragmaticplaylive" in u
        and ("desktop/lobby" in u or "lobby2" in u or "apps/lobby" in u)
    )


def find_lobby_frames(page) -> list[Any]:
    frames: list[Any] = []
    try:
        for f in page.frames:
            try:
                if _is_lobby_frame_url(f.url) or f.name == "shell-app":
                    if f not in frames:
                        frames.append(f)
            except Exception:
                continue
    except Exception:
        pass
    if not frames:
        frames.append(page)
    return frames


def _refresh_game_frame(page, game_frame):
    try:
        if game_frame and not game_frame.is_detached():
            u = game_frame.url or ""
            if "pragmaticplaylive" in u or "qpidreoxcc.net" in u:
                return game_frame
    except Exception:
        pass
    gf = find_game_frame(page, attempts=1)
    if gf and gf != game_frame:
        try:
            gf.evaluate(_WS_BRIDGE_INIT)
        except Exception:
            pass
        try:
            send_log(f"[session] game frame updated: {gf.url[:120]}")
        except Exception:
            pass
    return gf or game_frame


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
    dga_ws_url: str = ""  # wss://dga.pragmaticplaylive.net/ws (lobby feed)
    dga_subscribed_keys: set[str] = None
    dga_last_subscribe_at: float = 0.0

    # ws activity timestamps (recv) — used to detect SESSION EXPIRED / dead connections
    last_ws_recv_at: float = 0.0
    last_lobby_ws_recv_at: float = 0.0
    last_game_ws_recv_at: float = 0.0
    last_stake_ws_recv_at: float = 0.0

    # result cache from lobby feed
    winners_by_table_game_id: dict[str, dict[str, str]] = None  # tableId -> (gameId -> winner)
    _seen_table_game: set[tuple[str, str]] = None

    # bet confirms (from game ws)
    last_bet_confirm: dict[str, Any] | None = None
    expected_bet_ck: str = ""

    # Stake (GraphQL WS) balance cache (used as alternative bet confirmation signal)
    stake_balance_by_currency: dict[str, float] = None
    stake_balance_delta_by_currency: dict[str, float] = None
    last_stake_balance_at: float = 0.0

    # Stake session takeover safety
    session_elsewhere_observed: bool = False
    session_elsewhere_unresolved: bool = False
    session_elsewhere_last_at: float = 0.0
    session_elsewhere_resolved_at: float = 0.0
    session_elsewhere_unresolved_since: float = 0.0
    session_elsewhere_reload_at: float = 0.0
    session_elsewhere_dumped_at: float = 0.0
    session_elsewhere_relogin_at: float = 0.0
    session_elsewhere_relogin_count: int = 0
    recover_exhausted: bool = False
    recover_attempts: int = 0
    recover_exhausted_at: float = 0.0

    # 自動復旧: Stake の「無操作による一時停止」モーダルの検知/dismiss 回数.
    inactivity_modal_observed: bool = False
    inactivity_modal_unresolved: bool = False
    inactivity_modal_last_at: float = 0.0
    inactivity_dismissed_count: int = 0
    inactivity_dumped_at: float = 0.0
    inactivity_reload_at: float = 0.0
    # Keep-alive: 最終ジェスチャー (mousemove) を送った時刻.
    keep_alive_last_at: float = 0.0
    # 自動復旧状態 (Master 画面にリアルタイム通知するフラグ).
    recovering: bool = False
    recovering_reason: str = ""
    recovering_started_at: float = 0.0

    def __post_init__(self) -> None:
        if self.winners_by_table_game_id is None:
            self.winners_by_table_game_id = {}
        if self._seen_table_game is None:
            self._seen_table_game = set()
        if self.stake_balance_by_currency is None:
            self.stake_balance_by_currency = {}
        if self.stake_balance_delta_by_currency is None:
            self.stake_balance_delta_by_currency = {}
        if self.dga_subscribed_keys is None:
            self.dga_subscribed_keys = set()
        if not self.last_ws_recv_at:
            self.last_ws_recv_at = time.time()


def _side_to_bc(side: str, *, assume_012: bool = False) -> Optional[str]:
    s = str(side or "").upper().strip()
    if s in ("P", "PLAYER"):
        return (os.getenv("BACOPY_PRAGMATIC_BC_PLAYER", "") or ("0" if assume_012 else "0")).strip() or "0"
    if s in ("B", "BANKER"):
        v = (os.getenv("BACOPY_PRAGMATIC_BC_BANKER", "") or "").strip()
        if v:
            return v
        return "1" if assume_012 else None
    if s in ("T", "TIE"):
        v = (os.getenv("BACOPY_PRAGMATIC_BC_TIE", "") or "").strip()
        if v:
            return v
        return "2" if assume_012 else None
    return None


def _normalize_bet_side(side: str) -> str:
    s = str(side or "").upper().strip()
    if s in ("", "P", "PLAYER"):
        return "PLAYER"
    if s in ("B", "BANKER"):
        return "BANKER"
    if s in ("T", "TIE"):
        return "TIE"
    return s


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
    # Only treat explicit server-side errors as confirmations.
    # (Do NOT treat an echo of our own <lpbet ...> send frame as confirmation.)
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

    # Best-effort: if we haven't resolved operator_table_id yet, map it from dga metadata by table name.
    try:
        tname = str(msg.get("tableName") or "")
        if not state.operator_table_id and tname and state.table_name:
            def _norm(s: str) -> str:
                return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())
            if _norm(tname) == _norm(state.table_name):
                state.operator_table_id = table_id
                # If internal qpid table_id is missing, recover it from the poster url.
                if not state.table_id:
                    img = str(msg.get("tableImage") or "")
                    m = re.search(r"/snaps/([^/]+)/", img)
                    if m:
                        state.table_id = str(m.group(1) or "")
    except Exception:
        pass

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

    now = time.time()
    updated = False

    for field in ("availableBalances", "vaultBalances"):
        ab = data.get(field)
        items: list[dict[str, Any]] = []
        if isinstance(ab, dict):
            items = [ab]
        elif isinstance(ab, list):
            items = [x for x in ab if isinstance(x, dict)]
        else:
            items = []

        for it in items:
            bal = it.get("balance") if isinstance(it.get("balance"), dict) else {}
            cur = str(bal.get("currency") or "").strip().upper()
            if not cur:
                continue
            try:
                amt = bal.get("amount")
                if amt is not None:
                    state.stake_balance_by_currency[cur] = float(amt)
                delta = it.get("amount")
                if delta is not None:
                    state.stake_balance_delta_by_currency[cur] = float(delta)
                updated = True
            except Exception:
                continue

    if updated:
        state.last_stake_balance_at = now
        # If we observed Stake's "session elsewhere" modal, treat fresh balance updates as a
        # (weak but useful) signal that the session is live again.
        if state.session_elsewhere_unresolved and state.session_elsewhere_last_at and now >= state.session_elsewhere_last_at:
            state.session_elsewhere_unresolved = False
            state.session_elsewhere_resolved_at = now
            try:
                send_log("[session] session elsewhere resolved (balance updates resumed)")
            except Exception:
                pass


def _wait_for(predicate, *, timeout_sec: float, tick_ms: int, page=None, on_tick=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if predicate():
            return True
        if on_tick is not None:
            try:
                on_tick()
            except Exception:
                pass
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


def _maybe_update_from_game_ws_url(state: _PragmaticState, url: str) -> None:
    if not url or "pragmaticplaylive.net/game" not in url:
        return
    if not state.game_ws_url:
        state.game_ws_url = url
    try:
        u = urlparse(url)
        qs = parse_qs(u.query or "")
        if not state.jsession_id:
            js = (qs.get("JSESSIONID") or qs.get("jsessionid") or [""])[0]
            if js:
                state.jsession_id = str(js)
        if not state.table_id:
            tid = (qs.get("tableId") or qs.get("tableid") or [""])[0]
            if tid:
                state.table_id = str(tid)
        if not state.user_id:
            uid = (qs.get("userId") or qs.get("userid") or [""])[0]
            if uid:
                state.user_id = str(uid)
    except Exception:
        return


def _discover_session_from_sockets(page_or_frame, state: _PragmaticState) -> None:
    """Recover session identifiers from already-open WebSocket URLs (no events needed)."""
    try:
        urls = page_or_frame.evaluate(
            "() => (window.__bacopy_sockets || []).map(ws => ws.__bacopy_url || ws.url || '').filter(Boolean)"
        )
        if not isinstance(urls, list):
            return
        for u in urls:
            if isinstance(u, str) and "pragmaticplaylive.net/game" in u:
                _maybe_update_from_game_ws_url(state, u)
            if isinstance(u, str) and "dga.pragmaticplaylive.net" in u and "/ws" in u:
                if not state.dga_ws_url:
                    state.dga_ws_url = u
    except Exception:
        return


def _ensure_dga_subscription(page, state: _PragmaticState, *, operator_table_id: str, currency: str) -> None:
    """Ensure we are subscribed to Pragmatic lobby feed for the target operator_table_id.

    This is critical for result detection (gameResult winner lookup).
    """
    op_tid = str(operator_table_id or "").strip()
    if not op_tid:
        return
    now = time.time()
    if op_tid in (state.dga_subscribed_keys or set()) and (now - float(state.dga_last_subscribe_at or 0)) < 300:
        return

    ws_url = state.dga_ws_url or "wss://dga.pragmaticplaylive.net/ws"
    casino_id = (os.getenv("BACOPY_PRAGMATIC_CASINO_ID", "ppcds00000003709") or "ppcds00000003709").strip()
    cur = (currency or "USD").strip().upper() or "USD"
    payloads = [
        {"type": "statistics"},
        {"type": "available", "casinoId": casino_id},
        {"type": "subscribe", "isDeltaEnabled": True, "casinoId": casino_id, "key": [op_tid], "currency": cur},
    ]
    try:
        # Open once then send all payloads on the same socket (fast, avoids repeated open timeouts).
        page.evaluate(
            """async (args) => {
  const url = String(args.url || "");
  const payloads = Array.isArray(args.payloads) ? args.payloads : [];
  const timeoutMs = (typeof args.timeoutMs === "number" && args.timeoutMs > 0) ? args.timeoutMs : 5000;
  if (!window.__bacopy_ws_open) return { ok: false, error: "bridge_missing" };

  // Ensure we have a socket instance.
  const res = window.__bacopy_ws_open(url);
  if (!res || !res.ok) return res || { ok: false, error: "open_failed" };

  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    for (const ws of (window.__bacopy_sockets || [])) {
      try {
        const wu = ws.__bacopy_url || ws.url || "";
        if (wu === url && ws.readyState === WebSocket.OPEN) {
          for (const p of payloads) {
            try { ws.send(p); } catch (e) {}
          }
          return { ok: true, url: wu, sent: payloads.length };
        }
      } catch (e) {}
    }
    await new Promise(r => setTimeout(r, 50));
  }
  return { ok: false, error: "open_timeout", url };
}""",
            {"url": ws_url, "payloads": [json.dumps(p, separators=(",", ":")) for p in payloads], "timeoutMs": 5000},
        )
    except Exception:
        pass

    try:
        state.dga_subscribed_keys.add(op_tid)
        state.dga_last_subscribe_at = now
    except Exception:
        pass


def _pump_ws_events(page, game_frame, state: _PragmaticState) -> None:
    frames: list[Any] = []
    if game_frame:
        frames.append(game_frame)
    try:
        for f in page.frames:
            if f not in frames:
                frames.append(f)
    except Exception:
        pass
    if page not in frames:
        frames.append(page)

    for fr in frames:
        _discover_session_from_sockets(fr, state)
        for ev in _drain_ws_events(fr, max_items=400):
            if not isinstance(ev, dict):
                continue
            ev_dir = str(ev.get("dir") or "").strip().lower()
            is_recv = (ev_dir != "send")
            url = str(ev.get("url") or "")
            if is_recv:
                now = time.time()
                state.last_ws_recv_at = now
                if "dga.pragmaticplaylive.net/ws" in url:
                    state.last_lobby_ws_recv_at = now
                elif "pragmaticplaylive.net/game" in url:
                    state.last_game_ws_recv_at = now
                elif "stake.com/_api/websockets" in url:
                    state.last_stake_ws_recv_at = now
            if "pragmaticplaylive.net/game" in url:
                _maybe_update_from_game_ws_url(state, url)
            data = ev.get("data")
            obj = _maybe_json(data)
            if not obj and isinstance(data, str) and "<" in data:
                if is_recv and "pragmaticplaylive.net/game" in url:
                    _update_from_game_xml(state, data)
                continue
            if not obj:
                continue
            if "chat.pragmaticplaylive.net" in url:
                _update_from_chat_msg(state, obj)
            if is_recv and "pragmaticplaylive.net/game" in url:
                _update_from_game_msg(state, obj)
            if is_recv and "dga.pragmaticplaylive.net/ws" in url:
                _update_from_lobby_msg(state, obj)
            if is_recv and "stake.com/_api/websockets" in url:
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


_SESSION_ELSEWHERE_DISMISS_JS = r"""
(opts) => {
  const doClick = !(opts && opts.click === false);
  const TEXT = [/他の場所でセッション/, /セッションが開始/, /session.*elsewhere/i, /session.*another/i, /logged.*in.*elsewhere/i];
  // 厳密マッチ: 短い語は ^...$ 固定、"Continue" 単体は禁止 (社会ログインと衝突)
  // 実際の Stake Pragmatic ボタン: 「ここに残る」(残す) / 「ロビーに移動」(去る).
  // 残す方だけ包括, 去る方は EXCLUDE で禁止.
  const BTN = [
    /ここに残る/, /このまま残る/, /このまま続ける/, /ここで続ける/,
    /このデバイスで.*続/, /この端末.*続/,
    /^続行$/, /^続ける$/, /残る$/,
    /^Stay\b/i, /^Stay here\b/i, /^Stay on this device/i,
    /^Keep (using|playing) here/i,
    /^Continue here$/i, /^Use this device/i,
    /^OK$/i, /^はい$/
  ];
  // 絶対クリック禁止ボタン: SNS ログイン + 「ロビーに移動」(セッション自ら切断)
  const EXCLUDE = /(with\s+)?(facebook|google|passkey|apple|twitter|metamask|telegram|line|yahoo)|another\s+way|forgot|register|sign\s*up|\u65b0\u898f|\u767b\u9332|\u30d1\u30b9\u30ef\u30fc\u30c9|\u30ed\u30d3\u30fc\u306b\u79fb\u52d5|go\s+to\s+lobby|leave/i;

  function* walk(root){
    if (!root) return;
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = it.nextNode())) {
      yield n;
      if (n.shadowRoot) {
        for (const x of walk(n.shadowRoot)) yield x;
      }
    }
  }
  function vis(el){
    try{
      const c = getComputedStyle(el);
      if (c.display === 'none' || c.visibility === 'hidden' || c.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      if ((r.width <= 1 || r.height <= 1) && (!el.getClientRects || el.getClientRects().length === 0)) return false;
      return true;
    }catch(_){ return false; }
  }
  function textOf(el){
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function ownText(el){
    let s = '';
    for (const ch of el.childNodes) if (ch.nodeType === 3) s += ch.textContent || '';
    return s.replace(/\s+/g, ' ').trim();
  }
  function findModalContainer(el){
    // 検知テキスト要素の最近傍 dialog / modal 祖先を探す
    let cur = el;
    for (let i=0; i<20 && cur; i++) {
      try{
        if (cur.nodeType === 1) {
          const role = cur.getAttribute && cur.getAttribute('role');
          const am = cur.getAttribute && cur.getAttribute('aria-modal');
          const tag = (cur.tagName || '').toUpperCase();
          const cls = (cur.className && typeof cur.className === 'string') ? cur.className : '';
          if (tag === 'DIALOG' || role === 'dialog' || role === 'alertdialog' || am === 'true' ||
              /modal|dialog|popup|overlay/i.test(cls)) {
            return cur;
          }
        }
      }catch(_){}
      cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host);
    }
    return null;
  }
  function dispatchClick(el){
    try{ el.focus(); }catch(_){}
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy, view: window };
    try{
      if (window.PointerEvent) {
        el.dispatchEvent(new PointerEvent('pointerdown', base));
        el.dispatchEvent(new PointerEvent('pointerup', base));
      }
      el.dispatchEvent(new MouseEvent('mousedown', base));
      el.dispatchEvent(new MouseEvent('mouseup', base));
      el.dispatchEvent(new MouseEvent('click', base));
    }catch(_){}
    try{ el.click(); }catch(_){}
  }

  // 1) テキスト検知 — 自身のテキストノードのみ (ownText) で判定 (誤検知削減)
  let foundEl = null;
  let foundText = '';
  for (const el of walk(document)) {
    if (!vis(el)) continue;
    const t = ownText(el);
    if (!t) continue;
    for (const p of TEXT) {
      if (p.test(t)) { foundEl = el; foundText = t.slice(0, 100); break; }
    }
    if (foundEl) break;
  }
  if (!foundEl) return { found: false };

  // 2) モーダル container をスコープとして確定 (無ければ直親から 4 階層)
  let scope = findModalContainer(foundEl);
  if (!scope) {
    scope = foundEl;
    for (let i=0; i<4 && scope.parentElement; i++) scope = scope.parentElement;
  }

  // 3) scope 内だけでボタン探索 + EXCLUDE 除外 + 最大 1 クリック
  const buttons = [];
  let clicked = 0;
  for (const el of walk(scope)) {
    if (!vis(el)) continue;
    const tag = (el.tagName || '').toUpperCase();
    const role = el.getAttribute ? el.getAttribute('role') : '';
    if (tag !== 'BUTTON' && tag !== 'A' && role !== 'button') continue;
    const t = textOf(el);
    if (!t) continue;
    if (EXCLUDE.test(t)) { buttons.push({ tag, text: t.slice(0,80), skipped: 'exclude' }); continue; }
    for (const p of BTN) {
      if (p.test(t)) {
        buttons.push({ tag, text: t.slice(0, 80) });
        if (doClick && clicked === 0) { dispatchClick(el); clicked += 1; }
        break;
      }
    }
    if (buttons.length >= 8) break;
  }
  return { found: true, text: foundText, scope: scope.tagName, clicked, buttons };
}
"""

_SESSION_ELSEWHERE_DUMP_JS = r"""
() => {
  const TEXT = [/他の場所でセッション/, /セッションが開始/, /session.*elsewhere/i, /session.*another/i, /logged.*in.*elsewhere/i];
  const BTN  = [/ここで続ける/, /このデバイスで/, /この端末/, /続行/, /続ける/, /Continue\s+here/i, /Keep\s+(using|playing)\s+here/i, /Continue/i, /^OK$/i];
  function* walk(root){
    if (!root) return;
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n;
    while ((n = it.nextNode())) {
      yield n;
      if (n.shadowRoot) {
        for (const x of walk(n.shadowRoot)) yield x;
      }
    }
  }
  function vis(el){
    try{
      const c = getComputedStyle(el);
      if (c.display === 'none' || c.visibility === 'hidden' || c.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      if ((r.width <= 1 || r.height <= 1) && (!el.getClientRects || el.getClientRects().length === 0)) return false;
      return true;
    }catch(_){ return false; }
  }
  function textOf(el){
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  const matches = [];
  for (const el of walk(document)) {
    if (!vis(el)) continue;
    const t = textOf(el);
    if (!t) continue;
    for (const p of TEXT) {
      if (p.test(t)) {
        matches.push({
          tag: el.tagName,
          text: t.slice(0, 160),
          html: (el.outerHTML || '').slice(0, 2000),
        });
        break;
      }
    }
    if (matches.length >= 8) break;
  }
  const buttons = [];
  for (const el of walk(document)) {
    if (!vis(el)) continue;
    const tag = (el.tagName || '').toUpperCase();
    const role = el.getAttribute ? el.getAttribute('role') : '';
    if (tag !== 'BUTTON' && tag !== 'A' && role !== 'button') continue;
    const t = textOf(el);
    if (!t) continue;
    for (const p of BTN) {
      if (p.test(t)) {
        buttons.push({ tag, text: t.slice(0, 80), html: (el.outerHTML || '').slice(0, 1200) });
        break;
      }
    }
    if (buttons.length >= 8) break;
  }
  return {
    found: matches.length > 0,
    url: location.href,
    title: document.title,
    matches,
    buttons,
  };
}
"""


def _dump_session_elsewhere_dom(roots, state: Optional[_PragmaticState]) -> None:
    if state is None:
        return
    now = time.time()
    if state.session_elsewhere_dumped_at and now - state.session_elsewhere_dumped_at < 60.0:
        return
    results: list[dict[str, Any]] = []
    for root in roots:
        try:
            res = root.evaluate(_SESSION_ELSEWHERE_DUMP_JS)
        except Exception:
            continue
        if isinstance(res, dict) and res.get("found"):
            try:
                loc = getattr(root, "url", "page")
            except Exception:
                loc = "page"
            results.append({"loc": loc, "data": res})
    if not results:
        return
    state.session_elsewhere_dumped_at = now
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dump_path = Path(__file__).parent / f"session_elsewhere_dom_{ts}.json"
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=2)
        try:
            send_log(f"[session] session elsewhere DOM dumped to {dump_path}")
        except Exception:
            pass
    except Exception:
        pass


def _force_relogin(page, state: Optional[_PragmaticState], reason: str = "") -> None:
    """Soft recovery: reload the lobby page ONLY.

    過去の実装は clear_cookies + localStorage.clear() でログインクレデンシャルを
    破壊し, Stake に "Sorry, an error has occurred" を出させていた (本日の事故).
    cookies は絶対に消さない. 単純な goto で十分に session_elsewhere 本体を
    更新できることが確認されている.
    """
    try:
        send_log(f"[session] soft reload (was force_relogin) {reason}".strip())
    except Exception:
        pass
    try:
        page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass
    if state is not None:
        state.session_elsewhere_relogin_at = time.time()
        state.session_elsewhere_relogin_count += 1


def _dismiss_session_elsewhere_modal(page, state: Optional[_PragmaticState] = None) -> bool:
    """Best-effort auto-dismiss for Stake 'session elsewhere' modal.

    Cross-origin iframe + Shadow DOM 対応 (JS TreeWalker ベース).
    Returns True if the modal was observed (dismissal may or may not succeed).
    """
    roots = []
    try:
        pages = list(getattr(getattr(page, "context", None), "pages", []) or [])
    except Exception:
        pages = []
    if page not in pages:
        pages = [page] + pages
    for p in pages:
        if p not in roots:
            roots.append(p)
        try:
            for fr in list(getattr(p, "frames", []) or []):
                if fr not in roots:
                    roots.append(fr)
        except Exception:
            continue
    observed_any = False
    clicked_any = 0
    detected_text = ""
    detected_buttons: list[dict] = []
    still_observed = False
    eval_errors = 0
    prev_unresolved = bool(state.session_elsewhere_unresolved) if state is not None else False
    for _ in range(3):
        attempt_observed = False
        attempt_clicked = 0
        for root in roots:
            try:
                res = root.evaluate(_SESSION_ELSEWHERE_DISMISS_JS, {"click": True})
            except Exception:
                eval_errors += 1
                continue
            if not isinstance(res, dict) or not res.get("found"):
                continue
            attempt_observed = True
            observed_any = True
            attempt_clicked += int(res.get("clicked") or 0)
            clicked_any += int(res.get("clicked") or 0)
            detected_text = res.get("text") or detected_text
            detected_buttons = res.get("buttons") or detected_buttons
        if not attempt_observed:
            break
        if attempt_clicked > 0:
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
            still_observed = False
            for root in roots:
                try:
                    chk = root.evaluate(_SESSION_ELSEWHERE_DISMISS_JS, {"click": False})
                except Exception:
                    eval_errors += 1
                    continue
                if isinstance(chk, dict) and chk.get("found"):
                    still_observed = True
                    detected_text = chk.get("text") or detected_text
                    detected_buttons = chk.get("buttons") or detected_buttons
                    break
            if not still_observed:
                break
        else:
            still_observed = True
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
    # NOTE: 旧 Playwright-locator フォールバックは削除済.
    # `a:has-text('Continue')` が "Continue with Facebook" に誤爆し
    # フッター SNS リンクがクリックされる重大バグの原因だった.
    # JS DISMISS が clicked_any=0 のときは reload/force_relogin エスカレーションで対応する.
    if not observed_any:
        if state is not None and state.session_elsewhere_unresolved:
            state.session_elsewhere_unresolved = False
            state.session_elsewhere_resolved_at = time.time()
            state.session_elsewhere_unresolved_since = 0.0
            try:
                send_log("[session] session elsewhere resolved (modal gone)")
            except Exception:
                pass
        if eval_errors:
            try:
                send_log(f"[session] session elsewhere detect failed on {eval_errors} roots (eval error)")
            except Exception:
                pass
        return False
    if state is not None:
        now = time.time()
        unresolved = not (clicked_any > 0 and not still_observed)
        state.session_elsewhere_observed = True
        state.session_elsewhere_unresolved = unresolved
        state.session_elsewhere_last_at = now
        if unresolved:
            if not prev_unresolved or not state.session_elsewhere_unresolved_since:
                state.session_elsewhere_unresolved_since = now
        else:
            state.session_elsewhere_unresolved_since = 0.0
    try:
        if clicked_any > 0 and not still_observed:
            send_log(f"[session] auto-dismissed session elsewhere (clicks={clicked_any}) text='{detected_text[:60]}'")
        elif clicked_any > 0 and still_observed:
            send_log(f"[session] dismiss attempted but modal persists text='{detected_text[:60]}'")
        else:
            send_log(f"[session] DETECTED but no clickable button — text='{detected_text[:60]}' buttons={detected_buttons[:5]}")
    except Exception:
        pass
    if eval_errors:
        try:
            send_log(f"[session] session elsewhere evaluate failed on {eval_errors} roots")
        except Exception:
            pass
    if state is not None and state.session_elsewhere_unresolved:
        _dump_session_elsewhere_dom(roots, state)
    if state is not None and state.session_elsewhere_unresolved and state.session_elsewhere_unresolved_since:
        now = time.time()
        # 以前は 5s で reload → click が間に合わず暴走. 20s まで余裕を与える.
        if now - state.session_elsewhere_unresolved_since >= 20.0 and now - float(state.session_elsewhere_reload_at or 0) >= 60.0:
            try:
                send_log("[session] session elsewhere unresolved >20s — reloading once")
            except Exception:
                pass
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                try:
                    send_log(f"[session] reload failed after session-elsewhere (err={e})")
                except Exception:
                    pass
            state.session_elsewhere_reload_at = now
        # force_relogin は cookies 削除しない soft reload 版 (defanged). 発火条件も緩和.
        if (
            now - state.session_elsewhere_unresolved_since >= 90.0
            and now - float(state.session_elsewhere_relogin_at or 0) >= 180.0
        ):
            _dump_session_elsewhere_dom(roots, state)
            _force_relogin(page, state, reason="(unresolved >90s)")
    return True


def _legacy_dismiss_session_elsewhere(page, state=None) -> bool:
    """Old Playwright-locator-based dismiss (kept as reference / fallback)."""
    observed_pats = [
        r"他の場所でセッション",
        r"セッションが開始",
        r"session.*elsewhere",
        r"session.*another",
    ]
    button_labels = [
        r"ここで続ける",
        r"続行",
        r"続ける",
        r"このデバイス",
        r"この端末",
        r"Continue\s+here",
        r"Keep\s+using\s+here",
        r"Continue",
        r"OK",
    ]
    try:
        roots = [page] + list(getattr(page, "frames", []) or [])
    except Exception:
        roots = [page]

    def _has_visible_text(root, pat: str) -> bool:
        try:
            loc = root.get_by_text(re.compile(pat, re.I))
            try:
                return bool(loc.first.is_visible())
            except Exception:
                return bool(loc.is_visible())
        except Exception:
            return False

    observed = False
    for root in roots:
        for pat in observed_pats:
            try:
                if _has_visible_text(root, pat):
                    observed = True
                    break
            except Exception:
                continue
        if observed:
            break
    if not observed:
        # If modal was previously observed but is now gone (manual or auto), clear the block.
        if state is not None and state.session_elsewhere_unresolved:
            state.session_elsewhere_unresolved = False
            state.session_elsewhere_resolved_at = time.time()
            try:
                send_log("[session] session elsewhere resolved (modal gone)")
            except Exception:
                pass
        return False

    if state is not None:
        state.session_elsewhere_observed = True
        state.session_elsewhere_unresolved = True
        state.session_elsewhere_last_at = time.time()

    for root in roots:
        for pat in button_labels:
            try:
                btn = root.get_by_role("button", name=re.compile(pat, re.I))
                if btn.count() > 0:
                    btn.first.click(timeout=2000, force=True)
                    try:
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass
                    try:
                        send_log(f"[session] dismissed 'session elsewhere' modal via '{pat}'")
                    except Exception:
                        pass
                    if state is not None:
                        # If the modal is no longer visible, unblock immediately.
                        try:
                            if not any(_has_visible_text(r, p) for r in roots for p in observed_pats):
                                state.session_elsewhere_unresolved = False
                                state.session_elsewhere_resolved_at = time.time()
                                send_log("[session] session elsewhere resolved (dismissed)")
                        except Exception:
                            pass
                    return True
            except Exception:
                continue
            try:
                loc = root.locator(f"button:has-text('{pat}')")
                if loc.count() > 0:
                    loc.first.click(timeout=2000, force=True)
                    try:
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass
                    try:
                        send_log(f"[session] dismissed 'session elsewhere' modal via button text '{pat}'")
                    except Exception:
                        pass
                    if state is not None:
                        try:
                            if not any(_has_visible_text(r, p) for r in roots for p in observed_pats):
                                state.session_elsewhere_unresolved = False
                                state.session_elsewhere_resolved_at = time.time()
                                send_log("[session] session elsewhere resolved (dismissed)")
                        except Exception:
                            pass
                    return True
            except Exception:
                continue
    try:
        send_log("[session] session elsewhere modal observed but dismiss failed")
    except Exception:
        pass
    return True


_INACTIVITY_DISMISS_JS = r"""
(opts) => {
  const doClick = !(opts && opts.click === false);
  const TEXT_PATTERNS = [
    /無操作/, /一時停止/, /タイムアウト/,
    /inactivity|inactive/i, /paused/i, /still\s+there/i,
    /are\s+you\s+still/i, /session\s+timed?\s+out/i,
    /still\s+with\s+us/i, /still\s+playing/i,
  ];
  // 厳密マッチ: "Continue" / "continue" 単体を禁止 (社会ログイン誤爆防止)
  const BTN_PATTERNS = [
    /^続ける$/, /^続行$/, /^再開$/, /^プレイ続行$/, /^プレイを再開$/, /^はい$/,
    /^OK$/i, /^Continue$/i, /^Continue playing$/i, /^Keep playing$/i,
    /^Resume$/i, /^I'?m here$/i, /^Still here$/i, /^I'?m still here$/i,
    /^Yes,?\s*I'?m here$/i, /^Yes$/i, /^Play on$/i,
  ];
  const EXCLUDE = /(with\s+)?(facebook|google|passkey|apple|twitter|metamask|telegram|line|yahoo)|another\s+way|forgot|register|sign\s*up|\u65b0\u898f|\u767b\u9332|\u30d1\u30b9\u30ef\u30fc\u30c9/i;

  function* walk(root){
    if (!root) return;
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = it.nextNode())) {
      yield node;
      if (node.shadowRoot) {
        for (const n of walk(node.shadowRoot)) yield n;
      }
    }
  }
  function visible(el){
    try{
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      if ((r.width <= 1 || r.height <= 1) && (!el.getClientRects || el.getClientRects().length === 0)) return false;
      return true;
    }catch(e){ return false; }
  }
  function textOf(el){
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function ownText(el){
    let s = '';
    for (const ch of el.childNodes) if (ch.nodeType === 3) s += ch.textContent || '';
    return s.replace(/\s+/g, ' ').trim();
  }
  function findModalContainer(el){
    let cur = el;
    for (let i=0; i<20 && cur; i++) {
      try{
        if (cur.nodeType === 1) {
          const role = cur.getAttribute && cur.getAttribute('role');
          const am = cur.getAttribute && cur.getAttribute('aria-modal');
          const tag = (cur.tagName || '').toUpperCase();
          const cls = (cur.className && typeof cur.className === 'string') ? cur.className : '';
          if (tag === 'DIALOG' || role === 'dialog' || role === 'alertdialog' || am === 'true' ||
              /modal|dialog|popup|overlay/i.test(cls)) {
            return cur;
          }
        }
      }catch(_){}
      cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host);
    }
    return null;
  }
  function dispatchClick(el){
    try{ el.focus(); }catch(_){}
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy, view: window };
    try{
      if (window.PointerEvent) {
        el.dispatchEvent(new PointerEvent('pointerdown', base));
        el.dispatchEvent(new PointerEvent('pointerup', base));
      }
      el.dispatchEvent(new MouseEvent('mousedown', base));
      el.dispatchEvent(new MouseEvent('mouseup', base));
      el.dispatchEvent(new MouseEvent('click', base));
    }catch(_){}
    try{ el.click(); }catch(_){}
  }
  let foundEl = null;
  let foundText = '';
  for (const el of walk(document)) {
    if (!visible(el)) continue;
    const own = ownText(el);
    if (!own) continue;
    for (const pat of TEXT_PATTERNS) {
      if (pat.test(own)) { foundEl = el; foundText = own.slice(0, 120); break; }
    }
    if (foundEl) break;
  }
  if (!foundEl) return { found: false };
  let scope = findModalContainer(foundEl);
  if (!scope) {
    scope = foundEl;
    for (let i=0; i<4 && scope.parentElement; i++) scope = scope.parentElement;
  }
  const buttons = [];
  let clicked = 0;
  for (const el of walk(scope)) {
    if (!visible(el)) continue;
    const tag = (el.tagName || '').toUpperCase();
    if (tag !== 'BUTTON' && tag !== 'A' && el.getAttribute && el.getAttribute('role') !== 'button') continue;
    const t = textOf(el);
    if (!t) continue;
    if (EXCLUDE.test(t)) { buttons.push({ tag, text: t.slice(0,80), skipped: 'exclude' }); continue; }
    for (const pat of BTN_PATTERNS) {
      if (pat.test(t)) {
        buttons.push({ tag, text: t.slice(0, 80) });
        if (doClick && clicked === 0) { dispatchClick(el); clicked += 1; }
        break;
      }
    }
    if (buttons.length >= 8) break;
  }
  return { found: true, text: foundText, scope: scope.tagName, clicked, buttons };
}
"""

_INACTIVITY_DUMP_JS = r"""
() => {
  const TEXT_PATTERNS = [
    /無操作/, /一時停止/, /タイムアウト/,
    /inactivity|inactive/i, /paused/i, /still\s+there/i,
    /are\s+you\s+still/i, /session\s+timed?\s+out/i,
    /still\s+with\s+us/i, /still\s+playing/i,
  ];
  const BTN_PATTERNS = [
    /続ける/, /続行/, /再開/, /プレイ続行/, /プレイを再開/, /はい/,
    /^OK$/i, /continue/i, /resume/i, /keep\s+playing/i, /i'?m\s+here/i,
    /still\s+here/i, /yes,?\s*i'?m/i, /^yes$/i,
  ];
  function* walk(root){
    if (!root) return;
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = it.nextNode())) {
      yield node;
      if (node.shadowRoot) {
        for (const n of walk(node.shadowRoot)) yield n;
      }
    }
  }
  function visible(el){
    try{
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      if ((r.width <= 1 || r.height <= 1) && (!el.getClientRects || el.getClientRects().length === 0)) return false;
      return true;
    }catch(e){ return false; }
  }
  function textOf(el){
    return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  const matches = [];
  for (const el of walk(document)) {
    if (!visible(el)) continue;
    const t = textOf(el);
    if (!t) continue;
    for (const pat of TEXT_PATTERNS) {
      if (pat.test(t)) {
        matches.push({ tag: el.tagName, text: t.slice(0, 160), html: (el.outerHTML || '').slice(0, 2000) });
        break;
      }
    }
    if (matches.length >= 8) break;
  }
  const buttons = [];
  for (const el of walk(document)) {
    if (!visible(el)) continue;
    const tag = (el.tagName || '').toUpperCase();
    if (tag !== 'BUTTON' && tag !== 'A' && el.getAttribute && el.getAttribute('role') !== 'button') continue;
    const t = textOf(el);
    if (!t) continue;
    for (const pat of BTN_PATTERNS) {
      if (pat.test(t)) {
        buttons.push({ tag, text: t.slice(0, 80), html: (el.outerHTML || '').slice(0, 1200) });
        break;
      }
    }
    if (buttons.length >= 8) break;
  }
  return { found: matches.length > 0, url: location.href, title: document.title, matches, buttons };
}
"""


def _dump_inactivity_dom(roots, state: Optional[_PragmaticState]) -> None:
    if state is None:
        return
    now = time.time()
    if state.inactivity_dumped_at and now - state.inactivity_dumped_at < 60.0:
        return
    results: list[dict[str, Any]] = []
    for root in roots:
        try:
            res = root.evaluate(_INACTIVITY_DUMP_JS)
        except Exception:
            continue
        if isinstance(res, dict) and res.get("found"):
            try:
                loc = getattr(root, "url", "page")
            except Exception:
                loc = "page"
            results.append({"loc": loc, "data": res})
    if not results:
        return
    state.inactivity_dumped_at = now
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dump_path = Path(__file__).parent / f"inactivity_modal_dom_{ts}.json"
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f, ensure_ascii=False, indent=2)
        try:
            send_log(f"[inactivity] modal DOM dumped to {dump_path}")
        except Exception:
            pass
    except Exception:
        pass


_SESSION_ENDED_DETECT_JS = r"""
() => {
  // Pragmatic Play の "セッションが終了しました" ハードタイムアウト.
  // OK を押すと TOP に戻されるので検知のみ (処理側で goto lobby する).
  const TEXT = [
    /セッションが終了しました/,
    /セッションの有効期限が切れました/,
    /session\s+has\s+ended/i,
    /session\s+expired/i,
    /session\s+ended/i,
  ];
  function* walk(root){
    if (!root) return;
    const it = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let n; while ((n = it.nextNode())) { yield n; if (n.shadowRoot) for (const x of walk(n.shadowRoot)) yield x; }
  }
  function visible(el){
    try{
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
      const r = el.getBoundingClientRect();
      return r.width > 20 && r.height > 10;
    }catch(_){ return false; }
  }
  function ownText(el){
    let s = '';
    for (const ch of el.childNodes) if (ch.nodeType === 3) s += ch.textContent || '';
    return s.replace(/\s+/g, ' ').trim();
  }
  for (const el of walk(document)) {
    if (!visible(el)) continue;
    const t = ownText(el);
    if (!t) continue;
    for (const p of TEXT) if (p.test(t)) return { found: true, text: t.slice(0, 120) };
  }
  return { found: false };
}
"""


def _dismiss_session_ended_modal(page, state: Optional[_PragmaticState] = None) -> bool:
    """Pragmatic の「セッションが終了しました」ハードタイムアウトを検出.

    OK ボタンを押すと Stake TOP に戻されセッション完全ロストするので,
    OK は絶対に押さず lobby URL に直接 goto することで復旧する.
    goto 後は main loop の _join_table が target テーブルに再入場する.
    """
    try:
        roots = [page] + list(getattr(page, "frames", []) or [])
    except Exception:
        roots = [page]
    try:
        ctx_pages = list(getattr(getattr(page, "context", None), "pages", []) or [])
    except Exception:
        ctx_pages = []
    for p in ctx_pages:
        if p not in roots:
            roots.append(p)
            try:
                for fr in list(getattr(p, "frames", []) or []):
                    if fr not in roots:
                        roots.append(fr)
            except Exception:
                continue
    detected = False
    detected_text = ""
    for root in roots:
        try:
            res = root.evaluate(_SESSION_ENDED_DETECT_JS)
        except Exception:
            continue
        if isinstance(res, dict) and res.get("found"):
            detected = True
            detected_text = (res.get("text") or detected_text)
            break
    if not detected:
        return False
    try:
        send_log(f"[session-ended] detected text='{detected_text[:80]}' — navigating to lobby (skip OK)")
    except Exception:
        pass
    # OK を押さず lobby に直接戻す (TOP ページ行きを回避).
    try:
        page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        try: send_log(f"[session-ended] goto lobby err={e}")
        except Exception: pass
    return True


def _dismiss_inactivity_modal(page, state: Optional[_PragmaticState] = None) -> bool:
    """Best-effort auto-dismiss for Stake 'inactivity paused' modal.

    Cross-origin iframe (qpidreoxcc.net) 対応:
    page + 全 frame に対して JS TreeWalker (Shadow DOM 含む) で無操作系テキスト検知、
    マッチする clickable を data-attribute 印 → evaluate 内で click() 実行.

    Returns True if modal observed (regardless of successful click).
    """
    roots = []
    try:
        pages = list(getattr(getattr(page, "context", None), "pages", []) or [])
    except Exception:
        pages = []
    if page not in pages:
        pages = [page] + pages
    for p in pages:
        if p not in roots:
            roots.append(p)
        try:
            for fr in list(getattr(p, "frames", []) or []):
                if fr not in roots:
                    roots.append(fr)
        except Exception:
            continue

    observed_any = False
    clicked_any = 0
    detected_text = ""
    detected_buttons = []
    still_observed = False
    for _ in range(3):
        attempt_observed = False
        attempt_clicked = 0
        for root in roots:
            try:
                res = root.evaluate(_INACTIVITY_DISMISS_JS, {"click": True})
            except Exception:
                continue
            if not isinstance(res, dict) or not res.get("found"):
                continue
            attempt_observed = True
            observed_any = True
            attempt_clicked += int(res.get("clicked") or 0)
            clicked_any += int(res.get("clicked") or 0)
            detected_text = res.get("text") or detected_text
            detected_buttons = res.get("buttons") or detected_buttons
        if not attempt_observed:
            break
        if attempt_clicked > 0:
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
            still_observed = False
            for root in roots:
                try:
                    chk = root.evaluate(_INACTIVITY_DISMISS_JS, {"click": False})
                except Exception:
                    continue
                if isinstance(chk, dict) and chk.get("found"):
                    still_observed = True
                    detected_text = chk.get("text") or detected_text
                    detected_buttons = chk.get("buttons") or detected_buttons
                    break
            if not still_observed:
                break
        else:
            still_observed = True
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
    # NOTE: 旧 Playwright-locator フォールバックは削除済 (Facebook 誤爆の原因).
    # 新フォールバック: Stake の inactivity 表示は動画プレイヤー内をクリックで解除できる.
    # iframe / canvas / video 領域の中心を 1 回だけ click する (ボタン検索なし = SNS 誤爆ゼロ).
    if observed_any and (clicked_any == 0 or still_observed):
        try:
            clicked_video = _click_live_video_center(page)
            if clicked_video:
                clicked_any += 1
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    time.sleep(0.6)
                # 再検知
                still_observed = False
                try:
                    chk = page.evaluate(_INACTIVITY_DISMISS_JS, {"click": False})
                    if isinstance(chk, dict) and chk.get("found"):
                        still_observed = True
                except Exception:
                    pass
                try:
                    send_log(f"[inactivity] dismissed by video-center click (still_observed={still_observed})")
                except Exception:
                    pass
        except Exception as _e:
            try: send_log(f"[inactivity] video-center click err={_e}")
            except Exception: pass

    if not observed_any:
        if state is not None and state.inactivity_modal_unresolved:
            state.inactivity_modal_unresolved = False
            try:
                send_log("[inactivity] modal gone (resolved)")
            except Exception:
                pass
        return False

    if state is not None:
        now = time.time()
        unresolved = not (clicked_any > 0 and not still_observed)
        state.inactivity_modal_observed = True
        state.inactivity_modal_unresolved = unresolved
        state.inactivity_modal_last_at = now
        if clicked_any > 0 and not still_observed:
            state.inactivity_dismissed_count += 1

    try:
        if clicked_any > 0 and not still_observed:
            send_log(f"[inactivity] auto-dismissed (clicks={clicked_any}) text='{detected_text[:60]}' buttons={detected_buttons[:3]}")
        elif clicked_any > 0 and still_observed:
            send_log(f"[inactivity] dismiss attempted but modal persists text='{detected_text[:60]}'")
        else:
            send_log(f"[inactivity] DETECTED but no clickable button found — text='{detected_text[:60]}' buttons={detected_buttons[:5]}")
    except Exception:
        pass
    if state is not None and state.inactivity_modal_unresolved:
        _dump_inactivity_dom(roots, state)
        now = time.time()
        # video-center click に十分な機会を与える. 60s 未解消で 1 回だけ reload.
        if now - state.inactivity_modal_last_at >= 60.0 and now - float(state.inactivity_reload_at or 0) >= 180.0:
            try:
                send_log("[inactivity] unresolved >60s — reloading once")
            except Exception:
                pass
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            state.inactivity_reload_at = now
    return True


_LIVE_VIDEO_LOCATE_JS = r"""
() => {
  // Pragmatic Live の動画再生領域の中心を安全に返す.
  // iframe / video / canvas / ゲーム container を優先順位付きで探索.
  function visible(el){
    try{
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 150) return false;
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
    }catch(_){ return false; }
  }
  function centerOf(el){
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: r.width, h: r.height };
  }
  // 優先: iframe[src*=pragmatic] / game container / canvas / video
  const sels = [
    'iframe[src*="pragmatic"]',
    'iframe[src*="dga"]',
    'iframe[title*="game" i]',
    'div[class*="game-container"]',
    'div[class*="live-table"]',
    'div[class*="GameArea"]',
    'canvas',
    'video',
    'iframe',
  ];
  for (const sel of sels) {
    const list = document.querySelectorAll(sel);
    for (const el of list) {
      if (visible(el)) return centerOf(el);
    }
  }
  // フォールバック: viewport 中心
  return { x: Math.round(window.innerWidth / 2), y: Math.round(window.innerHeight / 2), w: window.innerWidth, h: window.innerHeight };
}
"""


def _click_live_video_center(page) -> bool:
    """Live 動画プレイヤー領域を強化 click (inactivity modal 解除用).

    Stake の inactivity モーダル (動画プレイヤー上にオーバーレイ) は,
    プレイヤー内 任意クリックで解除される. 確実化のため複数位置 + 複数手段で
    連打し, 加えて内部の iframe にも JS-level synthetic click を注入する.
    SNS ボタン誤爆を避けるため テキストマッチは使わず 座標クリック + frame 内 click.
    """
    clicked_any = False
    # 1) 位置特定
    try:
        pos = page.evaluate(_LIVE_VIDEO_LOCATE_JS)
    except Exception:
        pos = None
    if not isinstance(pos, dict):
        return False
    cx = int(pos.get("x") or 0)
    cy = int(pos.get("y") or 0)
    w = int(pos.get("w") or 0)
    h = int(pos.get("h") or 0)
    if cx <= 0 or cy <= 0:
        return False

    # 2) 中心 + offset の 5 点を連打 (1 点だけ弾かれても他で成功することを期待)
    offsets = [(0, 0), (-30, -30), (30, -30), (-30, 30), (30, 30)]
    for dx, dy in offsets:
        x = max(5, cx + dx)
        y = max(5, cy + dy)
        try:
            page.mouse.click(x, y, delay=20)
            clicked_any = True
        except Exception:
            try:
                page.mouse.move(x, y)
                page.mouse.down()
                page.mouse.up()
                clicked_any = True
            except Exception:
                continue

    # 3) Pragmatic iframe 内で JS 側 dispatchEvent も試行 (canvas/video へのネイティブ click を補強)
    try:
        for f in page.frames:
            u = str(getattr(f, "url", "") or "")
            if "pragmaticplaylive" not in u:
                continue
            try:
                f.evaluate(r"""
                () => {
                  const sel = ['canvas','video','iframe[src*="pragmatic"]','div[class*="game"]'];
                  for (const s of sel) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    try { el.focus(); } catch(_){}
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width/2;
                    const cy = r.top + r.height/2;
                    const base = { bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy, view: window };
                    try { if (window.PointerEvent) { el.dispatchEvent(new PointerEvent('pointerdown', base)); el.dispatchEvent(new PointerEvent('pointerup', base)); } } catch(_){}
                    try { el.dispatchEvent(new MouseEvent('mousedown', base)); el.dispatchEvent(new MouseEvent('mouseup', base)); el.dispatchEvent(new MouseEvent('click', base)); } catch(_){}
                    try { el.click(); } catch(_){}
                    return true;
                  }
                  return false;
                }
                """)
                clicked_any = True
                break
            except Exception:
                continue
    except Exception:
        pass
    return clicked_any


def _send_keep_alive(page, state: Optional[_PragmaticState] = None) -> None:
    """Send a tiny synthetic user gesture to prevent Stake's inactivity timer.

    Stake は約 3-5 分無操作で inactivity modal を出す.
    戦略:
      - 60s 毎: マウス微動 + window focus イベント
      - 180s 毎 (3 回に 1 回): 動画プレイヤー領域内でもマウス移動 (より確実)
      - game_ws silence が 150s 超えた場合: 動画中心を click して強制復活
    """
    try:
        now = time.time()
        last = float((state.keep_alive_last_at if state is not None else 0) or 0)
        elapsed = now - last
        if elapsed < 60.0:
            return

        # Phase 1: 軽量マウス微動 (inactive timer リセット)
        try:
            page.mouse.move(10, 10)
            page.mouse.move(12, 11)
        except Exception:
            pass
        try:
            page.evaluate(
                """() => {
                  try {
                    window.dispatchEvent(new Event('focus'));
                    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 10, clientY: 10 }));
                  } catch (_) {}
                }"""
            )
        except Exception:
            pass

        # Phase 2: 180s 以上経過で強化. 動画エリアでマウス移動 (inactive 判定を確実回避).
        if elapsed >= 180.0:
            try:
                pos = page.evaluate(_LIVE_VIDEO_LOCATE_JS)
                if isinstance(pos, dict):
                    x = int(pos.get("x") or 0)
                    y = int(pos.get("y") or 0)
                    if x > 0 and y > 0:
                        page.mouse.move(x, y)
                        page.mouse.move(x + 2, y + 2)
            except Exception:
                pass

        # Phase 3: game_ws 長時間沈黙検知 → 強制 video click.
        # Stake 側で inactive 判定される前にユーザー操作を偽装して予防.
        try:
            if state is not None:
                last_game = float(getattr(state, "last_game_ws_recv_at", 0) or 0)
                if last_game and (now - last_game) >= 150.0:
                    _click_live_video_center(page)
                    try: send_log(f"[keep_alive] preventive video click (ws silence {now-last_game:.0f}s)")
                    except Exception: pass
        except Exception:
            pass

        if state is not None:
            state.keep_alive_last_at = now
    except Exception:
        pass


def _translate_en_to_ja(en: str) -> str:
    """Master UI の toJaTableName と同じ変換 (Stake ja ロケール表記に合わせる)."""
    if not en:
        return en
    n = str(en).strip()
    OVERRIDES = {
        "BACCARAT_MULTIPLAY": "BACCARAT_MULTIPLAY",
        "STAKE SPEED BACCARAT": "STAKE スピードバカラ",
        "Mega Sic Bac": "Mega Sic Bac",
        "MEGA BACCARAT": "メガバカラ",
    }
    if n in OVERRIDES:
        return OVERRIDES[n]
    if n == n.upper() and "_" in n:
        return n
    patterns = [
        (r"Priv[e\u00e9]\s*Lounge\s*Baccarat\s*Squeeze", "プライベラウンジ・スクイーズバカラ"),
        (r"Priv[e\u00e9]\s*Lounge\s*Baccarat", "プライベラウンジバカラ"),
        (r"Korean\s+Priv[e\u00e9]\s*Lounge\s*Baccarat", "韓国プライベラウンジバカラ"),
        (r"Korean\s+Turbo\s+Baccarat", "韓国ターボバカラ"),
        (r"Korean\s+Speed\s+Baccarat", "韓国スピードバカラ"),
        (r"Korean\s+Baccarat", "韓国バカラ"),
        (r"Japanese\s+Speed\s+Baccarat", "日本語スピードバカラ"),
        (r"Japanese\s+Baccarat", "日本語バカラ"),
        (r"Chinese\s+Speed\s+Baccarat", "中国スピードバカラ"),
        (r"Chinese\s+Baccarat", "中国バカラ"),
        (r"Thai\s+Speed\s+Baccarat", "タイスピードバカラ"),
        (r"Thai\s+Baccarat", "タイバカラ"),
        (r"Vietnamese\s+Speed\s+Baccarat", "ベトナムスピードバカラ"),
        (r"Vietnamese\s+Baccarat", "ベトナムバカラ"),
        (r"Indonesian\s+Speed\s+Baccarat", "インドネシアスピードバカラ"),
        (r"Indonesian\s+Baccarat", "インドネシアバカラ"),
        (r"Fortune\s*6\s+Baccarat", "フォーチュン6バカラ"),
        (r"Super\s*8\s+Baccarat", "スーパー8バカラ"),
        (r"Speed\s+Baccarat", "スピードバカラ"),
        (r"Turbo\s+Baccarat", "ターボバカラ"),
        (r"Squeeze\s+Baccarat", "スクイーズバカラ"),
        (r"Baccarat\s+Squeeze", "バカラスクイーズ"),
        (r"Baccarat\s+Lobby", "バカラロビー"),
        (r"Mega\s+Baccarat", "メガバカラ"),
        (r"\bBaccarat\b", "バカラ"),
    ]
    for pat, rep in patterns:
        n = re.sub(pat, rep, n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip()


def _build_substr_candidates(user_input: str) -> list[str]:
    """Accept pipe/comma-separated substrings, auto-add JA translation for EN terms.

    Pragmatic lobby の実際のボタンは "スピードバカラ18$ 0.2" のように
    バカラ名と数字間にスペースがない. さらにチップ範囲が連結されるので,
    EN → JA 変換時に「space あり / なし」両バリアントを候補化する.
    """
    raw = (user_input or "").replace(",", "|")
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    out: list[str] = []

    def _push(val: str) -> None:
        if val and val not in out:
            out.append(val)

    for p in parts:
        _push(p)
        # EN → JA auto-add
        if re.search(r"[A-Za-z]", p):
            ja = _translate_en_to_ja(p)
            if ja and ja != p:
                _push(ja)
                # space なし版 (Pragmatic lobby の実表記). 例: "スピードバカラ 18" → "スピードバカラ18"
                ja_nospace = re.sub(r"([\u3040-\u30FF\u4E00-\u9FAF])\s+(\d)", r"\1\2", ja)
                _push(ja_nospace)
                # 同上を EN 側にも適用 (Speed Baccarat 18 → SpeedBaccarat18 等はここでは不要)
        else:
            # 既に JA の場合, space なし版も追加
            ja_nospace = re.sub(r"([\u3040-\u30FF\u4E00-\u9FAF])\s+(\d)", r"\1\2", p)
            _push(ja_nospace)
    return out


_LOBBY_TRY_CLICK_JS = r"""
async (args) => {
  // 引数:
  //   qpid         — Pragmatic の unique table id (例: "h22z8qhp17sa0vkh"). 最優先.
  //   candidates   — 表示名候補 (fallback 用, 完全一致マッチ)
  //   maxScroll    — 最大 scroll 回数
  //   categories   — click 候補のカテゴリタブラベル (正規表現ソース)
  const qpid = String((args && args.qpid) || '').trim();
  const candidates = (args && args.candidates) || [];
  const maxScroll = (args && args.maxScroll) || 30;
  const categoryPatterns = (args && args.categories) || [
    'すべて', '全て', '全テーブル', '全部',
    'All', 'All Tables', 'All Games', 'View All',
    // Classic (Baccarat 1-9 等 regular 卓) - 最優先. Speed だけ見えて Classic が抜けた事例への対応.
    'クラシック', 'Classic', 'クラシックバカラ', 'Classic Baccarat',
    'ノーコミッション', 'No Commission', 'ノーコミ',
    'マルチプレイ', 'Multi', 'Multiplayer',
    'バカラ', 'Baccarat',
    'スピード', 'Speed', 'スピードバカラ', 'Speed Baccarat',
    '日本語', 'Japanese',
    '韓国', 'Korean',
    'ライブ', 'Live',
    'VIP', 'プライベ', 'Prive',
    'スクイーズ', 'Squeeze',
    'その他', 'Other',
  ];

  function norm(s){
    return (s || '').replace(/\s+/g, '').replace(/[\$￥¥]/g, '').toLowerCase();
  }
  function extractNamePart(text){
    // カード innerText は "スピードバカラ6\n$ 0.23" の形. name 部だけ切り出す.
    if (!text) return '';
    let s = text.replace(/\r/g, '\n');
    const nl = s.indexOf('\n');
    const dol = s.indexOf('$');
    const cuts = [nl, dol].filter(i => i >= 0);
    if (cuts.length) s = s.slice(0, Math.min(...cuts));
    return s.trim();
  }
  function normCands(){
    const out = [];
    for (const c of candidates) {
      const n = norm(c);
      if (n && out.indexOf(n) === -1) out.push({ orig: c, n: n });
    }
    return out;
  }
  const normedCands = normCands();

  // ---- qpid 発見ロジック ----
  // Pragmatic lobby のカードは以下のいずれかに qpid を含んでいる:
  //   (a) data-qpid / data-table-qpid 属性
  //   (b) data-table-id / data-tableid 属性 (numeric の operator ID ではなく qpid の場合あり)
  //   (c) href / src / background-image に /snaps/<qpid>/ パス
  //   (d) query string の ?tableId=<qpid>
  //   (e) 親要素のスタイル (最大 3 階層上)
  // qpid マッチしたら問答無用で click できる唯一要素なので誤爆ゼロ.
  function containsQpid(v, q){
    if (!v || !q) return false;
    const s = String(v);
    if (s.indexOf('/snaps/' + q + '/') >= 0) return true;
    if (s.indexOf('tableId=' + q) >= 0) return true;
    if (s.indexOf('tableid=' + q) >= 0) return true;
    return false;
  }
  function elementHasQpid(el, q){
    if (!el || !q) return false;
    try {
      // data-* 属性
      const ds = el.dataset || {};
      if (ds.qpid === q || ds.tableQpid === q || ds.tableId === q || ds.tableid === q) return true;
      // すべての属性を横断的にチェック
      if (el.getAttributeNames) {
        for (const a of el.getAttributeNames()) {
          const v = el.getAttribute(a) || '';
          if (containsQpid(v, q)) return true;
          // 完全一致も (data-table-id など)
          if (v === q) return true;
        }
      }
      // style 属性 (background-image の url(...) 内の qpid)
      const style = (el.getAttribute && el.getAttribute('style')) || '';
      if (containsQpid(style, q)) return true;
      if (el.src && containsQpid(String(el.src), q)) return true;
      if (el.href && containsQpid(String(el.href), q)) return true;
      // computed background-image
      try {
        const cs = getComputedStyle(el);
        if (cs && cs.backgroundImage && containsQpid(cs.backgroundImage, q)) return true;
      } catch(_) {}
    } catch(_) {}
    return false;
  }
  // qpid ヒットを持つ最上位の clickable な祖先 (role=button / button / a) を返す
  function clickableAncestor(el){
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      try {
        const role = cur.getAttribute && cur.getAttribute('role');
        const tag = (cur.tagName || '').toLowerCase();
        if (role === 'button' || tag === 'button' || tag === 'a') return cur;
        if (cur.onclick || (cur.dataset && (cur.dataset.testid || '').toLowerCase().indexOf('table') >= 0)) return cur;
      } catch(_) {}
      cur = cur.parentElement;
    }
    return el;  // fallback: 自分自身 (最後の手段で click 試行)
  }

  function clickEl(el){
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch(_) {}
    try { el.focus(); } catch(_) {}
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const base = { bubbles: true, cancelable: true, composed: true, clientX: cx, clientY: cy, view: window };
    try {
      if (window.PointerEvent) {
        el.dispatchEvent(new PointerEvent('pointerdown', base));
        el.dispatchEvent(new PointerEvent('pointerup', base));
      }
      el.dispatchEvent(new MouseEvent('mousedown', base));
      el.dispatchEvent(new MouseEvent('mouseup', base));
      el.dispatchEvent(new MouseEvent('click', base));
    } catch (_) {}
    try { el.click(); } catch(_) {}
  }

  // ---- qpid 優先 → 文字列 fallback の順で探索 ----
  function findByQpid(){
    if (!qpid) return null;
    // まず data-* や href/src で直接検索
    const direct = [
      'a[href*="/snaps/' + qpid + '/"]',
      'a[href*="tableId=' + qpid + '"]',
      '[data-qpid="' + qpid + '"]',
      '[data-table-qpid="' + qpid + '"]',
      '[data-tableid="' + qpid + '"]',
      '[data-table-id="' + qpid + '"]',
      'img[src*="/snaps/' + qpid + '/"]',
    ];
    for (const sel of direct) {
      try {
        const el = document.querySelector(sel);
        if (el) return { el: clickableAncestor(el), via: 'selector', sel: sel };
      } catch(_) {}
    }
    // 全要素走査 (style url / computed style 等)
    const all = document.querySelectorAll('*');
    for (const el of all) {
      if (elementHasQpid(el, qpid)) {
        return { el: clickableAncestor(el), via: 'scan' };
      }
    }
    return null;
  }

  function findByName(){
    // name fallback: 完全一致のみ. 誤爆しないのが最優先.
    if (!normedCands.length) return null;
    const nodes = document.querySelectorAll('[role="button"], button, a, div[role="button"], [data-testid*="table"], [data-testid*="lobby"]');
    for (const el of nodes) {
      const raw = (el.innerText || el.textContent || '');
      const np = extractNamePart(raw);
      const t = norm(np);
      if (!t) continue;
      for (const cc of normedCands) {
        if (t === cc.n) return { el: el, via: 'name', match: cc.orig };
      }
    }
    return null;
  }

  function findAndClick(){
    let r = findByQpid();
    if (r) {
      clickEl(r.el);
      return { clicked: true, match: qpid, via: r.via, sel: r.sel || '', matchType: 'qpid' };
    }
    r = findByName();
    if (r) {
      clickEl(r.el);
      return { clicked: true, match: r.match, via: r.via, matchType: 'name' };
    }
    return { clicked: false };
  }

  // ---- カテゴリタブの click (ループ中に使う) ----
  function clickCategory(label){
    const selectors = '[role="tab"], [role="button"], button, a, [data-category], [data-testid*="tab"], [data-testid*="category"]';
    const els = document.querySelectorAll(selectors);
    for (const el of els) {
      try {
        const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
        if (!t) continue;
        // label を substring で含むか exact か
        if (t === label || t.toLowerCase() === label.toLowerCase() ||
            new RegExp('^' + label.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&') + '$', 'i').test(t) ||
            new RegExp(label.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&'), 'i').test(t)) {
          clickEl(el);
          return { matched: t.slice(0, 60) };
        }
      } catch(_) {}
    }
    return null;
  }

  // ---- scroll ロジック ----
  // scrollTop 操作は Pragmatic lobby の virtual scroll が反応しないことがある.
  // 真の user wheel event も dispatch して lazy-load を確実に trigger する.
  function scrollables(){
    const out = [];
    const root = document.scrollingElement || document.documentElement || document.body;
    if (root) out.push(root);
    const all = document.querySelectorAll('*');
    for (const el of all) {
      try {
        const cs = getComputedStyle(el);
        if (!/(auto|scroll)/.test(cs.overflowY)) continue;
        if (el.scrollHeight - el.clientHeight > 50) out.push(el);
      } catch(_) {}
    }
    return out;
  }

  // virtual scroll 対応: 複数の手法を全部試す. 1 つでも効けば lazy load 発火.
  function scrollStep(sc, dirDown){
    const delta = (sc.clientHeight || 500) * 0.85 * (dirDown ? 1 : -1);
    let prev = 0;
    try { prev = sc.scrollTop; } catch(_) {}

    // 方法 1: scrollTo
    try {
      const target = Math.max(0, Math.min(sc.scrollHeight || 99999, prev + delta));
      if (sc.scrollTo) sc.scrollTo({ top: target, behavior: 'instant' });
      else sc.scrollTop = target;
    } catch(_) {}

    // 方法 2: WheelEvent (scroller 本体 + その中央にある element + window)
    try {
      const rect = sc.getBoundingClientRect ? sc.getBoundingClientRect() : {left:0, top:0, width:0, height:0};
      const cx = rect.left + (rect.width || 0) / 2;
      const cy = rect.top + (rect.height || 0) / 2;
      const base = {
        deltaY: delta, deltaMode: 0, bubbles: true, cancelable: true, composed: true,
        clientX: cx, clientY: cy, view: window,
      };
      sc.dispatchEvent(new WheelEvent('wheel', base));
      // 内部 child 要素にも投げる (scroll を消費する layer を探す)
      try {
        const inner = document.elementFromPoint ? document.elementFromPoint(cx, cy) : null;
        if (inner && inner !== sc) inner.dispatchEvent(new WheelEvent('wheel', base));
      } catch(_) {}
      // window 全体にも投げる (iframe root scroll 用)
      try { window.dispatchEvent(new WheelEvent('wheel', base)); } catch(_) {}
    } catch(_) {}

    // 方法 3: Keyboard PageDown/PageUp on document (virtual list が key event に反応する実装用)
    try {
      const kopts = {
        key: dirDown ? 'PageDown' : 'PageUp',
        code: dirDown ? 'PageDown' : 'PageUp',
        keyCode: dirDown ? 34 : 33,
        which: dirDown ? 34 : 33,
        bubbles: true, cancelable: true, composed: true,
      };
      document.dispatchEvent(new KeyboardEvent('keydown', kopts));
      document.dispatchEvent(new KeyboardEvent('keyup', kopts));
    } catch(_) {}

    // 方法 4: 'scroll' イベント明示 (listener が監視している場合に render trigger)
    try {
      sc.dispatchEvent(new Event('scroll', { bubbles: true }));
    } catch(_) {}

    let after = 0;
    try { after = sc.scrollTop; } catch(_) {}
    return { prev: prev, after: after, changed: Math.abs(after - prev) > 3 };
  }

  // 「最適な」scroller を選ぶ: scrollHeight が最大で、かつ virtual list の可能性が高いもの.
  // 全 scroller を試すより効率的.
  function pickBestScroller(list){
    let best = null;
    let bestDelta = 0;
    for (const el of list) {
      try {
        const delta = (el.scrollHeight || 0) - (el.clientHeight || 0);
        if (delta > bestDelta) {
          bestDelta = delta;
          best = el;
        }
      } catch(_) {}
    }
    return best;
  }

  // ---- 実行本体 ----
  const scrollers = scrollables();
  const phaseLog = [];

  // 同期 sleep (JS は setTimeout しかないので, wait_for_timeout を呼ぶ Python 側経由では sync 不可.
  // Playwright の evaluate は JS の await を許可するので Promise ベースで待機).
  function sleepMs(ms){ return new Promise(r => setTimeout(r, ms)); }

  async function sweep(phaseLabel, downDir){
    // scroll しながら探索. 最も大きな scroller を軸に回す + 他の scroller にも補助で scroll 投げる.
    let lastDomCount = 0;
    let stagnantRounds = 0;
    const primary = pickBestScroller(scrollers);
    for (let i = 0; i <= maxScroll; i++) {
      const res = findAndClick();
      if (res.clicked) return { ...res, attempts: i, phase: phaseLabel };
      // DOM 進捗監視
      let domCount = 0;
      try { domCount = document.querySelectorAll('*').length; } catch(_) {}
      if (domCount === lastDomCount) stagnantRounds++; else stagnantRounds = 0;
      lastDomCount = domCount;
      // 方向: 最初は下へ続ける (ユーザ報告: 下スクロールで Baccarat 1 が出る). 10回停滞で上→再度下.
      const dirDown = stagnantRounds < 10 ? downDir : (stagnantRounds < 15 ? !downDir : downDir);
      // primary scroller を先に全力で動かし, 他も補助.
      let anyScrolled = false;
      if (primary) {
        const r = scrollStep(primary, dirDown);
        if (r.changed) anyScrolled = true;
      }
      for (const sc of scrollers) {
        if (sc === primary) continue;
        const r = scrollStep(sc, dirDown);
        if (r.changed) anyScrolled = true;
      }
      // virtual scroll が lazy load する時間を与える (300ms).
      await sleepMs(300);
      if (!anyScrolled && stagnantRounds > 15) break;
    }
    return { clicked: false, attempts: maxScroll, phase: phaseLabel };
  }

  // Pass 1: 現状カテゴリで下方向 sweep
  let r = await sweep('default-down', true);
  if (r.clicked) return { ...r, scrollers: scrollers.length };

  // Pass 2: カテゴリタブを順次 click してから sweep
  for (const cat of categoryPatterns) {
    const hit = clickCategory(cat);
    if (!hit) continue;
    phaseLog.push('cat:' + hit.matched);
    await sleepMs(500);  // カテゴリ切替後 DOM 再構築を待つ
    const q = findAndClick();
    if (q.clicked) return { ...q, phase: 'cat-immediate:' + hit.matched, scrollers: scrollers.length };
    const r2 = await sweep('cat:' + hit.matched, true);
    if (r2.clicked) return { ...r2, scrollers: scrollers.length };
  }

  // 失敗時: lobby の全 tab/button テキストを列挙して返す.
  // 「バカラ/スピード/日本語/その他」以外にカテゴリがあるか可視化するため.
  function scanTabTexts(){
    const labels = [];
    const sel = '[role="tab"], [role="button"], button, a, [data-category], [data-testid*="tab"], [data-testid*="category"], [data-testid*="nav"], [class*="tab"], [class*="Tab"], [class*="category"], [class*="Category"], [class*="nav"], [class*="Nav"]';
    try {
      const els = document.querySelectorAll(sel);
      const seen = new Set();
      for (const el of els) {
        try {
          const t = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
          if (!t || t.length > 40) continue;
          if (seen.has(t)) continue;
          seen.add(t);
          labels.push(t);
          if (labels.length > 60) break;
        } catch(_) {}
      }
    } catch(_) {}
    return labels;
  }
  // 失敗時の診断情報: 現在 DOM に存在する qpid 候補文字列を列挙して返す.
  // これで「該当 qpid が DOM に居ない」のか「居るのに click できていない」のか切り分け可能.
  function scanAllQpids(){
    const qpidRe = /\/snaps\/([A-Za-z0-9]+)\/|[?&]tableId=([A-Za-z0-9]+)/g;
    const seen = new Set();
    const samples = [];
    try {
      const all = document.querySelectorAll('*');
      for (const el of all) {
        try {
          // href / src
          if (el.href) {
            const s = String(el.href);
            let m;
            while ((m = qpidRe.exec(s))) {
              const q = m[1] || m[2];
              if (q) seen.add(q);
            }
          }
          if (el.src) {
            const s = String(el.src);
            let m;
            while ((m = qpidRe.exec(s))) {
              const q = m[1] || m[2];
              if (q) seen.add(q);
            }
          }
          // style attribute
          const style = (el.getAttribute && el.getAttribute('style')) || '';
          if (style) {
            let m;
            while ((m = qpidRe.exec(style))) {
              const q = m[1] || m[2];
              if (q) seen.add(q);
            }
          }
          // data attributes
          const ds = el.dataset || {};
          for (const k of ['qpid', 'tableQpid', 'tableId', 'tableid']) {
            if (ds[k]) seen.add(String(ds[k]));
          }
        } catch(_) {}
      }
    } catch(_) {}
    return Array.from(seen).slice(0, 80);
  }
  const dom_qpids = scanAllQpids();
  const visible_tabs = scanTabTexts();
  return {
    clicked: false,
    attempts: maxScroll,
    scrollers: scrollers.length,
    phaseLog: phaseLog,
    qpid: qpid,
    candidates: normedCands.map(x => x.orig),
    dom_qpids_found: dom_qpids,
    dom_qpid_count: dom_qpids.length,
    dom_total_elements: (function(){try{return document.querySelectorAll('*').length;}catch(_){return -1;}})(),
    visible_tabs: visible_tabs,
  };
}
"""


class _SwitchTableInterrupted(Exception):
    """新しい SWITCH_TABLE decision が到着して現処理を中断したい時に投げる."""
    def __init__(self, new_decision: dict):
        super().__init__(f"interrupted by {new_decision.get('decision_id','?')[-12:]}")
        self.new_decision = new_decision


def _peek_new_switch_decision(q, current_did: str, current_received_at: str = ""):
    """decision_q から**より新しい** SWITCH_TABLE を非破壊で覗く.

    Returns:
        current_received_at より新しい SWITCH_TABLE decision (current_did 以外), または None.
    これにより fetcher が古い pending/processing 決定を再注入した時に誤爆しない.
    """
    import queue as _q
    seen: list[dict] = []
    found: Optional[dict] = None
    try:
        while True:
            d = q.get_nowait()
            seen.append(d)
            fa = d.get("friend_action") or {}
            if not isinstance(fa, dict) or str(fa.get("action") or "").upper() != "SWITCH_TABLE":
                continue
            did = str(d.get("decision_id") or "")
            if not did or did == current_did:
                continue
            d_at = str(d.get("received_at") or d.get("captured_at") or "")
            # current_received_at が明示されていない時は比較不可なので受け入れる (互換).
            if current_received_at and d_at and d_at <= current_received_at:
                continue
            if found is None:
                found = d
    except _q.Empty:
        pass
    for it in seen:
        try: q.put_nowait(it)
        except Exception: pass
    return found


def _join_table(page, *, table_substr: str, auto_click_wait_sec: int, state: Optional[_PragmaticState] = None, on_tick=None, is_initial: bool = False, interrupt_check=None, qpid_table_id: str = "") -> None:
    send_phase("entering", "OPEN STAKE")
    # 初回起動時のみ「Stake ログインしてください」表示. SWITCH_TABLE / recovery
    # から呼ばれた場合は action バーに残ったシグナル表示を上書きしない.
    if is_initial:
        send_action("Opening Stake lobby. If prompted, please log in to Stake.")
    print("[Stage 1] goto stake pragmatic lobby ...", flush=True)
    page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(10_000)

    # Stake loader overlay を除去 (クリック遮断防止)
    _dismiss_stake_loader(page)
    _dismiss_session_elsewhere_modal(page, state)

    print("[Stage 2] wait pragmatic shell ...", flush=True)
    gf = find_game_frame(page)
    if not gf:
        send_phase("entering", "STAKE LOGIN")
        if is_initial:
            send_action("Stake login may be required. Please complete login in the opened browser window.")
        try:
            print(f"[WARN] pragmatic frame not detected yet (url={page.url})", flush=True)
        except Exception:
            print("[WARN] pragmatic frame not detected yet", flush=True)

    if gf:
        page.wait_for_timeout(5_000)
    _dismiss_stake_loader(page)

    print("[Stage 3] find internal lobby (shell-app) ...", flush=True)
    shell = find_shell_app_frame(page)
    if not shell:
        send_phase("entering", "WAIT LOGIN")
        if is_initial:
            send_action("Waiting for Stake lobby... Please finish Stake login in the opened browser window.")
        t0 = time.time()
        last_notice = 0.0
        while not shell:
            _dismiss_session_elsewhere_modal(page, state)
            shell = find_shell_app_frame(page, attempts=1)
            if shell:
                break
            _dismiss_stake_loader(page)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:
                    pass
            if time.time() - last_notice >= 30.0:
                last_notice = time.time()
                try:
                    print(f"[INFO] waiting for shell-app (login) elapsed={time.time()-t0:.0f}s url={page.url}", flush=True)
                except Exception:
                    print(f"[INFO] waiting for shell-app (login) elapsed={time.time()-t0:.0f}s", flush=True)

    def _pick_lobby_frame() -> Any:
        frames = find_lobby_frames(page)
        best = frames[0] if frames else page
        best_cnt = 0
        for fr in frames:
            try:
                cnt = fr.locator('[role="button"]').count()
            except Exception:
                cnt = 0
            if cnt > best_cnt:
                best_cnt = cnt
                best = fr
        return best

    lobby_frame = _pick_lobby_frame()
    # SPA 描画待ち: [role="button"] が出現するまで最大30秒待機
    print("[Stage 3b] waiting for SPA render (role=button elements) ...", flush=True)
    for _w in range(30):
        try:
            cnt = lobby_frame.locator('[role="button"]').count()
            if cnt > 0:
                print(f"[Stage 3b] SPA rendered ({cnt} buttons) after {_w}s", flush=True)
                break
        except Exception:
            pass
        if on_tick is not None:
            try:
                on_tick()
            except Exception:
                pass
        page.wait_for_timeout(1000)

    clicked = False
    table_substr = (table_substr or "").strip()

    # 診断: Stake lobby の DOM を一度ダンプ (テキスト一致失敗の原因特定用).
    def _dump_lobby_diag(label: str) -> None:
        try:
            js = r"""
            () => {
              const out = { anchors: 0, baccarat_anchors: [], buttons: [], texts_with_baccarat: [], iframe_count: 0 };
              const anchors = document.querySelectorAll('a[href]');
              out.anchors = anchors.length;
              for (const a of anchors) {
                const h = a.getAttribute('href') || '';
                if (/baccarat/i.test(h) || /バカラ/.test(h)) {
                  out.baccarat_anchors.push({
                    href: h.slice(0, 100),
                    text: (a.textContent||'').replace(/\s+/g,' ').trim().slice(0, 80),
                    aria: a.getAttribute('aria-label') || '',
                  });
                }
                if (out.baccarat_anchors.length >= 30) break;
              }
              const btns = document.querySelectorAll('[role="button"], button, [role="link"]');
              let bi = 0;
              for (const b of btns) {
                const t = (b.textContent||'').replace(/\s+/g,' ').trim();
                if (!t) continue;
                if (/baccarat|バカラ/i.test(t)) {
                  out.buttons.push({ tag: b.tagName, text: t.slice(0, 80), role: b.getAttribute('role')||'' });
                  if (out.buttons.length >= 30) break;
                }
                bi++; if (bi > 500) break;
              }
              // XPath で baccarat テキストを含むノード探索
              const xp = "//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'baccarat') or contains(text(),'バカラ')]";
              const it = document.evaluate(xp, document, null, XPathResult.UNORDERED_NODE_ITERATOR_TYPE, null);
              let n; let step = 0;
              while ((n = it.iterateNext()) && step < 200) {
                step++;
                const t = (n.textContent||'').replace(/\s+/g,' ').trim();
                if (t && out.texts_with_baccarat.length < 20) {
                  out.texts_with_baccarat.push({ tag: n.tagName, text: t.slice(0, 80), cls: (n.className||'').toString().slice(0, 60) });
                }
              }
              out.iframe_count = document.querySelectorAll('iframe').length;
              out.url = location.href;
              out.title = document.title;
              return out;
            }
            """
            # Try both page and all frames
            results = []
            try:
                r = page.evaluate(js)
                results.append(("page", r))
            except Exception as _e:
                pass
            try:
                for fr in (getattr(page, "frames", []) or []):
                    try:
                        r = fr.evaluate(js)
                        if r and (r.get("baccarat_anchors") or r.get("buttons") or r.get("texts_with_baccarat")):
                            results.append((fr.url[:80], r))
                    except Exception:
                        continue
            except Exception:
                pass
            # ファイル出力 (私が読めるように).
            try:
                import json as _j
                dump_path = Path(__file__).parent / f"debug_lobby_dom_{label}.json"
                with open(dump_path, "w", encoding="utf-8") as f:
                    _j.dump({"results": [{"loc": loc, "data": r} for (loc, r) in results]}, f, ensure_ascii=False, indent=2)
                print(f"[lobby DIAG {label}] saved to {dump_path}", flush=True)
            except Exception as _e2:
                pass
            for (loc, r) in results[:4]:
                try:
                    import json as _j
                    print(f"[lobby DIAG {label}] loc={loc} {_j.dumps(r, ensure_ascii=False)[:1500]}", flush=True)
                except Exception:
                    pass
        except Exception as e:
            print(f"[lobby DIAG {label}] failed: {e}", flush=True)

    # 候補文字列 (EN + JA + ユーザー指定) を列挙.
    candidates = _build_substr_candidates(table_substr) or []
    qpid = str(qpid_table_id or "").strip()
    # qpid があれば文字列候補が空でも実行 (qpid だけで一意特定できる).
    if candidates or qpid:
        print(f"[Stage 4] wait (<= {auto_click_wait_sec}s) qpid={qpid or '-'} candidates={candidates}", flush=True)
        _dump_lobby_diag("initial")
        deadline = time.time() + float(max(auto_click_wait_sec, 1))
        while time.time() < deadline and not clicked:
            # 連打対応: 新しい SWITCH_TABLE が来ていたら即中断.
            if interrupt_check is not None:
                try:
                    new_dec = interrupt_check()
                except Exception:
                    new_dec = None
                if new_dec:
                    try: send_log(f"[switch] interrupted by {str(new_dec.get('decision_id','') or '')[-12:]}")
                    except Exception: pass
                    raise _SwitchTableInterrupted(new_dec)
            _dismiss_stake_loader(page)
            _dismiss_session_elsewhere_modal(page, state)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:
                    pass
            lobby_frames = find_lobby_frames(page)
            clicked_info = None
            _last_diag_attempt = getattr(_join_table, "_last_diag_attempt", {})
            for fr in lobby_frames:
                try:
                    res = fr.evaluate(_LOBBY_TRY_CLICK_JS, {
                        "qpid": qpid,
                        "candidates": candidates,
                        "maxScroll": 30,
                    })
                except Exception:
                    continue
                if isinstance(res, dict) and res.get("clicked"):
                    clicked_info = res
                    try:
                        send_log(
                            f"[lobby] clicked type={res.get('matchType')} via={res.get('via')} "
                            f"match='{res.get('match')}' phase='{res.get('phase','')}' "
                            f"frame={getattr(fr,'url','')[:80]}"
                        )
                    except Exception:
                        pass
                    clicked = True
                    break
                # 診断ログ: click 失敗時に「DOM に qpid 見えているか」を 15s 毎に 1 回だけ出力.
                if isinstance(res, dict) and not res.get("clicked"):
                    _now = time.time()
                    _fu = getattr(fr, "url", "") or ""
                    _last = _last_diag_attempt.get(_fu, 0)
                    if _now - _last > 15:
                        _last_diag_attempt[_fu] = _now
                        try:
                            dom_qs = res.get("dom_qpids_found", []) or []
                            qpid_visible = qpid in dom_qs if qpid else False
                            tabs = res.get("visible_tabs", []) or []
                            send_log(
                                f"[lobby DIAG] no-click frame={_fu[:60]} "
                                f"qpid={qpid or '-'}({'VISIBLE' if qpid_visible else 'NOT_IN_DOM'}) "
                                f"dom_qpid_count={res.get('dom_qpid_count',0)} "
                                f"dom_els={res.get('dom_total_elements',-1)} "
                                f"phaseLog={','.join(res.get('phaseLog',[]) or [])[:120]} "
                                f"sample_qpids={','.join(dom_qs[:12])} "
                                f"tabs={'|'.join(tabs[:20])}"
                            )
                        except Exception as _e:
                            try: send_log(f"[lobby DIAG] log failed: {_e}")
                            except Exception: pass
            _join_table._last_diag_attempt = _last_diag_attempt  # type: ignore
            # NOTE: 以前ここにあった Python 側 text/has-text fallback は削除.
            # Playwright の get_by_text / has-text は substring 判定なので,
            # 候補 "バカラ1" が "日本語スピードバカラ 1" に誤ヒットして
            # 間違うテーブルに入ってしまう主因だった (実銭BET 時の致命リスク).
            # JS matcher (Pass A strict) で見つからなかったら, そのまま
            # 次のスクロール iteration に進めて "見つからないなら timeout" で失敗させる.

            # NOTE: 旧フォールバック「60s 経過後 nth(1) 無条件 click」を削除.
            # 候補が 1 つも match しない場合に lobby の 2 番目ボタン (= Speed Baccarat 6)
            # を誤クリックして SWITCH_TABLE を破綻させる主因だった.
            # 代わりに候補が match しないまま timeout したらエラーで返し,
            # 上位の SWITCH_TABLE が明示的に失敗として扱う.

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
    ap.add_argument("--chip-base", type=float, default=0.0, help="Base bet ($) for SEQ7 (falls back to --flat-amount)")
    ap.add_argument("--profit-target", type=float, default=50.0, help="Session profit target in $ (converted to chips by chip_base)")
    ap.add_argument("--profit-session-limit", type=int, default=0, help="Stop after N profit resets (0=unlimited)")
    ap.add_argument("--loss-cut", type=float, default=200.0, help="Session loss cut in $ (converted to chips by chip_base)")
    ap.add_argument("--only-table-id", default=os.getenv("BACOPY_ONLY_TABLE_ID", ""), help="operator tableId (numeric) to accept")
    ap.add_argument("--bet-timeout-sec", type=int, default=20)
    ap.add_argument("--min-timer-sec", type=float, default=2.0, help="Refuse bets if timer is below this (when available)")
    ap.add_argument("--result-timeout-sec", type=int, default=90)
    ap.add_argument("--allow-switch-table", action="store_true", help="Allow SWITCH_TABLE action to navigate/click table")
    ap.add_argument(
        "--ws-silence-sec",
        type=float,
        default=float(os.getenv("BACOPY_WS_SILENCE_SEC", "180") or 180),
        help="Auto-recover if no WS recv frames for this many seconds (0=disabled)",
    )
    ap.add_argument(
        "--ws-recover-cooldown-sec",
        type=float,
        default=float(os.getenv("BACOPY_WS_RECOVER_COOLDOWN_SEC", "60") or 60),
        help="Minimum seconds between auto-recover attempts",
    )
    ap.add_argument("--allow-banker", action="store_true", help="Allow BANKER bets (experimental)")
    ap.add_argument("--allow-tie", action="store_true", help="Allow TIE bets (experimental)")
    ap.add_argument("--assume-bc-012", action="store_true", help="Assume bc mapping PLAYER=0,BANKER=1,TIE=2 (unsafe; prefer sniff+env)")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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

    decision_q: queue.Queue[dict[str, Any]] = queue.Queue()
    stop_fetcher = threading.Event()
    seen_ids_order: deque[str] = deque()
    seen_ids_set: set[str] = set()

    def _seen_add(did: str, *, maxlen: int = 2000) -> bool:
        if not did:
            return False
        if did in seen_ids_set:
            return False
        seen_ids_set.add(did)
        seen_ids_order.append(did)
        while len(seen_ids_order) > maxlen:
            old = seen_ids_order.popleft()
            seen_ids_set.discard(old)
        return True

    def _decision_fetcher() -> None:
        # Initial drain: crash-safe resume (processing first), then pending.
        try:
            for st in ("processing", "pending"):
                for d in _fetch_decisions(st, limit=int(args.limit)):
                    if not isinstance(d, dict):
                        continue
                    if str(d.get("provider") or "") != "pragmatic":
                        continue
                    did = str(d.get("decision_id") or "")
                    if not did:
                        continue
                    tgt = str(d.get("target_executor_id") or "")
                    if tgt and tgt != executor_id:
                        continue
                    if args.only_table_id:
                        dtid = str(d.get("table_id") or "")
                        if dtid and str(args.only_table_id) != dtid:
                            continue
                    decision_q.put(d)
        except Exception:
            pass

        backoff = 0.5
        while not stop_fetcher.is_set():
            wait_sec = max(5.0, float(os.getenv("BACOPY_DECISION_WAIT_SEC", "20") or "20"))
            started = time.time()
            items = _wait_decisions(
                status="pending",
                limit=int(args.limit),
                provider="pragmatic",
                executor_id=executor_id,
                wait_sec=wait_sec,
            )
            elapsed = time.time() - started
            if not items:
                # If it returned too quickly (network/DNS error), backoff to avoid hot-loop.
                if elapsed < 1.0:
                    time.sleep(backoff)
                    backoff = min(8.0, backoff * 2.0)
                else:
                    backoff = 0.5
                continue
            for d in items:
                if not isinstance(d, dict):
                    continue
                did = str(d.get("decision_id") or "")
                if not did:
                    continue
                # Don't drop duplicates here; ack timing can cause pending re-appearance. Main loop dedupes.
                decision_q.put(d)
            backoff = 0.5
            # A safety sleep prevents tight loops if server returns instantly with same pending items.
            time.sleep(0.05)

    atexit.register(lambda: stop_fetcher.set())
    threading.Thread(target=_decision_fetcher, name="decision_fetcher", daemon=True).start()

    chip_base = float(args.chip_base) if float(args.chip_base or 0) > 0 else float(args.flat_amount or 1.0)
    profit_stop_chips = max(1, int(round(float(args.profit_target) / max(chip_base, 0.01))))
    loss_cut_chips = max(1, int(round(float(args.loss_cut) / max(chip_base, 0.01))))
    seq7 = Seq7Session(
        chip_base=chip_base,
        profit_stop_chips=profit_stop_chips,
        loss_cut_chips=loss_cut_chips,
        profit_session_limit=int(args.profit_session_limit or 0),
        state_path=Path(args.profile_dir) / "seq7_state.json",
    )
    bet_currency = (os.getenv("BACOPY_BET_CURRENCY", "USD") or "USD").strip().upper()

    # Master connection / activity monitor (for GUI)
    master_last_ok_ts = 0.0
    master_last_ok_at = ""
    master_last_err = ""
    master_last_decision_id = ""
    master_last_decision_action = ""
    master_last_decision_at = ""
    master_last_active_ts = 0.0
    master_pending_for_me = 0
    master_prev_active: Optional[bool] = None

    hb_lock = threading.Lock()
    hb_latest_payload: dict[str, Any] = {}
    hb_stop = threading.Event()
    hb_last_sent = 0.0

    def _hb_sender() -> None:
        nonlocal hb_last_sent
        nonlocal master_last_ok_ts, master_last_ok_at, master_last_err
        while not hb_stop.is_set():
            time.sleep(0.25)
            now2 = time.time()
            # 1s 間隔で heartbeat POST. Master UI の反映速度を 5 倍向上.
            if now2 - hb_last_sent < 1.0:
                continue
            with hb_lock:
                payload = dict(hb_latest_payload) if hb_latest_payload else None
            if not payload:
                continue
            ok, err = _post_heartbeat(payload)
            hb_last_sent = now2
            if ok:
                master_last_ok_ts = now2
                master_last_ok_at = _utc_now_iso()
                master_last_err = ""
            elif err:
                master_last_err = err

    atexit.register(lambda: hb_stop.set())
    threading.Thread(target=_hb_sender, name="hb_sender", daemon=True).start()

    send_log(
        f"Config: chip_base=${chip_base:.2f} profit_target=${float(args.profit_target):.0f} "
        f"(={profit_stop_chips} chips) loss_cut=${float(args.loss_cut):.0f} (={loss_cut_chips} chips)"
    )
    send_phase("idle", "ARMED")
    send_action("Armed. Waiting for master signal...")
    try:
        send_msg({"type": "shoe_history", "sets": [s.__dict__ for s in seq7.tracker.sets], "chip_base": chip_base})
    except Exception:
        pass

    last_error = ""
    last_hb = 0.0

    def heartbeat(status: str) -> None:
        nonlocal last_hb
        nonlocal master_last_ok_ts, master_last_ok_at, master_last_err
        nonlocal master_last_decision_id, master_last_decision_action, master_last_decision_at
        nonlocal master_last_active_ts, master_pending_for_me, master_prev_active
        now = time.time()
        # 24/7 運用でレスポンシビリティ重視: 1s 間隔. state 変化即座に Master UI へ.
        if now - last_hb < 1.0:
            return
        last_hb = now
        user_email = (os.getenv("BACOPY_USER_EMAIL", "") or os.getenv("BACOPY_BAFATHER_EMAIL", "") or "").strip()
        user_id = (os.getenv("BACOPY_USER_ID", "") or "").strip()
        os_name = (os.getenv("BACOPY_OS", "") or "").strip() or sys.platform
        phase_name = ""
        phase_detail = ""
        try:
            ph = str(_LAST_PHASE[0] or "")
            if "|" in ph:
                phase_name, phase_detail = ph.split("|", 1)
        except Exception:
            pass
        bal = state.stake_balance_by_currency.get(bet_currency)
        if bal is None and state.stake_balance_by_currency:
            # fallback: configured bet_currency が Stake 上に存在しない時は
            # 「今最も残高が多い通貨」を active 通貨として採用して表示する.
            # (Stake は通常 USD/BTC/ETH/USDT など複数通貨を返すので, USD 固定だと
            #  BTC プレイヤーで残高 0 表示になる原因だった).
            try:
                # USD 優先 → その次に額の大きい通貨.
                if "USD" in state.stake_balance_by_currency:
                    bal = state.stake_balance_by_currency.get("USD")
                else:
                    pairs = sorted(state.stake_balance_by_currency.items(), key=lambda kv: float(kv[1] or 0), reverse=True)
                    if pairs:
                        bal = pairs[0][1]
            except Exception:
                try:
                    bal = next(iter(state.stake_balance_by_currency.values()))
                except Exception:
                    pass
        try:
            seq7.update_balance(bal)
        except Exception:
            pass
        seq7_payload: dict[str, Any] = {}
        try:
            seq7_payload = seq7.status_payload()
            send_msg(seq7_payload)
        except Exception:
            pass

        connected = bool(master_last_ok_ts and (now - master_last_ok_ts) < 45.0)
        active = connected and (master_pending_for_me > 0 or (master_last_active_ts and (now - master_last_active_ts) < 30.0))
        if master_prev_active is None:
            master_prev_active = active
        elif master_prev_active != active:
            master_prev_active = active
            try:
                send_log("Master control started." if active else "Master control stopped.")
            except Exception:
                pass

        try:
            send_msg(
                {
                    "type": "master_status",
                    "connected": bool(connected),
                    "active": bool(active),
                    "pending": int(master_pending_for_me),
                    "last_ok_at": master_last_ok_at,
                    "last_error": master_last_err,
                    "last_decision_id": master_last_decision_id,
                    "last_decision_action": master_last_decision_action,
                    "last_decision_at": master_last_decision_at,
                }
            )
        except Exception:
            pass
        ws_silence_sec = (now - float(state.last_ws_recv_at or 0)) if state.last_ws_recv_at else None
        ws_silence_limit = float(args.ws_silence_sec or 0)
        # Bettable gating should be stricter than full auto-recover threshold.
        # If WS has been silent for > ~30s, treat as not bettable (prevents "send into dead WS").
        bettable_silence_sec = 0.0
        try:
            bettable_silence_sec = float(os.getenv("BACOPY_BETTABLE_SILENCE_SEC", "") or 0)
        except Exception:
            bettable_silence_sec = 0.0
        if bettable_silence_sec <= 0 and ws_silence_limit > 0:
            bettable_silence_sec = max(15.0, min(30.0, ws_silence_limit * 0.5))
        ws_dead = bool(bettable_silence_sec > 0 and ws_silence_sec is not None and ws_silence_sec > bettable_silence_sec)
        daily_pnl_val = None
        daily_pnl_date_val = ""
        try:
            if isinstance(seq7_payload, dict):
                if seq7_payload.get("daily_pnl") is not None:
                    daily_pnl_val = float(seq7_payload.get("daily_pnl") or 0)
                if seq7_payload.get("daily_open_date"):
                    daily_pnl_date_val = str(seq7_payload.get("daily_open_date") or "")
        except Exception:
            pass
        payload = {
            "executor_id": executor_id,
            "label": executor_label,
            "username": executor_username,
            "user_email": user_email,
            "user_id": user_id,
            "os": os_name,
            "provider": "pragmatic",
            "table_id": state.operator_table_id,
            "table_name": state.table_name,
            "balance": bal,
            "daily_pnl": daily_pnl_val,
            "daily_pnl_date": daily_pnl_date_val,
            "phase": {"name": phase_name, "detail": phase_detail},
            "gui": seq7_payload,
            "seq": {
                "mode": "counter_seq7",
                "chip_base": chip_base,
                "unit_idx": seq7.tracker.current_unit_idx,
                "unit": seq7.bet_unit(),
                "bet_amount": seq7.bet_amount(),
                "turn": len(seq7.tracker.current_turns) + 1,
                "overshoot": getattr(seq7.tracker, "prev_overshoot", 0),
            },
            "caps": {
                "allow_switch_table": bool(args.allow_switch_table),
                "allow_banker": bool(args.allow_banker),
                "allow_tie": bool(args.allow_tie),
                "assume_bc_012": bool(args.assume_bc_012),
            },
            "bettable": bool(
                state.table_id
                and state.user_id
                and state.game_ws_url
                and not state.session_elsewhere_unresolved
                and not state.inactivity_modal_unresolved
                and not ws_dead
                and not state.recover_exhausted
            ),
            "session_elsewhere_unresolved": bool(state.session_elsewhere_unresolved),
            "inactivity_modal_unresolved": bool(state.inactivity_modal_unresolved),
            "inactivity_dismissed_count": int(state.inactivity_dismissed_count or 0),
            "recovering": bool(state.recovering),
            "recovering_reason": str(state.recovering_reason or ""),
            "recovering_started_at": float(state.recovering_started_at or 0),
            "keep_alive_last_at": float(state.keep_alive_last_at or 0),
            "ws": {
                "last_recv_at": state.last_ws_recv_at,
                "silence_sec": ws_silence_sec,
                "last_game_recv_at": state.last_game_ws_recv_at,
                "last_lobby_recv_at": state.last_lobby_ws_recv_at,
                "last_stake_recv_at": state.last_stake_ws_recv_at,
                "bettable_silence_sec": bettable_silence_sec,
                "recover_exhausted": bool(state.recover_exhausted),
                "recover_attempts": int(state.recover_attempts or 0),
                "recover_exhausted_at": float(state.recover_exhausted_at or 0),
            },
            "status": status,
            "error": last_error,
        }
        with hb_lock:
            hb_latest_payload.clear()
            hb_latest_payload.update(payload)

    def on_ws(ws):
        url = str(ws.url or "")
        if "pragmaticplaylive.net/game" in url:
            state.game_ws_url = url

        def on_recv(frame_data):
            try:
                now = time.time()
                state.last_ws_recv_at = now
                if "dga.pragmaticplaylive.net/ws" in url:
                    state.last_lobby_ws_recv_at = now
                elif "pragmaticplaylive.net/game" in url:
                    state.last_game_ws_recv_at = now
                elif "stake.com/_api/websockets" in url:
                    state.last_stake_ws_recv_at = now
            except Exception:
                pass
            p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
            obj = _maybe_json(p)
            if not obj:
                try:
                    if isinstance(p, bytes):
                        p = p.decode("utf-8", errors="replace")
                except Exception:
                    pass
                if isinstance(p, str) and "<" in p and "pragmaticplaylive.net/game" in url:
                    _update_from_game_xml(state, p)
                return
            if "stake.com/_api/websockets" in url:
                _update_from_stake_ws_msg(state, obj)
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
        try:
            now = time.time()
            state.last_ws_recv_at = now
            state.last_lobby_ws_recv_at = now
        except Exception:
            pass
        p = frame_data.payload if hasattr(frame_data, "payload") else frame_data
        obj = _maybe_json(p)
        if obj:
            _update_from_lobby_msg(state, obj)

    with Camoufox(
        headless=bool(args.headless),
        persistent_context=True,
        user_data_dir=str(Path(args.profile_dir)),
    ) as ctx:
        # SIGTERM/SIGINT で Camoufox を clean close (orphan camoufox プロセス防止).
        def _clean_shutdown(signum=None, frame=None):
            print(f"[exec] shutdown signal {signum} - closing camoufox ...", flush=True)
            # 最終 heartbeat で status="stopped" を送信 → Master UI が即座にオフライン表示.
            try:
                with hb_lock:
                    final_payload = dict(hb_latest_payload) if hb_latest_payload else {}
                if final_payload:
                    final_payload["status"] = "stopped"
                    final_payload["bettable"] = False
                    _post_heartbeat(final_payload)
                    print("[exec] final stopped heartbeat posted", flush=True)
            except Exception as _e:
                print(f"[exec] final heartbeat failed: {_e}", flush=True)
            try:
                ctx.close()
            except Exception:
                pass
            try:
                hb_stop.set()
            except Exception:
                pass
            try:
                stop_fetcher.set()
            except Exception:
                pass
            # os._exit で残存スレッドを強制終了 (atexit もここで発火).
            os._exit(0)

        # Normal exit path (uncaught exception 等) でも atexit で status=stopped を送る.
        def _atexit_notify_stopped():
            try:
                with hb_lock:
                    fp = dict(hb_latest_payload) if hb_latest_payload else {}
                if fp:
                    fp["status"] = "stopped"
                    fp["bettable"] = False
                    _post_heartbeat(fp)
            except Exception:
                pass
        try:
            atexit.register(_atexit_notify_stopped)
        except Exception:
            pass
        try:
            import signal as _signal
            _signal.signal(_signal.SIGTERM, _clean_shutdown)
            _signal.signal(_signal.SIGINT, _clean_shutdown)
            # Windows で CTRL_BREAK も拾う
            if hasattr(_signal, "SIGBREAK"):
                _signal.signal(_signal.SIGBREAK, _clean_shutdown)
        except Exception as _e:
            print(f"[exec] signal handler install failed: {_e}", flush=True)
        # persistent_context=True creates a default Page; using ctx.new_page() would open a 2nd window.
        # Keep a single window to avoid Stake session conflicts / user confusion.
        try:
            pages = list(getattr(ctx, "pages", []) or [])
        except Exception:
            pages = []
        page = pages[0] if pages else ctx.new_page()
        for p in pages[1:]:
            try:
                p.close()
            except Exception:
                pass

        # ==== 最終安全網: Stake 以外のドメインへ遷移した tab/page は即閉じる ====
        # (誤クリックで Facebook/Google ログインページが開くのを物理的に遮断)
        _FOREIGN_HOSTS = re.compile(
            r"(^|\.)(facebook\.com|accounts\.google\.com|appleid\.apple\.com|"
            r"twitter\.com|x\.com|metamask\.io|telegram\.me|t\.me|yahoo\.co\.jp)$",
            re.IGNORECASE,
        )

        def _is_foreign(url: str) -> bool:
            try:
                from urllib.parse import urlparse
                host = (urlparse(url).hostname or "").lower()
                return bool(_FOREIGN_HOSTS.search(host))
            except Exception:
                return False

        def _on_new_page(new_page):
            try:
                url = new_page.url or ""
            except Exception:
                url = ""
            if _is_foreign(url):
                try:
                    send_log(f"[safety] foreign new tab blocked url={url[:120]}")
                except Exception:
                    pass
                try: new_page.close()
                except Exception: pass
                return
            # Wait briefly for initial navigation then recheck
            def _recheck():
                try:
                    new_page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                try:
                    u = new_page.url or ""
                except Exception:
                    u = ""
                if _is_foreign(u):
                    try:
                        send_log(f"[safety] foreign nav detected on new tab — closing url={u[:120]}")
                    except Exception:
                        pass
                    try: new_page.close()
                    except Exception: pass
            import threading as _t
            _t.Thread(target=_recheck, daemon=True).start()

        def _on_framenavigated(frame):
            try:
                u = frame.url or ""
            except Exception:
                u = ""
            if "pragmaticplaylive" in u or "qpidreoxcc.net" in u:
                try:
                    frame.evaluate(_WS_BRIDGE_INIT)
                except Exception:
                    pass
            try:
                if frame != getattr(frame, "page", None) and frame.parent_frame is not None:
                    return  # child frame — ignore for navigation safety checks
            except Exception:
                pass
            if _is_foreign(u):
                try:
                    send_log(f"[safety] main-frame foreign nav — go_back url={u[:120]}")
                except Exception:
                    pass
                try:
                    frame.page.go_back(timeout=5000)
                except Exception:
                    try:
                        frame.page.goto("https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat", timeout=15000)
                    except Exception:
                        pass

        try:
            ctx.on("page", _on_new_page)
        except Exception as _e:
            try: send_log(f"[safety] ctx.on(page) install failed: {_e}")
            except Exception: pass
        try:
            page.on("framenavigated", _on_framenavigated)
        except Exception:
            pass
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

        # Enter lobby and (attempt to) join table (initial startup path).
        # 初期テーブルは以下の優先順で決定:
        #   1. Master API の最新 done SWITCH_TABLE decision (admin が最後に選んだ卓)
        #   2. env BACOPY_TABLE_SUBSTR (GUI Settings)
        #   3. CLI default "Speed Baccarat"
        # これにより「GUI スタートで毎回 Speed Baccarat 6 に戻る」問題を解消.
        def _fetch_master_last_table() -> tuple[str, str]:
            """最新 done SWITCH_TABLE から (table_name, qpid_table_id) を返す.

            - decisions API は ASC (古い順) で返すので captured_at 降順にソート.
            - decision root の qpid_table_id を優先 (Master UI 新実装).
            - 無ければ decision snapshot.qpid_table_id を fallback.
            - それでも無ければ snapshots API を引いて operator_table_id から qpid を補完.
            """
            try:
                import urllib.request as _ur
                base = (os.getenv("BACOPY_API_URL") or "https://master.bafather.uk").rstrip("/")
                key = os.getenv("BACOPY_API_KEY") or ""
                req = _ur.Request(
                    f"{base}/api/decisions?status=done&limit=200",
                    headers={"Authorization": f"Bearer {key}"},
                )
                with _ur.urlopen(req, timeout=5) as resp:
                    data = json.load(resp)
                decisions = data.get("decisions", []) or []
                decisions.sort(key=lambda x: (x.get("captured_at") or x.get("received_at") or ""), reverse=True)
                for d in decisions:
                    fa = d.get("friend_action") or {}
                    if not (isinstance(fa, dict) and str(fa.get("action") or "").upper() == "SWITCH_TABLE"):
                        continue
                    tn = str(d.get("table_name") or "").strip()
                    if not tn:
                        continue
                    qpid = str(d.get("qpid_table_id") or "").strip()
                    if not qpid:
                        snap = d.get("snapshot") if isinstance(d.get("snapshot"), dict) else None
                        if snap:
                            qpid = str(snap.get("qpid_table_id") or "").strip()
                    if not qpid:
                        # 最終 fallback: snapshots API から operator_table_id で逆引き
                        try:
                            tid = str(d.get("table_id") or "").strip()
                            if tid:
                                req2 = _ur.Request(
                                    f"{base}/api/snapshots?provider=pragmatic&table_id={tid}",
                                    headers={"Authorization": f"Bearer {key}"},
                                )
                                with _ur.urlopen(req2, timeout=5) as r2:
                                    snap2 = json.load(r2).get("snapshot") or {}
                                qpid = str(snap2.get("qpid_table_id") or "").strip()
                        except Exception:
                            pass
                    if qpid:
                        print(f"[startup] resolved qpid for '{tn}': {qpid}", flush=True)
                    return tn, qpid
            except Exception:
                pass
            return "", ""

        _master_target, _master_qpid = _fetch_master_last_table()
        _initial_substr = _master_target or str(args.table_name_substr or "")
        if _master_target:
            try: send_log(f"[startup] using Master UI last target: {_master_target} qpid={_master_qpid or '-'}")
            except Exception: pass
        _join_table(
            page,
            table_substr=_initial_substr,
            qpid_table_id=_master_qpid,
            auto_click_wait_sec=int(args.auto_click_wait_sec),
            state=state,
            on_tick=lambda: heartbeat("running"),
            is_initial=True,
        )

        # Ensure WS bridge exists in the pragmatic iframe context (send must be evaluated in-frame).
        game_frame = find_game_frame(page, attempts=60)
        if game_frame:
            try:
                game_frame.evaluate(_WS_BRIDGE_INIT)
            except Exception:
                pass

        # Wait until we have game ws + chat mapping (user_id/table_id/jsession/operator_table_id)
        print("[Stage 5] waiting for Pragmatic session identifiers ...", flush=True)
        send_phase("entering", "WAIT TABLE")
        send_action("Waiting for Stake table entry... Please enter a baccarat table in the opened browser window.")
        t0 = time.time()
        last_notice = 0.0
        while not (state.game_ws_url and state.table_id and state.user_id and state.jsession_id):
            _dismiss_session_elsewhere_modal(page, state)
            try:
                game_frame = _refresh_game_frame(page, game_frame)
            except Exception:
                pass
            try:
                _pump_ws_events(page, game_frame, state)
            except Exception:
                pass
            try:
                heartbeat("running")
            except Exception:
                pass

            if time.time() - last_notice >= 30.0:
                last_notice = time.time()
                try:
                    print(f"[Stage 5] waiting... elapsed={time.time()-t0:.0f}s url={page.url}", flush=True)
                except Exception:
                    print(f"[Stage 5] waiting... elapsed={time.time()-t0:.0f}s", flush=True)
                send_action("Waiting for Stake table entry... (login/click a table in the browser window)")
            page.wait_for_timeout(500)

        send_phase("idle", "ARMED")
        send_action("Armed. Waiting for master signal...")
        # Telegram 通知: ARMED 到達 (友人が遠隔でスタート状態を確認できる).
        try:
            from notify import send_user_notification as _notify
            _notify(f"✅ bacopy ARMED | table={state.table_name or '?'} | waiting for master signal")
        except Exception:
            pass
        if state.game_ws_url:
            print(f"[session] game_ws={_redact_jsession(state.game_ws_url)}", flush=True)
        if state.operator_table_id:
            print(f"[session] operator_table_id={state.operator_table_id} table_name={state.table_name}", flush=True)
        print(f"[executor-live] executor_id={executor_id}", flush=True)
        if state.game_ws_url:
            # Keep an always-controllable WS in the top page context too (iframe can reload/detach).
            try:
                page.evaluate(_WS_BRIDGE_INIT)
            except Exception:
                pass
            try:
                page.evaluate("(u) => (window.__bacopy_ws_open ? window.__bacopy_ws_open(u) : null)", state.game_ws_url)
            except Exception:
                pass
            if game_frame:
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
                nonlocal game_frame
                game_frame = _refresh_game_frame(page, game_frame)
                try:
                    if state.game_ws_url:
                        page.evaluate("(u) => (window.__bacopy_ws_open ? window.__bacopy_ws_open(u) : null)", state.game_ws_url)
                except Exception:
                    pass
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

            ok = _wait_for(_pred, timeout_sec=timeout_sec, tick_ms=200, page=page, on_tick=lambda: heartbeat("running"))
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
                # Explicit server-side bet error (if any) should short-circuit.
                if isinstance(state.last_bet_confirm, dict) and state.last_bet_confirm.get("type") == "xml_error":
                    return True

                if state.last_stake_balance_at < start - 0.5:
                    return False

                cur = str(currency or "").strip().upper()
                if cur and cur not in state.stake_balance_by_currency and state.stake_balance_by_currency:
                    # If user didn't set BACOPY_BET_CURRENCY correctly, fall back only when unambiguous.
                    if len(state.stake_balance_by_currency) == 1:
                        cur = next(iter(state.stake_balance_by_currency.keys()))

                after = state.stake_balance_by_currency.get(cur) if cur else None
                delta = state.stake_balance_delta_by_currency.get(cur) if cur else None

                # Prefer delta-based confirmation (doesn't require prior snapshot).
                if delta is not None and float(delta) <= -max(0.0, float(bet_amount) * 0.9):
                    bb = before_balance
                    if bb is None and after is not None:
                        try:
                            bb = float(after) - float(delta)
                        except Exception:
                            bb = None
                    state.last_bet_confirm = {
                        "type": "stake_delta",
                        "currency": cur,
                        "delta": float(delta),
                        "before": bb,
                        "after": after,
                    }
                    return True

                # Fallback: if we do have before_balance, use absolute drop.
                if before_balance is not None and after is not None:
                    if (float(before_balance) - float(after)) >= max(0.0, float(bet_amount) * 0.9):
                        state.last_bet_confirm = {
                            "type": "stake_balance",
                            "currency": cur,
                            "before": float(before_balance),
                            "after": float(after),
                        }
                        return True

                return False

            _wait_for(_pred, timeout_sec=timeout_sec, tick_ms=200, page=page, on_tick=lambda: heartbeat("running"))
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

            def _pred() -> bool:
                _pump_ws_events(page, game_frame, state)
                return _winner() is not None

            ok = _wait_for(_pred, timeout_sec=timeout_sec, tick_ms=250, page=page, on_tick=lambda: heartbeat("running"))
            return _winner() if ok else None

        desired_table_substr = str(args.table_name_substr or state.table_name or "").strip()

        last_recover_at = 0.0
        recover_attempts = 0
        recover_exhausted = False
        try:
            max_recover_attempts = int(os.getenv("BACOPY_MAX_RECOVER_ATTEMPTS", "5") or "5")
        except Exception:
            max_recover_attempts = 5

        def _clear_pragmatic_session_state() -> None:
            # identifiers
            state.operator_table_id = ""
            state.table_name = ""
            state.table_id = ""
            state.user_id = ""
            state.jsession_id = ""
            state.game_ws_url = ""

            # betting phase cache
            state.current_game_id = ""
            state.bets_open_game_id = ""
            state.bets_closed_game_id = ""
            state.last_timer = ""
            state.last_bets_open_at = 0.0
            state.last_bets_closed_at = 0.0
            state.last_bet_confirm = None
            state.expected_bet_ck = ""

            # dga subscribe cache must be cleared (new WS instance after reload)
            try:
                state.dga_subscribed_keys.clear()
            except Exception:
                state.dga_subscribed_keys = set()
            state.dga_last_subscribe_at = 0.0
            try:
                state._seen_table_game.clear()
            except Exception:
                state._seen_table_game = set()

        def recover_session(reason: str) -> bool:
            nonlocal game_frame
            nonlocal last_error, consecutive_hard_errors
            nonlocal last_recover_at, recover_attempts, recover_exhausted, max_recover_attempts

            # Master 画面に「復旧中」を即時反映
            state.recovering = True
            state.recovering_reason = str(reason or "recover_session")
            state.recovering_started_at = time.time()
            try:
                heartbeat("running")
            except Exception:
                pass
            if recover_exhausted:
                state.recovering = False
                return False
            if max_recover_attempts > 0 and recover_attempts >= max_recover_attempts:
                recover_exhausted = True
                state.recover_exhausted = True
                state.recover_attempts = int(recover_attempts)
                state.recover_exhausted_at = time.time()
                last_error = f"recover attempts exhausted ({recover_attempts}) — manual intervention required"
                try:
                    send_action(last_error)
                except Exception:
                    pass
                heartbeat("error")
                return False

            last_recover_at = time.time()
            started_at = last_recover_at
            recover_attempts += 1
            state.recover_attempts = int(recover_attempts)

            target = str(desired_table_substr or state.table_name or "").strip()
            send_phase("entering", "RECOVER")
            send_action(
                f"Recovering session ({reason})... "
                "If Stake login / table click is required, please operate the opened browser window."
            )
            last_error = f"recovering: {reason}"
            heartbeat("error")

            try:
                _clear_pragmatic_session_state()
            except Exception:
                pass

            try:
                # recovery 時も qpid を渡せば文字列マッチ不要で確実.
                _recover_qpid = str(state.table_id or "").strip() if state else ""
                _join_table(
                    page,
                    table_substr=target,
                    qpid_table_id=_recover_qpid,
                    auto_click_wait_sec=int(args.auto_click_wait_sec),
                    state=state,
                    on_tick=lambda: heartbeat("running"),
                )
                game_frame = find_game_frame(page, attempts=60)
                if game_frame:
                    try:
                        game_frame.evaluate(_WS_BRIDGE_INIT)
                    except Exception:
                        pass

                def _pred_ids() -> bool:
                    nonlocal game_frame
                    game_frame = _refresh_game_frame(page, game_frame)
                    _pump_ws_events(page, game_frame, state)
                    return bool(state.game_ws_url and state.table_id and state.user_id and state.jsession_id)

                ok = _wait_for(_pred_ids, timeout_sec=180, tick_ms=500, page=page, on_tick=lambda: heartbeat("running"))
                if not ok:
                    raise RuntimeError("session identifiers not populated (table/user/ws missing)")

                # Keep WS controllable in top + game frame contexts (iframe can reload/detach).
                if state.game_ws_url:
                    try:
                        page.evaluate(_WS_BRIDGE_INIT)
                    except Exception:
                        pass
                    try:
                        page.evaluate("(u) => (window.__bacopy_ws_open ? window.__bacopy_ws_open(u) : null)", state.game_ws_url)
                    except Exception:
                        pass
                    if game_frame:
                        try:
                            game_frame.evaluate(_WS_BRIDGE_INIT)
                        except Exception:
                            pass
                        try:
                            game_frame.evaluate(
                                "(u) => (window.__bacopy_ws_open ? window.__bacopy_ws_open(u) : null)",
                                state.game_ws_url,
                            )
                        except Exception:
                            pass

                # Do not mark "WS recv" here — only actual framereceived callbacks should bump last_ws_recv_at.
                # Instead, verify we observed at least one ws recv after starting recovery.
                ok_ws = False
                t_ws = time.time()
                while time.time() - t_ws < 10.0:
                    try:
                        _pump_ws_events(page, game_frame, state)
                    except Exception:
                        pass
                    try:
                        heartbeat("running")
                    except Exception:
                        pass
                    try:
                        if state.last_ws_recv_at and float(state.last_ws_recv_at) >= started_at:
                            ok_ws = True
                            break
                    except Exception:
                        pass
                    try:
                        page.wait_for_timeout(200)
                    except Exception:
                        time.sleep(0.2)
                if not ok_ws:
                    raise RuntimeError("ws did not receive frames after recover (still silent)")

                consecutive_hard_errors = 0
                last_error = ""
                recover_attempts = 0
                recover_exhausted = False
                state.recover_exhausted = False
                state.recover_attempts = 0
                state.recovering = False
                state.recovering_reason = ""
                state.recover_exhausted_at = 0.0
                send_phase("idle", "ARMED")
                send_action("Recovered. Waiting for master signal...")
                heartbeat("running")
                return True
            except Exception as e:
                last_error = f"recover failed: {e}"
                state.recovering = False
                heartbeat("error")
                try:
                    send_action(last_error)
                except Exception:
                    pass
                return False

        while True:
            heartbeat("running")
            try:
                _dismiss_session_elsewhere_modal(page, state)
            except Exception:
                pass
            # ハードセッションタイムアウト (OK 押すと TOP に飛ぶ危険) を検出して lobby goto.
            try:
                _dismiss_session_ended_modal(page, state)
            except Exception:
                pass
            # 自動復旧: 無操作モーダル (Stake のタイムアウト) を検出してクリック.
            try:
                _dismiss_inactivity_modal(page, state)
            except Exception:
                pass
            # 予防: 一定間隔で微小 gesture を送り inactivity 判定を回避.
            try:
                _send_keep_alive(page, state)
            except Exception:
                pass
            try:
                game_frame = _refresh_game_frame(page, game_frame)
                _pump_ws_events(page, game_frame, state)
            except Exception:
                pass

            # SESSION EXPIRED / WS dead detection: if no recv frames for long, try to reload + re-join.
            try:
                ws_silence = time.time() - float(state.last_ws_recv_at or 0)
                if float(args.ws_silence_sec or 0) > 0 and state.last_ws_recv_at and ws_silence > float(args.ws_silence_sec or 0):
                    # If Stake explicitly says the session was taken elsewhere, do not thrash recovery.
                    if state.session_elsewhere_unresolved:
                        last_error = "ws silent + session taken elsewhere (blocked)"
                        heartbeat("error")
                    elif not recover_exhausted and time.time() - float(last_recover_at or 0) >= float(args.ws_recover_cooldown_sec or 60):
                        recover_session(f"ws silent {int(ws_silence)}s")
                        continue
            except Exception:
                pass

            # Decisions are fetched via long-poll in a background thread (reduces DNS churn + timeouts).
            try:
                d0 = decision_q.get(timeout=max(float(args.poll_sec), 0.2))
                items = [d0]
                while len(items) < int(args.limit):
                    try:
                        items.append(decision_q.get_nowait())
                    except queue.Empty:
                        break
                master_pending_for_me = decision_q.qsize() + len(items)
            except queue.Empty:
                master_pending_for_me = decision_q.qsize()
                if args.once:
                    break
                page.wait_for_timeout(int(max(args.poll_sec, 0.2) * 1000))
                continue

            # Coalesce SWITCH_TABLE floods: keep only the latest SWITCH_TABLE in this batch.
            try:
                sw: list[dict[str, Any]] = []
                non_sw: list[dict[str, Any]] = []
                for _d in items:
                    fa0 = _d.get("friend_action") or {}
                    if isinstance(fa0, dict) and str(fa0.get("action") or "").upper() == "SWITCH_TABLE":
                        sw.append(_d)
                    else:
                        non_sw.append(_d)
                if len(sw) > 1:
                    # sw list order is not stable due to duplicate enqueue; choose by timestamp, not by list order.
                    keep = max(sw, key=lambda x: str(x.get("received_at") or x.get("captured_at") or ""))
                    keep_id = str(keep.get("decision_id") or "")
                    superseded: set[str] = set()
                    for old in sw:
                        old_id = str(old.get("decision_id") or "")
                        if not old_id or old_id == keep_id or old_id in superseded:
                            continue
                        superseded.add(old_id)
                        try:
                            _post_result(
                                old_id,
                                {"error": "superseded by later SWITCH_TABLE", "superseded_by": keep_id},
                                status="error",
                            )
                        except Exception:
                            pass
                        try:
                            _seen_add(old_id)
                        except Exception:
                            pass
                    items = [keep] + non_sw
            except Exception:
                pass

            for d in items:
                did = str(d.get("decision_id") or "")
                provider = str(d.get("provider") or "")
                if provider != "pragmatic" or not did:
                    continue
                if not _seen_add(did):
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

                # Persistent idempotency guard: never execute the same decision_id after restart.
                # (Safer to skip than to risk betting a different round.)
                mark_ok: Optional[bool] = None
                last_exc: Optional[Exception] = None
                for attempt in range(3):
                    try:
                        mark_ok = try_mark_decision_executed(did, executor_id=executor_id)
                        break
                    except sqlite3.OperationalError as e:
                        last_exc = e
                        time.sleep(0.2 * (attempt + 1))
                    except Exception as e:
                        last_exc = e
                        break
                if mark_ok is None:
                    _post_result(did, {"error": "decision_exec_guard_failed", "detail": str(last_exc)[:200] if last_exc else ""}, status="error")
                    continue
                if not mark_ok:
                    err_payload = {"error": "duplicate_decision_retry", "decision_id": did}
                    if action == "BET":
                        err_payload["hint"] = (
                            "A previous attempt was started for this decision_id. "
                            "If it crashed mid-BET, the actual BET status on Stake is UNKNOWN. "
                            "Check Stake bet history manually."
                        )
                        try:
                            send_log(f"[bet][WARN] decision {did[-12:]} duplicate — manual Stake history check advised")
                        except Exception:
                            pass
                    _post_result(did, err_payload, status="error")
                    continue

                master_pending_for_me += 1

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
                master_last_decision_id = did
                master_last_decision_action = action
                master_last_decision_at = ack.get("acked_at") or _utc_now_iso()
                master_last_active_ts = time.time()

                # ユーザー可視性: 受信したマスターシグナルを ACTION バーに即表示.
                # これにより「シグナルが実際に届いたかどうか」が常に目視できる.
                try:
                    _tn = str(decision_table_name or "")
                    _sig_desc_parts = [f"📡 SIGNAL {action}"]
                    if _tn: _sig_desc_parts.append(_tn)
                    if action == "BET":
                        if side: _sig_desc_parts.append(side)
                        _amt = fa.get("amount")
                        if _amt: _sig_desc_parts.append(f"${_amt}")
                    send_action(" | ".join(_sig_desc_parts))
                    send_log(f"[signal] RECEIVED {action} table='{_tn}' side='{side}' did={did[-12:]}")
                except Exception:
                    pass

                if action == "SWITCH_TABLE":
                    if not args.allow_switch_table:
                        _post_result(did, {"error": "switch_table disabled (start executor with --allow-switch-table)"}, status="error")
                        continue
                    target = decision_table_name or (decision_snapshot.get("table_name") if isinstance(decision_snapshot, dict) else "") or ""
                    # qpid 優先: decision root に qpid_table_id があればそれを使い, 無ければ
                    # snapshot 内の qpid_table_id を使い, 無ければ snapshots API で補完.
                    target_qpid = str(d.get("qpid_table_id") or "").strip()
                    if not target_qpid and isinstance(decision_snapshot, dict):
                        target_qpid = str(decision_snapshot.get("qpid_table_id") or "").strip()
                    if not target_qpid:
                        # fallback: Master UI (旧版) からの decision には qpid が無いので
                        # snapshots API を operator_table_id で引いて qpid を補完する.
                        _op_tid = str(d.get("table_id") or "").strip()
                        if _op_tid:
                            try:
                                import urllib.request as _ur
                                _base = (os.getenv("BACOPY_API_URL") or "https://master.bafather.uk").rstrip("/")
                                _key = os.getenv("BACOPY_API_KEY") or ""
                                _req = _ur.Request(
                                    f"{_base}/api/snapshots?provider=pragmatic&table_id={_op_tid}",
                                    headers={"Authorization": f"Bearer {_key}"},
                                )
                                with _ur.urlopen(_req, timeout=5) as _r:
                                    _snap = json.load(_r).get("snapshot") or {}
                                target_qpid = str(_snap.get("qpid_table_id") or "").strip()
                                if target_qpid:
                                    try: send_log(f"[qpid] resolved via snapshots API: {_op_tid} -> {target_qpid}")
                                    except Exception: pass
                            except Exception as _e:
                                try: send_log(f"[qpid] snapshot lookup failed: {_e}")
                                except Exception: pass
                    if not target and not target_qpid:
                        _post_result(did, {"error": "table_name or qpid_table_id required for SWITCH_TABLE"}, status="error")
                        continue

                    # If we're already in the requested table, treat as no-op (prevents queue stalls).
                    try:
                        same_table = False
                        if decision_table_id and state.operator_table_id and decision_table_id == state.operator_table_id:
                            same_table = True
                        if state.table_name and target and str(state.table_name).strip().upper() == str(target).strip().upper():
                            same_table = True
                        if same_table:
                            res = {
                                "mode": "live_ws",
                                "observed_at": _utc_now_iso(),
                                "executor_id": executor_id,
                                "note": "already_in_table",
                                "current": {
                                    "operator_table_id": state.operator_table_id,
                                    "table_name": state.table_name,
                                    "table_id": state.table_id,
                                },
                            }
                            _post_result(did, res, status="done")
                            send_action(f"Already in table: {state.table_name or target}")
                            send_phase("idle", "ARMED")
                            continue
                    except Exception:
                        pass

                    send_phase("entering", str(target)[:40])
                    send_action(f"Switching table: {target}")
                    desired_table_substr = str(target)

                    # Clear identifiers so we can wait for new mapping.
                    state.operator_table_id = ""
                    state.table_name = ""
                    state.table_id = ""
                    state.user_id = ""
                    state.jsession_id = ""
                    state.game_ws_url = ""

                    try:
                        _join_table(
                            page,
                            table_substr=str(target),
                            qpid_table_id=target_qpid,
                            auto_click_wait_sec=int(args.auto_click_wait_sec),
                            state=state,
                            on_tick=lambda: heartbeat("running"),
                            is_initial=False,
                            interrupt_check=lambda: _peek_new_switch_decision(
                                decision_q,
                                did,
                                str(d.get("received_at") or d.get("captured_at") or ""),
                            ),
                        )
                        game_frame = find_game_frame(page, attempts=60)
                        if game_frame:
                            try:
                                game_frame.evaluate(_WS_BRIDGE_INIT)
                            except Exception:
                                pass
                        def _pred_ids2() -> bool:
                            nonlocal game_frame
                            game_frame = _refresh_game_frame(page, game_frame)
                            _pump_ws_events(page, game_frame, state)
                            return bool(state.game_ws_url and state.table_id and state.user_id and state.jsession_id)

                        ok = _wait_for(_pred_ids2, timeout_sec=180, tick_ms=500, page=page, on_tick=lambda: heartbeat("running"))
                        if not ok:
                            raise RuntimeError("session identifiers not populated (table/user/ws missing)")
                        # If numeric table_id provided, wait for operator table id match too (best-effort).
                        if decision_table_id:
                            def _pred_op_match() -> bool:
                                nonlocal game_frame
                                game_frame = _refresh_game_frame(page, game_frame)
                                _pump_ws_events(page, game_frame, state)
                                return bool(state.operator_table_id and state.operator_table_id == decision_table_id)

                            _wait_for(_pred_op_match, timeout_sec=30, tick_ms=500, page=page, on_tick=lambda: heartbeat("running"))
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
                        send_action(f"Table ready: {state.table_name or target}")
                        send_phase("idle", "ARMED")
                    except _SwitchTableInterrupted as ex:
                        # 新 SWITCH_TABLE で中断された — 現 decision を superseded でエラー返却.
                        new_did = str(ex.new_decision.get("decision_id", "") or "")
                        try:
                            send_log(f"[switch] aborted {did[-12:]} superseded by {new_did[-12:]}")
                            send_action(f"Superseded by newer SWITCH → will process next")
                        except Exception: pass
                        _post_result(did, {"error": "superseded by later SWITCH_TABLE", "superseded_by": new_did}, status="error")
                    except Exception as e:
                        last_error = f"switch_table failed: {e}"
                        heartbeat("error")
                        _post_result(did, {"error": last_error}, status="error")
                    continue

                if action == "LOOK":
                    send_action("LOOK (no bet)")
                    send_phase("idle", "ARMED")
                    res = {"mode": "live_ws", "observed_at": _utc_now_iso(), "note": "LOOK no-op (live)"}
                    _post_result(did, res, status="done")
                    continue

                if action != "BET":
                    _post_result(did, {"error": f"unsupported action: {action}"}, status="error")
                    continue

                bet_side = _normalize_bet_side(side)
                if bet_side == "BANKER" and not args.allow_banker:
                    _post_result(did, {"error": "BANKER disabled (start executor with --allow-banker)"}, status="error")
                    continue
                if bet_side == "TIE" and not args.allow_tie:
                    _post_result(did, {"error": "TIE disabled (start executor with --allow-tie)"}, status="error")
                    continue
                bc = _side_to_bc(bet_side, assume_012=bool(args.assume_bc_012))
                if not bc:
                    _post_result(
                        did,
                        {
                            "error": "unknown_bc_mapping_for_side",
                            "side": bet_side,
                            "hint": "Run sniff_pragmatic_bet_ws.py and set BACOPY_PRAGMATIC_BC_BANKER / BACOPY_PRAGMATIC_BC_TIE (or pass --assume-bc-012 at your own risk).",
                        },
                        status="error",
                    )
                    continue
                side = bet_side

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

                # Safety: block betting if Stake indicates session was taken elsewhere.
                # NOTE: main loop が毎秒 _dismiss_session_elsewhere_modal を呼んでいるので
                # BET 毎の追加チェックは不要. むしろ DOM 走査の偽陽性で session_elsewhere_unresolved
                # が一時的に True になり BET が不当に block される事故を招く.
                # → ここでは state.session_elsewhere_unresolved フラグを信じるのみ.
                if state.session_elsewhere_unresolved:
                    _post_result(
                        did,
                        {
                            "error": "session_taken_by_other_client (BET blocked)",
                            "session_elsewhere_observed": state.session_elsewhere_observed,
                            "session_elsewhere_last_at": state.session_elsewhere_last_at,
                            "session_elsewhere_resolved_at": state.session_elsewhere_resolved_at,
                        },
                        status="error",
                    )
                    last_error = "session_taken_by_other_client"
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
                if key in processed_keys:
                    _post_result(
                        did,
                        {"error": "duplicate_bet_guard", "operator_table_id": op_tid, "game_id": game_id},
                        status="error",
                    )
                    continue
                try:
                    locked = try_lock_bet(executor_id=executor_id, provider=provider, table_id=str(op_tid), game_id=str(game_id), decision_id=did)
                except sqlite3.OperationalError as e:
                    _post_result(
                        did,
                        {"error": "bet_guard_db_busy", "operator_table_id": op_tid, "game_id": game_id, "detail": str(e)[:200]},
                        status="error",
                    )
                    last_error = "bet_guard_db_busy"
                    consecutive_hard_errors += 1
                    heartbeat("error")
                    continue
                if not locked:
                    _post_result(
                        did,
                        {"error": "duplicate_bet_guard", "operator_table_id": op_tid, "game_id": game_id},
                        status="error",
                    )
                    continue
                processed_keys.add(key)

                amt = float(seq7.bet_amount())
                send_phase(f"betting_{side.lower()}", f"${amt:.0f}")
                send_action(f"BET {side} ${amt:.0f}")
                stake_cur = bet_currency
                if stake_cur not in state.stake_balance_by_currency and state.stake_balance_by_currency:
                    if len(state.stake_balance_by_currency) == 1:
                        stake_cur = next(iter(state.stake_balance_by_currency.keys()))
                before_balance = state.stake_balance_by_currency.get(stake_cur)
                try:
                    seq7.update_balance(before_balance)
                except Exception:
                    pass
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
                    currency=stake_cur,
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
                if isinstance(confirm, dict) and confirm.get("type") == "xml_error":
                    _post_result(
                        did,
                        {"error": "bet_rejected", "game_id": game_id, "operator_table_id": op_tid, "bet_ck": state.expected_bet_ck, "bet_confirm": confirm},
                        status="error",
                    )
                    consecutive_hard_errors += 1
                    last_error = "bet_rejected"
                    heartbeat("error")
                    continue

                # If stake currency was auto-detected, use it for subsequent balance reads.
                if isinstance(confirm, dict) and confirm.get("currency"):
                    stake_cur = str(confirm.get("currency") or stake_cur)
                if isinstance(confirm, dict) and "before" in confirm and confirm.get("before") is not None:
                    before_balance = float(confirm.get("before"))
                consecutive_hard_errors = 0
                last_error = ""

                # Resolve by dga feed winner
                try:
                    _ensure_dga_subscription(page, state, operator_table_id=op_tid, currency=bet_currency)
                except Exception:
                    pass
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

                # Update SEQ7 + push GUI metrics (stdout JSON)
                try:
                    _pump_ws_events(page, game_frame, state)
                except Exception:
                    pass
                after_balance = state.stake_balance_by_currency.get(stake_cur)
                try:
                    seq7.update_balance(after_balance)
                except Exception:
                    pass

                bet_side_low = str(side or "").strip().lower()
                if bet_side_low == "tie":
                    won = bool(outcome == "tie")
                else:
                    won = None if outcome == "tie" else bool(outcome == bet_side_low)
                rr_meta = seq7.apply_round(outcome, won, bet_side=bet_side_low)

                try:
                    # 3-way 分岐を厳密に (TIE / WIN / LOSS). won is None でも LOSE にしない.
                    if outcome == "tie" and bet_side_low != "tie":
                        # PLAYER/BANKER BET で TIE は PUSH (引き分け, 賭け金返却)
                        if after_balance is not None:
                            send_action(f"TIE (引き分け) {side} — BET returned. Balance: ${after_balance:.2f}")
                        else:
                            send_action(f"TIE (引き分け) {side} — BET returned")
                    elif won is True:
                        if after_balance is not None:
                            send_action(f"WIN {side} ${amt:.0f}. Balance: ${after_balance:.2f}")
                        else:
                            send_action(f"WIN {side} ${amt:.0f}")
                    elif won is False:
                        if after_balance is not None:
                            send_action(f"LOSE {side} ${amt:.0f}. Balance: ${after_balance:.2f}")
                        else:
                            send_action(f"LOSE {side} ${amt:.0f}")
                    else:
                        # 万一 won=None が別経路で来た時の保険 (PUSH 扱い)
                        send_action(f"PUSH {side} — BET returned")
                    send_phase("idle", "ARMED")

                    send_msg(
                        {
                            "type": "round_result",
                            "result": outcome,
                            "won": won,
                            "bet_amount": amt,
                            "bet_side": bet_side_low,
                            "balance": seq7.current_balance if seq7.current_balance is not None else after_balance,
                            "session_open_balance": seq7.session_open_balance,
                            "daily_open_date": seq7.daily_open_date,
                            "daily_open_balance": seq7.daily_open_balance,
                            "current_turn": rr_meta.get("pre_turn_count"),
                            "turns_display": "".join(seq7.tracker.current_turns),
                            "overshoot": getattr(seq7.tracker, "prev_overshoot", 0),
                            "pre_wins": rr_meta.get("pre_wins"),
                            "pre_losses": rr_meta.get("pre_losses"),
                        }
                    )
                    send_msg(seq7.status_payload())
                except Exception:
                    pass

                if rr_meta.get("completed_set") is not None:
                    s = rr_meta["completed_set"]
                    try:
                        send_msg(
                            {
                                "type": "set_complete",
                                "set_index": s.set_index,
                                "results": s.results,
                                "wins": s.wins,
                                "losses": s.losses,
                                "set_profit": s.set_profit,
                                "cumulative_profit": s.cumulative_profit,
                                "money_set": s.set_profit * chip_base,
                                "money_cum": s.cumulative_profit * chip_base,
                                "overshoot": s.overshoot,
                            }
                        )
                        send_msg({"type": "shoe_history", "sets": [x.__dict__ for x in seq7.tracker.sets], "chip_base": chip_base})
                    except Exception:
                        pass

                if seq7.should_reset():
                    cp = seq7.effective_profit_chips()
                    reason = "profit" if cp >= seq7.profit_stop else "loss"
                    reset_msg = seq7.reset_session(reason)
                    send_msg(reset_msg)
                    send_msg({"type": "shoe_history", "sets": [], "chip_base": chip_base})
                    send_msg(seq7.status_payload())
                    send_phase("idle", "ARMED")
                    if seq7.profit_session_limit > 0 and reset_msg.get("is_profit") and seq7.profit_sessions >= seq7.profit_session_limit:
                        send_action("Profit session limit reached. Stopping.")
                        raise SystemExit("profit_session_limit reached")

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

                # ML 契約 JSONL は Master 側 (POST /api/decisions/{id}/result) が append_result_event で処理.
                # executor 側で再度 append するとホスト/event_type が重複して reconstruct が壊れる.
                print(f"[done] {did} {state.table_name} game={game_id} side={side} -> {outcome}", flush=True)

            if args.once:
                break
            page.wait_for_timeout(int(max(args.poll_sec, 0.2) * 1000))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
