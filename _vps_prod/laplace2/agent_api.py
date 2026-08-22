"""LAPLACE -- Python Agent (BET mode)

Uses proven BaccaratScraper + BetExecutor + MaruBatsuBetSession.
Electron GUI communicates via stdin/stdout JSON IPC.

Flow:
  1. GUI sends start → agent launches Camoufox
  2. Scraper: login → lobby → WS intercept → find table
  3. Executor: enter table
  4. BetSession: run_round loop (BET → result → logic)
  5. All status/events sent to GUI via stdout
"""
import json
import sys
import os
import faulthandler
import threading
import time
import logging
import io
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error
import urllib.parse

# Write fatal (native) crashes to a file so we can diagnose "traceback無しで突然死" を拾う
try:
    _base = os.path.dirname(os.path.abspath(__file__))
    _fh_dir = os.path.join(_base, "auth_state")
    os.makedirs(_fh_dir, exist_ok=True)
    _fh_path = os.path.join(_fh_dir, "faulthandler.log")
    faulthandler.enable(open(_fh_path, "a", buffering=1), all_threads=True)
except Exception:
    pass

# ---- Force stdio to UTF-8 (MUST run before any send_log/send_msg) -------
# PyInstaller-bundled Python on a Japanese Windows install defaults to
# cp932 for sys.stdout / sys.stderr, which chokes on characters like
# the em dash U+2014 used throughout our log messages and crashes the
# whole BET session with UnicodeEncodeError. The parent Electron process
# always decodes the child pipes as UTF-8, so forcing UTF-8 on both ends
# is the right fix.
for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except Exception:
            pass

# ---- Eager imports (MUST happen on the main thread) --------------------
# Importing numpy from a worker thread while another thread is blocked on
# sys.stdin.readline() deadlocks inside numpy._core.overrides on Python
# 3.12 Windows. camoufox.async_api transitively imports numpy, so the
# scraper import used to hang the whole BET session.
#
# Pull every heavy / native dependency in up front from the main thread
# so the worker thread only needs to reference already-loaded modules.
# Order matters: numpy first, then playwright/camoufox, then our own
# modules that depend on them.
try:
    import numpy  # noqa: F401 -- pre-warm numpy on main thread
except Exception:
    pass
try:
    import playwright.sync_api  # noqa: F401
except Exception:
    pass
try:
    import camoufox.sync_api  # noqa: F401
except Exception:
    pass

# ---- Bundled camoufox browser bootstrap --------------------------------
# When the Engine is packaged via PyInstaller, the build script can include
# a pre-fetched camoufox browser tree at <exe_dir>/camoufox_cache/.
# On first launch we copy it into the platform cache dir that
# camoufox.pkgman.INSTALL_DIR points to, so users never have to run
# `camoufox fetch` or download 530 MB themselves.
def _bootstrap_camoufox_cache():
    try:
        from camoufox.pkgman import INSTALL_DIR
        if INSTALL_DIR.exists() and (INSTALL_DIR / "version.json").exists():
            return  # already installed, nothing to do
        exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
        bundled = os.path.join(exe_dir, "camoufox_cache")
        if not os.path.isdir(bundled):
            return  # no bundled cache
        import shutil
        INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundled, str(INSTALL_DIR), dirs_exist_ok=True)
    except Exception:
        pass

_bootstrap_camoufox_cache()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env early so LAPLACE_USE_REMOTE etc. are available
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log"),
            encoding="utf-8",
        )
    ],
)
logger = logging.getLogger("valhalla.agent")

# Global reference to current active BET session (for live config updates)
_active_session = None
# Pending config updates received before session was created
_pending_config_update = {}
# BET mode: mutable box for cross-thread access (stdin_reader ↔ bet loop)
_bet_mode_box = ["1drop"]       # ユーザー選択モード: "normal" | "1drop" | "mix"
_effective_mode_box = ["1drop"] # 実行時モード（mixの場合 normal→1drop に自動切替）
_profit_session_limit_box = [0]  # 利確回数上限（0=無制限）

MAX_ROUNDS = 9999


# ======== IPC ========

def send_msg(msg: dict):
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # Belt-and-braces: if the stdout text wrapper somehow still has a
        # non-UTF-8 encoding (e.g. cp932 on a Japanese Windows PyInstaller
        # build that ignored our reconfigure), push raw UTF-8 bytes onto
        # the underlying binary buffer instead. The Electron parent always
        # decodes the child pipe as UTF-8, so this works.
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            try:
                buf.write(line.encode("utf-8", errors="replace"))
                buf.flush()
            except Exception:
                pass
        else:
            # Last resort: ASCII-safe JSON (escapes everything non-ASCII)
            try:
                ascii_line = json.dumps(msg, ensure_ascii=True) + "\n"
                sys.stdout.write(ascii_line)
                sys.stdout.flush()
            except Exception:
                pass
    except Exception:
        pass

def send_log(text: str):
    send_msg({"type": "log", "message": text})

def send_action(text: str):
    """Send browser action status for GUI display"""
    send_msg({"type": "action", "message": text})

# 状態バッジ用 Phase メッセージ
# Phase名: idle | scanning | entering | betting | betting_player | betting_banker | ws_stall | error | stopped
_LAST_PHASE = [""]
def send_phase(name: str, detail: str = ""):
    """GUIの状態バッジを更新。同じ name+detail が連続する時は送らない(ノイズ低減)。"""
    key = f"{name}|{detail}"
    if _LAST_PHASE[0] == key:
        return
    _LAST_PHASE[0] = key
    send_msg({"type": "phase", "name": name, "detail": detail, "ts": time.time()})

def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False

def _apply_counter_params(cfg: dict, source: str = "config") -> bool:
    if not cfg:
        return False
    try:
        import counter_logic as _cl
    except Exception as e:
        send_log(f"[counter] Param update skipped ({e})")
        return False
    updated = False
    if "entry_window" in cfg:
        _cl.ENTRY_WINDOW = int(cfg["entry_window"])
        updated = True
    if "entry_threshold" in cfg:
        _cl.ENTRY_THRESHOLD = float(cfg["entry_threshold"])
        updated = True
    if "exit_drop3_limit" in cfg:
        _cl.EXIT_DROP3_LIMIT = int(cfg["exit_drop3_limit"])
        updated = True
    if "exit_drop5_immediate" in cfg:
        _cl.EXIT_DROP5_IMMEDIATE = _parse_bool(cfg["exit_drop5_immediate"])
        updated = True
    if updated:
        send_log(
            f"[counter] Params updated ({source}): "
            f"W={_cl.ENTRY_WINDOW} T={_cl.ENTRY_THRESHOLD} "
            f"D3={_cl.EXIT_DROP3_LIMIT} D5={_cl.EXIT_DROP5_IMMEDIATE}"
        )
    return updated

def send_result(result: str, won: bool | None, bet_amount: float, balance: float,
                turn: int, turns_display: str, cumulative_profit: int, cumulative_money: float,
                round_profit_dollars: float = 0.0,
                round_profit_actual: float | None = None,
                cumulative_money_actual: float | None = None):
    payload = {
        "type": "round_result",
        "result": result,
        "won": won,
        "bet_amount": bet_amount,
        "balance": balance,
        "turn": turn,
        "turns_display": turns_display,
        "cumulative_profit": cumulative_profit,
        "cumulative_money": cumulative_money,
        "round_profit": round_profit_dollars,
    }
    if round_profit_actual is not None:
        payload["round_profit_actual"] = round_profit_actual
    if cumulative_money_actual is not None:
        payload["cumulative_money_actual"] = cumulative_money_actual
    # 残高スナップショット (GUI側でPNL計算に使用)
    sess = _active_session
    if sess is not None and hasattr(sess, 'session_open_balance'):
        payload["session_open_balance"] = sess.session_open_balance
        do = getattr(sess, 'daily_open', None)
        if isinstance(do, dict):
            payload["daily_open_date"] = do.get("date")
            payload["daily_open_balance"] = do.get("balance")
    send_msg(payload)

def send_set_complete(set_data, chip_base: float):
    send_msg({
        "type": "set_complete",
        "set_index": set_data.set_index,
        "results": set_data.results,
        "wins": set_data.wins,
        "losses": set_data.losses,
        "set_profit": set_data.set_profit,
        "cumulative_profit": set_data.cumulative_profit,
        "money_set": set_data.set_profit * chip_base,
        "money_cum": set_data.cumulative_profit * chip_base,
        "overshoot": set_data.overshoot,
    })

def send_status(session, balance: float = 0, cumulative_money_actual: float | None = None):
    s = session.get_summary()
    turns = session.tracker.current_turns
    turns_display = "".join("O" if t == "O" else "X" for t in turns)
    overshoot = session.tracker.prev_overshoot
    payload = {
        "type": "status",
        "cumulative_profit": s["cumulative_profit"],
        "cumulative_money": s["cumulative_money"],
        "wins": s["total_wins"],
        "losses": s["total_losses"],
        "ties": s["total_ties"],
        "set_count": s["sets"],
        "current_turn": s["current_turn"],
        "current_unit": s["current_unit"],
        "current_unit_idx": s["current_unit_idx"],
        "total_bets": s["total_bets"],
        "overshoot": overshoot,
        "running": True,
        "balance": balance,
        "turns_display": turns_display,
        "session_count": s["session_count"],
    }
    if cumulative_money_actual is not None:
        payload["cumulative_money_actual"] = cumulative_money_actual
    # 残高スナップショット (GUI側でPNL計算に使用)
    if hasattr(session, 'session_open_balance'):
        payload["session_open_balance"] = session.session_open_balance
        do = getattr(session, 'daily_open', None)
        if isinstance(do, dict):
            payload["daily_open_date"] = do.get("date")
            payload["daily_open_balance"] = do.get("balance")
    send_msg(payload)


# ======== Supabase session-state sync ========

_SESSION_SITE_URL = os.getenv("LAPLACE_SITE_URL", "https://bafather.uk").rstrip("/")
_SESSION_API_KEY = os.getenv("LAPLACE_SITE_API_KEY", "").strip() or os.getenv("LAPLACE_API_KEY", "").strip()
_SESSION_SYNC_INTERVAL = 5.0
_session_sync_inflight = False
_session_sync_last = 0.0


def _extract_session_state(session) -> dict | None:
    if session is None:
        return None
    if hasattr(session, "to_state_dict"):
        try:
            return session.to_state_dict()
        except Exception:
            return None
    if hasattr(session, "get_state_dict"):
        try:
            return session.get_state_dict()
        except Exception:
            return None
    return None


