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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import requests

from decision_logger import append_decision_event
from bacopy_db import init_db, try_lock_bet
from marubatsu_strategy import MaruBatsuTracker, SEQ_COUNTER, SetData

BA_ROOT = Path(__file__).parent.parent / "ba"
sys.path.insert(0, str(BA_ROOT))

LOBBY_URL = "https://stake.com/ja/casino/games/pragmatic-play-live-lobby-baccarat"

# ======== GUI IPC (stdout JSON) ========

def send_msg(msg: dict) -> None:
    line = json.dumps(msg, ensure_ascii=False) + "\n"
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

    def apply_round(self, outcome: str, won: bool | None) -> dict:
        # outcome: player|banker|tie
        if outcome == "tie":
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
            if "pragmaticplaylive.net/game" in url:
                _maybe_update_from_game_ws_url(state, url)
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
    send_phase("entering", "OPEN STAKE")
    send_action("Opening Stake lobby. If prompted, please log in to Stake.")
    print("[Stage 1] goto stake pragmatic lobby ...", flush=True)
    page.goto(LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(10_000)

    # Stake loader overlay を除去 (クリック遮断防止)
    _dismiss_stake_loader(page)

    print("[Stage 2] wait pragmatic shell ...", flush=True)
    gf = find_game_frame(page)
    if not gf:
        send_phase("entering", "STAKE LOGIN")
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
        send_action("Waiting for Stake lobby... Please finish Stake login in the opened browser window.")
        t0 = time.time()
        last_notice = 0.0
        while not shell:
            shell = find_shell_app_frame(page, attempts=1)
            if shell:
                break
            _dismiss_stake_loader(page)
            if time.time() - last_notice >= 30.0:
                last_notice = time.time()
                try:
                    print(f"[INFO] waiting for shell-app (login) elapsed={time.time()-t0:.0f}s url={page.url}", flush=True)
                except Exception:
                    print(f"[INFO] waiting for shell-app (login) elapsed={time.time()-t0:.0f}s", flush=True)

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
    ap.add_argument("--chip-base", type=float, default=0.0, help="Base bet ($) for SEQ7 (falls back to --flat-amount)")
    ap.add_argument("--profit-target", type=float, default=50.0, help="Session profit target in $ (converted to chips by chip_base)")
    ap.add_argument("--profit-session-limit", type=int, default=0, help="Stop after N profit resets (0=unlimited)")
    ap.add_argument("--loss-cut", type=float, default=200.0, help="Session loss cut in $ (converted to chips by chip_base)")
    ap.add_argument("--only-table-id", default=os.getenv("BACOPY_ONLY_TABLE_ID", ""), help="operator tableId (numeric) to accept")
    ap.add_argument("--bet-timeout-sec", type=int, default=20)
    ap.add_argument("--min-timer-sec", type=float, default=2.0, help="Refuse bets if timer is below this (when available)")
    ap.add_argument("--result-timeout-sec", type=int, default=90)
    ap.add_argument("--allow-switch-table", action="store_true", help="Allow SWITCH_TABLE action to navigate/click table")
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
    master_last_check_ts = 0.0
    master_last_decision_id = ""
    master_last_decision_action = ""
    master_last_decision_at = ""
    master_last_active_ts = 0.0
    master_pending_for_me = 0
    master_prev_active: Optional[bool] = None

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
        nonlocal master_last_ok_ts, master_last_ok_at, master_last_err, master_last_check_ts
        nonlocal master_last_decision_id, master_last_decision_action, master_last_decision_at
        nonlocal master_last_active_ts, master_pending_for_me, master_prev_active
        now = time.time()
        if now - last_hb < 5.0:
            return
        last_hb = now
        bal = state.stake_balance_by_currency.get(bet_currency)
        try:
            seq7.update_balance(bal)
        except Exception:
            pass
        try:
            send_msg(seq7.status_payload())
        except Exception:
            pass

        # Master connectivity check (auth-required endpoint)
        try:
            if now - master_last_check_ts >= 5.0:
                master_last_check_ts = now
                r = _http_request(
                    "GET",
                    f"{_api_url()}/api/status",
                    headers=_headers(),
                    timeout=(_api_connect_timeout_sec(), 5.0),
                    retries=1,
                )
                r.raise_for_status()
                master_last_ok_ts = now
                master_last_ok_at = _utc_now_iso()
                master_last_err = ""
        except Exception as e:
            master_last_err = str(e)[:200]

        connected = bool(master_last_ok_ts and (now - master_last_ok_ts) < 20.0)
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
        _post_heartbeat(
            {
                "executor_id": executor_id,
                "label": executor_label,
                "username": executor_username,
                "provider": "pragmatic",
                "table_id": state.operator_table_id,
                "table_name": state.table_name,
                "balance": bal,
                "seq": {
                    "mode": "counter_seq7",
                    "chip_base": chip_base,
                    "unit_idx": seq7.tracker.current_unit_idx,
                    "unit": seq7.bet_unit(),
                    "bet_amount": seq7.bet_amount(),
                    "turn": len(seq7.tracker.current_turns) + 1,
                    "overshoot": getattr(seq7.tracker, "prev_overshoot", 0),
                },
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
        send_phase("entering", "WAIT TABLE")
        send_action("Waiting for Stake table entry... Please enter a baccarat table in the opened browser window.")
        t0 = time.time()
        last_notice = 0.0
        while not (state.game_ws_url and state.table_id and state.user_id and state.jsession_id):
            try:
                if not game_frame:
                    game_frame = find_game_frame(page, attempts=1)
                    if game_frame:
                        try:
                            game_frame.evaluate(_WS_BRIDGE_INIT)
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                _pump_ws_events(page, game_frame, state)
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
            master_pending_for_me = 0
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

                if action == "SWITCH_TABLE":
                    if not args.allow_switch_table:
                        _post_result(did, {"error": "switch_table disabled (start executor with --allow-switch-table)"}, status="error")
                        continue
                    target = decision_table_name or (decision_snapshot.get("table_name") if isinstance(decision_snapshot, dict) else "") or ""
                    if not target:
                        _post_result(did, {"error": "table_name required for SWITCH_TABLE"}, status="error")
                        continue
                    send_phase("entering", str(target)[:40])
                    send_action(f"Switching table: {target}")

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
                        send_action(f"Table ready: {state.table_name or target}")
                        send_phase("idle", "ARMED")
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

                # Receiver bets are always PLAYER (master sends only PLAYER BET / LOOK).
                if side and side not in ("PLAYER", "P"):
                    send_log(f"[warn] ignoring non-player side from master: {side}")
                side = "PLAYER"
                bc = _side_to_bc(side) or "0"

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

                amt = float(seq7.bet_amount())
                send_phase("betting_player", f"${amt:.0f}")
                send_action(f"BET PLAYER ${amt:.0f}")
                before_balance = state.stake_balance_by_currency.get(bet_currency)
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

                # Update SEQ7 + push GUI metrics (stdout JSON)
                try:
                    _pump_ws_events(page, game_frame, state)
                except Exception:
                    pass
                after_balance = state.stake_balance_by_currency.get(bet_currency)
                try:
                    seq7.update_balance(after_balance)
                except Exception:
                    pass

                won = None if outcome == "tie" else (outcome == "player")
                rr_meta = seq7.apply_round(outcome, won)

                try:
                    if outcome == "tie":
                        send_action("Tie — BET returned")
                    elif won:
                        if after_balance is not None:
                            send_action(f"WIN PLAYER ${amt:.0f}. Balance: ${after_balance:.2f}")
                        else:
                            send_action(f"WIN PLAYER ${amt:.0f}")
                    else:
                        if after_balance is not None:
                            send_action(f"LOSE PLAYER ${amt:.0f}. Balance: ${after_balance:.2f}")
                        else:
                            send_action(f"LOSE PLAYER ${amt:.0f}")
                    send_phase("idle", "ARMED")

                    send_msg(
                        {
                            "type": "round_result",
                            "result": outcome,
                            "won": won,
                            "bet_amount": amt,
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