def _has_session_state(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("sets"):
        return True
    turns = state.get("current_turns") or state.get("turns_display")
    if turns:
        return True
    if (state.get("total_bets") or 0) > 0:
        return True
    return False


def _build_session_state_from_results(results: list, chip_base: float, profit_stop: int, loss_cut: int,
                                      counter_mode: bool = False, counter_set_size: int | None = None) -> dict | None:
    if not results or not isinstance(results, list):
        return None
    try:
        from marubatsu_strategy import MaruBatsuTracker, SEQ_COUNTER, SET_SIZE_COUNTER
    except Exception:
        return None

    if counter_mode:
        set_size = counter_set_size or SET_SIZE_COUNTER
        tracker = MaruBatsuTracker(chip_base=chip_base, seq=SEQ_COUNTER, set_size=set_size)
    else:
        tracker = MaruBatsuTracker(chip_base=chip_base)
    total_wins = 0
    total_losses = 0
    total_ties = 0

    for raw in results:
        if not isinstance(raw, str):
            continue
        mark = raw.strip().upper()
        if not mark:
            continue
        if mark in ("T", "TIE"):
            total_ties += 1
            continue
        if mark in ("W", "WIN"):
            total_wins += 1
            tracker.add_result("player")
            continue
        if mark in ("L", "LOSE", "LOSS"):
            total_losses += 1
            tracker.add_result("banker")
            continue

    sets_payload = [
        {
            "set_index": s.set_index,
            "results": s.results,
            "wins": s.wins,
            "losses": s.losses,
            "overshoot": s.overshoot,
            "slashed": s.slashed,
            "used_unit_idx": s.used_unit_idx,
            "next_unit_idx": s.next_unit_idx,
            "set_profit": s.set_profit,
            "cumulative_profit": s.cumulative_profit,
        }
        for s in tracker.sets
    ]
    return {
        "chip_base": chip_base,
        "profit_stop": profit_stop,
        "loss_cut": loss_cut,
        "sets": sets_payload,
        "current_turns": tracker.current_turns,
        "total_o": tracker.total_o,
        "total_x": tracker.total_x,
        "session_count": 0,
        "total_bets": total_wins + total_losses + total_ties,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_ties": total_ties,
    }


def _load_session_state_from_server(email: str, api_key: str = "") -> dict | None:
    key = api_key or _SESSION_API_KEY
    if not email or not key:
        return None
    qs = urllib.parse.urlencode({"email": email, "api_key": key})
    url = f"{_SESSION_SITE_URL}/api/session-state?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            state = data.get("session_state")
            return state if isinstance(state, dict) and state else None
    except Exception:
        return None


def _post_session_state_to_server(email: str, state: dict, api_key: str = "") -> bool:
    key = api_key or _SESSION_API_KEY
    if not email or not key or not state:
        return False
    payload = json.dumps({"email": email, "api_key": key, "session_state": state}).encode("utf-8")
    url = f"{_SESSION_SITE_URL}/api/session-state"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-engine/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        logger.warning(f"[session] Supabase sync failed: {e}")
        return False


def _jst_date_str() -> str:
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")


def _post_daily_settlement(email: str, net_profit: float, api_key: str = "", date_str: str = "") -> tuple[bool, str]:
    key = api_key or _SESSION_API_KEY
    if not email or not key:
        return False, "missing email/api_key"
    payload = json.dumps({
        "email": email,
        "api_key": key,
        "date": date_str or _jst_date_str(),
        "net_profit": float(net_profit),
    }).encode("utf-8")
    url = f"{_SESSION_SITE_URL}/api/cron/settle"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-engine/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, ""
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 409:
            return True, "already settled"
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = str(e)
        return False, f"http {getattr(e, 'code', 'error')}: {detail}"
    except Exception as e:
        return False, str(e)


def _post_daily_settlement_retry(email: str, net_profit: float, api_key: str = "", date_str: str = "",
                                 attempts: int = 3, base_delay: float = 2.0) -> tuple[bool, str]:
    last_err = ""
    for idx in range(attempts):
        ok, err = _post_daily_settlement(email, net_profit, api_key, date_str)
        if ok:
            return True, err
        last_err = err
        time.sleep(base_delay * (idx + 1))
    return False, last_err


def _schedule_session_state_sync(email: str, session, user_id: str = "", api_key: str = "") -> None:
    global _session_sync_inflight, _session_sync_last
    key = api_key or _SESSION_API_KEY
    if not email or not key:
        return
    now = time.time()
    if now - _session_sync_last < _SESSION_SYNC_INTERVAL:
        return
    if _session_sync_inflight:
        return
    state = _extract_session_state(session)
    if not state:
        return
    if user_id:
        state["user_id"] = user_id
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _session_sync_last = now
    _session_sync_inflight = True

    def _worker():
        global _session_sync_inflight
        try:
            _post_session_state_to_server(email, state, key)
        finally:
            _session_sync_inflight = False

    threading.Thread(target=_worker, daemon=True).start()


def _backfill_session_state(email: str, session, user_id: str = "", api_key: str = "") -> bool:
    key = api_key or _SESSION_API_KEY
    if not email or not key:
        return False
    state = _extract_session_state(session)
    if not _has_session_state(state):
        return False
    if user_id:
        state["user_id"] = user_id
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"

    return _post_session_state_to_server(email, state, key)


def _apply_session_state(session, state: dict) -> bool:
    if not state or session is None:
        return False
    if hasattr(session, "apply_state_dict"):
        try:
            session.apply_state_dict(state)
            return True
        except Exception:
            return False
    if hasattr(session, "restore_state"):
        try:
            session.restore_state(state)
            return True
        except Exception:
            return False
    return False


# ======== Heartbeat (watchdog stale-log prevention) ========

def start_heartbeat(stop_event: threading.Event, mode_box: list[str]):
    """Write a periodic heartbeat to agent.log so external watchdogs don't
    treat 'quiet but healthy' periods as a freeze."""
    FILE_INTERVAL = 60
    GUI_INTERVAL = 300
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")

    def _runner():
        last_gui = 0.0
        while not stop_event.is_set():
            try:
                # Ensure file mtime keeps moving even if the logger is quiet.
                try:
                    os.utime(log_path, None)
                except Exception:
                    pass
                logger.info(f"[hb] alive mode={mode_box[0]}")
                now = time.time()
                if now - last_gui >= GUI_INTERVAL:
                    send_log(f"[hb] alive mode={mode_box[0]}")
                    last_gui = now
            except Exception:
                pass
            # Sleep in small chunks so STOP reacts quickly
            for _ in range(FILE_INTERVAL):
                if stop_event.is_set():
                    break
                time.sleep(1)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t

def send_shoe_history(sets: list, chip_base: float):
    """Send all completed sets for shoe display"""
    data = []
    for s in sets:
        data.append({
            "set_index": s.set_index,
            "results": s.results,
            "wins": s.wins,
            "losses": s.losses,
            "set_profit": s.set_profit,
            "cumulative_profit": s.cumulative_profit,
            "overshoot": s.overshoot,
            "slashed": s.slashed,
        })
    send_msg({"type": "shoe_history", "sets": data, "chip_base": chip_base})


# ======== BET Runner ========

def run_bet_session(config: dict, stop_event: threading.Event, skip_event: threading.Event = None):
    """Main BET loop entry — runs in a thread. Wraps the real body so that
    any unhandled exception is surfaced to the GUI instead of silently
    killing the daemon thread."""
    import traceback as _tb
    try:
        return _run_bet_session_inner(config, stop_event, skip_event)
    except Exception as _err:
        try:
            stop_event.set()
        except Exception:
            pass
        tb = _tb.format_exc()
        try:
            send_log(f"FATAL: BET session crashed: {_err}")
            for _line in tb.splitlines():
                if _line.strip():
                    send_log(_line)
        except Exception:
            pass
        try:
            logger.error("BET session crashed", exc_info=True)
        except Exception:
            pass
        try:
            send_msg({"type": "error", "message": f"BET session crashed: {_err}"})
            send_phase("error", str(_err)[:60])
            send_msg({"type": "stopped", "code": -1})
        except Exception:
            pass


def _run_bet_session_inner(config: dict, stop_event: threading.Event, skip_event: threading.Event = None):
    global _active_session, _pending_config_update, _bet_mode_box, _effective_mode_box, _profit_session_limit_box
    """Main BET loop — runs in a thread."""
    # BETモード初期化
    _bet_mode_box[0] = config.get("bet_mode", "1drop")
    _effective_mode_box[0] = "normal" if _bet_mode_box[0] == "mix" else _bet_mode_box[0]
    send_log(f"BET mode: {_bet_mode_box[0]} (effective: {_effective_mode_box[0]})")
    start_heartbeat(stop_event, _effective_mode_box)
    import config as cfg
    # Headless is determined by env (VPS sets LAPLACE_HEADLESS=1) or config.ini; default False
    if os.getenv("LAPLACE_HEADLESS", "").strip() in ("1", "true", "True", "yes"):
        cfg.HEADLESS = True
    cfg.PROFILE_NAME = os.getenv("LAPLACE_PROFILE_NAME", "bet")

    # These modules were pre-imported on the main thread at agent_api load
    # time (see top of file) so Python's import machinery does NOT deadlock
    # when the worker thread touches numpy-backed code.
    from scraper import BaccaratScraper
    from executor import BetExecutor
    from game_ws import GameWSMonitor
    from humanizer import Humanizer
    from notify import TelegramNotifier, PublicNotifier, AdminNotifier, UserNotifier, CompositeNotifier
    # NOTE: marubatsu_bet / marubatsu_strategy / table_selector are lazily imported ONLY
    # in local-fallback mode. In production (LAPLACE_USE_REMOTE=1) these modules must
    # NEVER be imported on the client so that the core logic / scoring formulas cannot
    # be extracted from a shipped binary.

    # Remote LAPLACE API mode (VPS-hosted logic engine)
    use_remote = os.getenv("LAPLACE_USE_REMOTE", "0").strip() in ("1", "true", "True", "yes")
    RemoteLaplaceSession = None
    RemoteTableSelector = None
    if use_remote:
        try:
            from laplace_client import RemoteLaplaceSession, RemoteTableSelector, LaplaceApiError  # noqa: F401
            send_log(f"LAPLACE Remote mode: API={os.getenv('LAPLACE_API_URL', 'http://127.0.0.1:8000')} user={os.getenv('LAPLACE_USER', 'dev-machine')}")
        except Exception as e:
            send_log(f"Remote mode requested but client import failed ({e}) — falling back to local MaruBatsuBetSession")
            use_remote = False

    chip_base = config.get("chip_base", 1.0)
    profit_target_dollars = config.get("profit_target", 50)
    loss_cut_dollars = config.get("loss_cut", 200)
    profit_session_limit = config.get("profit_session_limit", config.get("profit_sessions_limit", 0))
    try:
        profit_session_limit = int(profit_session_limit)
    except Exception:
        profit_session_limit = 0
    if profit_session_limit < 0:
        profit_session_limit = 0
    _profit_session_limit_box[0] = profit_session_limit
    dry_run = config.get("dry_run", False)
    resume = config.get("resume", False)
    user_email = (config.get("user_email") or "").strip()
    telegram_bot_token = (config.get("telegram_bot_token") or "").strip()
    telegram_chat_id = (config.get("telegram_chat_id") or "").strip()
    table_filter = config.get("table_filter")
    if not isinstance(table_filter, dict):
        table_filter = {}
    counter_params_cfg: dict = {}
    if isinstance(config.get("counter_params"), dict):
        counter_params_cfg.update(config.get("counter_params") or {})
    for _k in ("entry_window", "entry_threshold", "exit_drop3_limit", "exit_drop5_immediate"):
        if _k in config:
            counter_params_cfg[_k] = config.get(_k)
    if table_filter:
        logger.info(f"Table filter: {table_filter}")
        send_log(f"Table filter: {table_filter}")

    # Allow overriding dry_run via environment (safe for CI / first-run testing)
    if os.getenv("LAPLACE_FORCE_DRYRUN", "").strip() in ("1", "true", "True", "yes"):
        if not dry_run:
            send_log("LAPLACE_FORCE_DRYRUN=1 detected — forcing dry_run=True")
        dry_run = True
    # Verification mode: fixed table, public channel notifications
    verification_mode = (
        config.get("verification_mode", False)
        or os.getenv("LAPLACE_MODE", "").lower() == "verification"
    )
    fixed_table_name = os.getenv("LAPLACE_FIXED_TABLE", "Japanese Speed Baccarat A")
    user_label = os.getenv("LAPLACE_USER", "anon")
    user_id = os.getenv("LAPLACE_USER", "dev-machine")
    session_api_key = (config.get("site_api_key") or _SESSION_API_KEY).strip()
    supabase_state = None
    supabase_missing = False
    supabase_built = False
    resume_results = config.get("resume_results") if resume else None
    if resume:
        if user_email and session_api_key:
            supabase_state = _load_session_state_from_server(user_email, session_api_key)
            if supabase_state:
                send_log("[session] Supabase session loaded")
            else:
                supabase_missing = True
                send_log("[session] Supabase session not found/empty")
        else:
            supabase_missing = True
            send_log("[session] Supabase session skipped (missing user_email or site_api_key)")

    # Convert dollar amounts to chip units
    profit_stop_chips = max(1, int(round(profit_target_dollars / max(chip_base, 0.01))))
    loss_cut_chips = max(1, int(round(loss_cut_dollars / max(chip_base, 0.01))))

    send_log(f"Start mode: {'RESUME' if resume else 'RESET'} (dry_run={dry_run})")
    send_log(f"Config: chip_base=${chip_base} profit_target=${profit_target_dollars} (={profit_stop_chips}chips) loss_cut=${loss_cut_dollars} (={loss_cut_chips}chips)")
    if profit_session_limit:
        send_log(f"Config: profit_session_limit={profit_session_limit}")

    if resume and not supabase_state and resume_results:
        _is_counter = _effective_mode_box[0] in ("counter", "counter_flat", "counter_seq7")
        counter_set_size = 7 if _effective_mode_box[0] == "counter_seq7" else None
        built_state = _build_session_state_from_results(
            resume_results, chip_base, profit_stop_chips, loss_cut_chips,
            counter_mode=_is_counter,
            counter_set_size=counter_set_size,
        )
        if built_state:
            supabase_state = built_state
            supabase_built = True
            send_log("[session] Supabase session built from GUI results")

    mode = "DRY RUN" if dry_run else "LIVE"
    send_action(f"Starting {mode} mode...")

    # Setup notifier system
    # Public channel is for independent monitoring only, NOT for GUI BET activity
    admin_notifier = AdminNotifier()  # reads ADMIN_BOT_TOKEN / ADMIN_CHAT_ID from env
    if dry_run:
        user_notifier = UserNotifier("", "")
    else:
        if telegram_bot_token or telegram_chat_id:
            user_notifier = UserNotifier(telegram_bot_token, telegram_chat_id)
        else:
            user_notifier = UserNotifier()  # reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from env
    composite = CompositeNotifier(public=None, admin=admin_notifier, user=user_notifier)
    # Legacy alias: existing code uses `notifier` with .send(), .notify_*() — use UserNotifier
    notifier = user_notifier

    daily_date = _jst_date_str()
    daily_profit = 0.0
    daily_profit_actual = 0.0
    daily_sessions = 0
    daily_profit_sessions = 0
    daily_loss_sessions = 0
    profit_sessions_done = 0
    money_pnl_actual = 0.0
    balance_last = None
    actual_profit_ready = False
    _actual_override_logged = False
    last_balance_diff = None
    pending_settlements: list[dict] = []
    settlement_lock = threading.Lock()
    settlement_inflight = False

    def _kick_settlement_worker():
        nonlocal settlement_inflight
        if settlement_inflight:
            return
        if not pending_settlements:
            return
        if not user_email or not session_api_key:
            return
        settlement_inflight = True

        def _worker():
            nonlocal settlement_inflight
            try:
                while True:
                    with settlement_lock:
                        item = pending_settlements[0] if pending_settlements else None
                    if not item:
                        break
                    ok, err = _post_daily_settlement_retry(
                        user_email,
                        item["net_profit"],
                        session_api_key,
                        item["date"],
                    )
                    if ok:
                        send_log(f"[settle] posted {item['date']} net=${item['net_profit']:.2f}")
                        with settlement_lock:
                            if pending_settlements:
                                pending_settlements.pop(0)
                    else:
                        send_log(f"[settle] failed {item['date']}: {err}")
                        break
                    time.sleep(1)
            finally:
                settlement_inflight = False

        threading.Thread(target=_worker, daemon=True).start()

    def _enqueue_settlement(date_str: str, net_profit: float):
        with settlement_lock:
            if pending_settlements and pending_settlements[-1]["date"] == date_str:
                pending_settlements[-1]["net_profit"] = net_profit
            else:
                pending_settlements.append({"date": date_str, "net_profit": float(net_profit)})
        _kick_settlement_worker()

    def _update_actual_profit(balance: float) -> float | None:
        """残高スナップショット方式でPNLを算出。

        session.session_open_balance / session.daily_open を使って現在残高との差分を計算。
        session が対応していない場合は旧来の差分積算にフォールバック。

        Returns: last_balance_diff (前回からの残高増減、round通知用)
        """
        nonlocal balance_last, money_pnl_actual, daily_profit_actual
        nonlocal actual_profit_ready, last_balance_diff, daily_date
        last_balance_diff = None
        if not balance or balance <= 0:
            return None

        today = _jst_date_str()

        # round-level diff (Telegram round通知等で使用)
        if balance_last is not None:
            last_balance_diff = balance - balance_last
        balance_last = balance

        sess = _active_session
        # 残高スナップショット方式 (MaruBatsuBetSession が対応)
        if sess is not None and hasattr(sess, 'session_open_balance'):
            # 初期化: session_open_balance が未設定なら現残高を起点に
            if sess.session_open_balance is None:
                sess.session_open_balance = balance
                logger.info(f"[pnl] session_open_balance initialized: ${balance:.2f}")
                try:
                    sess._save_state()
                except Exception:
                    pass
            # 日付ロールオーバー or 初期化
            do = sess.daily_open or {}
            if do.get("date") != today:
                old_date = do.get("date")
                old_bal = do.get("balance")
                
                # 前日の確定PNLを VPS settlement キューに保存 (データ欠損防止)
                # balance が不正（0以下）でも、前日データが有効なら保存試行
                if old_date and isinstance(old_bal, (int, float)) and old_bal > 0:
                    if balance and balance > 0:
                        prev_pnl = balance - float(old_bal)
                        try:
                            _enqueue_settlement(old_date, prev_pnl)
                            logger.info(f"[pnl] Rollover settle {old_date}: ${prev_pnl:+.2f}")
                        except Exception as _e:
                            logger.warning(f"[pnl] Rollover settle enqueue failed: {_e}")
                    else:
                        # balance 不正時: 前日の daily_open をそのまま基準として記録
                        logger.warning(f"[pnl] Rollover with invalid balance ({balance}), using daily_open as baseline")
                
                # daily_open 更新: balance が有効なら更新、無効なら日付のみ進める
                if balance and balance > 0:
                    sess.daily_open = {"date": today, "balance": balance}
                    if old_date:
                        logger.info(f"[pnl] Date rollover {old_date}→{today}, daily_open=${balance:.2f}")
                    else:
                        logger.info(f"[pnl] daily_open initialized: ${balance:.2f} ({today})")
                else:
                    # balance 不正: 日付だけ進めて前回の balance を保持（無限ループ防止）
                    sess.daily_open = {"date": today, "balance": old_bal or 0}
                    logger.warning(f"[pnl] Date rollover with invalid balance, kept old balance: ${old_bal}")
                
                try:
                    sess._save_state()
                except Exception:
                    pass
            # PNL は常に現残高 - open の差分 (累積誤差なし)
            money_pnl_actual = balance - sess.session_open_balance
            daily_profit_actual = balance - sess.daily_open["balance"]
            # Vercel cron 日次 settle 用に最新残高を保存
            sess.current_balance = balance
            sess.last_balance_at = datetime.utcnow().isoformat() + "Z"
        else:
            # Fallback: 旧来の差分積算 (Remote等で state_dict 非対応のケース)
            if last_balance_diff is not None:
                money_pnl_actual += last_balance_diff
                daily_profit_actual += last_balance_diff

        actual_profit_ready = True
        daily_date = today
        return last_balance_diff

    def _reset_session_open(balance: float):
        """利確/損切後に session_open_balance を現残高にリセット。

        残高スナップショット方式では session_open_balance を動かすだけでPNL=0になる。
        """
        if not balance or balance <= 0:
            return
        sess = _active_session
        if sess is not None and hasattr(sess, 'session_open_balance'):
            sess.session_open_balance = balance
            try:
                sess._save_state()
            except Exception:
                pass
            logger.info(f"[pnl] session_open_balance reset to ${balance:.2f}")

    def _flush_daily_summary(force: bool = False, table_name: str = ""):
        nonlocal daily_date, daily_profit, daily_profit_actual
        nonlocal daily_sessions, daily_profit_sessions, daily_loss_sessions, actual_profit_ready
        today = _jst_date_str()
        if not force and today == daily_date:
            if pending_settlements:
                _kick_settlement_worker()
            return
        summary_profit = daily_profit_actual if actual_profit_ready else daily_profit
        if daily_sessions > 0 or abs(summary_profit) >= 0.01:
            try:
                composite.on_daily_summary(
                    daily_date,
                    daily_sessions,
                    daily_profit_sessions,
                    daily_loss_sessions,
                    summary_profit,
                    table_name,
                )
            except Exception as e:
                logger.warning(f"Daily summary notify failed: {e}")
            try:
                if user_email and session_api_key:
                    _enqueue_settlement(daily_date, summary_profit)
                else:
                    send_log("[settle] skipped (missing user_email/api_key)")
            except Exception as e:
                logger.warning(f"Daily settlement post failed: {e}")
        daily_date = today
        daily_profit = 0.0
        daily_profit_actual = 0.0
        daily_sessions = 0
        daily_profit_sessions = 0
        daily_loss_sessions = 0

    def pick_table():
        """Verification modeなら固定テーブル、それ以外は通常選定"""
        if verification_mode:
            return selector.find_best_table(fixed_name=fixed_table_name, selector_config=table_filter)
        return selector.find_best_table(selector_config=table_filter)

    def find_1_drop_table() -> tuple[str, str] | None:
        """全テーブルをスキャンしてPlayerが出ている（1落ち）テーブルを探す。

        1落ち = 最新の非タイ結果がPlayer（R）であること。
        lobby WS の _evo_table_raw_histories をポーリングするだけで
        Playwright APIは呼ばないため、WSリスナースレッドをブロックしない。
        90秒ごとにEvo iframeに触れてSESSION EXPIREDを防ぐ。
        直前に使ったテーブル（target_tid）は候補から除外してランダム選択。
        見つかったら (table_id, table_name) を返す。STOPされた場合はNone。
        """
        import random as _random
        _MIN_HISTORY = 5  # シューリセット直後の信頼性低いテーブルを除外

        def _has_1drop(raw: list) -> bool:
            """最新の非タイ結果がPlayer（R）かつ履歴が十分あるか"""
            non_tie = [e for e in raw if e.get("c") in ("B", "R")]
            return len(non_tie) >= _MIN_HISTORY and non_tie[-1].get("c") == "R"

        send_action("Scanning all tables for Player 1-drop...")
        send_log("[observe] Scanning lobby WS — looking for latest Player result on any table")
        last_heartbeat = time.time()

        while not stop_event.is_set():
            # ── ハートビート（90秒ごと）Evolution iframeタッチでSESSION EXPIRED防止 ──
            if time.time() - last_heartbeat > 90:
                try:
                    inner = executor._get_evo_inner()
                    if inner:
                        inner.evaluate("() => document.documentElement.scrollTop", timeout=3000)
                except Exception:
                    pass
                if not scraper.get_all_table_configs():
                    send_log("[observe] Lobby WS lost — reconnecting...")
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                last_heartbeat = time.time()

            # ── 全テーブルをスキャンして1落ちリストを作成 ──
            configs = scraper.get_all_table_configs()
            candidates = []
            for tid, cfg in configs.items():
                raw = scraper.get_raw_history(tid)
                if _has_1drop(raw):
                    candidates.append((tid, cfg.get("title", tid)))

            if candidates:
                # 直前に使ったテーブルを除外（テーブルバリエーション確保）
                preferred = [(tid, tname) for tid, tname in candidates if tid != target_tid]
                chosen_list = preferred if preferred else candidates
                tid, tname = _random.choice(chosen_list)
                send_action(f"Player 1落ち検出: {tname} ({len(candidates)}件中) → 入場します")
                send_log(f"[observe] Player 1-drop found on {tname} ({tid}) — {len(candidates)} candidates")
                return tid, tname

            stop_event.wait(2)

        return None  # STOPされた

    def confirm_2nd_drop() -> str:
        """テーブル内で1ハンド観察し、2落ち目もPlayerかどうかを確認する。

        BETはしない。ロビーWSはテーブル入場後に切断されるため、
        ビーズロードDOM（executor.read_bead_road）から結果を読む。
        タイは無視して継続観察。
        Returns:
          "confirmed"   — Playerが出た（2落ち確認）→ BET開始
          "invalidated" — Bankerが出た → 退室してロビー監視に戻る
          "stopped"     — STOPイベント
        """
        send_action("In table — observing for 2nd Player (no BET)...")
        send_log("[2nd-drop] Observing bead road DOM to confirm Player streak")

        # 現在のビーズロード長を記録（最大3回リトライ）
        pre_bead = ""
        for _br in range(3):
            pre_bead = executor.read_bead_road()
            if pre_bead:
                break
            time.sleep(1)
        pre_len = len(pre_bead)
        if pre_len == 0:
            send_log("[2nd-drop] Bead road empty — skipping this table")
            return "invalidated"  # ビーズロード取得不可 → 別テーブルへ
        send_log(f"[2nd-drop] pre_bead len={pre_len} tail={pre_bead[-5:] if pre_bead else ''}")

        _observe_fail = 0  # ビーズロード更新失敗カウンタ
        while not stop_event.is_set():
            # 1ハンド見送り（BETせず結果を待つ）
            if not executor.wait_for_betting_phase(timeout=180, skip_round=True):
                return "stopped"

            # ビーズロードDOMから結果を確認（最大10秒、0.5秒ポーリング）
            deadline = time.time() + 10
            _got_update = False
            while time.time() < deadline and not stop_event.is_set():
                new_bead = executor.read_bead_road()
                if len(new_bead) > pre_len:
                    _observe_fail = 0  # リセット
                    new_chars = new_bead[pre_len:]
                    send_log(f"[2nd-drop] new chars: {new_chars!r}")
                    for ch in new_chars:
                        if ch == 'P':
                            send_action("2nd Player confirmed → BET Player next hand!")
                            send_log("[2nd-drop] Player streak confirmed — starting BET")
                            return "confirmed"
                        elif ch == 'B':
                            send_action("Banker appeared — streak broken → returning to lobby")
                            send_log("[2nd-drop] Banker appeared — exit table")
                            return "invalidated"
                        # T = タイ → pre_len更新して次のハンドを待つ
                    pre_len = len(new_bead)
                    _got_update = True
                    break  # タイのみ → outer whileループで再観察
                time.sleep(0.5)
            if not _got_update:
                _observe_fail += 1
                send_log(f"[2nd-drop] Bead road not updated ({_observe_fail}/3) — re-observing")
                if _observe_fail >= 3:
                    send_log("[2nd-drop] Bead road stuck — switching table")
                    return "invalidated"

        return "stopped"

    def observe_until_1_drop(tid: str, tname: str) -> bool:
        """後方互換ラッパー: find_1_drop_table() に委譲し、結果でtarget_tid/nameを更新"""
        result = find_1_drop_table()
        if result is None:
            return False
        nonlocal target_tid, target_name
        target_tid, target_name = result
        return True

    BACCARAT_LOBBY_URL = "https://stake.com/casino/games/evolution-baccarat-lobby"

    # === Sync Mode: 推奨テーブル ===
    # 87万ハンド・5日間データの 3-filter (Reg+P/B+Pause) シミュで
    # 生存上位15テーブル。シミュ通算 +$12,815 (現状の2.7倍) / 破綻ゼロ
    # Supabaseから動的取得 (config.recommended_tables) もサポート
    SYNC_RECOMMENDED_TABLES = config.get("recommended_tables") or [
        "Korean Speed Baccarat A",
        "Speed Baccarat W",
        "Korean Speed Baccarat D",
        "Speed Baccarat X",
        "Japanese Speed Baccarat A",
        "Lotus Speed Baccarat A",
        "Thai Speed Baccarat B",
        "Lotus Speed Baccarat B",
        "Baccarat B",
        "Speed Baccarat T",
        "Stake Exclusive Speed Baccarat 1",
        "Dynasty Speed Baccarat 1",
        "Dynasty Speed Baccarat 8",
        "Korean Speed Baccarat E",
        "Japanese Speed Baccarat C",
    ]

    # 退避クールダウン: テーブル名 → 退避時刻 (epoch)
    # ロビーWS の bead road は古いシューを保持していることがあるため、
    # 退避直後の同じテーブルを即再選定すると再び Banker dominant にハマる
    _exited_tables_cooldown: dict[str, float] = {}
    EXIT_COOLDOWN_SEC = 300  # 5分間は同じテーブルを除外

    def mark_table_exited(table_name: str):
        _exited_tables_cooldown[table_name] = time.time()

    def is_table_in_cooldown(table_name: str) -> bool:
        ts = _exited_tables_cooldown.get(table_name)
        if ts is None:
            return False
        if time.time() - ts > EXIT_COOLDOWN_SEC:
            del _exited_tables_cooldown[table_name]
            return False
        return True

    def find_sync_table() -> tuple[str, str] | None:
        """推奨テーブルから規則性の高いものを選択（入場前の静的判断）

        条件:
          1. 推奨リストにあるテーブル
          2. ハンド数 >= 35（シュー約50%経過）
          3. 規則性スコア >= dynamic_threshold (70→65→60、時間経過で緩和)

        待機時間対策:
          - 60秒ごとにダイアログチェック+ iframeハートビート
          - 60秒経過: casino_detour で別ゲームに寄り道 (iframe劣化対策)
          - 60秒経過: 閾値 70→65 に緩和
          - 120秒経過: 閾値 65→60 に緩和
          - 180秒経過: 強制ピック (最高規則性のテーブル)
          - 5分ごとにStakeログイン確認
        """
        from regularity_monitor import evaluate_table, raw_history_to_results, ENTRY_THRESHOLD, MIN_HANDS_FOR_ENTRY, MAX_HANDS_FOR_ENTRY

        send_action("🔍 Syncモード: 推奨テーブルを監視中...")
        send_log(f"[Sync] 推奨テーブル: {', '.join(SYNC_RECOMMENDED_TABLES)}")
        send_log(f"[Sync] 入場条件: ハンド数≥{MIN_HANDS_FOR_ENTRY}, 規則性≥{ENTRY_THRESHOLD} (時間経過で緩和)")
        wait_start = time.time()  # find_sync_table 開始時刻 (緩和判定用)
        last_detour = time.time()  # 最後のcasino detour時刻
        last_heartbeat = time.time()
        last_login_check = time.time()
        last_status = time.time()
        scan_count = 0

        while not stop_event.is_set():
            scan_count += 1
            # ハートビート（60秒ごと）: ダイアログチェック+iframeタッチ
            if time.time() - last_heartbeat > 60:
                send_log("[Sync] 💓 ハートビート（ダイアログ+WS確認）")
                # 1. エラーダイアログ検出 → 自動dismiss
                try:
                    if not executor.check_and_dismiss_error():
                        send_log("[Sync] ⚠️ SESSION EXPIRED検出 → フルリカバリ")
                        fr = full_recovery()
                        if not fr:
                            return None
                        nonlocal target_tid, target_name
                        target_tid, target_name = fr
                        # テーブルに入ってしまったので、退室してロビーに戻す
                        try:
                            executor.exit_table()
                        except Exception:
                            pass
                except Exception as _de:
                    send_log(f"[Sync] ダイアログチェックエラー: {_de}")
                # 2. iframeタッチ
                try:
                    inner = executor._get_evo_inner()
                    if inner:
                        inner.evaluate("() => document.documentElement.scrollTop", timeout=3000)
                except Exception:
                    pass
                # 3. ロビーWS再接続
                if not scraper.get_all_table_configs():
                    send_log("[Sync] ⚠️ ロビーWS切断 → 再接続中...")
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                last_heartbeat = time.time()

            # Stakeログイン確認（5分ごと）
            if time.time() - last_login_check > 300:
                last_login_check = time.time()
                try:
                    if not scraper._is_logged_in():
                        send_log("[Sync] ⚠️ Stakeログアウト検出 → 再ログイン")
                        try:
                            scraper._login_from_lobby()
                            time.sleep(3)
                            scraper.setup_ws_intercept()
                            send_log("[Sync] ✅ 再ログイン成功")
                        except Exception as _le:
                            send_log(f"[Sync] ⚠️ 再ログイン失敗: {_le} → フルリカバリ")
                            fr = full_recovery()
                            if not fr:
                                return None
                except Exception as _ce:
                    send_log(f"[Sync] ログインチェックエラー: {_ce}")

            # === 動的閾値: 時間経過で緩和 ===
            elapsed_wait = time.time() - wait_start
            if elapsed_wait < 60:
                dynamic_threshold = ENTRY_THRESHOLD  # 70
            elif elapsed_wait < 120:
                dynamic_threshold = max(ENTRY_THRESHOLD - 5, 65)  # 65
            else:
                dynamic_threshold = max(ENTRY_THRESHOLD - 10, 60)  # 60
            force_pick = elapsed_wait >= 180  # 3分経過で強制ピック

            # === Casino detour: 不安定化要因になりうるためデフォルト無効 ===
            if ENABLE_CASINO_DETOUR and time.time() - last_detour > 60 and elapsed_wait > 60:
                send_log(f"[Sync] ⏱️ 60秒経過 → casino detour で iframe維持")
                if casino_detour(reason="ロビー待機中"):
                    last_detour = time.time()
                    last_heartbeat = time.time()  # detour成功でheartbeat更新

            configs = scraper.get_all_table_configs()
            name_to_tid = {cfg.get("title", ""): tid for tid, cfg in configs.items()}

            # === Pattern モード: classify_pattern を有効化 ===
            _is_pattern_mode = (_effective_mode_box[0] in ("pattern", "pattern_test"))
            if _is_pattern_mode:
                try:
                    from pattern_classifier import classify_pattern
                except Exception:
                    classify_pattern = None
            else:
                classify_pattern = None

            # 推奨テーブルを順に評価
            # best は7要素タプル: (tid, name, reg, pc, bc, pr, priority)
            #   priority: 2=テレコ+ニコ混合(★最強), 1=テレコ崩れ(中位), 0=BET禁止
            #   pattern モード以外は priority=1 で扱う
            best = None
            force_best = None  # 強制ピック用 (閾値未達でも最高reg)
            table_reports = []
            for rec_name in SYNC_RECOMMENDED_TABLES:
                tid = name_to_tid.get(rec_name)
                if not tid:
                    table_reports.append(f"❌ {rec_name}: ロビーに存在しません")
                    continue
                # クールダウン中のテーブルはスキップ
                if is_table_in_cooldown(rec_name):
                    remain = int(EXIT_COOLDOWN_SEC - (time.time() - _exited_tables_cooldown[rec_name]))
                    table_reports.append(f"⏰ {rec_name}: クールダウン中 (残{remain}s)")
                    continue
                raw = scraper.get_raw_history(tid)
                results = raw_history_to_results(raw)
                eval_result = evaluate_table(results)
                reg = eval_result['regularity']
                hands = eval_result['hands']
                p_ratio = eval_result.get('p_ratio', 0.5)
                p_count = eval_result.get('p_count', 0)
                b_count = eval_result.get('b_count', 0)

                # === Pattern モード: 大路罫線パターンを判定 ===
                pattern_priority = 1  # default: 中位
                pattern_label = ""
                if _is_pattern_mode and classify_pattern:
                    try:
                        seq_str = ''.join(r for r in results if r in ('P', 'B', 'T'))
                        pat = classify_pattern(seq_str)
                        pattern_label = f" pat={pat}"
                        if pat == "縦流れ":
                            pattern_priority = 3  # ドラゴン優先
                        elif pat == "テレコ+ニコ混合":
                            pattern_priority = 2  # ★最強 (ROI +12〜15%)
                        elif pat == "テレコ崩れ":
                            pattern_priority = 1  # 中位 (ROI +0〜+7%)
                        elif pat in ("ブリッジ", "ニコニコ・ニコイチ", "不規則", "偏在"):
                            pattern_priority = 0  # BET禁止
                        else:  # 不明
                            pattern_priority = 0  # シュー序盤は選ばない
                    except Exception:
                        pattern_priority = 1

                # 強制ピック候補 (規則性のみ、ハンド数最低35とP比率0.4以上)
                # pattern モードでは BET禁止パターンは強制ピック対象外
                if hands >= MIN_HANDS_FOR_ENTRY and p_ratio >= 0.40 and pattern_priority > 0:
                    if force_best is None or reg > force_best[2]:
                        force_best = (tid, rec_name, reg, p_count, b_count, p_ratio, pattern_priority)

                if hands < MIN_HANDS_FOR_ENTRY:
                    table_reports.append(f"⏳ {rec_name}: {hands}ハンド（{MIN_HANDS_FOR_ENTRY}まで待機）")
                elif hands > MAX_HANDS_FOR_ENTRY:
                    table_reports.append(f"⏰ {rec_name}: {hands}ハンド（シュー終盤、残り少ない → スキップ）")
                elif reg < dynamic_threshold:
                    table_reports.append(f"⚠️ {rec_name}: {hands}h reg={reg:.0f} P{p_count}/B{b_count}（規則性<{dynamic_threshold}）")
                elif p_ratio < 0.42:
                    table_reports.append(f"⚠️ {rec_name}: {hands}h reg={reg:.0f} P{p_count}/B{b_count}（Banker dominant P{p_ratio:.0%}）")
                elif _is_pattern_mode and pattern_priority == 0:
                    table_reports.append(f"❌ {rec_name}: {hands}h reg={reg:.0f}{pattern_label} → BET禁止パターン")
                else:
                    star = "★★" if pattern_priority == 2 else "★" if pattern_priority == 1 else ""
                    table_reports.append(f"✅ {rec_name}: {hands}h reg={reg:.0f} P{p_count}/B{b_count}{pattern_label} {star}クリア! (閾値{dynamic_threshold})")
                    candidate = (tid, rec_name, reg, p_count, b_count, p_ratio, pattern_priority)
                    # priority 優先、同 priority なら reg 優先
                    if best is None:
                        best = candidate
                    elif (pattern_priority, reg) > (best[6], best[2]):
                        best = candidate

            # 15秒ごとに候補状況をログ出力
            if time.time() - last_status > 15:
                for report in table_reports:
                    send_log(f"[Sync] {report}")
                last_status = time.time()

            if best:
                tid, tname, reg, p_count, b_count, p_ratio, pri = best
                pri_label = "★★テレコ+ニコ混合" if pri == 2 else "★テレコ崩れ" if pri == 1 else ""
                send_action(f"🎯 Sync: {tname} に入場 (reg={reg:.0f} P{p_count}/B{b_count}) {pri_label}")
                send_log(f"[Sync] ★ 入場決定: {tname} 規則性={reg:.0f} P{p_count}/B{b_count} (P{p_ratio:.0%}) {pri_label} [閾値{dynamic_threshold}]")
                return tid, tname

            # 強制ピック (3分経過): 通常条件を満たすテーブルがなくても最高規則性を選ぶ
            if force_pick and force_best:
                tid, tname, reg, p_count, b_count, p_ratio, pri = force_best
                send_action(f"⚡ 強制ピック (3分経過): {tname} reg={reg:.0f}")
                send_log(f"[Sync] 🆘 強制ピック: {tname} 規則性={reg:.0f} P{p_count}/B{b_count} (P{p_ratio:.0%}) — 待機回避")
                return tid, tname

            # 待機メッセージ（30秒ごと）
            if scan_count % 10 == 1:
                _wait_sec = int(elapsed_wait)
                send_action(f"⏱️ 待機中 ({_wait_sec}s) — 閾値{dynamic_threshold}")

            stop_event.wait(3)

        return None

    def find_pattern_table() -> tuple[str, str] | None:
        """Pattern モード専用: エボリューション全テーブルから最強パターンを探す

        SYNC_RECOMMENDED_TABLES (15件) ではなく、scraper.get_all_table_configs() で
        取得できる全 baccarat テーブル (除外フィルタ済) を対象にスキャンする。

        scraper の _TABLE_EXCLUDE で除外:
          - salon / prive / elite vip / first person / rng
          - lightning / prosperity / golden wealth / peek / control squeeze
          - no commission / super speed / always 9

        パターン分類:
          - テレコ+ニコ混合 (priority 2) → ★最強 (ROI +12〜15%)
          - テレコ崩れ (priority 1)      → 中位 (ROI +0〜+7%)
          - 縦流れ/ブリッジ/不明         → BET禁止

        条件:
          - hands ≥ MIN_HANDS_FOR_ENTRY (35)
          - reg ≥ ENTRY_THRESHOLD (時間で緩和)
          - p_ratio ≥ 0.42 (Banker dominant 排除)
          - パターンが BET 可能 (priority > 0)
        """
        from regularity_monitor import evaluate_table, raw_history_to_results, ENTRY_THRESHOLD, MIN_HANDS_FOR_ENTRY, MAX_HANDS_FOR_ENTRY
        try:
            from pattern_classifier import classify_pattern
        except Exception:
            classify_pattern = None

        send_action("🔍 Pattern モード: 全テーブルスキャン中...")
        send_log("[Pattern] 全エボリューションバカラテーブルから最強パターンを探索")
        send_log(f"[Pattern] 除外: lightning/super speed/always 9/salon/elite vip 等")
        send_log(f"[Pattern] 入場条件: {MIN_HANDS_FOR_ENTRY}≤ハンド数≤{MAX_HANDS_FOR_ENTRY}, 規則性≥{ENTRY_THRESHOLD}")

        wait_start = time.time()
        last_heartbeat = time.time()
        last_login_check = time.time()
        last_status = time.time()
        last_detour = time.time()
        scan_count = 0

        while not stop_event.is_set():
            scan_count += 1

            # === ハートビート (60秒ごと) ===
            if time.time() - last_heartbeat > 60:
                send_log("[Pattern] 💓 ハートビート")
                try:
                    if not executor.check_and_dismiss_error():
                        send_log("[Pattern] ⚠️ SESSION EXPIRED → フルリカバリ")
                        fr = full_recovery()
                        if not fr:
                            return None
                        nonlocal target_tid, target_name
                        target_tid, target_name = fr
                        try:
                            executor.exit_table()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    inner = executor._get_evo_inner()
                    if inner:
                        inner.evaluate("() => document.documentElement.scrollTop", timeout=3000)
                except Exception:
                    pass
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                last_heartbeat = time.time()

            # === Stake ログイン確認 (5分ごと) ===
            if time.time() - last_login_check > 300:
                last_login_check = time.time()
                try:
                    if not scraper._is_logged_in():
                        send_log("[Pattern] ⚠️ Stakeログアウト → 再ログイン")
                        try:
                            scraper._login_from_lobby()
                            time.sleep(3)
                            scraper.setup_ws_intercept()
                        except Exception:
                            fr = full_recovery()
                            if not fr:
                                return None
                except Exception:
                    pass

            # === 動的閾値: 時間経過で緩和 ===
            elapsed_wait = time.time() - wait_start
            if elapsed_wait < 60:
                dynamic_threshold = ENTRY_THRESHOLD  # 70
            elif elapsed_wait < 120:
                dynamic_threshold = max(ENTRY_THRESHOLD - 5, 65)
            else:
                dynamic_threshold = max(ENTRY_THRESHOLD - 10, 60)

            # === Casino detour: 不安定化要因になりうるためデフォルト無効 ===
            if ENABLE_CASINO_DETOUR and time.time() - last_detour > 60 and elapsed_wait > 60:
                send_log("[Pattern] ⏱️ 60秒経過 → casino detour で iframe維持")
                if casino_detour(reason="Pattern待機中"):
                    last_detour = time.time()
                    last_heartbeat = time.time()

            # === 全テーブルスキャン ===
            configs = scraper.get_all_table_configs()
            if not configs:
                if time.time() - last_status > 15:
                    send_log("[Pattern] ⏳ ロビーWS にテーブル情報なし — 待機")
                    last_status = time.time()
                stop_event.wait(3)
                continue

            best = None  # (tid, name, reg, pc, bc, pr, priority, pattern)
            scanned = 0
            tnm_count = 0   # テレコ+ニコ混合 候補数
            tk_count = 0    # テレコ崩れ 候補数
            tate_count = 0  # 縦流れ 候補数
            reject_count = 0  # BET禁止 数

            for tid, cfg in configs.items():
                rec_name = cfg.get("title", tid)
                # クールダウン中はスキップ
                if is_table_in_cooldown(rec_name):
                    continue
                raw = scraper.get_raw_history(tid)
                results = raw_history_to_results(raw)
                eval_result = evaluate_table(results)
                reg = eval_result['regularity']
                hands = eval_result['hands']
                p_ratio = eval_result.get('p_ratio', 0.5)
                p_count = eval_result.get('p_count', 0)
                b_count = eval_result.get('b_count', 0)

                # ハンド数 / 規則性 / P比率 フィルタ
                # MAX_HANDS_FOR_ENTRY: シュー終盤すぎ (残り少ない) を排除
                if hands < MIN_HANDS_FOR_ENTRY or hands > MAX_HANDS_FOR_ENTRY:
                    continue
                if reg < dynamic_threshold or p_ratio < 0.42:
                    continue

                scanned += 1

                # パターン分類
                pattern_priority = 0
                pat = "不明"
                if classify_pattern:
                    try:
                        seq_str = ''.join(r for r in results if r in ('P', 'B', 'T'))
                        pat = classify_pattern(seq_str)
                        b_lead = b_count - p_count
                        if b_lead >= 0:
                            pattern_priority = 0
                            reject_count += 1
                        elif pat == "縦流れ":
                            pattern_priority = 3
                            tate_count += 1
                        elif pat == "テレコ+ニコ混合":
                            pattern_priority = 2
                            tnm_count += 1
                        elif pat == "テレコ崩れ":
                            pattern_priority = 1
                            tk_count += 1
                        else:
                            pattern_priority = 0
                            reject_count += 1
                    except Exception:
                        pattern_priority = 0

                if pattern_priority == 0:
                    continue

                # priority 優先、同 priority なら reg 優先
                candidate = (tid, rec_name, reg, p_count, b_count, p_ratio, pattern_priority, pat)
                if best is None:
                    best = candidate
                elif (pattern_priority, reg) > (best[6], best[2]):
                    best = candidate

            # === 状況ログ (15秒ごと) ===
            if time.time() - last_status > 15:
                send_log(
                    f"[Pattern] スキャン: 全{len(configs)}台 / 候補{scanned}台 / "
                    f"縦流れ★★★={tate_count} / テレコ+ニコ混合★★={tnm_count} / テレコ崩れ★={tk_count} / "
                    f"BET禁止={reject_count} (閾値reg≥{dynamic_threshold})"
                )
                last_status = time.time()
                _wait_sec = int(elapsed_wait)
                send_action(f"⏱️ Pattern待機 ({_wait_sec}s) — ★★★{tate_count} ★★{tnm_count} ★{tk_count}")

            # === 結果 ===
            if best:
                tid, tname, reg, pc, bc, pr, pri, pat = best
                if pri == 3:
                    pri_label = "★★★縦流れ"
                elif pri == 2:
                    pri_label = "★★テレコ+ニコ混合"
                else:
                    pri_label = "★テレコ崩れ"
                send_action(f"🎯 Pattern: {tname} に入場 ({pri_label} reg={reg:.0f})")
                send_log(
                    f"[Pattern] ★ 入場決定: {tname} {pri_label} "
                    f"reg={reg:.0f} P{pc}/B{bc} (P{pr:.0%}) [閾値{dynamic_threshold}]"
                )
                with scraper._lock:
                    scraper._target_table_ids.add(tid)
                    scraper._target_table_names[tid] = tname
                    scraper._new_shoe_signals[tid] = False
                    scraper._shoe_epochs[tid] = int(time.time())
                return tid, tname

            stop_event.wait(3)

        return None

    def find_table() -> tuple[str, str] | None:
        """モードに応じて適切なテーブル選定関数を呼ぶ"""
        if _effective_mode_box[0] in ("pattern", "pattern_test"):
            return find_pattern_table()
        return find_sync_table()

    def check_sync_regularity(tid) -> dict:
        """動的監視: 現在のテーブルの規則性をチェック（BET中）

        ロビーWSではなくexecutor.read_bead_road()でDOMから読む
        （入場後はロビーWSが切断されるため）
        """
        from regularity_monitor import evaluate_table
        try:
            bead = executor.read_bead_road()
            if bead:
                return evaluate_table(list(bead))
        except Exception as e:
            send_log(f"[Sync-Monitor] ⚠️ ビーズロード読み取り失敗: {e}")
        return {'regularity': 0, 'hands': 0, 'can_enter': False, 'should_exit': False}

    def full_recovery() -> tuple[str, str] | None:
        """最終手段フルリカバリ: ページ完全リロード→ログイン確認→WS再接続→テーブル再選定

        iframe壊死・WS接続不能・entry連続失敗など、通常のリカバリで復旧できない場合の最終手段。
        正常に動いている既存ロジックには干渉しない。
        Returns: (table_id, table_name) or None (STOP時)
        """
        nonlocal target_tid, target_name
        send_action("Full recovery — reloading page...")
        send_log("[recovery] Starting full page recovery")

        # 1. テーブル状態をクリア
        try:
            executor.game_ws.reset()
            executor._reset_state()
        except Exception:
            pass

        # 2. ページを完全にリロード（lobby URLに直接遷移）
        try:
            send_log("[recovery] Navigating to lobby...")
            scraper.page.goto(BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
        except Exception as e:
            send_log(f"[recovery] page.goto failed: {e}")
            # それでも続行を試みる

        if stop_event.is_set():
            return None

        # 3. ログイン状態を確認、必要なら再ログイン
        # SPAのハイドレーション完了を待つため最大25秒ポーリング
        # （5秒だけだとReactが描画前で誤検知し、不要な再ログインに入る）
        try:
            logged_in = False
            for _wait_attempt in range(25):
                if stop_event.is_set():
                    return None
                if scraper._is_logged_in():
                    logged_in = True
                    break
                time.sleep(1)

            if not logged_in:
                send_log("[recovery] Not logged in — attempting re-login...")
                send_action("Re-logging in to Stake...")
                scraper._login_from_lobby()
                time.sleep(3)
                send_log("[recovery] Re-login completed")
            else:
                send_log("[recovery] Login state confirmed")
        except Exception as e:
            send_log(f"[recovery] Re-login failed: {e}")
            # それでもWS接続を試みる

        if stop_event.is_set():
            return None

        # 4. WS傍受を再設定
        try:
            send_log("[recovery] Reconnecting lobby WS...")
            scraper.setup_ws_intercept()
            send_log("[recovery] Lobby WS reconnected")
        except Exception as e:
            send_log(f"[recovery] WS reconnect failed: {e}")

        if stop_event.is_set():
            return None

        # 4.5. Evolution iframe の機能的存在確認
        # URL マッチだけでは不十分 (死んだ iframe / ルーレット残骸 / TRY AGAIN 表示中も
        # frame URL は "evo-games.com/frontend/" を含む)。
        # frame 存在 + URL が baccarat 系 + エラーダイアログ無し の3条件で判定。
        # 不健全なら casino_detour → Lv4a (Page rebuild) → 中断 の順でエスカレーション。
        def _iframe_healthy() -> tuple[bool, str]:
            """iframe が機能的に生存しているか深くチェック
            Returns: (healthy, reason)
            """
            try:
                frames = executor._get_evo_frames()
                if not frames:
                    return False, "no frames"
                url0 = (frames[0].url or "").lower()
                if "roulette" in url0:
                    return False, f"roulette残骸: {url0[:60]}"
                if "baccarat" not in url0 and "category=" not in url0:
                    return False, f"non-baccarat: {url0[:60]}"
                # エラーダイアログチェック (SESSION EXPIRED 等は False を返す)
                try:
                    if not executor.check_and_dismiss_error():
                        return False, "error dialog (SESSION EXPIRED/TRY AGAIN失敗)"
                except Exception:
                    pass
                return True, "ok"
            except Exception as e:
                return False, f"exception: {e}"

        try:
            iframe_alive, _hr = _iframe_healthy()
            if iframe_alive:
                send_log(f"[recovery] ✅ iframe healthy ({_hr})")
            else:
                if ENABLE_CASINO_DETOUR:
                    send_log(f"[recovery] ⚠️ Evolution iframe unhealthy ({_hr}) → attempting detour recovery")
                    send_action("Evolution iframe unhealthy — trying detour")
                    # Evolution game URL を順番に試行
                    for revival_url in EVOLUTION_GAME_URLS:
                        game_name = revival_url.split('/')[-1]
                        send_log(f"[recovery] attempting via: {game_name}")
                        if casino_detour(reason=f"iframe復活({game_name})", target_url=revival_url):
                            time.sleep(3)
                            h, hr = _iframe_healthy()
                            if h:
                                send_log(f"[recovery] ✅ iframe recovered via {game_name} ({hr})")
                                iframe_alive = True
                                break
                            else:
                                send_log(f"[recovery] ⚠️ still unhealthy via {game_name} ({hr}) — trying next")
                else:
                    send_log(f"[recovery] ⚠️ Evolution iframe unhealthy ({_hr}) → detour disabled, falling back to Lv4a")

                # === casino_detour 失敗 → Lv4a (Page rebuild) を強制実行 ===
                if not iframe_alive:
                    send_log("[recovery] ❌ all casino_detour failed → forcing Lv4a (page rebuild)")
                    send_action("🔧 Lv4a: Page rebuild — forced for iframe recovery")
                    try:
                        if scraper.rebuild_page():
                            send_log("[recovery-lv4a] ✅ new page created — re-visiting lobby")
                            try:
                                scraper.page.goto(BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                                time.sleep(10)  # SPA hydration 待機
                                # ログイン確認
                                for _lw in range(20):
                                    if scraper._is_logged_in():
                                        break
                                    time.sleep(1)
                                # WS 再接続
                                try:
                                    scraper.setup_ws_intercept()
                                    send_log("[recovery-lv4a] Lobby WS reconnected")
                                except Exception as _wse:
                                    send_log(f"[recovery-lv4a] WS reconnect exception: {_wse}")
                                time.sleep(3)
                                # iframe 健全性確認 (機能チェック)
                                lv4a_h, lv4a_hr = _iframe_healthy()
                                if lv4a_h:
                                    send_log(f"[recovery-lv4a] ✅ iframe healthy after rebuild ({lv4a_hr})")
                                    iframe_alive = True
                                else:
                                    if ENABLE_CASINO_DETOUR:
                                        # rebuild 後でも不健全 → もう一度 casino_detour
                                        send_log(f"[recovery-lv4a] ⚠️ still unhealthy after rebuild ({lv4a_hr}) — attempting detour")
                                        for revival_url in EVOLUTION_GAME_URLS:
                                            if stop_event.is_set():
                                                return None
                                            if casino_detour(reason="lv4a後detour", target_url=revival_url):
                                                time.sleep(3)
                                                redet_h, redet_hr = _iframe_healthy()
                                                if redet_h:
                                                    send_log(f"[recovery-lv4a] ✅ iframe healthy via rebuild + detour ({redet_hr})")
                                                    iframe_alive = True
                                                    break
                            except Exception as _gse:
                                send_log(f"[recovery-lv4a] lobby goto exception: {_gse}")
                    except Exception as _re:
                        send_log(f"[recovery-lv4a] rebuild_page exception: {_re}")

                    if not iframe_alive:
                        send_log("[recovery] ❌ Lv4a failed too — aborting full_recovery (prevent stale table)")
                        send_action("Recovery failed — iframe revival impossible")
                        rb = restart_browser("iframe revival failed (Lv4a)")
                        if rb:
                            return rb
                        return None
        except Exception as _eve:
            send_log(f"[recovery] iframe check exception: {_eve}")

        if stop_event.is_set():
            return None

        # 5. テーブル再選定
        send_action("Recovery — selecting table...")
        target_tid = None
        target_name = None
        for _rt in range(5):
            if stop_event.is_set():
                return None
            best = pick_table()
            if best:
                target_tid = best.table_id
                target_name = best.title
                with scraper._lock:
                    scraper._target_table_ids.add(target_tid)
                    scraper._target_table_names[target_tid] = target_name
                    scraper._new_shoe_signals[target_tid] = False
                    scraper._shoe_epochs[target_tid] = int(time.time())
                send_action(f"Recovery complete — picked {target_name}")
                send_log(f"[recovery] Table selected: {target_name}")
                return target_tid, target_name
            else:
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                if stop_event.wait(10):
                    return None

        send_log("[recovery] Full recovery failed — no table available")
        # === Lv4a: Page 破棄 + 新規 Page (最終手段) ===
        # 通常の full_recovery で復活できなかった場合、
        # ページ自体を破棄して新規作成 (Cookie は Browser context 側で維持)
        send_action("🔧 Lv4a: Page rebuild — attempting new page")
        send_log("[recovery-lv4a] attempting page rebuild")
        try:
            if scraper.rebuild_page():
                send_log("[recovery-lv4a] ✅ new page created — re-visiting lobby")
                # 新しいページで lobby にアクセス
                try:
                    scraper.page.goto(BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(10)  # SPA hydration 待機
                    # ログイン確認
                    logged_in_retry = False
                    for _lw in range(20):
                        if scraper._is_logged_in():
                            logged_in_retry = True
                            break
                        time.sleep(1)
                    if not logged_in_retry:
                        send_log("[recovery-lv4a] ⚠️ login unconfirmed on new page — continuing")
                    # WS 再接続
                    try:
                        scraper.setup_ws_intercept()
                        send_log("[recovery-lv4a] Lobby WS reconnected")
                    except Exception as _wse:
                        send_log(f"[recovery-lv4a] WS reconnect exception: {_wse}")
                    # テーブル選定リトライ
                    time.sleep(3)
                    for _rt in range(3):
                        if stop_event.is_set():
                            return None
                        best = pick_table()
                        if best:
                            target_tid = best.table_id
                            target_name = best.title
                            with scraper._lock:
                                scraper._target_table_ids.add(target_tid)
                                scraper._target_table_names[target_tid] = target_name
                                scraper._new_shoe_signals[target_tid] = False
                                scraper._shoe_epochs[target_tid] = int(time.time())
                            send_action(f"✅ Lv4a success — {target_name}")
                            send_log(f"[recovery-lv4a] ✅ Table selected: {target_name}")
                            return target_tid, target_name
                        if stop_event.wait(5):
                            return None
                except Exception as _gse:
                    send_log(f"[recovery-lv4a] lobby goto exception: {_gse}")
        except Exception as _re:
            send_log(f"[recovery-lv4a] rebuild_page exception: {_re}")
        send_log("[recovery-lv4a] ❌ Lv4a failed too — giving up entirely")
        rb = restart_browser("full_recovery exhausted")
        if rb:
            return rb
        return None

    def observe_one_hand_no_bet() -> str | None:
        """sync_pause モード: BET せず1ハンド観戦して bead road から結果を読む。
        Returns: 'P' | 'B' | None (失敗/STOP)
        Tieは無視（カウントしない、次の非Tie結果まで待つ）
        """
        try:
            pre_bead = executor.read_bead_road() or ""
        except Exception:
            pre_bead = ""
        pre_len = len(pre_bead)

        # 1ハンド見送り (skip_round=True で現在のBET phaseをスキップ)
        if not executor.wait_for_betting_phase(timeout=120, skip_round=True):
            return None

        # bead road を最大15秒ポーリング
        deadline = time.time() + 15
        while time.time() < deadline:
            if stop_event.is_set():
                return None
            try:
                new_bead = executor.read_bead_road() or ""
            except Exception:
                new_bead = ""
            if len(new_bead) > pre_len:
                new_chars = new_bead[pre_len:]
                # 新しい非タイ結果を返す
                for ch in new_chars:
                    if ch in ('P', 'B'):
                        return ch
                # タイのみだったら更に待つ
                pre_len = len(new_bead)
            time.sleep(0.3)
        return None

    # Startup broadcast (admin + user + public if verification)
    try:
        composite.on_startup(user_label, {
            "dry_run": dry_run,
            "chip_base": chip_base,
            "profit_target": profit_target_dollars,
            "loss_cut": loss_cut_dollars,
            "verification_mode": verification_mode,
        }, fixed_table_name if verification_mode else "auto-select")
    except Exception as e:
        logger.warning(f"Startup notification failed: {e}")

    # === Browser launch ===
    send_action("Launching browser...")
    scraper = BaccaratScraper()
    scraper.table_name = "all"

    try:
        scraper.start()
    except Exception as e:
        send_log(f"Browser launch failed: {e}")
        send_action("Browser launch failed")
        return

    send_action("Browser ready. Waiting for Evolution WS...")
    scraper.setup_ws_intercept()

    if scraper._evo_ws_connected:
        send_action("Evolution WS connected")
    else:
        send_action("Evolution WS timeout — continuing...")

    # === Table Selector ===
    send_action("Loading table data...")
    time.sleep(12)  # configs/histories/playersCount の初期ロード待機

    _selector_client = None
    _user_id = user_id
    if use_remote:
        # All scoring / exclusion / threshold logic runs on the VPS.
        # This client never ships table_selector.py.
        from laplace_client import LaplaceClient as _LaplaceClient
        _api_url = os.getenv("LAPLACE_API_URL", "http://127.0.0.1:8000")
        _api_key = os.getenv("LAPLACE_API_KEY", "")
        _selector_client = _LaplaceClient(_api_url, _api_key)
        selector = RemoteTableSelector(scraper, _selector_client, _user_id)
    else:
        # Local fallback — requires table_selector.py on disk
        from table_selector import TableSelector
        selector = TableSelector(scraper)
    humanizer = Humanizer(cfg.HUMANIZE_CONFIG)
    executor_config = {"demo_mode": dry_run}
    executor = BetExecutor(scraper.page, scraper.game_ws, executor_config, humanizer=humanizer)

    # === Counter mode (テレコ逆張り) ===
    if _effective_mode_box[0] in ("counter", "counter_flat", "counter_seq7"):
        from counter_logic import (
            compute_column_lengths,
            decide_counter_bet,
            is_tereko_state,
            short_rate,
            should_exit,
            apply_optimal_params,
            ENTRY_WINDOW,
            FLAT_BET_AMOUNT,
            SEARCH_INTERVAL,
        )
        from regularity_monitor import raw_history_to_results

        # Supabase から最新パラメータを読み込み
        try:
            if apply_optimal_params():
                from counter_logic import ENTRY_WINDOW, ENTRY_THRESHOLD, EXIT_DROP3_LIMIT, EXIT_DROP5_IMMEDIATE
                send_log(f"[counter] Cloud params loaded: W={ENTRY_WINDOW} T={ENTRY_THRESHOLD} D3={EXIT_DROP3_LIMIT} D5={EXIT_DROP5_IMMEDIATE}")
            else:
                send_log("[counter] Using default params (cloud unavailable)")
        except Exception as e:
            send_log(f"[counter] Param load error: {e} — using defaults")
        if counter_params_cfg:
            _apply_counter_params(counter_params_cfg, "gui")

        is_flat = (_effective_mode_box[0] == "counter_flat")
        counter_set_size = 7 if _effective_mode_box[0] == "counter_seq7" else None
        counter_session = None
        if not is_flat:
            try:
                from marubatsu_bet import MaruBatsuBetSession
                counter_session = MaruBatsuBetSession(
                    executor=executor,
                    notifier=notifier,
                    chip_base=chip_base,
                    loss_cut=loss_cut_chips,
                    profit_stop=profit_stop_chips,
                    dry_run=dry_run,
                    resume=resume,
                    counter_mode=True,
                    counter_set_size=counter_set_size,
                )
            except Exception as e:
                send_log(f"[counter] FATAL: MaruBatsuBetSession init failed: {e}")
                return
        # For live config updates UI -> engine
        _active_session = counter_session
        if counter_session is not None and supabase_state:
            if _apply_session_state(counter_session, supabase_state):
                send_log("[counter] Supabase session restored")
                if supabase_built or supabase_missing:
                    if _backfill_session_state(user_email, counter_session, user_id, session_api_key):
                        send_log("[counter] Supabase session saved from GUI results")
        elif counter_session is not None and supabase_missing:
            if _backfill_session_state(user_email, counter_session, user_id, session_api_key):
                send_log("[counter] Supabase session backfilled from local state")

        entry_fail_streak = 0
        result_timeout_streak = 0
        no_configs_streak = 0

        def _restart_counter_browser(reason: str) -> bool:
            nonlocal scraper, executor, counter_session, entry_fail_streak, result_timeout_streak, no_configs_streak, last_bead
            send_action(f"🔁 Browser restart: {reason}")
            send_log(f"[counter] browser restart: {reason}")
            try:
                scraper.stop()
            except Exception:
                pass
            time.sleep(2)
            scraper = BaccaratScraper()
            scraper.table_name = "all"
            try:
                scraper.start()
            except Exception as e:
                send_log(f"[counter] Browser launch failed: {e}")
                return False
            try:
                scraper.setup_ws_intercept()
            except Exception:
                pass
            time.sleep(12)
            executor = BetExecutor(scraper.page, scraper.game_ws, executor_config, humanizer=humanizer)
            try:
                if counter_session is not None:
                    counter_session.executor = executor
            except Exception:
                pass
            entry_fail_streak = 0
            result_timeout_streak = 0
            no_configs_streak = 0
            last_bead = ""
            return True

        def _get_table_title(tid: str) -> str:
            cfgs = scraper.get_all_table_configs()
            return (cfgs.get(tid, {}) or {}).get("title", tid)

        def _find_best_tereko_table() -> tuple[str, str, float] | None:
            cfgs = scraper.get_all_table_configs()
            if not cfgs:
                return None
            best: tuple[str, str, float] | None = None
            for tid, cfg in cfgs.items():
                tname = cfg.get("title", tid)
                if is_table_in_cooldown(tname):
                    continue
                raw = scraper.get_raw_history(tid)
                results = raw_history_to_results(raw)
                cols = compute_column_lengths(results)
                if not is_tereko_state(cols):
                    continue
                rate = short_rate(cols, ENTRY_WINDOW)
                if best is None or rate > best[2]:
                    best = (tid, tname, rate)
            return best

        def _last_non_tie_from_seq(seq: str) -> str | None:
            for ch in reversed(seq):
                if ch in ("P", "B"):
                    return ch
            return None

        def _wait_bead_change(prev_bead: str, timeout_sec: float = 180.0) -> tuple[str, str] | None:
            deadline = time.time() + timeout_sec
            while time.time() < deadline and not stop_event.is_set():
                # Auto-dismiss TRY AGAIN/BACK TO LOBBY overlays.
                if not executor.check_and_dismiss_error():
                    return None
                bead = executor.read_bead_road() or ""
                if bead and bead != prev_bead:
                    last = None
                    for ch in reversed(bead):
                        if ch in ("P", "B", "T"):
                            last = ch
                            break
                    if last:
                        return bead, last
                time.sleep(0.5)
            return None

        # State
        current_tid: str | None = None
        current_name: str | None = None
        current_short_rate: float = 0.0
        last_non_tie: str | None = None  # 'P' or 'B'
        columns_since_entry: list[int] = []
        current_col_len = 0
        current_col_side: str | None = None  # 'P' or 'B'
        last_bead = ""

        # Money PNL (GUI smooth update uses round_profit)
        money_pnl = 0.0
        flat_total_bets = 0
        flat_wins = 0
        flat_losses = 0
        flat_ties = 0
        flat_session_count = 0
        chip_fail_streak = 0

        send_action("Counter mode starting...")
        send_log(f"[counter] mode={_effective_mode_box[0]} entry={int(ENTRY_WINDOW)}cols short>={int(100*0.85)}%")

        while not stop_event.is_set():
            # Table selection / entry
            if current_tid is None:
                send_phase("scanning")
                send_log("[counter] Scanning...")
                # Lobby WS health
                if not scraper.get_all_table_configs():
                    no_configs_streak += 1
                    if no_configs_streak >= 3:
                        send_log("[counter] No lobby data — reconnecting")
                        try:
                            scraper.setup_ws_intercept()
                        except Exception:
                            pass
                        no_configs_streak = 0
                    if stop_event.wait(3):
                        break
                    continue
                no_configs_streak = 0
                best = _find_best_tereko_table()
                if not best:
                    send_log(f"[counter] No target. Waiting {SEARCH_INTERVAL}s")
                    if stop_event.wait(SEARCH_INTERVAL):
                        break
                    continue
                current_tid, current_name, current_short_rate = best
                columns_since_entry = []
                current_col_len = 0
                current_col_side = None
                last_non_tie = None
                last_bead = ""

                send_log(f"[counter] Entered: {current_name} ({current_short_rate:.0%})")
                send_action(f"Entering {current_name}...")
                send_phase("entering", current_name)
                if not executor.enter_table(current_tid, current_name):
                    send_log(f"[counter] Entry failed: {current_name}")
                    entry_fail_streak += 1
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    if entry_fail_streak >= 3:
                        _restart_counter_browser("enter_table failed x3")
                    current_tid = None
                    current_name = None
                    continue

                entry_fail_streak = 0
                # In-table validation: lobby histories can be stale; avoid betting on a new shoe (bead empty).
                # Wait briefly for bead-road to appear, otherwise treat as shuffle/new-shoe and exit.
                last_bead = ""
                for _vb in range(10):  # up to ~5s
                    if stop_event.is_set():
                        break
                    if not executor.check_and_dismiss_error():
                        break
                    last_bead = executor.read_bead_road() or ""
                    if last_bead:
                        break
                    time.sleep(0.5)
                if not last_bead:
                    send_log("[counter] Empty board on entry — exiting")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue
                # Validate in-table tereko state using actual bead road
                in_cols = compute_column_lengths(last_bead)
                if not is_tereko_state(in_cols):
                    send_log(f"[counter] Condition not met ({short_rate(in_cols, ENTRY_WINDOW):.0%}) — exiting")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue
                last_non_tie = _last_non_tie_from_seq(last_bead)
                # Initialize column state from existing bead road (entry might be mid-column)
                if in_cols:
                    current_col_len = in_cols[-1]
                    current_col_side = last_non_tie
                else:
                    current_col_len = 0
                    current_col_side = None
                # If already in a long column at entry, exit immediately (e.g., 5+ drop ongoing)
                exit_reason = should_exit(columns_since_entry, current_col_len)
                if exit_reason:
                    send_log(f"[counter] Exit on entry: {exit_reason}")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue
                continue

            # ── BET前にビーズロードを再確認 (新シュー検出) ──
            try:
                _pre_bead = executor.read_bead_road() or ""
                if _pre_bead and last_bead and len(_pre_bead) < len(last_bead):
                    # ビーズロードが短くなった → 新シュー
                    send_log("[counter] Board reset detected — exiting")
                    last_non_tie = None
                    last_bead = _pre_bead
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue
                if not _pre_bead:
                    send_log("[counter] Empty board — exiting")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue
                last_bead = _pre_bead
                last_non_tie = _last_non_tie_from_seq(last_bead) or last_non_tie
            except Exception:
                pass

            # ── 逆張り側の決定 ──
            side = decide_counter_bet(last_non_tie)
            if side is None:
                # 前手がない → 1ラウンド観測のみ (run_round は使わない)
                send_log("[counter] Observing...")
                fallback = _wait_bead_change(last_bead, timeout_sec=120.0)
                if fallback:
                    last_bead, hand_fb = fallback
                    if hand_fb in ("P", "B"):
                        last_non_tie = hand_fb
                continue

            # ── run_round() で BET→結果→GUI送信を一体処理 ──
            # counter_session が None (flat mode) の場合は簡易セッションを使う
            if counter_session is not None:
                from marubatsu_strategy import SEQ_COUNTER as _SEQ
                _bet_amt = counter_session.get_bet_amount()
                send_log(f"[counter] BET {side.upper()} ${_bet_amt:.0f}")
                _phase_name = "betting_player" if side == "player" else "betting_banker" if side == "banker" else "betting"
                send_phase(_phase_name, f"${_bet_amt:.0f}")
                round_result = counter_session.run_round(
                    lambda: not stop_event.is_set(),
                    side=side,
                )
                if round_result.get("action") == "exit":
                    _reason = round_result.get("reason", "unknown")
                    _el = round_result.get("elapsed")
                    _to = round_result.get("timeout")
                    _reason_msg = {
                        "stop_requested": "stop requested",
                        "error_dialog": "error dialog",
                        "error_dialog_after_phase": "error dialog (after phase)",
                        "phase_timeout": f"BET phase timeout ({_el:.1f}s/{_to}s, late data or WS stall)" if _el is not None else "BET phase timeout",
                        "result_timeout": f"result timeout ({_el:.1f}s/{_to}s, WS delayed or shuffle)" if _el is not None else "result timeout",
                        "insufficient_balance": "insufficient balance",
                    }.get(_reason, _reason)
                    send_log(f"[counter] Round exit — {_reason_msg} → re-scanning")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    chip_fail_streak = 0
                    continue

                # ── GUI送信 (他モードと同じ send_result + status) ──
                rr_res = round_result.get("result")
                rr_won = round_result.get("won")
                rr_ba = round_result.get("bet_amount", 0)
                confirmed_total = 0.0
                dom_total = 0.0
                try:
                    dom_total = executor._get_total_bet()
                except Exception:
                    pass
                try:
                    if executor.game_ws:
                        confirmed = getattr(executor.game_ws, "_last_confirmed", {})
                        if isinstance(confirmed, dict):
                            confirmed_total = sum(
                                v for v in confirmed.values()
                                if isinstance(v, (int, float))
                            )
                except Exception:
                    pass
                actual_total = confirmed_total if confirmed_total > 0 else dom_total
                bet_confirmed = bool(rr_ba and actual_total > 0)
                partial_detected = False
                if actual_total > 0 and rr_ba and abs(actual_total - rr_ba) > 0.5:
                    partial_detected = True
                    send_log(f"[counter] Partial: planned ${rr_ba:.0f} actual ${actual_total:.2f}")
                    rr_ba = actual_total

                if not bet_confirmed:
                    chip_fail_streak += 1
                    send_log("[counter] Unconfirmed — skipped")
                    if chip_fail_streak >= 2:
                        send_log(f"[counter] Chip fail x{chip_fail_streak} — exiting")
                        try:
                            mark_table_exited(current_name)
                            executor.exit_table()
                        except Exception:
                            pass
                        if chip_fail_streak >= 4:
                            _restart_counter_browser("chip select failed x2 after re-entry")
                            chip_fail_streak = 0
                        current_tid = None
                        current_name = None
                        continue
                    continue

                if partial_detected:
                    chip_fail_streak += 1
                else:
                    chip_fail_streak = 0

                if rr_res:
                    bal = executor.get_balance() if not dry_run else 0
                    _update_actual_profit(bal)
                    actual_cum = money_pnl_actual if actual_profit_ready else None
                    # pre_turn情報を使う (セット完了でクリアされた後でも正確)
                    ptc = round_result.get("pre_turn_count", len(counter_session.tracker.current_turns))
                    pw = round_result.get("pre_wins", 0)
                    pl = round_result.get("pre_losses", 0)
                    turns = counter_session.tracker.current_turns
                    turns_disp = "".join("O" if t == "O" else "X" for t in turns)
                    cp = counter_session.tracker.cumulative_profit

                    if rr_res == "tie":
                        # Tie: BET返還、PNL影響なし。round_profit=0でGUI送信 (STREAM用)
                        send_result(
                            rr_res, None, rr_ba, bal, ptc, turns_disp, cp, money_pnl, 0.0,
                            round_profit_actual=last_balance_diff, cumulative_money_actual=actual_cum
                        )
                        send_action("Tie — BET returned")
                    else:
                        # Win/Lose: PNL計算 → GUI送信
                        if rr_won:
                            round_profit = rr_ba * (0.95 if side == "banker" else 1.0)
                        else:
                            round_profit = -rr_ba
                        daily_profit += round_profit
                        money_pnl += round_profit

                        send_result(
                            rr_res, rr_won, rr_ba, bal, ptc, turns_disp, cp, money_pnl, round_profit,
                            round_profit_actual=last_balance_diff, cumulative_money_actual=actual_cum
                        )

                        send_msg({
                            "type": "status",
                            "wins": counter_session.total_wins,
                            "losses": counter_session.total_losses,
                            "ties": counter_session.total_ties,
                            "total_bets": counter_session.total_bets,
                            "cumulative_profit": cp,
                            "cumulative_money": money_pnl,
                            "sets": len(counter_session.tracker.sets),
                            "current_turn": ptc,
                            "current_unit": _SEQ[min(counter_session.tracker.current_unit_idx, len(_SEQ)-1)],
                            "current_unit_idx": counter_session.tracker.current_unit_idx,
                            "overshoot": counter_session.tracker.prev_overshoot,
                            "balance": bal,
                            "turns_display": turns_disp,
                            "running": True,
                            "session_count": counter_session.session_count,
                            "pre_wins": pw,
                            "pre_losses": pl,
                            "cumulative_money_actual": actual_cum,
                            "session_open_balance": counter_session.session_open_balance,
                            "daily_open_date": (counter_session.daily_open or {}).get("date"),
                            "daily_open_balance": (counter_session.daily_open or {}).get("balance"),
                        })
                        _schedule_session_state_sync(user_email, counter_session, user_id, session_api_key)
                        _flush_daily_summary(table_name=current_name or "")

                        send_action(f"{'WIN' if rr_won else 'LOSE'} {side.upper()} ${rr_ba:.0f}. Balance: ${bal:.2f}")

                # Set complete
                if round_result.get("completed_set"):
                    s = round_result["completed_set"]
                    send_set_complete(s, chip_base)
                    send_shoe_history(counter_session.tracker.sets, chip_base)

                if partial_detected and chip_fail_streak >= 2:
                    send_log("[counter] Partial streak — re-scanning")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    if chip_fail_streak >= 4:
                        _restart_counter_browser("chip select failed x2 after re-entry")
                        chip_fail_streak = 0
                    current_tid = None
                    current_name = None
                    continue
            else:
                # flat mode: 直接BET
                send_log(f"[counter] BET {side.upper()} ${FLAT_BET_AMOUNT:.0f} (flat)")
                _pt0 = time.time()
                if not executor.wait_for_betting_phase(timeout=120, skip_round=False):
                    _el = time.time() - _pt0
                    send_log(f"[counter] Phase timeout ({_el:.1f}s/120s, late data or WS stall) — exiting")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue

                placed = executor.place_bet(side, FLAT_BET_AMOUNT)
                if not placed:
                    actual = executor._get_total_bet()
                    if not actual or actual < 0.5:
                        send_log("[counter] BET failed (place_bet returned False, no chips detected) — continuing")
                        continue

                _rt0 = time.time()
                result_info = executor.wait_for_result(timeout=90, bet_amount=FLAT_BET_AMOUNT)
                _rel = time.time() - _rt0
                if not result_info or result_info.get("result") in (None, "unknown"):
                    send_log(f"[counter] Result unknown ({_rel:.1f}s/90s, WS delayed or shuffle) — exiting")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    current_tid = None
                    current_name = None
                    continue

                result = result_info["result"]
                confirmed_total = 0.0
                dom_total = 0.0
                try:
                    dom_total = executor._get_total_bet()
                except Exception:
                    pass
                try:
                    if executor.game_ws:
                        confirmed = getattr(executor.game_ws, "_last_confirmed", {})
                        if isinstance(confirmed, dict):
                            confirmed_total = sum(
                                v for v in confirmed.values()
                                if isinstance(v, (int, float))
                            )
                except Exception:
                    pass
                actual_total = confirmed_total if confirmed_total > 0 else dom_total
                bet_confirmed = actual_total > 0
                if not bet_confirmed:
                    chip_fail_streak += 1
                    send_log("[counter] Unconfirmed — skipped")
                    if chip_fail_streak >= 2:
                        send_log(f"[counter] Chip fail x{chip_fail_streak} — exiting")
                        try:
                            mark_table_exited(current_name)
                            executor.exit_table()
                        except Exception:
                            pass
                        if chip_fail_streak >= 4:
                            _restart_counter_browser("chip select failed x2 after re-entry")
                            chip_fail_streak = 0
                        current_tid = None
                        current_name = None
                    continue
                if actual_total > 0 and abs(actual_total - FLAT_BET_AMOUNT) > 0.5:
                    send_log(f"[counter] Partial: planned ${FLAT_BET_AMOUNT:.0f} actual ${actual_total:.2f}")
                    chip_fail_streak += 1
                else:
                    chip_fail_streak = 0

                won = None if result == "tie" else (result == side)
                round_profit = 0.0
                bet_amt = actual_total if actual_total > 0 else FLAT_BET_AMOUNT
                if won is True:
                    round_profit = bet_amt * (0.95 if side == "banker" else 1.0)
                    flat_wins += 1
                elif won is False:
                    round_profit = -bet_amt
                    flat_losses += 1
                else:
                    flat_ties += 1
                flat_total_bets += 1

                if result == "tie":
                    send_action("Tie — BET returned")
                else:
                    daily_profit += round_profit
                    money_pnl += round_profit
                    bal = float(result_info.get("balance", 0) or 0)
                    _update_actual_profit(bal)
                    actual_cum = money_pnl_actual if actual_profit_ready else None
                    send_result(
                        result=result, won=won, bet_amount=bet_amt,
                        balance=bal, turn=0, turns_display="",
                        cumulative_profit=0, cumulative_money=money_pnl,
                        round_profit_dollars=round_profit,
                        round_profit_actual=last_balance_diff,
                        cumulative_money_actual=actual_cum,
                    )
                    _sob = None
                    _do_date = None
                    _do_bal = None
                    if _active_session is not None and hasattr(_active_session, 'session_open_balance'):
                        _sob = _active_session.session_open_balance
                        _do = getattr(_active_session, 'daily_open', None) or {}
                        _do_date = _do.get("date")
                        _do_bal = _do.get("balance")
                    send_msg({
                        "type": "status",
                        "wins": flat_wins, "losses": flat_losses, "ties": flat_ties,
                        "total_bets": flat_total_bets,
                        "cumulative_profit": 0, "cumulative_money": money_pnl,
                        "sets": 0, "current_turn": 0, "current_unit": 1,
                        "current_unit_idx": 0, "overshoot": 0,
                        "balance": bal, "turns_display": "", "running": True,
                        "session_count": 0,
                        "cumulative_money_actual": actual_cum,
                        "session_open_balance": _sob,
                        "daily_open_date": _do_date,
                        "daily_open_balance": _do_bal,
                    })
                    _flush_daily_summary(table_name=current_name or "")
                    money_actual = money_pnl_actual if actual_profit_ready else money_pnl
                    if money_actual >= profit_target_dollars or money_actual <= -loss_cut_dollars:
                        is_profit = money_actual >= profit_target_dollars
                        reason_en = "PROFIT TARGET" if is_profit else "LOSS CUT"
                        send_msg({
                            "type": "session_reset",
                            "is_profit": is_profit,
                            "amount": money_actual,
                            "reason": reason_en,
                        })
                        send_action(f"{reason_en} HIT! {'+$' if money_actual >= 0 else '-$'}{abs(money_actual):.0f} locked in -- new session starting")
                        send_log(f"[{reason_en}] Session ended at {'+$' if money_actual >= 0 else '-$'}{abs(money_actual):.0f}")
                        daily_sessions += 1
                        if is_profit:
                            daily_profit_sessions += 1
                        else:
                            daily_loss_sessions += 1
                        try:
                            flat_session_count += 1
                            hands_count = flat_total_bets
                            if is_profit:
                                composite.on_profit_target(
                                    user_label, flat_session_count, money_actual, hands_count,
                                    daily_profit_actual if actual_profit_ready else daily_profit,
                                    verification_mode, current_name or ""
                                )
                            else:
                                composite.on_loss_cut(
                                    user_label, flat_session_count, money_actual, hands_count,
                                    daily_profit_actual if actual_profit_ready else daily_profit,
                                    verification_mode, current_name or ""
                                )
                        except Exception as e:
                            logger.warning(f"Reset notify failed (counter flat): {e}")
                        if is_profit:
                            profit_sessions_done += 1
                            limit = _profit_session_limit_box[0]
                            if limit and profit_sessions_done >= limit:
                                send_action("Profit session limit reached — stopping")
                                send_log(f"[profit-limit] reached {profit_sessions_done}/{limit} — stopping")
                                stop_event.set()
                        money_pnl = 0.0
                        money_pnl_actual = 0.0
                        _actual_override_logged = False
                        if bal > 0:
                            balance_last = bal
                            actual_profit_ready = True
                            _reset_session_open(bal)
                        else:
                            balance_last = None
                            actual_profit_ready = False
                        if stop_event.is_set():
                            break
                if chip_fail_streak >= 2:
                    send_log("[counter] Partial streak — re-scanning")
                    try:
                        mark_table_exited(current_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    if chip_fail_streak >= 4:
                        _restart_counter_browser("chip select failed x2 after re-entry")
                        chip_fail_streak = 0
                    current_tid = None
                    current_name = None
                    continue
                round_result = {
                    "action": "bet", "result": result, "won": won,
                    "bet_amount": FLAT_BET_AMOUNT,
                }

            # ── run_round / flat の結果を処理 ──
            if round_result.get("action") == "exit":
                send_log("[counter] Round exit — re-scanning")
                try:
                    mark_table_exited(current_name)
                    executor.exit_table()
                except Exception:
                    pass
                current_tid = None
                current_name = None
                continue

            # ビーズロード更新 & 列長追跡
            try:
                new_bead = executor.read_bead_road() or ""
                if new_bead:
                    last_bead = new_bead
            except Exception:
                pass

            rr_result = round_result.get("result")
            if rr_result and rr_result != "tie":
                hand = "P" if rr_result == "player" else "B"
                last_non_tie = hand
                if hand == current_col_side:
                    current_col_len += 1
                else:
                    if current_col_side is not None and current_col_len > 0:
                        columns_since_entry.append(current_col_len)
                    current_col_side = hand
                    current_col_len = 1
            elif rr_result == "tie":
                pass  # タイは列長に影響なし

            # 退室判定
            exit_reason = should_exit(columns_since_entry, current_col_len)
            if exit_reason:
                send_log(f"[counter] Exited: {exit_reason}")
                send_action(f"Exiting: {exit_reason}")
                try:
                    mark_table_exited(current_name)
                    executor.exit_table()
                except Exception:
                    pass
                current_tid = None
                current_name = None
                continue

            # Supabase同期
            if counter_session is not None:
                _schedule_session_state_sync(user_email, counter_session, user_id, session_api_key)

            # 利確/損切チェック (actual balance優先)
            if counter_session is not None:
                should_reset = bool(round_result.get("should_reset"))
                actual_hit = False
                if actual_profit_ready:
                    actual_hit = (money_pnl_actual >= profit_target_dollars or money_pnl_actual <= -loss_cut_dollars)
                    if should_reset and not actual_hit and not _actual_override_logged:
                        send_log(
                            f"[profit] chip target hit but actual ${money_pnl_actual:.2f} < target ${profit_target_dollars:.0f} — continue"
                        )
                        _actual_override_logged = True

                trigger_reset = actual_hit if actual_profit_ready else should_reset
                if trigger_reset:
                    if actual_profit_ready:
                        is_profit = money_pnl_actual >= profit_target_dollars
                        money = money_pnl_actual
                    else:
                        cp = counter_session.effective_profit()
                        is_profit = cp >= 0
                        money = cp * chip_base
                    reason = "profit" if is_profit else "losscut"
                    reason_en = "PROFIT TARGET" if is_profit else "LOSS CUT"
                    send_msg({
                        "type": "session_reset",
                        "is_profit": is_profit,
                        "amount": money,
                        "reason": reason_en,
                    })
                    send_action(
                        f"{reason_en} HIT! {'+$' if money >= 0 else '-$'}{abs(money):.0f} locked in -- new session starting"
                    )
                    send_log(f"[{reason_en}] Session ended at {'+$' if money >= 0 else '-$'}{abs(money):.0f}")
                    daily_sessions += 1
                    if is_profit:
                        daily_profit_sessions += 1
                    else:
                        daily_loss_sessions += 1
                    try:
                        sess_num = counter_session.session_count + 1
                        hands_count = counter_session.total_bets
                        if is_profit:
                            composite.on_profit_target(
                                user_label, sess_num, money, hands_count,
                                daily_profit_actual if actual_profit_ready else daily_profit,
                                verification_mode, current_name or ""
                            )
                        else:
                            composite.on_loss_cut(
                                user_label, sess_num, money, hands_count,
                                daily_profit_actual if actual_profit_ready else daily_profit,
                                verification_mode, current_name or ""
                            )
                    except Exception as e:
                        logger.warning(f"Reset notify failed (counter): {e}")
                    bal_now = executor.get_balance() if not dry_run else 0
                    try:
                        counter_session.reset_session(reason, actual_amount=money, balance=bal_now)
                    except TypeError:
                        counter_session.reset_session(reason)
                    if is_profit:
                        profit_sessions_done += 1
                        limit = _profit_session_limit_box[0]
                        if limit and profit_sessions_done >= limit:
                            send_action("Profit session limit reached — stopping")
                            send_log(f"[profit-limit] reached {profit_sessions_done}/{limit} — stopping")
                            stop_event.set()
                    money_pnl = 0.0
                    money_pnl_actual = 0.0
                    _actual_override_logged = False
                    if bal_now > 0:
                        balance_last = bal_now
                        actual_profit_ready = True
                        _reset_session_open(bal_now)
                    else:
                        balance_last = None
                        actual_profit_ready = False
                    if stop_event.is_set():
                        break

        # === Shutdown (counter mode) ===
        send_action("Stopping...")
        # Telegram通知 (ブラウザ停止前に送る)
        _flush_daily_summary(force=True, table_name=current_name or "")
        try:
            composite.on_shutdown(user_label, "Normal stop")
        except Exception:
            pass
        # Supabase にセッション状態を保存
        if counter_session is not None:
            try:
                counter_session._save_state()
                send_log("[counter] State saved (local)")
            except Exception:
                pass
            try:
                _schedule_session_state_sync(user_email, counter_session, user_id, session_api_key)
                send_log("[counter] State synced (cloud)")
            except Exception as e:
                send_log(f"[counter] Cloud sync failed: {e}")
            if user_email and session_api_key:
                try:
                    state = _extract_session_state(counter_session)
                    if _has_session_state(state):
                        if user_id:
                            state["user_id"] = user_id
                        state["updated_at"] = datetime.utcnow().isoformat() + "Z"
                        if _post_session_state_to_server(user_email, state, session_api_key):
                            send_log("[counter] State synced (cloud, flush)")
                except Exception as e:
                    send_log(f"[counter] Cloud sync flush failed: {e}")
        try:
            executor.exit_table()
        except Exception:
            pass
        try:
            scraper.stop()
        except Exception:
            pass
        send_action("Stopped.")
        _active_session = None
        return

    def _make_local_session():
        # Lazy import — only when running in local fallback mode.
        # On shipped client binaries, marubatsu_bet / marubatsu_strategy are NOT included,
        # so this import will fail and force the user to use VPS remote mode.
        from marubatsu_bet import MaruBatsuBetSession
        return MaruBatsuBetSession(
            executor=executor,
            notifier=notifier,
            chip_base=chip_base,
            loss_cut=loss_cut_chips,
            profit_stop=profit_stop_chips,
            dry_run=dry_run,
            resume=resume,
        )

    if use_remote:
        try:
            session = RemoteLaplaceSession(
                executor=executor,
                notifier=notifier,
                chip_base=chip_base,
                loss_cut=loss_cut_chips,
                profit_stop=profit_stop_chips,
                dry_run=dry_run,
                resume=resume,
            )
            send_action(f"LAPLACE Remote session ready (sets={len(session.tracker.sets)}, cp={session.tracker.cumulative_profit:+d})")
        except Exception as e:
            send_log(f"Remote session creation failed ({e}) — falling back to local MaruBatsuBetSession")
            try:
                session = _make_local_session()
            except ImportError as imp_err:
                send_log(f"FATAL: local fallback unavailable on this build ({imp_err}). VPS API is required.")
                return
    else:
        try:
            session = _make_local_session()
        except ImportError as imp_err:
            send_log(f"FATAL: local mode requires marubatsu_bet/marubatsu_strategy ({imp_err}). Set LAPLACE_USE_REMOTE=1 to use the VPS API instead.")
            return
    if supabase_state and _apply_session_state(session, supabase_state):
        send_log("[session] Supabase session restored")
        if supabase_built or supabase_missing:
            if _backfill_session_state(user_email, session, user_id, session_api_key):
                send_log("[session] Supabase session saved from GUI results")
    elif supabase_missing:
        if _backfill_session_state(user_email, session, user_id, session_api_key):
            send_log("[session] Supabase session backfilled from local/VPS state")
    _active_session = session

    def restart_browser(reason: str) -> tuple[str, str] | None:
        """Lv5: Camoufox プロセス再起動 + ロビー再接続 + テーブル再選定"""
        nonlocal scraper, selector, executor, session, target_tid, target_name, _last_camoufox_restart
        send_action(f"🔁 Browser restart: {reason}")
        send_log(f"[restart] Camoufox restart triggered: {reason}")
        try:
            scraper.stop()
        except Exception as e:
            send_log(f"[restart] scraper.stop error: {e}")
        time.sleep(3)

        scraper = BaccaratScraper()
        scraper.table_name = "all"
        try:
            scraper.start()
        except Exception as e:
            send_log(f"[restart] Browser launch failed: {e}")
            return None

        scraper.setup_ws_intercept()
        time.sleep(12)  # configs/histories/playersCount の初期ロード待機

        if use_remote:
            selector = RemoteTableSelector(scraper, _selector_client, _user_id)
        else:
            from table_selector import TableSelector
            selector = TableSelector(scraper)

        executor = BetExecutor(scraper.page, scraper.game_ws, executor_config, humanizer=humanizer)
        try:
            session.executor = executor
        except Exception:
            pass
        _last_camoufox_restart = time.time()

        # テーブル再選定
        send_action("Restart — selecting table...")
        target_tid = None
        target_name = None
        for _rt in range(5):
            if stop_event.is_set():
                return None
            best = pick_table()
            if best:
                target_tid = best.table_id
                target_name = best.title
                with scraper._lock:
                    scraper._target_table_ids.add(target_tid)
                    scraper._target_table_names[target_tid] = target_name
                    scraper._new_shoe_signals[target_tid] = False
                    scraper._shoe_epochs[target_tid] = int(time.time())
                send_action(f"Restart complete — picked {target_name}")
                send_log(f"[restart] Table selected: {target_name}")
                return target_tid, target_name
            if stop_event.wait(10):
                return None
        send_log("[restart] Failed to select table after restart")
        return None

    # Apply any pending config updates received before session creation
    if _pending_config_update:
        pending = _pending_config_update
        _pending_config_update = {}
        new_pt_chips = None
        new_lc_chips = None
        if "profit_target" in pending:
            new_pt = float(pending["profit_target"])
            new_pt_chips = max(1, int(round(new_pt / max(chip_base, 0.01))))
            session.profit_stop = new_pt_chips
            send_log(f"Applied pending profit target: ${new_pt:.0f} ({new_pt_chips} chips)")
        if "loss_cut" in pending:
            new_lc = float(pending["loss_cut"])
            new_lc_chips = max(1, int(round(new_lc / max(chip_base, 0.01))))
            session.loss_cut = new_lc_chips
            send_log(f"Applied pending loss cut: ${new_lc:.0f} ({new_lc_chips} chips)")
        pending_counter = {
            k: pending[k]
            for k in ("entry_window", "entry_threshold", "exit_drop3_limit", "exit_drop5_immediate")
            if k in pending
        }
        if pending_counter:
            _apply_counter_params(pending_counter, "pending")
        if pending.get("use_cloud_params"):
            try:
                from counter_logic import apply_optimal_params, ENTRY_WINDOW, ENTRY_THRESHOLD, EXIT_DROP3_LIMIT, EXIT_DROP5_IMMEDIATE
                if apply_optimal_params():
                    send_log(f"[counter] Cloud params reloaded: W={ENTRY_WINDOW} T={ENTRY_THRESHOLD} D3={EXIT_DROP3_LIMIT} D5={EXIT_DROP5_IMMEDIATE}")
                else:
                    send_log("[counter] Cloud params reload skipped (unavailable)")
            except Exception as e:
                send_log(f"[counter] Cloud params reload failed: {e}")
        # Sync to remote if applicable
        if use_remote and hasattr(session, "update_config"):
            try:
                session.update_config(profit_stop=new_pt_chips, loss_cut=new_lc_chips)
            except Exception as e:
                send_log(f"Remote config sync failed: {e}")

    # === Select initial table ===
    target_tid = None
    target_name = None
    while not stop_event.is_set() and target_tid is None:
        if verification_mode:
            send_action(f"[VERIFICATION] Looking for fixed table: {fixed_table_name}")
            best = selector.find_best_table(fixed_name=fixed_table_name, selector_config=table_filter)
        else:
            send_action("Selecting best table...")
            best = selector.find_best_table(selector_config=table_filter)
        if best:
            target_tid = best.table_id
            target_name = best.title
            send_action(f"Picked: {target_name} ({best.players}p, {best.hands}h, P:{best.p_count}/B:{best.b_count})")
        else:
            send_action("No suitable table — waiting 15s...")
            if stop_event.wait(15):
                break

    if not target_tid:
        send_log("Stopped before selecting table")
        scraper.stop()
        return

    # scraper に監視対象として追加 (shoe signal等のため)
    with scraper._lock:
        scraper._target_table_ids.add(target_tid)
        scraper._target_table_names[target_tid] = target_name
        if target_tid not in scraper._shoe_epochs:
            scraper._shoe_epochs[target_tid] = int(time.time())
            scraper._new_shoe_signals[target_tid] = False

    # === 1落ち待機（ロビー観察）=== — 1-dropモードのみ
    if _effective_mode_box[0] == "1drop":
        if not observe_until_1_drop(target_tid, target_name):
            send_log("Stopped during lobby observation")
            scraper.stop()
            return

    # === Syncモード(+sync_pause): 推奨テーブルから規則性が高いものを選定 ===
    if _effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"):
        res_sync = find_table()
        if not res_sync:
            send_log("Stopped during sync table selection")
            scraper.stop()
            return
        target_tid, target_name = res_sync
        with scraper._lock:
            scraper._target_table_ids.add(target_tid)
            scraper._target_table_names[target_tid] = target_name
            scraper._new_shoe_signals[target_tid] = False
            scraper._shoe_epochs[target_tid] = int(time.time())

    # === Enter table (診断ログ付き) ===
    send_action(f"Entering table: {target_name}...")

    # 診断: ブラウザ状態を出力
    try:
        page_url = scraper.page.url
        frame_urls = [f.url[:80] for f in scraper.page.frames]
        evo_frames = [u for u in frame_urls if "evo" in u.lower()]
        send_log(f"[DIAG] page={page_url[:80]}")
        send_log(f"[DIAG] frames={len(frame_urls)} evo={len(evo_frames)}")
        for eu in evo_frames:
            send_log(f"[DIAG] evo_frame: {eu}")
        if not evo_frames:
            send_log(f"[DIAG] ALL frames: {frame_urls}")
        # スクリーンショット
        try:
            import config as _cfg
            scraper.page.screenshot(path=str(_cfg.SCREENSHOTS_DIR / "before_entry.png"))
            send_log("[DIAG] screenshot saved: before_entry.png")
        except Exception as ss_err:
            send_log(f"[DIAG] screenshot failed: {ss_err}")
    except Exception as diag_err:
        send_log(f"[DIAG] diagnostic failed: {diag_err}")

    _entry_ok = False
    for _attempt in range(3):
        if executor.enter_table(target_tid, target_name):
            _entry_ok = True
            break
        # 失敗時の詳細診断
        try:
            evo_frames_now = [f.url[:80] for f in scraper.page.frames if "evo" in f.url.lower()]
            send_log(f"[DIAG] after fail: evo_frames={len(evo_frames_now)} page={scraper.page.url[:60]}")
        except Exception:
            pass
        send_log(f"Table entry failed (attempt {_attempt+1}/3) — retrying in 10s...")
        send_action(f"Entry failed — retrying ({_attempt+1}/3)...")
        if _attempt < 2:
            # リトライ前にロビーに戻る
            try:
                send_log("[DIAG] Navigating back to lobby before retry...")
                import config as _cfg
                scraper.page.goto(_cfg.BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
            except Exception as nav_err:
                send_log(f"[DIAG] lobby navigation failed: {nav_err}")
            time.sleep(5)
    if not _entry_ok:
        send_log("Table entry failed after 3 attempts — stopping")
        send_action("Table entry failed — stopping")
        scraper.stop()
        return

    balance = executor.get_balance() if not dry_run else 0
    send_action(f"In table. Balance: ${balance:.2f}")
    send_log(f"BET session started [{mode}] table={target_name} chip=${chip_base} balance=${balance:.2f}")
    send_shoe_history(session.tracker.sets, chip_base)

    # === Main BET loop ===
    round_count = 0
    entry_fail_count = 0
    session_start = time.time()

    _FREEZE_TIMEOUT = 10 * 60  # WS無活動10分でフリーズ判定
    _SESSION_CHECK_INTERVAL = 300  # 5分おきにStakeログイン確認
    _last_session_check = time.time()
    TRY_AGAIN_WINDOW = 5 * 60  # TRY AGAIN ループ検知ウィンドウ
    TRY_AGAIN_LIMIT = 3        # 連続N回で再起動
    _try_again_hits = 0
    _try_again_window_start = time.time()
    _deferred_exit_reason = None   # BETウィンドウ保護: exit checkは前ラウンドで実行済み
    _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")  # 1-dropモード時のみ2落ち確認
    _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))  # syncモード: 入場直後の再確認
    _bet_fail_count = 0            # BET完全失敗カウンタ（3連続で退室）
    _sync_monitor_counter = 0      # Syncモード: 動的規則性監視カウンタ
    # ── sync_pause モード用: BB で観戦、Pで再開 (原点 465843d 版) ──
    _consec_banker = 0             # 連続Banker回数
    _paused_for_dragon = False     # ドラゴン中の観戦フラグ
    PAUSE_THRESHOLD = 2            # N連続Bで観戦モード突入
    _observe_fail_count = 0        # 観戦失敗連続カウンタ (テーブル死亡検知用)
    OBSERVE_FAIL_LIMIT = 5         # N回連続失敗 → テーブル死亡疑い → full_recovery
    _pattern_unknown_count = 0     # Pattern モード: 連続「不明」カウンタ
    PATTERN_UNKNOWN_LIMIT = 1      # 1回でも不明 → 新シュー/シャッフル疑い → 即退避
    _ws_warn_level = [0]            # WS無通信の段階的警戒レベル (mutable list で closure から更新)
                                    # (流れがわからないテーブルで時間を浪費しない)
    # Pattern Test モード用カウンタ ($1 固定 BET、VPS 記録なし、ローカル W/L カウント)
    _test_wins = 0
    _test_losses = 0
    _test_ties = 0
    # ── Phase 1: シャッフル検知用 連続失敗カウンタ ──
    _consec_wait_result_fail = 0   # wait_for_result が None を返した連続回数
    WAIT_RESULT_FAIL_LIMIT = 2     # N回連続で失敗 → シャッフル中と判断 → 即テーブル退避
    BB_EXIT_LOBBY_WAIT = 15        # 退避後ロビー待機 (shuffle/error 検出時で使用)
    BEAD_FAIL_LIMIT = 30           # bead road 連続失敗回数で iframe 劣化判定
    BEAD_FAIL_GRACE_SEC = 60       # 入場直後の猶予時間

    # ── iframe 劣化対策: 予防的フルリカバリ + ヘルスチェック ──
    # Evolution iframe は長時間プレイで徐々に劣化し、最終的に完全消失する
    # 対策: 30分経過 / 利確時 / 5分ごとのヘルスチェック で予防
    _last_recovery_time = time.time()
    PROACTIVE_RECOVERY_INTERVAL = 30 * 60  # 30分
    _last_iframe_health_check = time.time()
    IFRAME_HEALTH_CHECK_INTERVAL = 5 * 60  # 5分
    _last_camoufox_restart = time.time()
    CAMOUFOX_RESTART_INTERVAL = 90 * 60  # 90分

    def proactive_full_recovery(reason: str) -> bool:
        """予防的フルリカバリ（BET中ではない時に呼出）
        Returns: True=成功, False=失敗 (main loop break)
        """
        nonlocal target_tid, target_name, _last_recovery_time, _last_iframe_health_check
        send_action(f"🔄 Proactive recovery: {reason}")
        send_log(f"[proactive-recovery] triggered: {reason}")
        fr = full_recovery()
        if not fr:
            return False
        target_tid, target_name = fr
        _last_recovery_time = time.time()
        _last_iframe_health_check = time.time()
        return True

    # Evolution game URLs (Evolution iframe を実際にロードするゲームページ)
    # NOTE: detour は不安定化要因になることがあるため、デフォルト無効。
    ENABLE_CASINO_DETOUR = os.getenv("LAPLACE_ENABLE_CASINO_DETOUR", "0").strip() in ("1", "true", "True", "yes")
    EVOLUTION_GAME_URLS = [
        "https://stake.com/casino/games/evolution-european-roulette",
        "https://stake.com/casino/games/evolution-lightning-roulette",
        "https://stake.com/casino/games/evolution-immersive-roulette",
    ]

    def casino_detour(reason: str = "iframe維持", target_url: str = None) -> bool:
        """別カジノゲームに寄り道してブラウザをアクティブに保つ。
        ロビー待機中の iframe 劣化対策。

        target_url: 指定した URL に navigate (None なら ランダム選択)
        Returns: True=detour成功（ロビー復帰済）

        注意: /casino (カジノトップ) は単なるディレクトリページで
        Evolution iframe がロードされない。Evolution iframe を強制
        ロードしたい場合は EVOLUTION_GAME_URLS から選ぶこと。
        """
        nonlocal _last_recovery_time, _last_iframe_health_check
        import random as _rand_d
        if target_url is None:
            # ランダム detour: 通常運用 (人間らしさ重視)
            # /casino は Evolution iframe がロードされず復旧に寄与しないため除外
            detour_targets = list(EVOLUTION_GAME_URLS)
            target_url = _rand_d.choice(detour_targets)
        send_action(f"🎰 Casino detour: {reason}")
        send_log(f"[detour] starting detour → {target_url}")
        try:
            scraper.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            # 人間らしく10-15秒滞在 (Evolution iframe ロード待ちも兼ねる)
            time.sleep(_rand_d.uniform(10, 15))
            # Evolution iframe が寄り道先でロードされたか確認 (情報のみ)
            try:
                temp_frames = executor._get_evo_frames()
                if temp_frames:
                    send_log(f"[detour] Evolution iframe load confirmed at detour ({len(temp_frames)} frames)")
                else:
                    send_log("[detour] Evolution iframe not confirmed at detour")
            except Exception:
                pass
            send_log("[detour] returning to baccarat lobby")
            scraper.page.goto(BACCARAT_LOBBY_URL, wait_until="domcontentloaded", timeout=30000)
            # lobby復帰後 Evolution iframe が再ロードされるまで待機 (15秒)
            time.sleep(15)
            try:
                scraper.setup_ws_intercept()
            except Exception as _wse:
                send_log(f"[detour] WS reconnect exception: {_wse}")
            _last_iframe_health_check = time.time()
            _last_recovery_time = time.time()
            send_log("[detour] ✅ complete")
            return True
        except Exception as e:
            send_log(f"[detour] ❌ failed: {e}")
            return False

    while not stop_event.is_set() and round_count < MAX_ROUNDS:
        # ── A0. エラーダイアログ即時検知 (TRY AGAIN / BACK TO LOBBY / SESSION EXPIRED) ──
        # NOTE: in_table でなくても iframe 内オーバーレイは出るため、常にチェックする。
        try:
            if not executor.check_and_dismiss_error():
                err = executor.get_last_error_type()
                send_action("⚠️ Error dialog — auto recovery")
                send_log(f"[session] error={err} → full_recovery")
                try:
                    executor.exit_table()
                except Exception:
                    pass
                fr = full_recovery()
                if not fr:
                    break
                target_tid, target_name = fr
                if not executor.enter_table(target_tid, target_name):
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        break
                _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                continue
        except Exception as _se:
            send_log(f"[session] error check failed: {_se}")

        # ── A0.5 TRY AGAIN ループ検知 ──
        try:
            err_type = executor.get_last_error_type()
            if err_type in ("try_again", "try_again_failed"):
                now = time.time()
                if now - _try_again_window_start > TRY_AGAIN_WINDOW:
                    _try_again_window_start = now
                    _try_again_hits = 0
                _try_again_hits += 1
                executor.clear_last_error_type()
                if err_type == "try_again_failed" or _try_again_hits >= TRY_AGAIN_LIMIT:
                    send_action("⚠️ TRY AGAIN loop — restarting browser")
                    send_log(f"[health] TRY AGAIN loop ({_try_again_hits}) → restart_browser")
                    _try_again_hits = 0
                    fr = restart_browser("TRY AGAIN loop")
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if not executor.enter_table(target_tid, target_name):
                            break
                    _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                    continue
            elif err_type is None and time.time() - _try_again_window_start > TRY_AGAIN_WINDOW:
                _try_again_hits = 0
                _try_again_window_start = time.time()
        except Exception as _ta:
            send_log(f"[health] try again guard error: {_ta}")

        # ── A. ブラウザ生存チェック (Camoufox死亡検知) ──
        try:
            if not scraper.is_page_alive():
                send_action("⚠️ Browser closed — restarting Camoufox...")
                send_log("[health] page not alive → Camoufox restart")
                fr = restart_browser("page not alive")
                if not fr:
                    break
                target_tid, target_name = fr
                if not executor.enter_table(target_tid, target_name):
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        break
                _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                continue
        except Exception as _pe:
            send_log(f"[health] page alive check error: {_pe}")

        # ── A.5 周期的 Camoufox 再起動（劣化予防） ──
        try:
            if time.time() - _last_camoufox_restart > CAMOUFOX_RESTART_INTERVAL:
                safe_to_restart = (not executor.in_table) or _paused_for_dragon or len(session.tracker.current_turns) == 0
                if safe_to_restart:
                    send_action("🔁 Periodic browser restart")
                    send_log("[health] periodic Camoufox restart")
                    fr = restart_browser("periodic maintenance")
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if not executor.enter_table(target_tid, target_name):
                            break
                    _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                    _last_camoufox_restart = time.time()
                    continue
        except Exception as _pr:
            send_log(f"[health] periodic restart error: {_pr}")

        # ── B. iframe ヘルスチェック (5分おき、BET中以外) ──
        # Evolution iframe が消失していないか定期確認
        # paused=True または set 完了直後など、BET中じゃない時に実行
        if (time.time() - _last_iframe_health_check > IFRAME_HEALTH_CHECK_INTERVAL
            and target_tid
            and (_paused_for_dragon or len(session.tracker.current_turns) == 0)):
            _last_iframe_health_check = time.time()
            try:
                evo_frames = executor._get_evo_frames()
                if not evo_frames:
                    send_log("[health] Evolution iframe disappeared → proactive recovery")
                    if not proactive_full_recovery("iframe消失"):
                        break
                    _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                    continue
            except Exception as _hce:
                send_log(f"[health] iframe check exception: {_hce}")

        # ── B.5 bead road 連続失敗チェック (iframe 劣化早期検知) ──
        try:
            if executor.in_table and executor.get_bead_fail_count() >= BEAD_FAIL_LIMIT:
                if executor.get_table_uptime() >= BEAD_FAIL_GRACE_SEC:
                    send_action(f"⚠️ Bead road fail x{executor.get_bead_fail_count()} — recovery")
                    send_log(f"[health] bead road {executor.get_bead_fail_count()} consecutive failures → full_recovery")
                    executor.reset_bead_fail_count()
                    try:
                        executor.exit_table()
                    except Exception:
                        pass
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if not executor.enter_table(target_tid, target_name):
                            break
                    _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
                    continue
        except Exception as _be:
            send_log(f"[health] bead fail check error: {_be}")

        # ── フリーズ検出ウォッチドッグ ──
        ws_idle = scraper.game_ws.seconds_since_last_message() if hasattr(scraper, 'game_ws') and scraper.game_ws else 0
        # 段階的警戒ログ (ユーザーが "動いていないのか待っているだけか" を判別できるように)
        if ws_idle >= 60 and _ws_warn_level[0] < 1:
            send_log(f"[ws-wait] WS silent {ws_idle:.0f}s — still waiting (watchdog at {_FREEZE_TIMEOUT:.0f}s)")
            send_phase("ws_stall", f"{ws_idle:.0f}s")
            _ws_warn_level[0] = 1
        elif ws_idle >= 180 and _ws_warn_level[0] < 2:
            send_log(f"[ws-wait] WS silent {ws_idle:.0f}s ⚠ — unusually long, may be shuffle/stall")
            send_phase("ws_stall", f"{ws_idle:.0f}s ⚠")
            _ws_warn_level[0] = 2
        elif ws_idle >= 360 and _ws_warn_level[0] < 3:
            send_log(f"[ws-wait] WS silent {ws_idle:.0f}s 🔴 — approaching freeze threshold ({_FREEZE_TIMEOUT:.0f}s)")
            send_phase("ws_stall", f"{ws_idle:.0f}s 🔴")
            _ws_warn_level[0] = 3
        elif ws_idle < 30 and _ws_warn_level[0] != 0:
            _ws_warn_level[0] = 0  # 通信回復 → リセット
        if ws_idle > _FREEZE_TIMEOUT and target_tid:
            send_action(f"Browser freeze detected ({ws_idle:.0f}s no WS) — reloading...")
            send_log(f"[watchdog] WS silent {ws_idle:.0f}s — page reload")
            try:
                scraper.page.reload(timeout=15000)
                import time as _t; _t.sleep(5)
                executor.exit_table()
                _t.sleep(3)
                if executor.enter_table(target_tid, target_name):
                    send_action(f"Recovered — re-entered {target_name}")
                    _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                else:
                    # 通常リカバリ失敗 → フルリカバリ
                    send_log("[watchdog] Normal recovery failed — escalating to full recovery")
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
            except Exception as _e:
                send_log(f"[watchdog] reload error: {_e} — escalating to full recovery")
                fr = full_recovery()
                if not fr:
                    break
                target_tid, target_name = fr
            # フルリカバリ後はWS silent タイマーを必ずリセット
            # (reset() で更新されるはずだが、リカバリ経路の保険として明示的に再リセット)
            try:
                if hasattr(scraper, 'game_ws') and scraper.game_ws:
                    scraper.game_ws._last_message_at = time.time()
            except Exception:
                pass
            continue

        # ── Stakeセッション確認（5分おき）──
        if time.time() - _last_session_check > _SESSION_CHECK_INTERVAL:
            _last_session_check = time.time()
            try:
                if not scraper._is_logged_in():
                    send_action("Stake session expired — re-logging in...")
                    send_log("[session] Stake logout detected — attempting re-login")
                    try:
                        scraper._login()
                        send_log("[session] Re-login successful")
                    except Exception as _le:
                        send_log(f"[session] Re-login failed: {_le} — escalating to full recovery")
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if executor.enter_table(target_tid, target_name):
                            _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                        continue
                    executor.exit_table()
                    time.sleep(3)
                    if executor.enter_table(target_tid, target_name):
                        send_action(f"Session restored — re-entered {target_name}")
                        _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                    else:
                        # 再入場失敗 → フルリカバリ
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if executor.enter_table(target_tid, target_name):
                            _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                    continue
            except Exception as _se:
                send_log(f"[session] re-login error: {_se}")

        # Shoe change check — 同じテーブルに滞在中のみ有効
        # 1-Dropモードでテーブルを変えた直後はshoe signalを無視する
        if not _awaiting_2nd_drop:  # 2落ち確認中 = 入場直後 → スキップ
            shoe_signals = scraper.get_new_shoe_signals()
            if target_tid in shoe_signals and shoe_signals[target_tid]:
                send_action("Shoe change detected")
                send_log("Shoe change — partial turns discarded")
                session.handle_shoe_change()

        # Session break (anti-bot) — 無効化
        # 1-dropモードではロビー監視+テーブル出入りが実質休憩になる。
        # 休憩中にEvolutionセッションが切れるためSESSION EXPIRED の主要原因だった。
        # session_start のリセットのみ残す（セッション時間トラッキング用）
        if len(session.tracker.current_turns) == 0:
            minutes_elapsed = (time.time() - session_start) / 60
            if humanizer.should_take_break(minutes_elapsed):
                session_start = time.time()  # タイマーリセットのみ

        # User-requested skip takes precedence
        user_skip = False
        if skip_event is not None and skip_event.is_set():
            user_skip = True
            skip_event.clear()

        # Deferred exit check (non-blocking: uses result from previous iteration)
        # should_exit_table API call moved to END of loop to avoid blocking BET window
        if len(session.tracker.current_turns) == 0 and (_deferred_exit_reason or user_skip):
            exit_reason = "User requested skip" if user_skip else _deferred_exit_reason
            _deferred_exit_reason = None
            if exit_reason:
                send_action(f"Table conditions broke: {exit_reason} — exiting...")
                send_log(f"Leaving {target_name}: {exit_reason}")
                executor.exit_table()
                time.sleep(3)

                # ロビーWS再接続 (テーブル退出後にconfigsが空になる対策)
                if not scraper.get_all_table_configs():
                    send_log("Lobby WS lost — reconnecting...")
                    try:
                        scraper.setup_ws_intercept()
                    except Exception as _ws_err:
                        send_log(f"Lobby WS reconnect failed: {_ws_err}")

                # 再選定
                target_tid = None
                target_name = None
                while not stop_event.is_set() and target_tid is None:
                    send_action("Re-selecting table...")
                    best = pick_table()
                    if best:
                        target_tid = best.table_id
                        target_name = best.title
                        send_action(f"Picked: {target_name} ({best.players}p, {best.hands}h)")
                    else:
                        # configsが空のままならlobby WS再接続を試行
                        if not scraper.get_all_table_configs():
                            send_log("Still no configs — lobby WS reconnect retry...")
                            try:
                                scraper.setup_ws_intercept()
                            except Exception:
                                pass
                        send_action("No suitable table — waiting 15s...")
                        if stop_event.wait(15):
                            break

                if stop_event.is_set():
                    break

                with scraper._lock:
                    scraper._target_table_ids.add(target_tid)
                    scraper._target_table_names[target_tid] = target_name
                    if target_tid not in scraper._shoe_epochs:
                        scraper._shoe_epochs[target_tid] = int(time.time())
                        scraper._new_shoe_signals[target_tid] = False

                if _effective_mode_box[0] == "1drop":
                    if not observe_until_1_drop(target_tid, target_name):
                        break
                send_action(f"Entering {target_name}...")
                if not executor.enter_table(target_tid, target_name):
                    send_action("Entry failed — retrying...")
                    time.sleep(5)
                    continue
                _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")  # deferred exit後再入場 → 2落ち確認必須

        # ── 2落ち確認フェーズ（入場直後のみ、BETしないで1ハンド観察）──
        if _awaiting_2nd_drop:
            cdr = confirm_2nd_drop()
            if cdr == "stopped":
                break
            elif cdr == "invalidated":
                # Bankerが来た → 退室してロビー監視へ
                executor.exit_table()
                # 退室後にランダム待機（10〜25秒）— 人間らしくロビーを眺める時間
                import random as _rand2
                _lobby_wait2 = _rand2.uniform(10, 25)
                send_action(f"Browsing lobby... ({_lobby_wait2:.0f}s)")
                if stop_event.wait(_lobby_wait2):
                    break
                if stop_event.is_set():
                    break
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                res_1drop = find_1_drop_table()
                if not res_1drop:
                    break
                target_tid, target_name = res_1drop
                with scraper._lock:
                    scraper._target_table_ids.add(target_tid)
                    scraper._target_table_names[target_tid] = target_name
                    if target_tid not in scraper._shoe_epochs:
                        scraper._shoe_epochs[target_tid] = int(time.time())
                        scraper._new_shoe_signals[target_tid] = False
                _reenter_ok = False
                for _r in range(3):
                    send_action(f"Entering {target_name} (attempt {_r+1}/3)...")
                    if executor.enter_table(target_tid, target_name):
                        _reenter_ok = True
                        break
                    time.sleep(5)
                if not _reenter_ok:
                    send_action("Entry failed — stopping")
                    break
                _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                continue
            else:  # "confirmed" — Playerが2落ち確認
                _awaiting_2nd_drop = False
                # 次のBETフェーズでrun_round()へ

        # ── Syncモード: 入場後の規則性再確認（DOM読み取り）──
        # ロビーWSとテーブル内DOMの不整合を検出、条件未達なら即退避
        if _awaiting_sync_confirm and executor.in_table:
            from regularity_monitor import evaluate_table, ENTRY_THRESHOLD, MIN_HANDS_FOR_ENTRY
            send_action("🔍 Sync: 入場後の規則性を再確認中...")
            send_log("[Sync-Entry] ビーズロードから規則性を再計算")
            try:
                bead = executor.read_bead_road()
                if bead:
                    eval_r = evaluate_table(list(bead))
                    reg = eval_r['regularity']
                    hands = eval_r['hands']
                    send_log(f"[Sync-Entry] テーブル内: {hands}ハンド reg={reg:.0f}")
                    if hands < MIN_HANDS_FOR_ENTRY or reg < ENTRY_THRESHOLD:
                        send_action(f"⚠️ Sync: 条件未達 (hands={hands} reg={reg:.0f}) — 退避")
                        send_log(f"[Sync-Entry] ❌ 入場後確認失敗: {hands}ハンド reg={reg:.0f} < 閾値 → 退避")
                        mark_table_exited(target_name)
                        executor.exit_table()
                        import random as _rand_se
                        _w = _rand_se.uniform(5, 10)
                        send_log(f"[Sync] 🚶 ロビーで{_w:.0f}秒待機...")
                        if stop_event.wait(_w):
                            break
                        # 次の候補テーブルを探す
                        if not scraper.get_all_table_configs():
                            try:
                                scraper.setup_ws_intercept()
                            except Exception:
                                pass
                        res_s = find_table()
                        if not res_s:
                            break
                        target_tid, target_name = res_s
                        with scraper._lock:
                            scraper._target_table_ids.add(target_tid)
                            scraper._target_table_names[target_tid] = target_name
                            scraper._new_shoe_signals[target_tid] = False
                            scraper._shoe_epochs[target_tid] = int(time.time())
                        send_action(f"🚪 Entering {target_name}...")
                        if not executor.enter_table(target_tid, target_name):
                            send_log("[Sync-Entry] ⚠️ 入場失敗 → full_recovery")
                            fr = full_recovery()
                            if not fr:
                                break
                            target_tid, target_name = fr
                            if not executor.enter_table(target_tid, target_name):
                                send_log("[Sync-Entry] ⚠️ full_recovery後も入場失敗 → restart_browser")
                                fr2 = restart_browser("Sync-Entry enter_table failed after full_recovery")
                                if not fr2:
                                    break
                                target_tid, target_name = fr2
                                if not executor.enter_table(target_tid, target_name):
                                    send_log("[Sync-Entry] ❌ restart後も入場失敗 → テーブル再選定へ")
                                    target_tid = None
                                    target_name = None
                                    continue
                        _awaiting_sync_confirm = True  # 新しいテーブルでも再確認
                        continue
                    else:
                        send_action(f"✅ Sync: 確認OK (hands={hands} reg={reg:.0f}) — BET開始")
                        send_log(f"[Sync-Entry] ✅ 入場後確認OK: {hands}ハンド reg={reg:.0f} → BET開始")
                        _awaiting_sync_confirm = False
                else:
                    # ビーズロード空 = シャッフル直後 / 新シュー直後 → BET不可テーブル
                    # 過去はここで「継続」していたが、Pattern モードでは判定不能 → 即退避
                    send_action("⚠️ Sync: ビーズロード空 (シャッフル/新シュー) — 退避")
                    send_log("[Sync-Entry] ❌ ビーズロード空 → シャッフル/新シュー疑い → 退避")
                    mark_table_exited(target_name)
                    executor.exit_table()
                    import random as _rand_empty
                    _w = _rand_empty.uniform(5, 10)
                    send_log(f"[Sync] 🚶 ロビーで{_w:.0f}秒待機...")
                    if stop_event.wait(_w):
                        break
                    if not scraper.get_all_table_configs():
                        try:
                            scraper.setup_ws_intercept()
                        except Exception:
                            pass
                    res_s = find_table()
                    if not res_s:
                        break
                    target_tid, target_name = res_s
                    with scraper._lock:
                        scraper._target_table_ids.add(target_tid)
                        scraper._target_table_names[target_tid] = target_name
                        scraper._new_shoe_signals[target_tid] = False
                        scraper._shoe_epochs[target_tid] = int(time.time())
                    send_action(f"🚪 Entering {target_name}...")
                    if not executor.enter_table(target_tid, target_name):
                        send_log("[Sync-Entry] ⚠️ 入場失敗 → full_recovery")
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if not executor.enter_table(target_tid, target_name):
                            send_log("[Sync-Entry] ⚠️ full_recovery後も入場失敗 → restart_browser")
                            fr2 = restart_browser("Sync-Entry enter_table failed after full_recovery")
                            if not fr2:
                                break
                            target_tid, target_name = fr2
                            if not executor.enter_table(target_tid, target_name):
                                send_log("[Sync-Entry] ❌ restart後も入場失敗 → テーブル再選定へ")
                                target_tid = None
                                target_name = None
                                continue
                    _awaiting_sync_confirm = True
                    continue
            except Exception as e:
                send_log(f"[Sync-Entry] ⚠️ エラー: {e}")
                _awaiting_sync_confirm = False

        # ── sync_pause: ドラゴン中は BET せず観戦 (原点 465843d 版 + 失敗時退避) ──
        if _effective_mode_box[0] == "sync_pause" and _paused_for_dragon:
            send_action(f"🐉 Dragon pause ({_consec_banker} Bs) — observing for Player...")
            obs = observe_one_hand_no_bet()
            if obs == 'P':
                _paused_for_dragon = False
                _consec_banker = 0
                _observe_fail_count = 0
                send_log(f"[sync_pause] ✅ Player出現 → BET再開 (MaruBatsu状態保持)")
                send_action("✅ Dragon ended — resuming BET")
            elif obs == 'B':
                _consec_banker += 1
                _observe_fail_count = 0
                send_log(f"[sync_pause] 🐉 Banker継続 ({_consec_banker}連) → 観戦継続")
            else:
                # obs=None — bead road 読み取り失敗 / wait_for_betting_phase 失敗
                # 連続 OBSERVE_FAIL_LIMIT 回でテーブル死亡疑い → full_recovery
                _observe_fail_count += 1
                send_log(f"[sync_pause] 観戦失敗 ({_observe_fail_count}/{OBSERVE_FAIL_LIMIT}) → 1秒待機")
                if _observe_fail_count >= OBSERVE_FAIL_LIMIT:
                    send_action(f"⚠️ 観戦失敗{_observe_fail_count}回連続 → テーブル死亡疑い → full_recovery")
                    send_log(f"[sync_pause] 観戦失敗{OBSERVE_FAIL_LIMIT}回連続 → 退避 + full_recovery")
                    _paused_for_dragon = False
                    _consec_banker = 0
                    _observe_fail_count = 0
                    try:
                        mark_table_exited(target_name)
                        executor.exit_table()
                    except Exception:
                        pass
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    _awaiting_sync_confirm = True
                    continue
                if stop_event.wait(1):
                    break
            continue

        # ── pattern モード: BET 直前に大路罫線パターンチェック ──
        # backtest 結果: テレコ+ニコ混合 + Strategy A で ROI +12〜15%
        # 詳細: PATTERN_STRATEGY_FINDINGS.md
        # pattern_test も同じパターン判定経路を通る ($1固定BETに分岐)
        if _effective_mode_box[0] in ("pattern", "pattern_test"):
            try:
                from pattern_classifier import classify_pattern
                from regularity_monitor import MIN_HANDS_FOR_ENTRY
                from strategy_router import decide_bet_blead, compute_b_lead
                bead = executor.read_bead_road() or ""
                pattern = classify_pattern(bead)
                b_lead, p_cnt, b_cnt = compute_b_lead(bead)

                if (p_cnt + b_cnt) >= MIN_HANDS_FOR_ENTRY and b_lead >= 0:
                    zone = "死亡ゾーン" if b_lead <= 5 else "Banker dominant"
                    send_action(f"⚠️ B-lead={b_lead} ({zone}) → 退避")
                    send_log(f"[pattern] B-lead={b_lead} P{p_cnt}/B{b_cnt} → {zone} → 退避")
                    mark_table_exited(target_name)
                    executor.exit_table()
                    if stop_event.wait(BB_EXIT_LOBBY_WAIT):
                        break
                    if not scraper.get_all_table_configs():
                        try:
                            scraper.setup_ws_intercept()
                        except Exception:
                            pass
                    res_sync = find_table()
                    if not res_sync:
                        break
                    target_tid, target_name = res_sync
                    with scraper._lock:
                        scraper._target_table_ids.add(target_tid)
                        scraper._target_table_names[target_tid] = target_name
                        scraper._new_shoe_signals[target_tid] = False
                        scraper._shoe_epochs[target_tid] = int(time.time())
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                    _awaiting_sync_confirm = True
                    continue

                # BET禁止パターン → テーブル退避 → 別テーブル
                if pattern in ("ブリッジ", "ニコニコ・ニコイチ", "不規則", "偏在"):
                    send_action(f"⚠️ パターン={pattern} → BET禁止 → 退避")
                    send_log(f"[pattern] パターン={pattern} → BET禁止 → 退避")
                    mark_table_exited(target_name)
                    executor.exit_table()
                    if stop_event.wait(BB_EXIT_LOBBY_WAIT):
                        break
                    if not scraper.get_all_table_configs():
                        try:
                            scraper.setup_ws_intercept()
                        except Exception:
                            pass
                    res_sync = find_table()
                    if not res_sync:
                        break
                    target_tid, target_name = res_sync
                    with scraper._lock:
                        scraper._target_table_ids.add(target_tid)
                        scraper._target_table_names[target_tid] = target_name
                        scraper._new_shoe_signals[target_tid] = False
                        scraper._shoe_epochs[target_tid] = int(time.time())
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                    _awaiting_sync_confirm = True
                    continue

                # 不明 → 新シュー / シャッフル直後 / 列数不足 → 即退避
                # 流れがわからないテーブルで時間を浪費しない (PATTERN_UNKNOWN_LIMIT=1)
                if pattern == "不明":
                    _pattern_unknown_count += 1
                    if _pattern_unknown_count >= PATTERN_UNKNOWN_LIMIT:
                        send_action(f"⚠️ パターン不明 → 新シュー疑い → 即退避")
                        send_log(f"[pattern] パターン不明 (列数不足/新シュー) → 即退避 + 別テーブル")
                        _pattern_unknown_count = 0
                        mark_table_exited(target_name)
                        executor.exit_table()
                        if stop_event.wait(BB_EXIT_LOBBY_WAIT):
                            break
                        if not scraper.get_all_table_configs():
                            try:
                                scraper.setup_ws_intercept()
                            except Exception:
                                pass
                        res_sync = find_table()
                        if not res_sync:
                            break
                        target_tid, target_name = res_sync
                        with scraper._lock:
                            scraper._target_table_ids.add(target_tid)
                            scraper._target_table_names[target_tid] = target_name
                            scraper._new_shoe_signals[target_tid] = False
                            scraper._shoe_epochs[target_tid] = int(time.time())
                        if not executor.enter_table(target_tid, target_name):
                            fr = full_recovery()
                            if not fr:
                                break
                            target_tid, target_name = fr
                        _awaiting_sync_confirm = True
                        continue
                    send_log(f"[pattern] パターン未確定 ({_pattern_unknown_count}/{PATTERN_UNKNOWN_LIMIT}) → 1ハンド観戦")
                    obs = observe_one_hand_no_bet()
                    continue
                else:
                    # 既知パターン検出 → 不明カウンタリセット
                    _pattern_unknown_count = 0

                # Strategy A / D の判定 (前手から SKIP or BET P)
                side, strat_name, reason = decide_bet_blead(pattern, bead)
                if side is None:
                    # SKIP — 1ハンド観戦して次へ
                    send_log(f"[pattern-{strat_name}] {pattern} SKIP: {reason}")
                    obs = observe_one_hand_no_bet()
                    continue

                # === Pattern Test モード: $1 固定 BET、VPS 記録なし、bead road 直読み ===
                # 結果判定は bead road を真実とする (balance diff の timing 問題を回避)
                if _effective_mode_box[0] == "pattern_test":
                    test_amount = 1.0

                    # BET 前の bead road 長を記録 (差分検知用)
                    try:
                        pre_bead = executor.read_bead_road() or ""
                    except Exception:
                        pre_bead = ""
                    pre_bead_len = len(pre_bead)

                    send_log(f"[pattern-test-{strat_name}] {pattern} BET ${test_amount}: {reason} (bead_len={pre_bead_len})")
                    send_action(f"🧪 [TEST] BET ${test_amount} PLAYER ({pattern})")

                    # BET phase 待機
                    if not executor.wait_for_betting_phase(timeout=60, skip_round=False):
                        send_log("[pattern-test] BET phase timeout — skip")
                        continue

                    # $1 固定 BET (実 BET だが最小単位)
                    if not executor.place_bet("player", test_amount, strict=True):
                        send_log("[pattern-test] place_bet failed — skip")
                        continue

                    # bead road 更新を待って結果を読む (90秒タイムアウト)
                    # balance diff の timing 問題を回避するため、bead road を真実とする
                    result_char = None
                    _deadline = time.time() + 90
                    while time.time() < _deadline:
                        if stop_event.is_set():
                            break
                        try:
                            new_bead = executor.read_bead_road() or ""
                            if len(new_bead) > pre_bead_len:
                                new_chars = new_bead[pre_bead_len:]
                                # 末尾の文字 = この BET の結果
                                for ch in reversed(new_chars):
                                    if ch in ('P', 'B', 'T'):
                                        result_char = ch
                                        break
                                if result_char:
                                    break
                        except Exception:
                            pass
                        if stop_event.wait(0.5):
                            break

                    if not result_char:
                        send_log("[pattern-test] bead road 更新タイムアウト — skip")
                        continue

                    # 結果分類
                    if result_char == 'P':
                        res = "player"
                        won = True
                        _test_wins += 1
                        send_action(f"🧪 [TEST] WIN +${test_amount:.0f} | {_test_wins}W/{_test_losses}L/{_test_ties}T")
                    elif result_char == 'B':
                        res = "banker"
                        won = False
                        _test_losses += 1
                        send_action(f"🧪 [TEST] LOSE -${test_amount:.0f} | {_test_wins}W/{_test_losses}L/{_test_ties}T")
                    else:  # 'T'
                        res = "tie"
                        won = None
                        _test_ties += 1
                        send_action(f"🧪 [TEST] TIE (BET返却) | {_test_wins}W/{_test_losses}L/{_test_ties}T")

                    bal = executor.get_balance() if not dry_run else 0

                    # GUI に test_status 送信 (sessionPNL は更新しない)
                    send_msg({
                        "type": "test_status",
                        "wins": _test_wins,
                        "losses": _test_losses,
                        "ties": _test_ties,
                        "last_result": res,
                        "last_won": won,
                        "balance": bal,
                    })
                    send_log(f"[pattern-test] Result: {res} (bead='{result_char}') → TEST {_test_wins}W/{_test_losses}L/{_test_ties}T (VPS未記録)")
                    continue  # 通常 BET フロー (run_round) はスキップ

                # else: side = 'P' → 通常 BET フロー (run_round) へ進む
                send_log(f"[pattern-{strat_name}] {pattern} BET: {reason}")
            except Exception as _pe:
                send_log(f"[pattern] エラー (素通り): {_pe}")

        # === Pattern Test モード安全装置: ここに到達したら絶対に BET しない ===
        # 通常パターンチェックで例外発生 / 素通りした場合、本来は session.run_round() に
        # 進むが、pattern_test では \$1 固定 BET 以外を絶対に許さない。
        # \$50 の本気 BET 事故を防ぐため、ここで即 continue する。
        if _effective_mode_box[0] == "pattern_test":
            send_log("[pattern-test] ⚠️ パターン分岐外 — $50 BET 事故防止のため SKIP (1ハンド観戦)")
            obs = observe_one_hand_no_bet()
            continue

        # BET phase — amount comes from session (remote: from VPS state; local: from tracker)
        bet_amount = session.get_bet_amount()
        total_hands = session.total_wins + session.total_losses + session.total_ties + 1
        send_action(f"Hand #{total_hands} -- Betting ${bet_amount:.0f}")

        result = session.run_round(lambda: not stop_event.is_set())

        if result["action"] == "exit":
            # Phase 1: wait_for_result 連続失敗 (2回) → シャッフル中と判断 → 即テーブル退避
            _consec_wait_result_fail += 1
            if _consec_wait_result_fail >= WAIT_RESULT_FAIL_LIMIT:
                send_action(f"⚠️ wait_for_result {_consec_wait_result_fail} consecutive failures — assuming shuffle → exit table")
                send_log(f"[shuffle-detect] wait_for_result {_consec_wait_result_fail} consecutive failures → cooldown + different table")
                _consec_wait_result_fail = 0
                mark_table_exited(target_name)
                executor.exit_table()
                if stop_event.wait(BB_EXIT_LOBBY_WAIT):
                    break
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                if _effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"):
                    res_sync = find_table()
                    if not res_sync:
                        break
                    target_tid, target_name = res_sync
                    with scraper._lock:
                        scraper._target_table_ids.add(target_tid)
                        scraper._target_table_names[target_tid] = target_name
                        scraper._new_shoe_signals[target_tid] = False
                        scraper._shoe_epochs[target_tid] = int(time.time())
                    if not executor.enter_table(target_tid, target_name):
                        fr = full_recovery()
                        if not fr:
                            break
                        target_tid, target_name = fr
                    _awaiting_sync_confirm = True
                    continue

            send_action("Session interrupted — attempting re-entry...")
            executor.exit_table()
            time.sleep(5)

            if stop_event.is_set():
                break

            send_action(f"Re-entering {target_name}...")
            if executor.enter_table(target_tid, target_name):
                entry_fail_count = 0
                _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                continue
            else:
                entry_fail_count += 1
                if entry_fail_count >= 2:
                    # EV.5 / SESSION EXPIRED — Stake再ログインを試行
                    send_action("Entry failed repeatedly — attempting Stake re-login...")
                    send_log("[session] Re-login attempt after entry failures")
                    try:
                        scraper._login_from_lobby()
                        time.sleep(5)
                        scraper.setup_ws_intercept()
                    except Exception as _rl_err:
                        send_log(f"[session] Re-login failed: {_rl_err}")
                        # ロビーに戻ってconfigsを復旧
                        try:
                            scraper.setup_ws_intercept()
                        except Exception:
                            pass
                    # 再選定
                    send_action("Re-selecting table after re-login...")
                    target_tid = None
                    target_name = None
                    _reselect_tries = 0
                    while not stop_event.is_set() and target_tid is None and _reselect_tries < 5:
                        _reselect_tries += 1
                        best = pick_table()
                        if best:
                            target_tid = best.table_id
                            target_name = best.title
                            with scraper._lock:
                                scraper._target_table_ids.add(target_tid)
                                scraper._target_table_names[target_tid] = target_name
                        else:
                            if not scraper.get_all_table_configs():
                                try:
                                    scraper.setup_ws_intercept()
                                except Exception:
                                    pass
                            if stop_event.wait(15):
                                break
                    entry_fail_count = 0
                    if target_tid and not stop_event.is_set():
                        send_action(f"Entering {target_name}...")
                        if executor.enter_table(target_tid, target_name):
                            _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                            continue
                    if stop_event.is_set():
                        break
                    # 通常リカバリ全失敗 → フルリカバリ（最終手段）
                    send_log("[recovery] All normal recovery failed — escalating to full recovery")
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if executor.enter_table(target_tid, target_name):
                        _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                        continue
                    send_action("Full recovery failed — stopping")
                    break
                else:
                    time.sleep(10)
                    continue

        round_count += 1

        # ── BET完全失敗チェック: bet_amount=0 なら失敗カウント ──
        # 「ネットワーク遅延ありき」設計: 2回連続でiframeリセット
        # （以前は3回だったが、復帰までに約2分かかっていたため短縮）
        if result.get("bet_amount", 0) == 0 and result.get("result"):
            _bet_fail_count += 1
            send_log(f"[bet-fail] BET failed ({_bet_fail_count}/2) on {target_name}")
            if _bet_fail_count >= 2:
                send_action(f"BET failed 2 times on {target_name} — resetting iframe...")
                send_log(f"[bet-fail] 2 consecutive failures — exit + re-enter (iframe reset)")
                executor.exit_table()
                # ロビー滞在を短縮: iframe リセットが目的なので Bot 感を出すための長い待機は不要
                _lobby_wait3 = 5
                send_action(f"Resetting iframe... ({_lobby_wait3}s)")
                if stop_event.wait(_lobby_wait3):
                    break
                _bet_fail_count = 0
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                if not observe_until_1_drop(target_tid, target_name):
                    break
                if not executor.enter_table(target_tid, target_name):
                    # 通常リカバリ失敗 → フルリカバリ
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        send_action("Full recovery failed — stopping")
                        break
                _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")
                continue
        else:
            _bet_fail_count = 0  # 成功したらリセット
            _consec_wait_result_fail = 0  # Phase 1: wait_for_result 成功 → 連続失敗カウンタリセット

        # Send round result to GUI
        if result.get("result"):
            res = result["result"]
            won = result.get("won")
            ba = result.get("bet_amount", 0)
            bal = executor.get_balance() if not dry_run else 0
            _update_actual_profit(bal)
            turns = session.tracker.current_turns
            turns_disp = "".join("O" if t == "O" else "X" for t in turns)
            cp = session.tracker.cumulative_profit
            cm = cp * chip_base

            # Bet confirmation guard: avoid GUI L/W when no bet was actually placed
            bet_confirmed = True
            if ba <= 0:
                bet_confirmed = False
            else:
                dom_total = 0.0
                confirmed_total = 0.0
                try:
                    dom_total = executor._get_total_bet()
                except Exception:
                    pass
                try:
                    if executor.game_ws:
                        confirmed = getattr(executor.game_ws, "_last_confirmed", {})
                        if isinstance(confirmed, dict):
                            confirmed_total = sum(
                                v for v in confirmed.values()
                                if isinstance(v, (int, float))
                            )
                except Exception:
                    pass
                if dom_total <= 0 and confirmed_total <= 0:
                    bet_confirmed = False

            if bet_confirmed:
                # Per-round profit in dollars (for daily P&L aggregation)
                if res == "tie":
                    round_profit = 0.0
                elif won:
                    round_profit = ba  # Player win returns 1x bet
                else:
                    round_profit = -ba
                daily_profit += round_profit

                if res == "tie":
                    send_action(f"Tie — BET returned. Balance: ${bal:.2f}")
                elif won:
                    send_action(f"WIN! +${ba:.0f}. Balance: ${bal:.2f}")
                else:
                    send_action(f"LOSE. -${ba:.0f}. Balance: ${bal:.2f}")

                send_result(
                    res, won, ba, bal, len(turns), turns_disp, cp, cm, round_profit,
                    round_profit_actual=last_balance_diff,
                    cumulative_money_actual=money_pnl_actual if actual_profit_ready else None
                )
            else:
                send_log("[bet] result observed but bet not confirmed — skipping GUI result")

            # ── sync_pause: 連B 検出と観戦突入判定 (原点 465843d 版) ──
            if _effective_mode_box[0] == "sync_pause":
                if res == "banker":
                    _consec_banker += 1
                    if _consec_banker >= PAUSE_THRESHOLD and not _paused_for_dragon:
                        _paused_for_dragon = True
                        send_action(f"🐉 Dragon pause ({_consec_banker} Bs) — pausing BET")
                        send_log(f"[sync_pause] {_consec_banker}連B検出 → 観戦モード突入 (MaruBatsu状態保持)")
                elif res == "player":
                    if _consec_banker > 0:
                        send_log(f"[sync_pause] Player出現 → 連B カウンタリセット")
                    _consec_banker = 0
                # tie の場合: 連Bカウントは維持

        # Set complete
        if result.get("completed_set"):
            s = result["completed_set"]
            send_set_complete(s, chip_base)
            send_shoe_history(session.tracker.sets, chip_base)
            send_action(f"Set #{s.set_index} done: {s.wins}W/{s.losses}L, P&L: {s.set_profit:+d}")
            # Public channel broadcast (verification only)
            try:
                composite.on_set_complete({
                    "set_index": s.set_index,
                    "results": s.results,
                    "wins": s.wins,
                    "losses": s.losses,
                    "set_profit": s.set_profit,
                }, s.cumulative_profit * chip_base, verification_mode)
            except Exception as e:
                logger.warning(f"Public notify failed: {e}")

            # ── A. 30分タイマー: set 完了 + 30分経過で casino detour ──
            # detour は不安定化要因になりうるためデフォルト無効
            if ENABLE_CASINO_DETOUR and time.time() - _last_recovery_time > PROACTIVE_RECOVERY_INTERVAL:
                elapsed_min = (time.time() - _last_recovery_time) / 60
                send_log(f"[proactive-detour] 30min elapsed ({elapsed_min:.0f}min) → casino detour")
                # 一旦テーブルを抜けてから detour
                executor.exit_table()
                time.sleep(2)
                if casino_detour(reason=f"30分経過 ({elapsed_min:.0f}分)"):
                    # detour 後に同じテーブルへ再入場 (sync mode は新規探索)
                    if _effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"):
                        res_sync = find_table()
                        if not res_sync:
                            break
                        target_tid, target_name = res_sync
                        with scraper._lock:
                            scraper._target_table_ids.add(target_tid)
                            scraper._target_table_names[target_tid] = target_name
                            scraper._new_shoe_signals[target_tid] = False
                            scraper._shoe_epochs[target_tid] = int(time.time())
                        if not executor.enter_table(target_tid, target_name):
                            fr = full_recovery()
                            if not fr:
                                break
                            target_tid, target_name = fr
                        _awaiting_sync_confirm = True
                    else:
                        if not executor.enter_table(target_tid, target_name):
                            fr = full_recovery()
                            if not fr:
                                break
                            target_tid, target_name = fr
                continue

        # Profit/loss reset (actual balance優先)
        should_reset = bool(result.get("should_reset"))
        actual_hit = False
        if actual_profit_ready:
            actual_hit = (money_pnl_actual >= profit_target_dollars or money_pnl_actual <= -loss_cut_dollars)
            if should_reset and not actual_hit and not _actual_override_logged:
                send_log(
                    f"[profit] chip target hit but actual ${money_pnl_actual:.2f} < target ${profit_target_dollars:.0f} — continue"
                )
                _actual_override_logged = True

        trigger_reset = actual_hit if actual_profit_ready else should_reset
        if trigger_reset:
            if actual_profit_ready:
                is_win = money_pnl_actual >= profit_target_dollars
                money = money_pnl_actual
            else:
                cp = session.effective_profit()
                money = cp * chip_base
                is_win = cp >= session.profit_stop
            reason_en = "PROFIT TARGET" if is_win else "LOSS CUT"
            send_msg({
                "type": "session_reset",
                "is_profit": is_win,
                "amount": money,
                "reason": reason_en,
            })
            send_action(f"{reason_en} HIT! {'+$' if money >= 0 else '-$'}{abs(money):.0f} locked in -- new session starting")
            send_log(f"[{reason_en}] Session ended at {'+$' if money >= 0 else '-$'}{abs(money):.0f}")
            daily_sessions += 1
            if is_win:
                daily_profit_sessions += 1
            else:
                daily_loss_sessions += 1
            # Admin + Public broadcast
            try:
                hands_count = session.total_bets
                sess_num = session.session_count + 1
                if is_win:
                    composite.on_profit_target(
                        user_label, sess_num, money, hands_count,
                        daily_profit_actual if actual_profit_ready else daily_profit,
                        verification_mode, target_name or ""
                    )
                else:
                    composite.on_loss_cut(
                        user_label, sess_num, money, hands_count,
                        daily_profit_actual if actual_profit_ready else daily_profit,
                        verification_mode, target_name or ""
                    )
            except Exception as e:
                logger.warning(f"Reset notify failed: {e}")
            bal_now = executor.get_balance() if not dry_run else 0
            try:
                session.reset_session("profit" if is_win else "losscut", actual_amount=money, balance=bal_now)
            except TypeError:
                session.reset_session("profit" if is_win else "losscut")
            send_shoe_history(session.tracker.sets, chip_base)

            # ── D'. 利確/損切り後の予防リカバリ ──
            # 完全な区切りなので最も安全なリフレッシュタイミング
            # iframe 劣化を完全リセット + 新しい推奨テーブルで再開
            if is_win:
                profit_sessions_done += 1
                limit = _profit_session_limit_box[0]
                if limit and profit_sessions_done >= limit:
                    send_action("Profit session limit reached — stopping")
                    send_log(f"[profit-limit] reached {profit_sessions_done}/{limit} — stopping")
                    stop_event.set()
                    break
            send_log(f"[proactive-recovery] {reason_en} → recovery")
            money_pnl_actual = 0.0
            _actual_override_logged = False
            if bal_now > 0:
                balance_last = bal_now
                actual_profit_ready = True
                _reset_session_open(bal_now)
            else:
                balance_last = None
                actual_profit_ready = False
            if not proactive_full_recovery(reason_en):
                break
            _awaiting_sync_confirm = (_effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test"))
            continue

        # ── Mix自動切替: セット確定後にOS>=20なら1-Dropモードへ ──
        if _bet_mode_box[0] == "mix" and _effective_mode_box[0] == "normal":
            os_val = getattr(session.tracker, 'prev_overshoot', 0)
            if os_val >= 20:
                _effective_mode_box[0] = "1drop"
                send_action(f"OS={os_val} ≥ 20 — switching to 1-Drop mode")
                send_log(f"[mix] Auto-switch to 1-drop (OS={os_val})")
                send_msg({"type": "mode_changed", "mode": "1drop"})

        # Periodic status
        bal = executor.get_balance() if not dry_run else 0
        _update_actual_profit(bal)
        send_status(session, bal, money_pnl_actual if actual_profit_ready else None)
        _flush_daily_summary(table_name=target_name or "")
        _schedule_session_state_sync(user_email, session, user_id, session_api_key)

        # ── Syncモード(+sync_pause): 動的規則性監視（毎ハンドチェック）──
        # シュー切替は一瞬で起きるため即検出が必要
        # 規則性崩壊・ハンド数不足・Banker dominantいずれも即退避
        if _effective_mode_box[0] in ("sync", "sync_pause", "pattern", "pattern_test") and target_tid:
            monitor = check_sync_regularity(target_tid)
            _pr = monitor.get('p_ratio', 0.5)
            _pc = monitor.get('p_count', 0)
            _bc = monitor.get('b_count', 0)
            _reason = monitor.get('exit_reason', '')
            if monitor['should_exit']:
                send_action(f"⚠️ Sync退避: {_reason} (reg={monitor['regularity']:.0f} hands={monitor['hands']} P{_pc}/B{_bc})")
                send_log(f"[Sync-Monitor] ❌ 退避判定: {_reason} (reg={monitor['regularity']:.0f} hands={monitor['hands']} P{_pc}/B{_bc} P比率={_pr:.0%})")
                mark_table_exited(target_name)
                executor.exit_table()
                import random as _rand_sync
                _wait = _rand_sync.uniform(5, 15)
                send_log(f"[Sync] 🚶 ロビーで{_wait:.0f}秒待機...")
                if stop_event.wait(_wait):
                    break
                if not scraper.get_all_table_configs():
                    try:
                        scraper.setup_ws_intercept()
                    except Exception:
                        pass
                send_action("🔍 Searching for next recommended table...")
                res_sync = find_table()
                if not res_sync:
                    break
                target_tid, target_name = res_sync
                with scraper._lock:
                    scraper._target_table_ids.add(target_tid)
                    scraper._target_table_names[target_tid] = target_name
                    scraper._new_shoe_signals[target_tid] = False
                    scraper._shoe_epochs[target_tid] = int(time.time())
                send_action(f"🚪 Entering {target_name}...")
                if not executor.enter_table(target_tid, target_name):
                    send_log("[Sync] ⚠️ 入場失敗 → フルリカバリ")
                    fr = full_recovery()
                    if not fr:
                        break
                    target_tid, target_name = fr
                    if not executor.enter_table(target_tid, target_name):
                        break
                _awaiting_sync_confirm = True  # 新テーブルで再確認
                continue
            else:
                send_log(f"[Sync-Monitor] ✅ reg={monitor['regularity']:.0f} hands={monitor['hands']} P{_pc}/B{_bc} (P{_pr:.0%}) → 継続")

        # ── 1落ちロジック: Player負け（Banker勝ち）→ テーブル退出 → ロビー観察 ──
        # normalモードではBanker負けでも退室せず、そのままテーブルに留まる
        if result.get("result") == "banker" and _effective_mode_box[0] == "1drop":
            send_action("Player lost — returning to lobby for 1-drop re-observation...")
            send_log("[1-drop] Banker won → exit table → observe lobby")
            executor.exit_table()
            # 退室後にランダム待機（10〜25秒）— 人間らしくロビーを眺める時間
            import random as _rand
            _lobby_wait = _rand.uniform(10, 25)
            send_action(f"Browsing lobby... ({_lobby_wait:.0f}s)")
            if stop_event.wait(_lobby_wait):
                break
            if stop_event.is_set():
                break
            # ロビーWS確認
            if not scraper.get_all_table_configs():
                send_log("[1-drop] Lobby WS lost — reconnecting...")
                try:
                    scraper.setup_ws_intercept()
                except Exception:
                    pass
            # 1落ち待機
            if not observe_until_1_drop(target_tid, target_name):
                break  # STOPされた
            # 再入場（3回リトライ）→ 失敗ならテーブル再選定
            _deferred_exit_reason = None  # 前ラウンドの判定を持ち越さない
            _reenter_ok = False
            for _retry in range(3):
                send_action(f"Re-entering {target_name} (attempt {_retry+1}/3)...")
                if executor.enter_table(target_tid, target_name):
                    _reenter_ok = True
                    _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")  # Banker負け再入場 → 2落ち確認必須
                    # テーブル変更後のshoe signalをクリア（誤検出防止）
                    with scraper._lock:
                        scraper._new_shoe_signals[target_tid] = False
                        scraper._shoe_epochs[target_tid] = int(time.time())
                    break
                send_log(f"[1-drop] Entry retry {_retry+1}/3 failed")
                time.sleep(5)
            if not _reenter_ok:
                send_action("Re-entry failed — re-selecting table...")
                send_log("[1-drop] All entry retries failed — re-selecting")
                target_tid = None
                target_name = None
                while not stop_event.is_set() and target_tid is None:
                    best = pick_table()
                    if best:
                        target_tid = best.table_id
                        target_name = best.title
                        with scraper._lock:
                            scraper._target_table_ids.add(target_tid)
                            scraper._target_table_names[target_tid] = target_name
                            if target_tid not in scraper._shoe_epochs:
                                scraper._shoe_epochs[target_tid] = int(time.time())
                                scraper._new_shoe_signals[target_tid] = False
                        send_action(f"Picked: {target_name}")
                    else:
                        send_action("No suitable table — waiting 15s...")
                        if stop_event.wait(15):
                            break
                if stop_event.is_set() or not target_tid:
                    break
                if not observe_until_1_drop(target_tid, target_name):
                    break
                if not executor.enter_table(target_tid, target_name):
                    send_action("New table entry failed — recovering...")
                    send_log("[1-drop] New table entry failed → full_recovery")
                    fr = full_recovery()
                    if not fr:
                        send_action("Full recovery failed — stopping")
                        break
                    target_tid, target_name = fr
                    if not observe_until_1_drop(target_tid, target_name):
                        break
                    _entry_ok = False
                    for _r in range(3):
                        send_action(f"Entering {target_name} (attempt {_r+1}/3)...")
                        if executor.enter_table(target_tid, target_name):
                            _entry_ok = True
                            break
                        time.sleep(5)
                    if not _entry_ok:
                        send_action("Entry still failing — restarting browser...")
                        fr = restart_browser("enter_table failed after full_recovery")
                        if not fr:
                            break
                        target_tid, target_name = fr
                        if not observe_until_1_drop(target_tid, target_name):
                            break
                        if not executor.enter_table(target_tid, target_name):
                            send_action("Entry failed after browser restart — re-selecting")
                            target_tid = None
                            target_name = None
                            continue
                _awaiting_2nd_drop = (_effective_mode_box[0] == "1drop")  # 新テーブル再入場 → 2落ち確認必須
            continue

        # Deferred exit check: runs during dealing phase (after result, before next BET window)
        # This avoids blocking the BET window with a VPS API call
        if len(session.tracker.current_turns) == 0 and target_tid:
            try:
                _deferred_exit_reason = selector.should_exit_table(target_tid, selector_config=table_filter)
            except Exception as _ec:
                logger.warning(f"Deferred exit check failed: {_ec}")
                _deferred_exit_reason = None

    # === Shutdown ===
    # Ensure any heartbeat threads (and external watchdog stale detection) won't be kept alive
    # after the bet loop has ended for any reason.
    try:
        stop_event.set()
    except Exception:
        pass
    send_action("Stopping...")
    summary = session.get_summary()
    balance = executor.get_balance() if not dry_run else 0
    send_log(
        f"Session ended. Bets:{summary['total_bets']} "
        f"W:{summary['total_wins']} L:{summary['total_losses']} "
        f"P&L:{summary['cumulative_profit']:+d} chips"
    )
    send_action("Closing table...")
    executor.exit_table()
    send_action("Closing browser...")
    scraper.stop()
    send_action("Stopped.")
    _flush_daily_summary(force=True, table_name=target_name or "")
    try:
        composite.on_shutdown(user_label, "Normal stop")
    except Exception:
        pass
    _active_session = None


# ======== Main ========

def main():
    stop_event = threading.Event()
    skip_event = threading.Event()
    bet_thread = None

    def stdin_reader():
        nonlocal bet_thread
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_type = msg.get("type", "")

                if msg_type == "start":
                    config = msg.get("config", {})
                    if bet_thread is None or not bet_thread.is_alive():
                        stop_event.clear()
                        skip_event.clear()
                        bet_thread = threading.Thread(
                            target=run_bet_session, args=(config, stop_event, skip_event), daemon=True
                        )
                        bet_thread.start()
                        send_log("BET session starting...")

                elif msg_type == "stop":
                    stop_event.set()
                    send_log("Stop requested.")

                elif msg_type == "skip_table":
                    skip_event.set()
                    send_log("Skip table requested by user.")

                elif msg_type == "update_config":
                    # Live update of profit_target / loss_cut
                    cfg = msg.get("config", {})
                    if _active_session is not None:
                        s = _active_session
                        chip_base_val = s.chip_base
                        new_pt_chips = None
                        new_lc_chips = None
                        if "profit_target" in cfg:
                            new_pt = float(cfg["profit_target"])
                            new_pt_chips = max(1, int(round(new_pt / max(chip_base_val, 0.01))))
                            s.profit_stop = new_pt_chips
                            send_log(f"Profit target updated: ${new_pt:.0f} ({new_pt_chips} chips)")
                        if "loss_cut" in cfg:
                            new_lc = float(cfg["loss_cut"])
                            new_lc_chips = max(1, int(round(new_lc / max(chip_base_val, 0.01))))
                            s.loss_cut = new_lc_chips
                            send_log(f"Loss cut updated: ${new_lc:.0f} ({new_lc_chips} chips)")
                        if "profit_session_limit" in cfg or "profit_sessions_limit" in cfg:
                            limit_val = cfg.get("profit_session_limit", cfg.get("profit_sessions_limit", 0))
                            try:
                                limit_val = int(limit_val)
                            except Exception:
                                limit_val = 0
                            if limit_val < 0:
                                limit_val = 0
                            _profit_session_limit_box[0] = limit_val
                            send_log(f"Profit session limit updated: {limit_val}")
                        counter_cfg = {
                            k: cfg[k]
                            for k in ("entry_window", "entry_threshold", "exit_drop3_limit", "exit_drop5_immediate")
                            if k in cfg
                        }
                        if counter_cfg:
                            _apply_counter_params(counter_cfg, "live")
                        if cfg.get("use_cloud_params"):
                            try:
                                from counter_logic import apply_optimal_params, ENTRY_WINDOW, ENTRY_THRESHOLD, EXIT_DROP3_LIMIT, EXIT_DROP5_IMMEDIATE
                                if apply_optimal_params():
                                    send_log(f"[counter] Cloud params reloaded: W={ENTRY_WINDOW} T={ENTRY_THRESHOLD} D3={EXIT_DROP3_LIMIT} D5={EXIT_DROP5_IMMEDIATE}")
                                else:
                                    send_log("[counter] Cloud params reload skipped (unavailable)")
                            except Exception as e:
                                send_log(f"[counter] Cloud params reload failed: {e}")
                        # Sync to remote session if applicable
                        if hasattr(s, "update_config") and hasattr(s, "client"):
                            try:
                                s.update_config(profit_stop=new_pt_chips, loss_cut=new_lc_chips)
                            except Exception as e:
                                send_log(f"Remote config sync failed: {e}")
                    else:
                        # Session not yet initialized, buffer for later
                        _pending_config_update.update(cfg)
                        if "profit_session_limit" in cfg or "profit_sessions_limit" in cfg:
                            limit_val = cfg.get("profit_session_limit", cfg.get("profit_sessions_limit", 0))
                            try:
                                limit_val = int(limit_val)
                            except Exception:
                                limit_val = 0
                            if limit_val < 0:
                                limit_val = 0
                            _profit_session_limit_box[0] = limit_val
                        send_log(f"Config buffered (session starting): {cfg}")

                elif msg_type == "change_mode":
                    new_mode = msg.get("mode", "1drop")
                    if new_mode in ("normal", "1drop", "mix", "sync", "sync_pause", "pattern", "pattern_test", "counter", "counter_flat", "counter_seq7"):
                        _bet_mode_box[0] = new_mode
                        _effective_mode_box[0] = "normal" if new_mode == "mix" else new_mode
                        send_log(f"BET mode changed to: {new_mode} (effective: {_effective_mode_box[0]})")
                        send_msg({"type": "mode_changed", "mode": new_mode})

                elif msg_type == "get_status":
                    # Status is sent periodically from bet loop
                    pass

            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON: {line}")
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                send_msg({"type": "error", "message": str(e)})

    reader_thread = threading.Thread(target=stdin_reader, daemon=True)
    reader_thread.start()

    send_log("LAPLACE Engine ready.")

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, EOFError):
        stop_event.set()


if __name__ == "__main__":
    main()
