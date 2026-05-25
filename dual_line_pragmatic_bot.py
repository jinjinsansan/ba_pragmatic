"""Dual-Line Match Pragmatic Autonomous Bot (DRY RUN / LIVE)

仕様: SPEC_DUAL_LINE_MATCHING.md
戦略コア: dual_line_match.py
ベース: collector_pragmatic.Collector を継承 (= bacopy_watch_pragmatic.py パターン)

動作:
  1. Camoufox で Stake Pragmatic Play lobby を開く
  2. WS から全テーブルのハンド情報をリアルタイムで観察
  3. 各テーブルごとに observed_sequence を維持
  4. 新ハンド到着 → 前回の prediction を resolve (= 当たり/外れ判定)
  5. 同時に次の手を decide() で予想 → v2 patterns に該当なら pending 保存 + Telegram 通知
  6. 統計を state JSON に persist

DRY RUN: 実 BET なし、予想と結果の対応のみ追跡。BetExecutor を no-op にする。
LIVE:   BetExecutor 経由で実 BET を発行。bacopy_executor_pragmatic_ws_live.py と統合。

Usage (VPS 上):
  python dual_line_pragmatic_bot.py [--headless] [--no-v2-filter] [--live]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

# このスクリプトは VPS の /opt/laplace2/ に置く想定
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# PyInstaller one-file exe では __file__ が毎回異なる _MEI* 一時ディレクトリを指すため
# state ファイルは exe と同じディレクトリ（再起動後も残る）に保存する
import sys as _sys
_PERSISTENT_DIR: Path = (
    Path(_sys.executable).resolve().parent
    if getattr(_sys, "frozen", False)
    else HERE
)

# .env を load (= TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 等)
# 複数候補を試し、TELEGRAM credentials が含まれているものを優先
try:
    from dotenv import load_dotenv
    _exe_dir = Path(getattr(sys, "executable", "")).resolve().parent if getattr(sys, "executable", "") else Path("")
    _env_candidates = [
        HERE / ".env",                            # /opt/laplace2/.env
        Path("/opt/laplace/.env"),                # production の場所
        HERE.parent / "bacopy" / ".env",          # /opt/bacopy/.env
        Path("/opt/bacopy/.env"),                 # 同上 (絶対パス)
        Path("C:/bacopy/.env"),                   # packaged Windows GUI source/runtime config
        _exe_dir / ".env",                        # .../engine/.env
        _exe_dir.parent / ".env",                 # .../resources/.env
    ]
    for _env_path in _env_candidates:
        if _env_path.exists():
            load_dotenv(_env_path, override=False)
except ImportError:
    pass

# Local imports (collector_pragmatic は VPS /opt/laplace2/ に存在)
try:
    import collector_pragmatic as cp
except Exception as e:
    sys.stderr.write(
        f"collector_pragmatic import failed: {e}\n"
        "Run on VPS at /opt/laplace2/, or ensure PYTHONPATH includes it.\n"
    )
    raise

from dual_line_match import (
    LIVE_SIGNAL_PATTERNS,
    decide,
    live_preposition_for_history,
    score_proximity,
    chinese_road_predict,
    big_road_predict,
)
from dual_line_money import BetManager, ALLOWED_MODES as MONEY_MODES, BET_MODES

# ── Logger 設定 ──────────────────────────────────────────────────────
# collector_pragmatic.py の basicConfig と競合しないよう dedicated logger を使う
_bot_logger = logging.getLogger("dual_line.bot")
_bot_logger.setLevel(logging.INFO)
_bot_logger.propagate = False  # root logger に伝播させない
if not _bot_logger.handlers:
    _fh = logging.FileHandler(_PERSISTENT_DIR / "dual_line_pragmatic_bot.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _bot_logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _bot_logger.addHandler(_sh)
logger = _bot_logger

# ── 定数 ─────────────────────────────────────────────────────────────

V2_PATTERNS = LIVE_SIGNAL_PATTERNS

STATE_PATH = _PERSISTENT_DIR / "dual_line_pragmatic_state.json"
STATE_TMP = _PERSISTENT_DIR / "dual_line_pragmatic_state.tmp"
_DEFAULT_PREPOSITION_HINT_FILE = (
    Path("/opt/bacopy/data/latest_preposition_pragmatic.json")
    if os.name != "nt"
    else _PERSISTENT_DIR / "data" / "latest_preposition_pragmatic.json"
)
PREPOSITION_HINT_FILE = Path(
    os.getenv("BACOPY_PREPOSITION_HINT_FILE", str(_DEFAULT_PREPOSITION_HINT_FILE))
)
LOGIC_VERSION = "v3_whitelist6_sansan"  # 変更でstateを自動リセット
COMMISSION_BANKER = 0.95

# bot の WS 切断 watchdog (collector の 180s より短く)
BOT_WS_STALE_SEC = "60"

# ── stdout JSON IPC (bacopy_executor_pragmatic_ws_live.py 互換) ────
# Electron GUI との通信プロトコル。各メッセージは stdout に JSONL で出力。


def send_msg(msg: dict) -> None:
    try:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(line.encode("utf-8", errors="replace"))
            buf.flush()
        else:
            sys.stdout.write(line)
            sys.stdout.flush()
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _winner_to_char(w) -> str:
    w = str(w or "").upper()
    if "PLAYER" in w:
        return "P"
    if "BANKER" in w:
        return "B"
    if "TIE" in w:
        return "T"
    return ""


def _atomic_write_json_file(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[PREPOS-HINT] write failed path={path}: {e}")
        try:
            if "tmp" in locals() and tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _publish_preposition_hint(payload: dict) -> None:
    if not payload.get("table_id"):
        return
    _atomic_write_json_file(PREPOSITION_HINT_FILE, payload)


def _is_fast_table_name(name: str) -> bool:
    n = str(name or "").lower()
    return ("speed" in n) or ("turbo" in n)


def _is_private_table_name(name: str) -> bool:
    n = str(name or "").casefold()
    return "priv" in n


def _is_unsupported_table_name(name: str) -> bool:
    n = str(name or "").casefold()
    return (
        _is_private_table_name(n)
        or "squeeze baccarat" in n
        or "bcadigitalsqz" in n
        or "seotda" in n
        or "sic bac" in n
    )


def _fmt_pattern(pkey: str) -> str:
    """'telecho|telecho|B' → 'china=telecho / big=telecho / side=B'"""
    parts = pkey.split("|")
    if len(parts) == 3:
        return f"china={parts[0]} / big={parts[1]} / side={parts[2]}"
    return pkey


def _send_preposition_legacy(
    table_label: str,
    score: int,
    side: str,
    china_pattern: str = "",
    big_pattern: str = "",
    pattern_keys: list[str] | None = None,
) -> None:
    remain = "あと1手で6パターン候補" if int(score or 0) >= 2 else "あと2手で6パターン候補"
    side_u = str(side or "").upper()
    side_name = "BANKER" if side_u == "B" else ("PLAYER" if side_u == "P" else "未確定")
    pattern_line = (
        "Patterns: " + ", ".join(pattern_keys or [])
        if pattern_keys else
        f"Pattern: china={china_pattern or '-'} / big={big_pattern or '-'} / side={side_u or '-'}"
    )
    sent = _send_telegram(
        f"予告 {table_label}\n"
        f"{remain}\n"
        f"→ {side_name} BET\n"
        f"{pattern_line}\n"
        f"GUIが事前入場します"
    )
    if sent:
        logger.info(f"[PREPOS-TELEGRAM] sent table={table_label} score={score}")


def _send_telegram(text: str) -> bool:
    # 優先順位: DUAL_LINE_* → TELEGRAM_* → ADMIN_TELEGRAM_*
    token = (
        os.getenv("DUAL_LINE_BOT_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        or os.getenv("ADMIN_TELEGRAM_BOT_TOKEN", "").strip()
    )
    chat_id = (
        os.getenv("DUAL_LINE_CHAT_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        or os.getenv("ADMIN_TELEGRAM_CHAT_ID", "").strip()
    )
    if not token or not chat_id:
        logger.warning("[TELEGRAM] send skipped: token/chat_id is not configured")
        return False
    try:
        import requests
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        if not response.ok:
            logger.warning(
                f"[TELEGRAM] send failed status={response.status_code} "
                f"response={response.text[:160]!r}"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"[TELEGRAM] send failed: {e}")
        return False


# ── BetExecutor インターフェース ─────────────────────────────────────
# DRY RUN と LIVE を統一的に扱うためのプラガブルなベット実行層。
# DRY RUN では no-op、LIVE では bacopy_executor_pragmatic_ws_live.py に
# ベットを委譲する実装に差し替える。

@runtime_checkable
class BetExecutor(Protocol):
    """ベット実行の抽象インターフェース。

    DRY RUN 用の DryRunBetExecutor と LIVE 用の実装を差し替え可能にする。
    """

    def place_bet(self, table_id: str, side: str, amount: float, metadata: dict) -> str | None:
        """ベットを発行し bet_id を返す。DRY RUN では None (ベットなし) を返す。"""
        ...

    @property
    def is_live(self) -> bool:
        """True なら実 BET を発行する。"""
        ...


class DryRunBetExecutor:
    """DRY RUN 用: ベットを発行せず、シグナル記録のみ。"""

    @property
    def is_live(self) -> bool:
        return False

    @property
    def is_bet_in_flight(self) -> bool:
        return False

    @property
    def has_pending_bet(self) -> bool:
        return False

    @property
    def is_ready(self) -> bool:
        return True

    def place_bet(self, table_id: str, side: str, amount: float, metadata: dict) -> None:
        return None

    def return_to_lobby(self) -> None:
        return None


# ── Bot 本体 ─────────────────────────────────────────────────────────

class DualLinePragmaticBot(cp.Collector):
    """Collector を継承し、ハンド観察ごとに dual_line 予想を行う bot。

    DRY RUN / LIVE 両対応:
      - bet_executor=DryRunBetExecutor() → シグナル記録のみ (デフォルト)
      - bet_executor=LiveBetExecutor(client) → 実 BET 発行
    """

    def __init__(
        self,
        *,
        headless: bool,
        raw_log: bool,
        use_v2_filter: bool = True,
        money_mode: str = "flat",
        money_unit: float = 100.0,
        profit_stop: float = 0.0,
        loss_cut: float = 0.0,
        on_limit: str = "stop",
        notify_signal: bool = True,
        notify_resolution: bool = True,
        notify_tie: bool = False,
        bet_executor: BetExecutor | None = None,
        no_vps_poll: bool = False,
    ):
        super().__init__(headless=headless, raw_log=raw_log)
        self.use_v2_filter = use_v2_filter
        self.no_vps_poll = no_vps_poll
        self.notify_signal = notify_signal
        self.notify_resolution = notify_resolution
        self.notify_tie = notify_tie
        self.bet_executor: BetExecutor = bet_executor or DryRunBetExecutor()
        self.money = BetManager(
            mode=money_mode, unit=money_unit,
            profit_stop=profit_stop, loss_cut=loss_cut,
            on_limit=on_limit,
            state_path=_PERSISTENT_DIR / "dual_line_money_state.json",
        )

        # per-table 状態
        self.last_hand_count: dict[str, int] = defaultdict(int)
        self.last_fresh_start: dict[str, bool] = defaultdict(bool)
        self.shoe_active: dict[str, bool] = defaultdict(bool)  # 完全観測中フラグ
        self.pending: dict[str, dict] = {}  # table_id -> pending prediction
        self.table_scores: dict[str, int] = defaultdict(int)  # table_id -> score (0-2)
        self._prev_table_scores: dict[str, int] = {}         # VPS予告用: 前回スコア
        self._prev_preposition_keys: dict[str, str] = {}
        self._last_prepos_notify_at: float = 0.0             # 予告通知レートリミット
        self._diag_skip_counts: dict[str, int] = defaultdict(int)


        # 累計統計
        self.total_signals = 0
        self.total_resolved = 0
        self.wins = 0
        self.losses = 0
        self.ties = 0
        self.virtual_pnl = 0.0
        self.shoe_changes: dict[str, int] = defaultdict(int)
        self.per_pattern: dict[str, dict] = defaultdict(
            lambda: {"pred": 0, "wins": 0, "losses": 0, "ties": 0, "pnl": 0.0}
        )
        self.started_at = _utc_now_iso()

        # WS 切断検知用
        self._ws_alive: bool = True
        self._ws_disconnected_at: float = 0.0
        self._game_ws_url: str = ""
        self._last_collector_ws_at: float = 0.0
        self._ws_input_seen: bool = False
        self._remote_snapshot_sig: dict[str, str] = {}
        self._last_remote_signal_log_at: float = 0.0
        self._last_remote_signal_warn_at: float = 0.0

        # collector の WS watchdog を bot 用に短縮
        os.environ["BACOPY_COLLECTOR_WS_STALE_SEC"] = BOT_WS_STALE_SEC

        self._load_state()
        mode_label = "LIVE" if self.bet_executor.is_live else "DRY RUN"
        ms = self.money.status_dict()
        logger.info(
            f"Bot 起動: mode={mode_label} money={self.money.mode} "
            f"unit=${self.money.unit} stop=${self.money.profit_stop} "
            f"cut=${self.money.loss_cut} on_limit={self.money.on_limit} "
            f"signals={self.total_signals}"
        )
        send_log(f"Bot 起動: {mode_label} {BET_MODES[self.money.mode]} unit=${self.money.unit} stop=${self.money.profit_stop} cut=${self.money.loss_cut}")
        send_phase("observing", "watching tables")
        self._send_gui_money_status()

    # ── WS フック ─────────────────────────────────────────────────

    def _on_ws(self, ws):
        """Collector._on_ws を拡張し、WS 切断検知 + executor context 渡し。"""
        super()._on_ws(ws)
        url = ws.url

        # game WS 検知 → executor 用 game_ws_url 記録
        if "pragmaticplaylive.net/game" in url:
            self._game_ws_url = url
            logger.info(f"[BOT] game WS detected: {url[-100:]}")

        # browser context を executor に渡す（初回のみ）
        if self.bet_executor.is_live:
            try:
                ws_page = getattr(ws, "page", None)
                ctx = ws_page.context if ws_page is not None else None
                already_set = bool(getattr(self.bet_executor, "_context", None))
                logger.info(f"[BOT-WS] game WS on page={getattr(ws_page,'url','?')[:60]} ctx={ctx is not None} executor_ctx_set={already_set}")
                if ctx is not None and not already_set:
                    self.bet_executor.setup(ctx, ws_page)
                    logger.info("[BOT] executor context injected (from _on_ws)")
            except Exception as e:
                logger.warning(f"[BOT] executor context injection failed: {e}")

        if cp.PRAGMATIC_WS_PATTERN not in url:
            return

        self._ws_alive = True

        def on_bot_close():
            logger.warning(f"[BOT] WS closed: {url}")
            self._ws_alive = False
            self._ws_disconnected_at = time.time()

        ws.on("close", on_bot_close)

    # ── ハンドオブザーバ ──────────────────────────────────────────

    def _collect_table_ids_from_msg(self, obj, out: set[str], depth: int = 0) -> None:
        if depth > 3:
            return
        if isinstance(obj, dict):
            tid = obj.get("tableId") or obj.get("tableid")
            if isinstance(tid, (str, int)):
                tid_s = str(tid).strip()
                if tid_s:
                    out.add(tid_s)
            for k in ("data", "payload", "message", "messages", "updates", "tables"):
                v = obj.get(k)
                if isinstance(v, (dict, list)):
                    self._collect_table_ids_from_msg(v, out, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:100]:
                if isinstance(item, (dict, list)):
                    self._collect_table_ids_from_msg(item, out, depth + 1)

    def _process_table_frame(self, table_id: str) -> None:
        buf = self.buffers.get(table_id)
        if not buf:
            return
        if self.bet_executor.is_live and not self.no_vps_poll:
            # Normal LIVE is executor-only. The VPS is the sole signal source.
            # Still consume observed results for locally confirmed bets so GUI
            # SEQ/WLT advances from the exact hand actually bet on.
            current_count = len(buf.hands or [])
            prev_count = self.last_hand_count[table_id]
            if current_count < prev_count:
                self.last_hand_count[table_id] = current_count
                return
            if current_count > prev_count:
                all_hands = buf.hands or []
                new_hands = all_hands[prev_count:current_count]
                self.last_hand_count[table_id] = current_count
                for new_hand in new_hands:
                    outcome_char = _winner_to_char(new_hand.get("winner"))
                    if not outcome_char:
                        continue
                    try:
                        self._settle_confirmed_decision_from_hand(
                            table_id, buf, new_hand, outcome_char
                        )
                    except Exception as e:
                        logger.warning(f"[DECISION] live result-only settlement failed: {e}")
            return

        # ── シュー変化検知 ──
        was_fresh = self.last_fresh_start[table_id]
        is_fresh = bool(getattr(buf, "fresh_start", False))
        current_count = len(buf.hands or [])
        prev_count = self.last_hand_count[table_id]

        shoe_changed = False
        if is_fresh and not was_fresh:
            # fresh_start の False→True 遷移 (初回 shuffle 検知)
            shoe_changed = True
        elif current_count < prev_count:
            # 手数が前回より減少 = 新シュー開始 (fresh_start が永続 True でも有効)
            shoe_changed = True

        if shoe_changed:
            was_active = self.shoe_active.get(table_id, False)
            self._on_shoe_change(table_id, buf)
            self.shoe_active[table_id] = True
            if not was_active:
                pass  # 観測開始は Telegram 通知しない（テーブル数が多すぎるため）
            prev_count = 0
        self.last_fresh_start[table_id] = is_fresh

        # 完全観測開始前 (初回 shuffle 前) はハンド処理をスキップ
        # mid-shoe の部分データでは next_n が実際の手番号とずれるため
        if not self.shoe_active.get(table_id, False):
            if self.bet_executor.is_live:
                try:
                    live_min_hands = int(os.getenv("BACOPY_LIVE_MIN_HANDS_FOR_MIDSHOE", "12") or 12)
                except Exception:
                    live_min_hands = 12
                live_min_hands = max(1, live_min_hands)
                if current_count >= live_min_hands:
                    self.shoe_active[table_id] = True
                    self.last_hand_count[table_id] = current_count
                    logger.info(
                        f"[LIVE-MIDSHOE] activate table={buf.table_name or table_id} "
                        f"hands={current_count} (no shuffle wait)"
                    )
                    self._update_table_score(table_id, buf)
                    return
            self.last_hand_count[table_id] = current_count
            return

        # ── 新規ハンド検知 ──
        if current_count <= prev_count:
            return
        all_hands = buf.hands or []
        new_hands = all_hands[prev_count:current_count]
        self.last_hand_count[table_id] = current_count

        for i, new_hand in enumerate(new_hands):
            hist_upto = all_hands[: prev_count + i + 1]
            self._on_new_hand(table_id, buf, new_hand, history_hands=hist_upto)

        # 手処理後、全テーブルのスコアを更新（LOOP でも BET でも）
        self._update_table_score(table_id, buf)

    def _diag_skip(self, reason: str, detail: str) -> None:
        n = int(self._diag_skip_counts.get(reason, 0)) + 1
        self._diag_skip_counts[reason] = n
        if n <= 20 or n % 200 == 0:
            logger.info(f"[SIGNAL-SKIP] reason={reason} count={n} {detail}")

    def _send_gui_money_status(self) -> None:
        """Push dual-line money/SEQ state using the existing GUI status protocol."""
        try:
            ms = self.money.status_dict()
            turns = ms.get("seq7_current_turns") or []
            send_msg(
                {
                    "type": "status",
                    "wins": self.wins,
                    "losses": self.losses,
                    "ties": self.ties,
                    "current_turn": ms.get("seq_turn"),
                    "overshoot": ms.get("seq_overshoot"),
                    "turns_display": "".join(turns) if isinstance(turns, list) else "",
                    "money_status": ms,
                }
            )
            send_msg(
                {
                    "type": "shoe_history",
                    "sets": ms.get("seq7_sets") or [],
                    "current_turns": turns if isinstance(turns, list) else [],
                    "chip_base": ms.get("unit"),
                }
            )
        except Exception:
            pass

    def on_ws_frame(self, payload):  # type: ignore[override]
        super().on_ws_frame(payload)
        try:
            if isinstance(payload, (dict, list)):
                msg = payload
            else:
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", errors="replace")
                msg = json.loads(payload)
        except Exception:
            return

        table_ids: set[str] = set()
        self._collect_table_ids_from_msg(msg, table_ids)
        if not table_ids:
            if isinstance(msg, dict) and msg.get("shuffle") is True:
                logger.warning(
                    f"shuffle=true but no tableId in msg: keys={list(msg.keys())[:10]}"
                )
            return
        self._last_collector_ws_at = time.time()
        self._ws_input_seen = True

        for table_id in table_ids:
            self._process_table_frame(table_id)

    def _on_shoe_change(self, table_id: str, buf):
        self._prev_table_scores.pop(table_id, None)  # 新シューで予告履歴リセット
        table_name = buf.table_name or table_id
        if table_id in self.pending:
            pending_pkey = self.pending[table_id].get("pattern_key", "?")
            logger.info(
                f"シュー変化 ({table_name}): pending {pending_pkey} クリア"
            )
            send_log(f"シュー変化: pending {pending_pkey} クリア ({table_name})")
            # pending があった場合のみ Telegram 通知（重要イベント）
            _send_telegram(f"🔄 シュー変化 (BET pending クリア)\n{table_name}\npattern: {pending_pkey}\n→ 観測リセット")
            del self.pending[table_id]
        self.shoe_changes[table_id] += 1
        self.last_hand_count[table_id] = 0
        send_phase("observing", "shoe changed")
        self._save_state()

    def _on_new_hand(self, table_id: str, buf, new_hand: dict, history_hands: list[dict] | None = None):
        outcome_char = _winner_to_char(new_hand.get("winner"))
        if not outcome_char:
            self._diag_skip("no_outcome", f"table={buf.table_name or table_id}")
            return

        # LIVE GUI mode receives VPS decisions, then confirms the actual Stake bet
        # via Pragmatic game WS.  In multi-area lobby the collector key can differ
        # from the QPID/bo_table used by the bet confirmation, so settle confirmed
        # bets by exact game_id before falling back to the older table_id pending map.
        if self.bet_executor.is_live:
            try:
                if self._settle_confirmed_decision_from_hand(table_id, buf, new_hand, outcome_char):
                    return
            except Exception as e:
                logger.warning(f"[DECISION] local settlement from hand failed: {e}")

        # 1) 前回の予想を resolve
        if table_id in self.pending:
            pending = self.pending.pop(table_id)
            self._resolve_prediction(table_id, buf, pending, outcome_char, new_hand)

        # 2) observed_sequence を構築
        seq_chars = []
        for h in (history_hands if history_hands is not None else (buf.hands or [])):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        observed_sequence = "".join(seq_chars)

        # 3) 次手の予想
        next_n = len(observed_sequence) + 1
        d = decide(observed_sequence, next_n=next_n)
        if d.action == "LOOK":
            self._diag_skip("look", f"table={buf.table_name or table_id} len={len(observed_sequence)}")
            return

        bet_side = "P" if d.action == "BET_P" else "B"
        pattern_key = f"{d.china_pattern}|{d.big_pattern}|{bet_side}"

        if self.use_v2_filter and pattern_key not in V2_PATTERNS:
            self._diag_skip("v2_filter", f"table={buf.table_name or table_id} pattern={pattern_key}")
            return

        # Private系など$1 BETやdual-line対象に合わないテーブルは対象外。
        # Speed/Turboはマルチエリアの事前入場でBET対象にする。
        if _is_unsupported_table_name(buf.table_name or table_id):
            self._diag_skip("unsupported_table", f"table={buf.table_name or table_id} pattern={pattern_key}")
            return

        # マルチロビー環境で、一度もBETSOPENを受け取っていないテーブルはスキップ
        # （例: bcadigitalsqz001 など、マルチプレイロビーに存在しないテーブル）
        # _multi_lobby_mode は起動直後から True → 起動直後のリモートスナップ誤シグナルを防ぐ
        # executor.place_bet は qpid or table_id を target として _table_states に記録するため同一キーで確認
        if self.bet_executor.is_live and getattr(self.bet_executor, "_multi_lobby_mode", False):
            _ts_map = getattr(self.bet_executor, "_table_states", {})
            _qpid = str(getattr(buf, "qpid_table_id", "") or "").strip()
            _effective_id = _qpid or table_id
            _tbl_st = (_ts_map.get(_effective_id) or _ts_map.get(table_id)) or {}
            if not float(_tbl_st.get("last_bets_open_at") or 0.0):
                self._diag_skip("no_betsopen_history", f"table={table_id}({_effective_id}) pattern={pattern_key}")
                return

        # 4) ベット発行
        # LIVE モード: 同時BETはしない（1件ずつ処理）
        if self.bet_executor.is_live:
            if self.pending:
                self._diag_skip("live_pending", f"table={buf.table_name or table_id} pattern={pattern_key}")
                return
            has_pending = False
            try:
                has_pending_fn = getattr(self.bet_executor, "has_pending_bet", None)
                has_pending = bool(has_pending_fn()) if callable(has_pending_fn) else bool(has_pending_fn)
            except Exception:
                has_pending = False
            if has_pending:
                self._diag_skip("executor_pending", f"table={buf.table_name or table_id} pattern={pattern_key}")
                return

        bet_amount = self.money.next_bet(side=bet_side)
        if bet_amount <= 0:
            reason = self.money.limit_reason
            _send_telegram(
                f"🛑 LIMIT 到達 — BET 停止\n"
                f"理由: {'利確' if reason == 'profit' else '損切'}\n"
                f"session PnL: ${self.money.session_pnl:+.2f}\n"
                f"on_limit: {self.money.on_limit}"
            )
            return
        bet_metadata = {
            "pattern_key": pattern_key,
            "china_pattern": d.china_pattern,
            "big_pattern": d.big_pattern,
            "table_name": buf.table_name or "",
            "qpid_table_id": str(getattr(buf, "qpid_table_id", "") or ""),
            "seq_at_predict": observed_sequence,
            # new_hand is the just-resolved hand used to form the six-pattern
            # signal. It is not the target betting hand.
            "source_last_game_id": str(new_hand.get("gameId") or new_hand.get("game_id") or ""),
            "signal_game_id": "",
            "signal_hand_count": len(history_hands if history_hands is not None else (buf.hands or [])),
        }
        bet_id = self.bet_executor.place_bet(
            table_id=table_id,
            side=bet_side,
            amount=bet_amount,
            metadata=bet_metadata,
        )
        decision_id = ""
        if not self.bet_executor.is_live:
            decision_id = self._publish_live_decision(table_id, buf, bet_side, bet_amount, bet_metadata) or ""
        # BET指示を出した → 事前入場タイマーをリセット（BET完了後はexecutorがlobbyに戻る）
        self._prepos_switch_at = 0.0

        # 5) pending 保存
        pending_entry: dict = {
            "side": bet_side,
            "pattern_key": pattern_key,
            "china_pattern": d.china_pattern,
            "china_pred": d.china_pred,
            "big_pattern": d.big_pattern,
            "big_pred": d.big_pred,
            "seq_at_predict": observed_sequence,
            "table_id": table_id,
            "table_name": buf.table_name or "",
            "qpid_table_id": str(getattr(buf, "qpid_table_id", "") or ""),
            "predicted_at": _utc_now_iso(),
            "predicting_n": next_n,
            "bet_amount": bet_amount,
        }
        if bet_id:
            pending_entry["bet_id"] = bet_id
        if decision_id:
            pending_entry["decision_id"] = decision_id
        self.pending[table_id] = pending_entry

        self.total_signals += 1
        send_phase("predicting", f"{bet_side} via {pattern_key}")
        send_action(f"🎯 #{self.total_signals} {pattern_key} → {bet_side} on {buf.table_name or table_id}")
        self._send_gui_money_status()
        if self.notify_signal:
            self._notify_signal(table_id, buf, pattern_key, bet_side, observed_sequence, bet_amount)
        self._save_state()

        # スコア更新: signal(=2) が確定
        self.table_scores[table_id] = 2

    def _update_table_score(self, table_id: str, buf) -> None:
        """Update six-pattern preposition state for a table."""
        seq_chars = []
        for h in (buf.hands or []):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        seq = "".join(seq_chars)
        preview = live_preposition_for_history(seq)
        score = int(preview.get("score") or 0)
        direction = str(preview.get("side") or "")
        self.table_scores[table_id] = score
        self._maybe_notify_preposition(table_id, buf, score, direction, seq, preview)

    def _maybe_notify_preposition(
        self, table_id: str, buf, score: int, direction: str, seq: str, preview: dict
    ) -> None:
        """VPS bot (non-live) がスコア上昇時に直接予告 Telegram を送信。
        GUI bot はこの通知を担当しない (重複排除)。"""
        if self.bet_executor.is_live:
            return
        if score <= 0:
            self._prev_preposition_keys.pop(table_id, None)
            return
        if _is_unsupported_table_name(str(buf.table_name or table_id)):
            return
        pattern_keys = [str(x) for x in (preview.get("pattern_keys") or []) if str(x) in V2_PATTERNS]
        if not pattern_keys:
            return
        dedup_key = f"{score}|{'/'.join(pattern_keys)}|{len(seq)}"
        if self._prev_preposition_keys.get(table_id) == dedup_key:
            return
        self._prev_preposition_keys[table_id] = dedup_key
        table_name = str(buf.table_name or table_id)
        qpid = str(getattr(buf, "qpid_table_id", "") or "").strip()
        now_iso = _utc_now_iso()
        hint = {
            "table_id": qpid or str(table_id or ""),
            "table_name": table_name,
            "qpid": qpid or str(table_id or ""),
            "source_table_id": str(table_id or ""),
            "score": int(score or 0),
            "steps_before": int(preview.get("steps_before") or (1 if int(score or 0) >= 2 else 2)),
            "direction": str(direction or "").upper(),
            "side": str(direction or "").upper(),
            "pattern_keys": pattern_keys,
            "candidates": list(preview.get("candidates") or []),
            "sequence_len": len(seq),
            "updated_at": now_iso,
            "server_updated_at": now_iso,
            "source": "dual_line_pragmatic_bot",
        }
        _publish_preposition_hint(hint)
        logger.info(f"[PREPOS-NOTIFY] score={score} table={table_name} direction={direction or '?'} patterns={pattern_keys}")
        _send_preposition_legacy(table_name, score, direction, pattern_keys=pattern_keys)

    def _select_best_table(self) -> str | None:
        """全アクティブテーブル中、最もスコアの高い table_id を返す。
        score=0 (COLD) のテーブルは選択しない。
        同スコアなら hands が多い方を優先（より観測の進んだテーブル）。
        """
        best_id: str | None = None
        best_score = 0  # 0 以下は選ばない (COLD テーブルへの無駄なスイッチを防止)
        best_hands = 0
        for tid, buf in self.buffers.items():
            if not self.shoe_active.get(tid, False):
                continue
            score = self.table_scores.get(tid, 0)
            n_hands = len(buf.hands or [])
            if score > best_score or (score == best_score and score > 0 and n_hands > best_hands):
                best_score = score
                best_hands = n_hands
                best_id = tid
        return best_id

    def _rebalance_tables(self) -> None:
        """LIVE モード時: 最適テーブルに game WS を切り替える。
        score >= 1 (WARM/HOT) のテーブルが見つかった場合のみスイッチ。
        同一テーブルへの連続リトライには 60 秒のクールダウンを設ける。
        """
        if not self.bet_executor.is_live:
            return

        best = self._select_best_table()
        if not best:
            return  # score >= 1 のテーブルなし → スイッチしない

        # pending があるテーブルは変えない（bet が終わるまで）
        if self.pending:
            return

        # すでにそのテーブルならスキップ
        if self.bet_executor.is_on_table(best):
            return

        # 同一テーブルへの連続リトライ: 60 秒クールダウン
        last_try = getattr(self, "_last_switch_attempt", {})
        now = time.time()
        if last_try.get(best, 0) > now - 60:
            return
        if not hasattr(self, "_last_switch_attempt"):
            self._last_switch_attempt = {}
        self._last_switch_attempt[best] = now

        # 切替可能なら切替
        if self.bet_executor.can_switch():
            score = self.table_scores.get(best, 0)
            score_label = {0: "COLD", 1: "WARM🟡", 2: "HOT🔴"}.get(score, str(score))
            buf = self.buffers.get(best)
            tname = (buf.table_name if buf else None) or best
            qpid = (buf.qpid_table_id if buf else None) or ""
            logger.info(
                f"[rebalance] switching game WS to table {best} name={tname!r} qpid={qpid!r} (score={score})"
            )
            send_log(f"pre-enter → {tname} qpid={qpid or '?'} (score={score_label})")
            # Telegram通知は省略（シグナル発生時に通知する）
            self.bet_executor._request_switch(best, tname, qpid)

    def _resolve_prediction(
        self, table_id: str, buf, pending: dict, outcome: str, new_hand: dict
    ):
        side = pending["side"]
        pkey = pending["pattern_key"]
        bet_id = str(pending.get("bet_id") or "").strip()

        mark_resolved = getattr(self.bet_executor, "mark_bet_resolved", None)
        if callable(mark_resolved):
            try:
                mark_resolved(bet_id=bet_id, table_id=table_id)
            except Exception:
                pass

        # LIVE: 実BET送信が確認できないシグナルは資金管理/勝敗に反映しない
        if self.bet_executor.is_live and bet_id:
            consume_sent = getattr(self.bet_executor, "consume_sent_bet", None)
            if callable(consume_sent):
                try:
                    sent = bool(consume_sent(bet_id))
                except Exception:
                    sent = False
                if not sent:
                    logger.info(
                        f"resolve skip(no-sent-bet) {buf.table_name or table_id}: "
                        f"pred={side} outcome={outcome} pattern={pkey} bet_id={bet_id}"
                    )
                    send_action(
                        f"⚪ SKIP {buf.table_name or table_id}: "
                        f"{side}→{outcome} (live bet not sent)"
                    )
                    if self.notify_resolution:
                        _send_telegram(
                            f"⚪ SKIP {buf.table_name or table_id} [LIVE]\n"
                            f"Pattern: {_fmt_pattern(pkey)}\n"
                            f"Pred: {side} → Got: {outcome}\n"
                            f"Reason: live bet was not sent"
                        )
                    self._save_state()
                    return

        self.total_resolved += 1
        pstats = self.per_pattern[pkey]
        pstats["pred"] += 1

        if outcome == "T":
            self.ties += 1
            pstats["ties"] += 1
            pnl = 0.0
            result = "TIE"
            self.money.apply_result(won=None, side=side)
        elif outcome == side:
            self.wins += 1
            pstats["wins"] += 1
            pnl = pending.get("bet_amount", 0) * (COMMISSION_BANKER if side == "B" else 1.0)
            result = "WIN"
            self.money.apply_result(won=True, side=side)
        else:
            self.losses += 1
            pstats["losses"] += 1
            pnl = -pending.get("bet_amount", 0)
            result = "LOSE"
            self.money.apply_result(won=False, side=side)

        self.virtual_pnl += pnl
        pstats["pnl"] += pnl
        bet_id_str = f" bet_id={pending.get('bet_id')}" if pending.get("bet_id") else ""
        logger.info(
            f"resolve {buf.table_name or table_id}: pred={side} outcome={outcome} "
            f"{result} pnl=${pnl:+.2f} cum=${self.virtual_pnl:+.2f} "
            f"pattern={pkey}{bet_id_str}"
        )
        decision_id = str(pending.get("decision_id") or "")
        if decision_id:
            self._post_decision_settlement(
                decision_id=decision_id,
                table_id=table_id,
                table_name=str(buf.table_name or table_id),
                pending=pending,
                outcome=outcome,
                result=result,
                pnl=pnl,
                new_hand=new_hand,
            )
            self._pending_decisions.pop(decision_id, None)
        n_nt = self.wins + self.losses
        wr = self.wins / n_nt * 100 if n_nt else 0
        ms = self.money.status_dict()
        send_msg({
            "type": "resolution",
            "table_id": table_id,
            "table_name": buf.table_name or "",
            "prediction": side,
            "outcome": outcome,
            "result": result,
            "pattern_key": pkey,
            "pnl": pnl,
            "cumulative_pnl": self.virtual_pnl,
            "wins": self.wins,
            "losses": self.losses,
            "ties": self.ties,
            "win_rate": round(wr, 1),
            "total_signals": self.total_signals,
            "total_resolved": self.total_resolved,
            "predicting_n": pending.get("predicting_n"),
            "money_status": ms,
            "bet_amount": pending.get("bet_amount", 0),
        })
        self._send_gui_money_status()
        status_icon = "✅" if result == "WIN" else ("🔵" if result == "TIE" else "❌")
        send_action(
            f"{status_icon} {result} {buf.table_name or table_id}: "
            f"{side}→{outcome} pnl=${pnl:+.2f} cum=${self.virtual_pnl:+.2f} "
            f"({self.wins}W/{self.losses}L/{self.ties}T {wr:.1f}%)"
        )

        if self.notify_resolution and (result != "TIE" or self.notify_tie):
            n_nt = self.wins + self.losses
            wr = self.wins / n_nt * 100 if n_nt else 0
            icon = "✅" if result == "WIN" else ("🔵" if result == "TIE" else "❌")
            mode_label = "[LIVE]" if self.bet_executor.is_live else "[DRY]"
            _send_telegram(
                f"{icon} {result} {buf.table_name or table_id} {mode_label}\n"
                f"Pattern: {_fmt_pattern(pkey)}\n"
                f"Pred: {side} → Got: {outcome}\n"
                f"W/L/T: {self.wins}/{self.losses}/{self.ties} ({wr:.1f}%)\n"
                f"signals: {self.total_signals} resolved: {self.total_resolved}"
            )

        # resolution 後も即座に state 保存（クラッシュ時のデータ消失防止）
        self._save_state()

    def _post_decision_settlement(
        self,
        decision_id: str,
        table_id: str,
        table_name: str,
        pending: dict,
        outcome: str,
        result: str,
        pnl: float,
        new_hand: dict,
    ) -> None:
        """Update the VPS decision row with the final settled outcome."""
        did = str(decision_id or "").strip()
        if not did:
            return
        src = self._pending_decisions.get(did) or {}
        base_url = str(src.get("source_base_url") or "").strip()
        api_key = str(src.get("source_api_key") or "").strip()
        won = result == "WIN"
        tie = result == "TIE"
        actual_amount = float(
            pending.get("actual_amount")
            or pending.get("bet_amount")
            or pending.get("amount")
            or 0.0
        )
        planned_amount = float(pending.get("bet_amount") or pending.get("amount") or actual_amount or 0.0)
        payload = {
            "mode": "dual_line_live",
            "observed_at": _utc_now_iso(),
            "provider": "pragmatic",
            "table_id": str(pending.get("qpid_table_id") or table_id or ""),
            "table_name": str(table_name or pending.get("table_name") or table_id),
            "bet": {
                "amount": actual_amount,
                "planned_amount": planned_amount,
                "side": str(pending.get("side") or ""),
                "bet_id": str(pending.get("bet_id") or ""),
            },
            "pattern_key": str(pending.get("pattern_key") or ""),
            "phase": "settled",
            "settled": True,
            "outcome": outcome,
            "result": result,
            "won": None if tie else bool(won),
            "tie": bool(tie),
            "pnl": float(pnl or 0.0),
            "hand": {
                "game_id": str(new_hand.get("gameId") or new_hand.get("game_id") or ""),
                "winner": str(new_hand.get("winner") or ""),
            },
        }
        res = self._api_post(
            f"/api/decisions/{did}/result",
            {"result": payload, "status": "done"},
            base_url=base_url,
            api_key=api_key,
        )
        if res.get("ok"):
            logger.info(f"[DECISION] result posted settled: {did} result={result} outcome={outcome}")
        else:
            logger.warning(f"[DECISION] result post settled failed: {did} result={res}")

    def _decision_matches_hand(self, pending: dict, table_id: str, buf, new_hand: dict) -> bool:
        """Return True only when a locally confirmed bet belongs to this result hand."""
        if not isinstance(pending, dict):
            return False
        confirmed = pending.get("confirmed_bet") if isinstance(pending.get("confirmed_bet"), dict) else {}
        if not confirmed:
            return False

        hand_gid = str(new_hand.get("gameId") or new_hand.get("game_id") or "").strip()
        confirmed_gid = str(
            confirmed.get("game_id")
            or confirmed.get("lpbet_game_id")
            or ""
        ).strip()
        if confirmed_gid:
            # The user's main concern is wrong-hand betting.  If the confirmed bet
            # has a game id, settlement must use that exact game id.
            if not hand_gid or hand_gid != confirmed_gid:
                return False

        hand_table_ids = {
            str(table_id or "").strip(),
            str(getattr(buf, "qpid_table_id", "") or "").strip(),
            str(getattr(buf, "table_id", "") or "").strip(),
        }
        hand_table_ids.discard("")
        confirmed_table_ids = {
            str(pending.get("table_id") or "").strip(),
            str(pending.get("qpid_table_id") or "").strip(),
            str(confirmed.get("table_id") or "").strip(),
            str(confirmed.get("confirm_table_id") or "").strip(),
        }
        detail = confirmed.get("detail") if isinstance(confirmed.get("detail"), dict) else {}
        confirmed_table_ids.add(str(detail.get("table") or "").strip())
        confirmed_table_ids.discard("")
        return bool(hand_table_ids and confirmed_table_ids and hand_table_ids.intersection(confirmed_table_ids))

    def _settle_confirmed_decision_from_hand(self, table_id: str, buf, new_hand: dict, outcome: str) -> bool:
        """Settle a locally confirmed VPS decision using the exact observed hand."""
        if not self._pending_decisions:
            return False
        matched: tuple[str, dict] | None = None
        for did, pending in list(self._pending_decisions.items()):
            if not isinstance(pending, dict):
                continue
            if pending.get("settlement_posted") or pending.get("local_bet_failed"):
                continue
            if not (
                pending.get("local_bet_sent")
                or pending.get("bet_sent_posted")
                or isinstance(pending.get("confirmed_bet"), dict)
            ):
                continue
            if self._decision_matches_hand(pending, table_id, buf, new_hand):
                matched = (did, pending)
                break
        if not matched:
            return False

        did, pending = matched
        side = str(pending.get("side") or "").upper()
        if side not in ("P", "B"):
            logger.warning(f"[DECISION] local settlement skipped invalid side: {did} side={side!r}")
            return False

        bet_id = str(pending.get("bet_id") or "")
        confirmed_info = pending.get("confirmed_bet") if isinstance(pending.get("confirmed_bet"), dict) else {}
        if bet_id and hasattr(self.bet_executor, "consume_confirmed_bet"):
            try:
                consumed = self.bet_executor.consume_confirmed_bet(bet_id)
                if isinstance(consumed, dict) and consumed:
                    confirmed_info = consumed
                    pending["confirmed_bet"] = consumed
            except Exception:
                pass
        elif bet_id and hasattr(self.bet_executor, "consume_sent_bet"):
            try:
                self.bet_executor.consume_sent_bet(bet_id)
            except Exception:
                pass

        try:
            confirmed_amount = float((confirmed_info or {}).get("confirmed_amount") or 0.0)
        except Exception:
            confirmed_amount = 0.0
        actual_amount = float(confirmed_amount or pending.get("actual_amount") or pending.get("amount") or 0.0)
        planned_amount = float(pending.get("amount") or actual_amount or 0.0)
        pending["actual_amount"] = actual_amount

        tie = outcome == "T"
        won = None if tie else (outcome == side)
        result = "TIE" if tie else ("WIN" if won else "LOSE")
        pnl_delta = (
            0.0 if tie else (
                actual_amount * COMMISSION_BANKER if won and side == "B"
                else actual_amount if won
                else -actual_amount
            )
        )

        try:
            if actual_amount > 0:
                self.money._last_bet_amount = actual_amount
        except Exception:
            pass
        self.money.apply_result(None if tie else bool(won), side=side)
        if tie:
            self.ties += 1
        elif won:
            self.wins += 1
        else:
            self.losses += 1
        self.total_resolved += 1
        self.virtual_pnl += pnl_delta

        mark_resolved = getattr(self.bet_executor, "mark_bet_resolved", None)
        if callable(mark_resolved):
            try:
                mark_resolved(bet_id=bet_id, table_id=str(pending.get("table_id") or table_id or ""))
            except Exception:
                pass

        table_name = str(pending.get("table_name") or getattr(buf, "table_name", "") or table_id)
        self._post_decision_settlement(
            decision_id=did,
            table_id=str(pending.get("table_id") or table_id),
            table_name=table_name,
            pending=pending,
            outcome=outcome,
            result=result,
            pnl=pnl_delta,
            new_hand=new_hand,
        )
        pending["settlement_posted"] = True
        self._pending_decisions.pop(did, None)
        self.pending.pop(str(pending.get("table_id") or ""), None)
        self.pending.pop(str(table_id or ""), None)

        self._save_state()
        ms = self.money.status_dict()
        icon = "✅" if result == "WIN" else ("🔵" if result == "TIE" else "❌")
        logger.info(
            f"[DECISION] local bet settled: {did} table={table_name} "
            f"game={new_hand.get('gameId') or new_hand.get('game_id') or '-'} "
            f"side={side} outcome={outcome} result={result} amount=${actual_amount:.2f}"
        )
        _send_telegram(
            f"{icon} {result}\n"
            f"{table_name}\n"
            f"Pred: {side} → Got: {outcome}\n"
            f"bet: ${actual_amount:.2f}"
            + (f" (planned ${planned_amount:.2f})" if abs(actual_amount - planned_amount) > 0.01 else "")
            + f"\npnl: ${pnl_delta:+.2f} | cum: ${self.virtual_pnl:+.2f}\n"
            f"W/L/T: {self.wins}/{self.losses}/{self.ties}\n"
            f"next: ${ms['next_bet']}"
        )
        send_msg(
            {
                "type": "round_result",
                "result": "tie" if tie else str(outcome or "").lower(),
                "won": None if tie else bool(won),
                "bet_amount": actual_amount,
                "planned_bet_amount": planned_amount,
                "bet_side": side.lower(),
                "current_turn": ms.get("seq_turn"),
                "turns_display": "".join(ms.get("seq7_current_turns") or []),
                "overshoot": ms.get("seq_overshoot"),
            }
        )
        send_msg(
            {
                "type": "resolution",
                "table_id": str(pending.get("table_id") or table_id),
                "table_name": table_name,
                "prediction": side,
                "outcome": outcome,
                "result": result,
                "pattern_key": str(pending.get("pattern_key") or ""),
                "pnl": pnl_delta,
                "cumulative_pnl": self.virtual_pnl,
                "wins": self.wins,
                "losses": self.losses,
                "ties": self.ties,
                "win_rate": round((self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) else 0, 1),
                "total_signals": self.total_signals,
                "total_resolved": self.total_resolved,
                "money_status": ms,
                "bet_amount": actual_amount,
            }
        )
        self._send_gui_money_status()
        send_action(
            f"{icon} {result} {table_name}: {side}→{outcome} "
            f"pnl=${pnl_delta:+.2f} cum=${self.virtual_pnl:+.2f}"
        )
        return True

    def _settle_confirmed_decisions_from_buffers(self) -> None:
        """Re-scan recent collector hands after a bet confirmation arrives."""
        if not self._pending_decisions:
            return
        for table_id, buf in list(getattr(self, "buffers", {}) or {}).items():
            if not self._pending_decisions:
                return
            hands = list(getattr(buf, "hands", None) or [])
            if not hands:
                continue
            for new_hand in reversed(hands[-30:]):
                if not self._pending_decisions:
                    return
                outcome_char = _winner_to_char(new_hand.get("winner"))
                if not outcome_char:
                    continue
                try:
                    self._settle_confirmed_decision_from_hand(table_id, buf, new_hand, outcome_char)
                except Exception as e:
                    logger.warning(f"[DECISION] buffered local settlement failed: {e}")

    def _notify_signal(
        self, table_id: str, buf, pattern_key: str, side: str, seq: str,
        bet_amount: float = 0.0,
    ):
        side_name = "BANKER" if side == "B" else "PLAYER"
        mode_label = "[LIVE]" if self.bet_executor.is_live else "[DRY]"
        bet_amt = bet_amount or self.money._last_bet_amount
        n_nt = self.wins + self.losses
        wr = self.wins / n_nt * 100 if n_nt else 0
        _send_telegram(
            f"🎯 v2 SIGNAL #{self.total_signals} {mode_label}\n"
            f"Table: {buf.table_name or table_id}\n"
            f"Pattern: {_fmt_pattern(pattern_key)}\n"
            f"Predict: {side} ({side_name}) | Bet: ${bet_amt:.2f}\n"
            f"W/L/T: {self.wins}/{self.losses}/{self.ties} ({wr:.1f}%) | 解決: {self.total_resolved}/{self.total_signals}"
        )

    def _publish_live_decision(
        self, table_id: str, buf, side: str, amount: float, metadata: dict
    ) -> str:
        """Publish the VPS-owned six-pattern signal for the GUI executor."""
        pattern_key = str(metadata.get("pattern_key") or "")
        if pattern_key not in V2_PATTERNS:
            return ""
        base_url = (
            os.getenv("BACOPY_SIGNAL_API_URL", "").rstrip("/")
            or os.getenv("BACOPY_REMOTE_API_URL", "").rstrip("/")
            or "https://master.bafather.uk"
        )
        api_key = (
            os.getenv("BACOPY_SIGNAL_API_KEY", "").strip()
            or os.getenv("BACOPY_REMOTE_API_KEY", "").strip()
            or os.getenv("BACOPY_API_KEY", "").strip()
            or os.getenv("LAPLACE_API_KEY", "").strip()
        )
        if not api_key:
            logger.error("[DECISION-PUBLISH] skipped: remote API key is not configured")
            return ""
        qpid = str(getattr(buf, "qpid_table_id", "") or table_id).strip()
        did = f"dl_vps_{uuid.uuid4().hex[:16]}"
        payload = {
            "decision_id": did,
            "provider": "pragmatic",
            "table_id": qpid,
            "table_name": str(buf.table_name or table_id),
            "game_id": str(metadata.get("signal_game_id") or ""),
            "captured_at": _utc_now_iso(),
            "source": "dual_line_vps_whitelist6",
            "friend_action": {
                "action": "BET",
                "side": side,
                "amount": float(amount),
                "pattern_key": pattern_key,
                "china_pattern": str(metadata.get("china_pattern") or ""),
                "big_pattern": str(metadata.get("big_pattern") or ""),
                "qpid_table_id": qpid,
                "signal_game_id": str(metadata.get("signal_game_id") or ""),
                "signal_hand_count": int(metadata.get("signal_hand_count") or 0),
                "seq_at_predict": str(metadata.get("seq_at_predict") or ""),
            },
        }
        result = self._api_post("/api/decisions", payload, base_url=base_url, api_key=api_key)
        if result.get("accepted") or result.get("ok"):
            logger.info(f"[DECISION-PUBLISH] accepted did={did} table={qpid} pattern={pattern_key}")
            return did
        else:
            logger.error(f"[DECISION-PUBLISH] failed did={did} base={base_url} result={result}")
            return ""

    # ── 状態保存/復元 ──────────────────────────────────────────────

    def _save_state(self):
        """状態をアトミックに保存（tmp ファイルに書いてから rename）。"""
        try:
            STATE_TMP.write_text(
                json.dumps(
                    {
                        "started_at": self.started_at,
                        "updated_at": _utc_now_iso(),
                        "use_v2_filter": self.use_v2_filter,
                        "money_mode": self.money.mode,
                        "money_unit": self.money.unit,
                        "live_mode": self.bet_executor.is_live,
                        "logic_version": LOGIC_VERSION,
                        "total_signals": self.total_signals,
                        "total_resolved": self.total_resolved,
                        "wins": self.wins,
                        "losses": self.losses,
                        "ties": self.ties,
                        "virtual_pnl": self.virtual_pnl,
                        "per_pattern": dict(self.per_pattern),
                        "pending": self.pending,
                        "shoe_changes": dict(self.shoe_changes),
                        "shoe_active": dict(self.shoe_active),
                        "last_fresh_start": dict(self.last_fresh_start),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            STATE_TMP.replace(STATE_PATH)
        except Exception as e:
            logger.warning(f"state save failed: {e}")

    def _load_state(self):
        if not STATE_PATH.exists():
            return
        try:
            s = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
            # ロジックバージョンが異なる場合は全カウンターをリセット
            if s.get("logic_version") != LOGIC_VERSION:
                logger.info(
                    f"[STATE] logic_version mismatch "
                    f"(saved={s.get('logic_version')!r} current={LOGIC_VERSION!r}) "
                    f"→ カウンター・パターン集計をリセット"
                )
                _send_telegram(
                    f"🔄 ロジックバージョン更新: {LOGIC_VERSION}\n"
                    f"シグナル統計・パターン集計をリセットしました"
                )
                STATE_PATH.unlink(missing_ok=True)
                return
            self.total_signals = s.get("total_signals", 0)
            self.total_resolved = s.get("total_resolved", 0)
            self.wins = s.get("wins", 0)
            self.losses = s.get("losses", 0)
            self.ties = s.get("ties", 0)
            self.virtual_pnl = float(s.get("virtual_pnl", 0.0))
            saved_pending = s.get("pending", {}) or {}
            if saved_pending:
                logger.warning(
                    f"[STATE] dropping stale pending predictions on startup: "
                    f"{len(saved_pending)} item(s)"
                )
            # Pending predictions are tied to a specific live hand/window and must
            # not survive a GUI/engine restart. Restoring them blocks preposition
            # and can advance SEQ from stale results.
            self.pending = {}
            for k, v in (s.get("per_pattern", {}) or {}).items():
                self.per_pattern[k] = v
            for k, v in (s.get("shoe_changes", {}) or {}).items():
                self.shoe_changes[k] = v
            # shoe_active は復元
            for k, v in (s.get("shoe_active", {}) or {}).items():
                self.shoe_active[k] = v
            # last_fresh_start は復元
            for k, v in (s.get("last_fresh_start", {}) or {}).items():
                self.last_fresh_start[k] = v
            # last_hand_count は復元しない（セッション依存値であり、
            # 前回起動時の値を使うと false positive シュー変化を引き起こす）
            self.last_hand_count.clear()
            logger.info(
                f"state 復元: signals={self.total_signals} pnl=${self.virtual_pnl:+.2f} "
                f"pending={len(self.pending)} shoe_active={len(self.shoe_active)}"
            )
        except Exception as e:
            logger.warning(f"state load failed: {e}")

    # ── 自動再入場 ────────────────────────────────────────────────

    def _auto_rejoin_last_table(self, page, profile_dir) -> bool:
        """再起動後、前回テーブルへ自動再入場を試みる。"""
        last_table_path = Path(profile_dir) / "last_table.json"
        if not last_table_path.exists():
            logger.info("[rejoin] last_table.json なし — 手動入場待ち")
            return False
        try:
            data = json.loads(last_table_path.read_text(encoding="utf-8"))
            table_id = str(data.get("table_id") or "").strip()
            table_name = str(data.get("table_name") or "").strip()
        except Exception as e:
            logger.warning(f"[rejoin] last_table.json 読み込み失敗: {e}")
            return False
        if not table_id:
            return False
        logger.info(f"[rejoin] 前回テーブル: {table_id} ({table_name}) — 自動クリック試行")
        _send_telegram(f"🔄 再起動: 前回テーブル {table_name or table_id} へ自動再入場試行中...")

        # 受け子モードの実績ある _join_table ロジックをそのまま再利用
        try:
            from bacopy_executor_pragmatic_ws_live import _join_table
        except Exception as e:
            logger.warning(f"[rejoin] import 失敗: {e} — フォールバック: 手動入場")
            _send_telegram(f"⚠️ 自動再入場不可\n手動でテーブルをクリックしてください:\n{table_name or table_id}")
            return False

        try:
            auto_click_wait_sec = int(os.getenv("BACOPY_AUTO_CLICK_WAIT_SEC", "90") or "90")
        except Exception:
            auto_click_wait_sec = 90
        try:
            _join_table(
                page,
                table_substr=(table_name or table_id),
                auto_click_wait_sec=auto_click_wait_sec,
                state=None,
                on_tick=None,
                is_initial=False,
                interrupt_check=None,
                qpid_table_id=table_id,
            )
            _send_telegram(f"✅ テーブル再入場クリック成功: {table_name or table_id}")
            return True
        except Exception as e:
            logger.warning(f"[rejoin] 自動再入場失敗: {e}")
            _send_telegram(f"⚠️ 自動再入場失敗\n手動でテーブルをクリックしてください:\n{table_name or table_id}")
            return True

    # ── VPS API ポーリング ────────────────────────────────────────────

    def _api_get(self, path: str, params: str = "", base_url: str = "", api_key: str = "") -> dict:
        """GET https://master.bafather.uk/api/<path>"""
        import urllib.request as _ur
        url_base = (base_url or os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk")
        url = url_base + path
        if params:
            url += "?" + params
        key = str(api_key or os.getenv("BACOPY_API_KEY", "")).strip()
        try:
            req = _ur.Request(url, headers={"Authorization": f"Bearer {key}"})
            with _ur.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if path == "/api/preposition":
                now = time.time()
                last = float(getattr(self, "_last_preposition_api_error_at", 0.0) or 0.0)
                if now - last >= 30.0:
                    logger.warning(f"[PREPOS] API GET failed base={url_base} err={e}")
                    self._last_preposition_api_error_at = now
            else:
                logger.debug(f"[API] GET {path} failed: {e}")
            return {}

    def _api_post(self, path: str, data: dict, base_url: str = "", api_key: str = "") -> dict:
        """POST https://master.bafather.uk/api/<path>"""
        import urllib.request as _ur
        url_base = (base_url or os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk")
        url = url_base + path
        key = str(api_key or os.getenv("BACOPY_API_KEY", "")).strip()
        try:
            body = json.dumps(data).encode("utf-8")
            req = _ur.Request(url, data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            }, method="POST")
            with _ur.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"[API] POST {path} failed: {e}")
            return {}

    def _decision_poll_targets(self) -> list[dict]:
        api_url = (os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk")
        key = os.getenv("BACOPY_API_KEY", "").strip()
        targets: list[dict] = []
        if api_url:
            targets.append({
                "name": "primary",
                "base_url": api_url,
                "api_key": key,
            })

        low = api_url.lower()
        is_local = ("127.0.0.1" in low) or ("localhost" in low)
        if is_local:
            remote_url = (os.getenv("BACOPY_REMOTE_API_URL", "").rstrip("/") or "https://master.bafather.uk")
            remote_key = (
                os.getenv("BACOPY_REMOTE_API_KEY", "").strip()
                or os.getenv("LAPLACE_API_KEY", "").strip()
            )
            if remote_url and remote_key and remote_url != api_url:
                targets.append({
                    "name": "remote_fallback",
                    "base_url": remote_url,
                    "api_key": remote_key,
                })
        return targets

    @staticmethod
    def _sequence_to_hands(sequence: str) -> list[dict]:
        hands: list[dict] = []
        for ch in str(sequence or ""):
            if ch == "P":
                hands.append({"winner": "PLAYER"})
            elif ch == "B":
                hands.append({"winner": "BANKER"})
            elif ch == "T":
                hands.append({"winner": "TIE"})
        return hands

    def _remote_signal_target(self) -> dict[str, str]:
        for t in self._decision_poll_targets():
            base = str(t.get("base_url") or "").rstrip("/")
            low = base.lower()
            if (not base) or ("127.0.0.1" in low) or ("localhost" in low):
                continue
            key = str(t.get("api_key") or "").strip()
            if key:
                return {
                    "name": str(t.get("name") or "remote"),
                    "base_url": base,
                    "api_key": key,
                }
        return {}

    def _poll_remote_snapshots_for_signals(self) -> None:
        if not self.bet_executor.is_live:
            return
        if not self.no_vps_poll:
            return
        enabled = str(os.getenv("BACOPY_ENABLE_REMOTE_SIGNAL_BRIDGE", "1") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not enabled:
            return

        now = time.time()
        stale_sec = float(os.getenv("BACOPY_REMOTE_SIGNAL_BRIDGE_STALE_SEC", "20") or 20)
        last_local = float(getattr(self, "_last_collector_ws_at", 0.0) or 0.0)
        if last_local > 0 and (now - last_local) <= stale_sec:
            return

        tgt = self._remote_signal_target()
        if not tgt:
            if now - self._last_remote_signal_warn_at >= 60.0:
                logger.warning("[REMOTE-SIGNAL] no remote target configured; bridge idle")
                self._last_remote_signal_warn_at = now
            return

        data = self._api_get(
            "/api/snapshots",
            "provider=pragmatic",
            base_url=str(tgt.get("base_url") or ""),
            api_key=str(tgt.get("api_key") or ""),
        )
        snaps = ((data.get("snapshots") or {}).get("pragmatic") or {})
        if not isinstance(snaps, dict):
            return

        changed = 0
        for table_id_raw, snap in snaps.items():
            table_id = str(table_id_raw or "").strip()
            if not table_id or not isinstance(snap, dict):
                continue

            seq = str(snap.get("sequence") or "")
            if not seq:
                continue
            last_hand = snap.get("last_hand") if isinstance(snap.get("last_hand"), dict) else {}
            gid = str((last_hand or {}).get("gameId") or "")
            sig = gid if gid else seq
            if self._remote_snapshot_sig.get(table_id) == sig:
                continue
            self._remote_snapshot_sig[table_id] = sig

            hands = self._sequence_to_hands(seq)
            if not hands:
                continue

            buf = self.buffers.get(table_id)
            if not buf:
                buf = cp.ShoeBuffer(table_id)
                self.buffers[table_id] = buf

            tname = str(snap.get("table_name") or "").strip()
            if tname:
                buf.table_name = tname
            ttype = snap.get("table_type")
            if ttype:
                buf.table_type = str(ttype)
            qpid = str(snap.get("qpid_table_id") or "").strip()
            if qpid:
                buf.qpid_table_id = qpid
            table_image = str(snap.get("table_image") or "").strip()
            if table_image:
                buf.table_image = table_image
            buf.fresh_start = bool(snap.get("fresh_start"))
            buf.hands = hands
            self._process_table_frame(table_id)
            changed += 1

        if changed and (now - self._last_remote_signal_log_at >= 30.0):
            stale_age = (now - last_local) if last_local > 0 else 9e9
            logger.info(
                f"[REMOTE-SIGNAL] applied={changed} source={tgt.get('name') or 'remote'} "
                f"collector_stale={stale_age:.0f}s"
            )
            self._last_remote_signal_log_at = now

    def _check_preposition(self) -> None:
        """VPS の事前入場指示をポーリング。score=1 遷移時に対象テーブルへ事前入場して待機。"""
        now_ts = time.time()
        if not self.bet_executor.is_live:
            last = float(getattr(self, "_last_preposition_skip_log_at", 0.0) or 0.0)
            if now_ts - last >= 30.0:
                logger.info("[PREPOS] skip: executor is not live")
                self._last_preposition_skip_log_at = now_ts
            return
        try:
            # has_pending_bet also includes queued/preposition switch requests in the
            # live executor.  Preposition should only be blocked by an actual BET
            # reservation; queued preposition switches are deduped by _request_switch.
            has_pb = bool(getattr(self.bet_executor, "_pending_bet", None))
        except Exception:
            has_pb = False
        try:
            bif_fn = getattr(self.bet_executor, "is_bet_in_flight", None)
            is_bif = bool(bif_fn()) if callable(bif_fn) else bool(bif_fn)
        except Exception:
            is_bif = False
        if has_pb or is_bif:
            last = float(getattr(self, "_last_preposition_skip_log_at", 0.0) or 0.0)
            if now_ts - last >= 15.0:
                logger.info(f"[PREPOS] skip: pending_bet={has_pb} is_bet_in_flight={is_bif}")
                self._last_preposition_skip_log_at = now_ts
            return

        targets = self._decision_poll_targets()
        if not targets:
            targets = [{"name": "default", "base_url": "", "api_key": ""}]

        data: dict = {}
        src_name = "default"
        saw_stale_candidate = False
        for t in targets:
            base = str(t.get("base_url") or "").rstrip("/")
            key = str(t.get("api_key") or "").strip()
            candidate = self._api_get("/api/preposition", base_url=base, api_key=key)
            if not isinstance(candidate, dict):
                continue
            if candidate.get("ok") is False and candidate.get("error"):
                last = float(getattr(self, "_last_preposition_api_reject_log_at", 0.0) or 0.0)
                reject_key = f"{base}|{candidate.get('error')}"
                if (
                    reject_key != str(getattr(self, "_last_preposition_api_reject_key", "") or "")
                    or now_ts - last >= 60.0
                ):
                    logger.warning(
                        f"[PREPOS] API rejected source={t.get('name') or base or 'default'} "
                        f"base={base or 'local'} error={candidate.get('error')}"
                    )
                    self._last_preposition_api_reject_key = reject_key
                    self._last_preposition_api_reject_log_at = now_ts
            table_id_c = str(candidate.get("table_id") or "").strip()
            if not table_id_c:
                continue
            updated_c = str(candidate.get("updated_at") or candidate.get("server_updated_at") or "")
            if updated_c:
                try:
                    import datetime as _dt
                    updated_at = _dt.datetime.fromisoformat(updated_c.replace("Z", "+00:00"))
                    now_utc = _dt.datetime.now(_dt.timezone.utc)
                    age_sec = (now_utc - updated_at).total_seconds()
                    if age_sec > 120:
                        saw_stale_candidate = True
                        now_ts = time.time()
                        stale_key = f"{base}|{table_id_c}|{updated_c}"
                        last_stale_key = str(getattr(self, "_last_preposition_stale_key", "") or "")
                        last_stale_log = float(getattr(self, "_last_preposition_stale_log_at", 0.0) or 0.0)
                        if stale_key != last_stale_key or now_ts - last_stale_log >= 60.0:
                            logger.warning(
                                f"[PREPOS] stale preposition ignored source={t.get('name') or base or 'default'} "
                                f"table={table_id_c} age={age_sec:.0f}s updated_at={updated_c}"
                            )
                            self._last_preposition_stale_key = stale_key
                            self._last_preposition_stale_log_at = now_ts
                        continue
                except Exception:
                    pass
            candidate_patterns = [
                str(x) for x in (candidate.get("pattern_keys") or []) if str(x)
            ]
            candidate_name = str(candidate.get("table_name") or "")
            unsupported_name = f"{candidate_name} {table_id_c}".casefold()
            reject_reason = ""
            if self.use_v2_filter and not any(
                pattern_key in V2_PATTERNS for pattern_key in candidate_patterns
            ):
                reject_reason = "non-whitelist-pattern"
            elif _is_unsupported_table_name(unsupported_name):
                reject_reason = "unsupported-multi-table"
            if reject_reason:
                now_ts = time.time()
                reject_key = (
                    f"{base}|{table_id_c}|{reject_reason}|"
                    f"{'/'.join(sorted(candidate_patterns)) or '-'}"
                )
                if (
                    reject_key
                    != str(getattr(self, "_last_preposition_reject_key", "") or "")
                    or now_ts
                    - float(getattr(self, "_last_preposition_reject_log_at", 0.0) or 0.0)
                    >= 60.0
                ):
                    logger.warning(
                        f"[PREPOS] rejected candidate reason={reject_reason} "
                        f"source={t.get('name') or base or 'default'} "
                        f"table={candidate_name or table_id_c} patterns={candidate_patterns or ['-']}"
                    )
                    self._last_preposition_reject_key = reject_key
                    self._last_preposition_reject_log_at = now_ts
                continue
            data = candidate
            src_name = str(t.get("name") or base or "default")
            break

        table_id = str(data.get("table_id") or "").strip()
        if not table_id:
            # Permit the same candidate again only after the API reports that
            # the previous forecast is no longer active.
            if not saw_stale_candidate:
                self._last_preposition_key = ""
            last = float(getattr(self, "_last_preposition_empty_log_at", 0.0) or 0.0)
            if now_ts - last >= 30.0:
                logger.info(f"[PREPOS] no active candidate from {len(targets)} target(s)")
                self._last_preposition_empty_log_at = now_ts
            if self.no_vps_poll:
                self._check_preposition_local_fallback()
            return
        table_name = str(data.get("table_name") or "")
        score = int(data.get("score") or 1)
        qpid = str(data.get("qpid") or table_id)
        updated_key = str(data.get("updated_at") or data.get("server_updated_at") or "")
        logger.info(
            f"[PREPOS] candidate source={src_name} table={table_name or table_id} "
            f"qpid={qpid or '-'} score={score} direction={str(data.get('direction') or data.get('side') or '?')} "
            f"updated_at={updated_key or '-'}"
        )
        if _is_unsupported_table_name(f"{table_name} {qpid} {table_id}"):
            logger.info(f"[PREPOS] ignored unsupported table: {table_name or table_id}")
            return
        direction = str(data.get("direction") or "").upper()
        pattern_keys = sorted(str(x) for x in (data.get("pattern_keys") or []) if str(x))
        china_pattern = str(data.get("china_pattern") or "").strip()
        big_pattern = str(data.get("big_pattern") or "").strip()
        if direction not in ("P", "B"):
            if "BANKER" in direction:
                direction = "B"
            elif "PLAYER" in direction:
                direction = "P"
            else:
                direction = ""
        current_table = str(getattr(self.bet_executor, "_table_id", "") or "")
        if current_table and (current_table == qpid or current_table == table_id):
            logger.info(f"[PREPOS] already at table {table_id}, skip")
            return
        current_key = f"{qpid or table_id}|{score}|{direction}|{'/'.join(pattern_keys)}|{src_name}|{updated_key}"
        if current_key == getattr(self, "_last_preposition_key", ""):
            now_retry = time.time()
            target = str(qpid or table_id or "").strip()
            prepared = str(getattr(self.bet_executor, "_prepared_table_id", "") or "")
            switch_req = getattr(self.bet_executor, "_switch_request", None) or {}
            req_target = str(switch_req.get("qpid") or switch_req.get("table_id") or "").strip()
            req_age = now_retry - float(switch_req.get("requested_at") or 0.0) if switch_req else 999.0
            last_req_age = now_retry - float(getattr(self, "_prepos_switch_at", 0.0) or 0.0)
            if prepared == target or (req_target == target and req_age < 8.0) or last_req_age < 8.0:
                logger.info(f"[PREPOS] dedup skip (same key): {current_key[:80]}")
                return
            logger.info(
                f"[PREPOS] retry same key after focus miss: target={target or '-'} "
                f"last_req_age={last_req_age:.1f}s prepared={prepared or '-'}"
            )
        self._last_preposition_key = current_key
        logger.info(
            f"[PREPOS] requesting switch → {table_name!r} qpid={qpid!r} score={score} "
            f"direction={direction or '?'} source={src_name}"
        )
        preselect_amount = float(self.money.next_bet() or 0.0)
        steps_before = int(data.get("steps_before") or max(0, 3 - score))
        try:
            self.bet_executor._request_switch(
                table_id,
                table_name,
                qpid,
                intent="preposition",
                side=direction,
                preselect_amount=preselect_amount,
                preposition_score=score,
                steps_before=steps_before,
            )
        except TypeError:
            self.bet_executor._request_switch(table_id, table_name, qpid)
        self._prepos_switch_at = time.time()

    def _check_preposition_local_fallback(self) -> None:
        """API preposition が使えない場合のローカル fallback（table score ベース）。"""
        best = self._select_best_table()
        if not best:
            return
        score = int(self.table_scores.get(best, 0) or 0)
        if score <= 0:
            return
        buf = self.buffers.get(best)
        if not buf:
            return
        table_id = str(best or "").strip()
        table_name = str(buf.table_name or table_id)
        qpid = str(getattr(buf, "qpid_table_id", "") or table_id)
        current_table = str(getattr(self.bet_executor, "_table_id", "") or "")
        if current_table and (current_table == qpid or current_table == table_id):
            return

        seq_chars = []
        for h in (buf.hands or []):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        seq = "".join(seq_chars)
        next_n = len(seq) + 1
        _china_pred, china_pattern = chinese_road_predict(seq, next_n)
        _big_pred, big_pattern = big_road_predict(seq)
        _score, direction = score_proximity(seq, next_n)
        hand_count = len(buf.hands or [])

        current_key = f"local|{table_id}|{score}|{direction}|{hand_count}"
        if current_key == getattr(self, "_last_preposition_key", ""):
            return
        self._last_preposition_key = current_key

        logger.info(
            f"[PREPOS-LOCAL] requesting switch → {table_name!r} qpid={qpid!r} "
            f"score={score} direction={direction or '?'}"
        )
        preselect_amount = float(self.money.next_bet() or 0.0)
        try:
            self.bet_executor._request_switch(
                table_id,
                table_name,
                qpid,
                intent="preposition",
                side=direction,
                preselect_amount=preselect_amount,
                preposition_score=score,
                steps_before=max(0, 3 - score),
            )
        except TypeError:
            self.bet_executor._request_switch(table_id, table_name, qpid)
        self._prepos_switch_at = time.time()

    def _handle_decision(self, decision: dict) -> None:
        """VPS からの BET decision を受け取り、executor 経由で BET 実行。"""
        if not self.bet_executor.is_live:
            return
        did = str(decision.get("decision_id") or "")
        fa = decision.get("friend_action") or {}
        if not isinstance(fa, dict):
            return
        side = str(fa.get("side") or "").upper()
        if side not in ("P", "B"):
            logger.warning(f"[DECISION] invalid side={side!r} in {did}")
            return
        pattern_key = str(fa.get("pattern_key") or decision.get("pattern_key") or "")
        if self.use_v2_filter and pattern_key not in V2_PATTERNS:
            logger.warning(f"[DECISION] rejected non-whitelist signal: did={did[:12]} pattern={pattern_key or '-'}")
            return
        captured_at = str(decision.get("captured_at") or "").strip()
        if captured_at:
            try:
                captured_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                decision_age = (datetime.now(timezone.utc) - captured_dt).total_seconds()
                max_age = float(os.getenv("BACOPY_MAX_DECISION_AGE_SEC", "20") or 20)
                if decision_age > max_age:
                    logger.warning(
                        f"[DECISION] SKIP stale: did={did[:12]} age={decision_age:.1f}s max={max_age:.1f}s"
                    )
                    self._api_post(
                        f"/api/decisions/{did}/ack",
                        {
                            "ack": {
                                "executor_id": "gui-1",
                                "skipped_at": _utc_now_iso(),
                                "reason": "stale_before_execution",
                            },
                            "status": "skipped_stale",
                        },
                        base_url=str(decision.get("_source_api_base") or ""),
                        api_key=str(decision.get("_source_api_key") or ""),
                    )
                    return
            except Exception as ex:
                logger.warning(f"[DECISION] captured_at parse failed for {did[:12]}: {ex}")
        table_id = str(decision.get("table_id") or "")
        table_name = str(decision.get("table_name") or "")
        if _is_unsupported_table_name(f"{table_name} {table_id}"):
            logger.warning(f"[DECISION] SKIP unsupported table: did={did[:12]} table={table_name or table_id}")
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "skipped_at": _utc_now_iso(),
                        "reason": "unsupported_table",
                    },
                    "status": "skipped_unsupported_table",
                },
                base_url=str(decision.get("_source_api_base") or ""),
                api_key=str(decision.get("_source_api_key") or ""),
            )
            return

        ex = self.bet_executor
        if (
            getattr(ex, "_multi_lobby_mode", False)
            and getattr(ex, "_multi_diagnostic_only", False)
        ):
            logger.info(
                f"[DECISION-DIAG] captured without BET: did={did} side={side} "
                f"table={table_name!r} tid={table_id!r}"
            )
            try:
                ex._request_switch(table_id, table_name, table_id, intent="preposition", side=side)
            except Exception as diag_ex:
                logger.warning(f"[DECISION-DIAG] probe request failed: {diag_ex}")
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "skipped_at": _utc_now_iso(),
                        "reason": "multi_diagnostic_only",
                    },
                    "status": "skipped_diagnostic",
                },
                base_url=str(decision.get("_source_api_base") or ""),
                api_key=str(decision.get("_source_api_key") or ""),
            )
            _send_telegram(
                f"🔎 診断モード: BET未送信\n{table_name}\n"
                f"Side: {side} $0.00\n対象卓とボタンの対応を採取中"
            )
            return

        try:
            bif_fn = getattr(ex, "is_bet_in_flight", None)
            is_bif = bool(bif_fn()) if callable(bif_fn) else bool(bif_fn)
        except Exception:
            is_bif = False
        pending_bet = getattr(ex, "_pending_bet", None)
        switch_request = getattr(ex, "_switch_request", None)
        switch_in_progress = bool(getattr(ex, "_switch_in_progress", False))
        ex_phase = getattr(ex, "_phase", "?")
        ex_table = getattr(ex, "_table_id", "?")
        known_tables = list(getattr(ex, "_table_states", {}).keys())[:6]
        logger.info(f"[DECISION] {did} side={side} table={table_name!r} tid={table_id!r}")
        logger.info(
            f"[DECISION] executor state: phase={ex_phase} table={ex_table!r} "
            f"is_bif={is_bif} pending={bool(pending_bet)} "
            f"switching={switch_in_progress} switch_request={bool(switch_request)} "
            f"known_tables={known_tables}"
        )

        if (
            getattr(ex, "_multi_lobby_mode", False)
            and str(getattr(ex, "_multi_bet_transport", "") or "") == "click"
            and not bool(getattr(ex, "_is_multi_table_ws", False))
        ):
            logger.info(
                f"[DECISION] WAIT: multi-table WS not ready yet; keep decision pending for retry {did}"
            )
            self._last_wait_decision_id = did
            return

        if is_bif:
            logger.info(f"[DECISION] SKIP: bet in flight for {did}")
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "skipped_at": _utc_now_iso(),
                        "reason": "executor_busy_bet_in_flight",
                    },
                    "status": "skipped_busy",
                },
                base_url=str(decision.get("_source_api_base") or ""),
                api_key=str(decision.get("_source_api_key") or ""),
            )
            return
        if switch_request:
            req_intent = str((switch_request or {}).get("intent") or "").strip().lower()
            req_target = str((switch_request or {}).get("qpid") or (switch_request or {}).get("table_id") or "").strip()
            if req_intent == "preposition":
                if req_target and req_target != table_id:
                    antenna_mismatch_ok = False
                    try:
                        antenna_fn = getattr(ex, "is_table_in_antenna_zone", None)
                        if callable(antenna_fn):
                            antenna_mismatch_ok = bool(antenna_fn(table_id, side))
                    except Exception:
                        antenna_mismatch_ok = False
                    if antenna_mismatch_ok:
                        logger.info(
                            f"[DECISION] preposition mismatch accepted by antenna zone for {did}: "
                            f"req={req_target} decision={table_id}"
                        )
                    else:
                        logger.info(
                            f"[DECISION] override queued preposition for live signal {did}: "
                            f"req={req_target} decision={table_id}"
                        )
                logger.info(
                    f"[DECISION] using queued preposition for BET decision {did}: "
                    f"target={req_target or '-'}"
                )
            else:
                logger.info(f"[DECISION] WAIT: executor has switch_request for {did}")
                self._last_wait_decision_id = did
                return
        if switch_in_progress:
            active_req = getattr(ex, "_active_switch_request", None) or {}
            active_intent = str((active_req or {}).get("intent") or "").strip().lower()
            active_target = str((active_req or {}).get("qpid") or (active_req or {}).get("table_id") or "").strip()
            if active_intent == "preposition":
                logger.info(
                    f"[DECISION] live signal preempts active preposition: "
                    f"did={did} active={active_target or '-'} decision={table_id}"
                )
            else:
                logger.info(f"[DECISION] WAIT: executor switching/preparing; keep decision pending for retry {did}")
                self._last_wait_decision_id = did
                return
        if isinstance(pending_bet, dict) and pending_bet:
            p_age = time.time() - float(pending_bet.get("queued_at") or time.time())
            pending_tid = str(pending_bet.get("table_id") or "")
            max_pending_age = float(os.getenv("BACOPY_PENDING_BET_MAX_SEC", "120") or 120)
            mismatch_grace = float(os.getenv("BACOPY_PENDING_BET_MISMATCH_MAX_SEC", "15") or 15)
            if pending_tid and table_id and pending_tid != table_id and p_age > mismatch_grace:
                logger.warning(
                    f"[DECISION] clearing mismatched pending bet "
                    f"(pending_table={pending_tid} incoming_table={table_id} age={p_age:.1f}s)"
                )
                try:
                    setattr(ex, "_pending_bet", None)
                except Exception:
                    pass
            elif p_age > max_pending_age:
                logger.warning(f"[DECISION] clearing stale pending bet (age={p_age:.1f}s) before {did}")
                try:
                    setattr(ex, "_pending_bet", None)
                except Exception:
                    pass
            else:
                if did != getattr(self, "_last_pending_skip_did", ""):
                    logger.info(f"[DECISION] SKIP: pending bet exists (age={p_age:.1f}s) for {did}")
                    self._last_pending_skip_did = did
                self._api_post(
                    f"/api/decisions/{did}/ack",
                    {
                        "ack": {
                            "executor_id": "gui-1",
                            "skipped_at": _utc_now_iso(),
                            "reason": "executor_busy_pending_bet",
                        },
                        "status": "skipped_busy",
                    },
                    base_url=str(decision.get("_source_api_base") or ""),
                    api_key=str(decision.get("_source_api_key") or ""),
                )
                return

        if getattr(ex, "_multi_lobby_mode", False) and str(getattr(ex, "_multi_bet_transport", "") or "") == "click":
            prepared = str(getattr(ex, "_prepared_table_id", "") or "").strip()
            table_states = getattr(ex, "_table_states", {}) or {}
            st = table_states.get(table_id) or {}
            open_gid = str((st or {}).get("bets_open_game_id") or "")
            closed_gid = str((st or {}).get("bets_closed_game_id") or "")
            last_open_at = float((st or {}).get("last_bets_open_at") or 0.0)
            open_age = time.time() - last_open_at if last_open_at else 9999.0
            window_max = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
            active_window = bool(open_gid and open_gid != closed_gid and open_age < window_max)
            antenna_ok = False
            try:
                antenna_fn = getattr(ex, "is_table_in_antenna_zone", None)
                if callable(antenna_fn):
                    antenna_ok = bool(antenna_fn(table_id, side))
            except Exception as ant_ex:
                logger.debug(f"[DECISION] antenna check failed: {ant_ex}")
            if prepared != table_id and not active_window and not antenna_ok:
                signal_game_id_preview = str(
                    decision.get("game_id")
                    or fa.get("signal_game_id")
                    or fa.get("game_id")
                    or ""
                )
                signal_guard_preview = signal_game_id_preview or captured_at
                if signal_guard_preview:
                    logger.warning(
                        f"[DECISION] target not prepared; queue guarded live signal: did={did[:12]} "
                        f"table={table_id} prepared={prepared or '-'} "
                        f"open_gid={open_gid or '-'} open_age={open_age:.1f}s "
                        f"signal_guard={signal_guard_preview}"
                    )
                else:
                    logger.warning(
                        f"[DECISION] SKIP not prepared without signal guard: did={did[:12]} "
                        f"table={table_id} prepared={prepared or '-'} "
                        f"open_gid={open_gid or '-'} open_age={open_age:.1f}s"
                    )
                    self._api_post(
                        f"/api/decisions/{did}/ack",
                        {
                            "ack": {
                                "executor_id": "gui-1",
                                "skipped_at": _utc_now_iso(),
                                "reason": "target_not_prepared_for_signal",
                                "prepared_table_id": prepared,
                                "open_age_sec": round(open_age, 2),
                            },
                            "status": "skipped_not_prepared",
                        },
                        base_url=str(decision.get("_source_api_base") or ""),
                        api_key=str(decision.get("_source_api_key") or ""),
                    )
                    return
            if antenna_ok and prepared != table_id:
                logger.info(
                    f"[DECISION] antenna-zone accepted: did={did[:12]} "
                    f"table={table_id} prepared={prepared or '-'}"
                )

        bet_amount = self.money.next_bet()
        logger.info(f"[BOT] decision received: {did} side={side} table={table_name} amount=${bet_amount}")

        metadata = {
            "table_name": table_name,
            "qpid_table_id": table_id,
            "pattern_key": pattern_key,
            "decision_id": did,
            "captured_at": captured_at,
            "signal_game_id": str(
                decision.get("game_id")
                or fa.get("signal_game_id")
                or fa.get("game_id")
                or ""
            ),
            "signal_hand_count": int(fa.get("signal_hand_count") or 0),
            "seq_at_predict": str(fa.get("seq_at_predict") or ""),
            "antenna_ok": bool(locals().get("antenna_ok", False)),
        }
        bet_id = self.bet_executor.place_bet(table_id, side, bet_amount, metadata)

        self._pending_decisions[did] = {
            "side": side, "amount": bet_amount,
            "table_id": table_id, "table_name": table_name,
            "pattern_key": pattern_key,
            "bet_id": str(bet_id or ""), "placed_at": time.time(),
            "result_posted": False,
            "settlement_posted": False,
            "bet_sent_posted": False,
            "local_bet_sent": False,
            "local_bet_failed": False,
            "bet_sent_notified": False,
            "source_base_url": str(decision.get("_source_api_base") or ""),
            "source_api_key": str(decision.get("_source_api_key") or ""),
        }
        self.pending[table_id] = {
            "side": side,
            "pattern_key": pattern_key,
            "china_pattern": "",
            "china_pred": "",
            "big_pattern": "",
            "big_pred": "",
            "seq_at_predict": "",
            "table_id": table_id,
            "table_name": table_name,
            "predicted_at": _utc_now_iso(),
            "predicting_n": None,
            "bet_amount": bet_amount,
            "bet_id": str(bet_id or ""),
            "decision_id": did,
        }
        self.total_signals += 1
        send_phase("predicting", f"{side} via VPS")
        send_action(f"🎯 #{self.total_signals} {pattern_key or 'VPS'} → {side} on {table_name or table_id}")
        self._send_gui_money_status()

        # ACK
        source_base = str(decision.get("_source_api_base") or "")
        source_key = str(decision.get("_source_api_key") or "")
        self._api_post(f"/api/decisions/{did}/ack", {
            "ack": {"executor_id": "gui-1", "placed_at": _utc_now_iso()},
            "status": "processing",
        }, base_url=source_base, api_key=source_key)
        _send_telegram(
            f"🎯 BET予約\n{table_name}\nSide: {side} ${bet_amount:.2f}\nstatus: 送信待機\ndecision: {did[:12]}"
        )

    def _flush_pending_decision_results(self) -> None:
        """送信済み/失敗を API に反映して processing 滞留を防ぐ。"""
        if not self._pending_decisions:
            return
        post_timeout = float(os.getenv("BACOPY_DECISION_POST_TIMEOUT_SEC", "180") or 180)
        settlement_timeout = float(os.getenv("BACOPY_DECISION_SETTLEMENT_TIMEOUT_SEC", "900") or 900)
        now = time.time()
        confirmed_changed = False
        for did, p in list(self._pending_decisions.items()):
            if not isinstance(p, dict) or p.get("settlement_posted"):
                continue
            bet_id = str(p.get("bet_id") or "")
            placed_at = float(p.get("placed_at") or now)
            age = max(0.0, now - placed_at)
            sent = False
            confirmed_info = None
            try:
                if bet_id and hasattr(self.bet_executor, "get_confirmed_bet"):
                    confirmed_info = self.bet_executor.get_confirmed_bet(bet_id)
                    sent = bool(confirmed_info)
                elif bet_id and hasattr(self.bet_executor, "has_sent_bet"):
                    sent = bool(self.bet_executor.has_sent_bet(bet_id))
            except Exception:
                sent = False
                confirmed_info = None
            if not sent and (
                p.get("local_bet_sent")
                or p.get("bet_sent_posted")
                or isinstance(p.get("confirmed_bet"), dict) and bool(p.get("confirmed_bet"))
            ):
                # Once Stake/Pragmatic has confirmed the bet, do not downgrade the
                # decision to bet_send_timeout just because the executor cache was
                # later consumed or expired.  The remaining wait is settlement only.
                sent = True
                if not confirmed_info and isinstance(p.get("confirmed_bet"), dict):
                    confirmed_info = p.get("confirmed_bet") or {}

            failed_info = None
            try:
                if bet_id and hasattr(self.bet_executor, "consume_failed_bet"):
                    failed_info = self.bet_executor.consume_failed_bet(bet_id)
            except Exception:
                failed_info = None
            if isinstance(failed_info, dict) and failed_info:
                reason = str(failed_info.get("reason") or "bet_failed")
                phase = str(failed_info.get("phase") or "bet_failed")
                res = self._api_post(
                    f"/api/decisions/{did}/result",
                    {
                        "result": {
                            "error": reason,
                            "phase": phase,
                            "age_sec": round(age, 2),
                            "table_id": str(p.get("table_id") or failed_info.get("table_id") or ""),
                            "table_name": str(p.get("table_name") or ""),
                            "bet": {
                                "amount": float(p.get("amount") or failed_info.get("amount") or 0.0),
                                "side": str(p.get("side") or failed_info.get("side") or ""),
                                "bet_id": bet_id,
                            },
                            "detail": failed_info,
                        },
                        "status": "error",
                    },
                    base_url=str(p.get("source_base_url") or ""),
                    api_key=str(p.get("source_api_key") or ""),
                )
                if res.get("ok"):
                    p["result_posted"] = True
                    p["settlement_posted"] = True
                    p["local_bet_failed"] = True
                    logger.warning(f"[DECISION] result posted error({reason}): {did}")
                continue

            if sent:
                p["local_bet_sent"] = True
                p["confirmed_bet"] = confirmed_info or {}
                if confirmed_info:
                    confirmed_changed = True
                try:
                    confirmed_amount = float((confirmed_info or {}).get("confirmed_amount") or 0.0)
                except Exception:
                    confirmed_amount = 0.0
                if confirmed_amount > 0:
                    planned_amount = float(p.get("amount") or 0.0)
                    p["actual_amount"] = confirmed_amount
                    if abs(confirmed_amount - planned_amount) > 0.01:
                        logger.warning(
                            f"[DECISION] partial local bet amount: {did} "
                            f"planned=${planned_amount:.2f} actual=${confirmed_amount:.2f}"
                        )
                if p.get("bet_sent_posted"):
                    if age > settlement_timeout:
                        res = self._api_post(
                            f"/api/decisions/{did}/result",
                            {
                                "result": {
                                    "error": "settlement_timeout",
                                    "phase": "settlement_timeout",
                                    "age_sec": round(age, 2),
                                    "table_id": str(p.get("table_id") or ""),
                                    "table_name": str(p.get("table_name") or ""),
                                    "bet": {
                                        "amount": float(p.get("amount") or 0.0),
                                        "side": str(p.get("side") or ""),
                                        "bet_id": bet_id,
                                    },
                                    "bet_confirm": confirmed_info or p.get("confirmed_bet") or {},
                                },
                                "status": "error",
                            },
                            base_url=str(p.get("source_base_url") or ""),
                            api_key=str(p.get("source_api_key") or ""),
                        )
                        if res.get("ok"):
                            p["result_posted"] = True
                            p["settlement_posted"] = True
                            logger.warning(
                                f"[DECISION] result posted error(settlement_timeout): {did} age={age:.1f}s"
                            )
                    continue
                result_payload = {
                    "mode": "dual_line_live",
                    "observed_at": _utc_now_iso(),
                    "provider": "pragmatic",
                    "table_id": str(p.get("table_id") or ""),
                    "table_name": str(p.get("table_name") or ""),
                    "bet": {
                        "amount": float(p.get("actual_amount") or p.get("amount") or 0.0),
                        "planned_amount": float(p.get("amount") or 0.0),
                        "side": str(p.get("side") or ""),
                        "bet_id": bet_id,
                    },
                    "bet_confirm": {
                        "type": str((confirmed_info or {}).get("confirm_type") or "confirmed_bet"),
                        "age_sec": round(age, 2),
                        "detail": confirmed_info or {},
                    },
                    "phase": "bet_sent",
                    "settled": False,
                    "outcome": "pending",
                }
                res = self._api_post(
                    f"/api/decisions/{did}/result",
                    {"result": result_payload, "status": "processing"},
                    base_url=str(p.get("source_base_url") or ""),
                    api_key=str(p.get("source_api_key") or ""),
                )
                if res.get("ok"):
                    p["bet_sent_posted"] = True
                    logger.info(f"[DECISION] result posted processing(phase=bet_sent): {did}")
                continue

            if age > post_timeout:
                res = self._api_post(
                    f"/api/decisions/{did}/result",
                    {
                        "result": {
                            "error": "bet_send_timeout",
                            "phase": "bet_pending_timeout",
                            "age_sec": round(age, 2),
                            "table_id": str(p.get("table_id") or ""),
                            "table_name": str(p.get("table_name") or ""),
                        },
                        "status": "error",
                    },
                    base_url=str(p.get("source_base_url") or ""),
                    api_key=str(p.get("source_api_key") or ""),
                )
                if res.get("ok"):
                    p["result_posted"] = True
                    p["settlement_posted"] = True
                    p["local_bet_failed"] = True
                    logger.warning(f"[DECISION] result posted error(timeout): {did} age={age:.1f}s")
        if confirmed_changed:
            self._settle_confirmed_decisions_from_buffers()

    def _check_decision_results(self) -> None:
        """status=done/error/processing の decision を取得して BetManager を更新。"""
        if not self._pending_decisions:
            return
        rows: list[dict] = []
        query_targets: list[dict[str, str]] = [{"base_url": "", "api_key": ""}]
        seen_target: set[tuple[str, str]] = set()
        for p in self._pending_decisions.values():
            if not isinstance(p, dict):
                continue
            b = str(p.get("source_base_url") or "").strip()
            k = str(p.get("source_api_key") or "").strip()
            if not b:
                continue
            tk = (b, k)
            if tk in seen_target:
                continue
            seen_target.add(tk)
            query_targets.append({"base_url": b, "api_key": k})
        seen_did: set[str] = set()
        for st in ("done", "error", "processing"):
            for tgt in query_targets:
                data = self._api_get(
                    "/api/decisions",
                    f"status={st}&limit=80",
                    base_url=tgt.get("base_url") or "",
                    api_key=tgt.get("api_key") or "",
                )
                for d in (data.get("decisions") or []):
                    if not isinstance(d, dict):
                        continue
                    did = str(d.get("decision_id") or "")
                    if did and did in seen_did:
                        continue
                    if did:
                        seen_did.add(did)
                    rows.append(d)

        for d in rows:
            did = str(d.get("decision_id") or "")
            if did not in self._pending_decisions:
                continue
            pending = self._pending_decisions.get(did) or {}
            result = d.get("result") or {}
            status = str(d.get("status") or "")
            outcome = str(result.get("outcome") or "?")
            phase = str(result.get("phase") or "")
            settled = bool(result.get("settled"))

            if status == "error" or result.get("error"):
                if (
                    str(result.get("error") or "") == "bet_send_timeout"
                    and (
                        pending.get("local_bet_sent")
                        or pending.get("bet_sent_posted")
                        or isinstance(pending.get("confirmed_bet"), dict) and bool(pending.get("confirmed_bet"))
                    )
                ):
                    logger.warning(
                        f"[DECISION] ignore stale bet_send_timeout after local confirmation: {did}"
                    )
                    continue
                self._pending_decisions.pop(did, None)
                self.total_resolved += 1
                _send_telegram(
                    f"⚠️ BET失敗\n{pending.get('table_name') or pending.get('table_id')}\n"
                    f"decision: {did[:12]}\n"
                    f"error: {result.get('error') or status}"
                )
                continue

            if phase == "bet_sent" and not settled:
                if not pending.get("bet_sent_notified"):
                    pending["bet_sent_notified"] = True
                    _send_telegram(
                        f"📤 BET送信確定\n{pending.get('table_name') or pending.get('table_id')}\n"
                        f"Side: {pending.get('side')} ${float(pending.get('amount') or 0):.2f}\n"
                        f"decision: {did[:12]}"
                    )
                    send_action(
                        f"✓ BET sent {pending.get('table_name') or pending.get('table_id')} "
                        f"{pending.get('side')} ${float(pending.get('amount') or 0):.2f}"
                    )
                    self._send_gui_money_status()
                continue

            if not settled:
                continue

            # VPS can settle a six-pattern signal even when this GUI did not actually
            # place the Stake bet (for example wrong-hand guard dropped it). Never
            # advance SEQ/UI from a settled VPS result unless the local executor has
            # confirmed this bet_id was really sent.
            if settled and self.bet_executor.is_live and not pending.get("local_bet_sent"):
                bet_id = str(pending.get("bet_id") or "")
                local_sent = False
                confirmed_info = None
                try:
                    if bet_id and hasattr(self.bet_executor, "get_confirmed_bet"):
                        confirmed_info = self.bet_executor.get_confirmed_bet(bet_id)
                        local_sent = bool(confirmed_info)
                    elif bet_id and hasattr(self.bet_executor, "has_sent_bet"):
                        local_sent = bool(self.bet_executor.has_sent_bet(bet_id))
                except Exception:
                    local_sent = False
                    confirmed_info = None
                if not local_sent:
                    logger.warning(
                        f"[DECISION] ignore settled result without local bet confirmation: "
                        f"{did} phase={phase or '-'} outcome={outcome} "
                        f"posted={bool(pending.get('result_posted'))} failed={bool(pending.get('local_bet_failed'))}"
                    )
                    if pending.get("local_bet_failed"):
                        self._pending_decisions.pop(did, None)
                    continue
                pending["local_bet_sent"] = True
                pending["confirmed_bet"] = confirmed_info or {}

            confirmed_info = pending.get("confirmed_bet") if isinstance(pending.get("confirmed_bet"), dict) else {}
            result_game_id = str(
                (result.get("hand") or {}).get("game_id")
                or result.get("game_id")
                or ""
            )
            confirmed_game_id = str((confirmed_info or {}).get("game_id") or "")
            if settled and self.bet_executor.is_live and confirmed_game_id and result_game_id and confirmed_game_id != result_game_id:
                logger.warning(
                    f"[DECISION] ignore settled result game mismatch: {did} "
                    f"confirmed_game={confirmed_game_id} result_game={result_game_id}"
                )
                continue

            won_raw = result.get("won")
            tie = bool(result.get("tie")) or outcome == "tie"
            won = bool(won_raw) if won_raw is not None else False
            try:
                bet_id_for_consume = str(pending.get("bet_id") or "")
                if bet_id_for_consume and hasattr(self.bet_executor, "consume_confirmed_bet"):
                    consumed_info = self.bet_executor.consume_confirmed_bet(bet_id_for_consume)
                    if isinstance(consumed_info, dict) and consumed_info:
                        pending["confirmed_bet"] = consumed_info
                        try:
                            confirmed_amount = float(consumed_info.get("confirmed_amount") or 0.0)
                        except Exception:
                            confirmed_amount = 0.0
                        if confirmed_amount > 0:
                            pending["actual_amount"] = confirmed_amount
                elif bet_id_for_consume and hasattr(self.bet_executor, "consume_sent_bet"):
                    self.bet_executor.consume_sent_bet(bet_id_for_consume)
            except Exception:
                pass
            mark_resolved = getattr(self.bet_executor, "mark_bet_resolved", None)
            if callable(mark_resolved):
                try:
                    mark_resolved(
                        bet_id=str(pending.get("bet_id") or ""),
                        table_id=str(pending.get("table_id") or ""),
                    )
                except Exception:
                    pass
            self._pending_decisions.pop(did, None)
            actual_amount = float(pending.get("actual_amount") or pending.get("amount") or 0.0)
            try:
                if actual_amount > 0:
                    self.money._last_bet_amount = actual_amount
            except Exception:
                pass
            self.money.apply_result(None if tie else won, side=str(pending.get("side") or "P"))
            if tie:
                self.ties += 1
            elif won:
                self.wins += 1
            else:
                self.losses += 1
            self.total_resolved += 1
            pnl_delta = (actual_amount * COMMISSION_BANKER if won and pending["side"] == "B"
                         else actual_amount if won else -actual_amount) if not tie else 0.0
            self.virtual_pnl += pnl_delta
            # A VPS collector can post the observed hand as "settled" while the
            # row is still processing. Once this GUI has verified the local Stake
            # bet was actually confirmed, rewrite the decision as done with the
            # real bet_id/amount so GUI/API state does not remain half-settled.
            try:
                normalized_result = "TIE" if tie else ("WIN" if won else "LOSE")
                result_hand = result.get("hand") if isinstance(result.get("hand"), dict) else {}
                self._post_decision_settlement(
                    decision_id=did,
                    table_id=str(pending.get("table_id") or d.get("table_id") or ""),
                    table_name=str(pending.get("table_name") or d.get("table_name") or ""),
                    pending=pending,
                    outcome=str(outcome or ""),
                    result=normalized_result,
                    pnl=pnl_delta,
                    new_hand={
                        "gameId": str(result_hand.get("game_id") or result.get("game_id") or ""),
                        "winner": str(result_hand.get("winner") or result.get("winner") or ""),
                    },
                )
            except Exception as e:
                logger.warning(f"[DECISION] final done rewrite failed: {did} err={e}")
            self._save_state()
            icon = "✅" if won else ("🔵" if tie else "❌")
            ms = self.money.status_dict()
            _send_telegram(
                f"{icon} {'WIN' if won else ('TIE' if tie else 'LOSE')}\n"
                f"outcome: {outcome}\n"
                f"bet: ${actual_amount:.2f}"
                + (f" (planned ${float(pending.get('amount') or 0):.2f})" if abs(actual_amount - float(pending.get("amount") or 0.0)) > 0.01 else "")
                + f"\npnl: ${pnl_delta:+.2f} | cum: ${self.virtual_pnl:+.2f}\n"
                f"W/L/T: {self.wins}/{self.losses}/{self.ties}\n"
                f"next: ${ms['next_bet']}"
            )
            send_msg(
                {
                    "type": "round_result",
                    "result": "tie" if tie else str(outcome or "").lower(),
                    "won": None if tie else bool(won),
                    "bet_amount": actual_amount,
                    "planned_bet_amount": float(pending.get("amount") or 0),
                    "bet_side": str(pending.get("side") or "").lower(),
                    "current_turn": ms.get("seq_turn"),
                    "turns_display": "".join(ms.get("seq7_current_turns") or []),
                    "overshoot": ms.get("seq_overshoot"),
                }
            )
            self._send_gui_money_status()

    def _decisions_poll_loop(self) -> None:
        """background thread: VPS API を long-poll して BET decision を受信。"""
        import urllib.request as _ur
        recent_ids: dict[str, float] = {}
        last_target_log_at = 0.0
        while not getattr(self, "_stop_decision_poll", False):
            try:
                targets = self._decision_poll_targets()
                now = time.time()
                if now - last_target_log_at >= 30:
                    logger.info(
                        "[BOT] decision poll targets: "
                        + ", ".join([f"{t.get('name')}={str(t.get('base_url') or '')[:48]}" for t in targets])
                    )
                    last_target_log_at = now
                if not targets:
                    time.sleep(2)
                    continue
                for t in targets:
                    if getattr(self, "_stop_decision_poll", False):
                        break
                    base = str(t.get("base_url") or "").rstrip("/")
                    key = str(t.get("api_key") or "").strip()
                    if not base:
                        continue
                    url = f"{base}/api/decisions/wait?provider=pragmatic&wait_sec=5"
                    req = _ur.Request(url, headers={"Authorization": f"Bearer {key}"})
                    try:
                        with _ur.urlopen(req, timeout=30) as r:
                            data = json.loads(r.read().decode("utf-8"))
                    except Exception as e:
                        err_key = f"{t.get('name') or 'target'}|{base}"
                        now_t = time.time()
                        err_map = getattr(self, "_decision_poll_target_error_at", {})
                        if not isinstance(err_map, dict):
                            err_map = {}
                        last_err = float(err_map.get(err_key, 0.0) or 0.0)
                        if now_t - last_err >= 30.0:
                            logger.warning(
                                f"[BOT] decision poll target failed: "
                                f"{t.get('name') or 'target'}={base} err={e}"
                            )
                            err_map[err_key] = now_t
                            self._decision_poll_target_error_at = err_map
                        continue
                    for d in (data.get("decisions") or []):
                        if not isinstance(d, dict):
                            continue
                        did = str(d.get("decision_id") or "")
                        if did and did in recent_ids:
                            continue
                        d["_source_api_base"] = base
                        d["_source_api_key"] = key
                        if not getattr(self, "_stop_decision_poll", False):
                            try:
                                self._last_wait_decision_id = ""
                                self._handle_decision(d)
                                if did and did != str(getattr(self, "_last_wait_decision_id", "") or ""):
                                    recent_ids[did] = now
                            except Exception as e:
                                logger.warning(f"[BOT] handle_decision error: {e}")
                # keep recent dedup map bounded
                if len(recent_ids) > 800:
                    cutoff = now - 1800
                    for _id in list(recent_ids.keys()):
                        if recent_ids.get(_id, 0) < cutoff:
                            recent_ids.pop(_id, None)
            except Exception as e:
                logger.debug(f"[BOT] decision poll error: {e}")
                time.sleep(3)

    def _poll_pending_decisions_fallback(self) -> None:
        """Short-poll pending decisions as a safety net for missed long-poll events."""
        if (not self.bet_executor.is_live) or self.no_vps_poll:
            return
        targets = self._decision_poll_targets()
        if not targets:
            return
        recent = getattr(self, "_fallback_recent_decision_ids", None)
        if not isinstance(recent, dict):
            recent = {}
        now = time.time()
        for did_key in list(recent.keys()):
            try:
                if now - float(recent.get(did_key) or 0.0) > 120.0:
                    recent.pop(did_key, None)
            except Exception:
                recent.pop(did_key, None)
        # Prefer the remote VPS target.  The local API can be empty even while the
        # VPS has a broadcast decision waiting.
        ordered = sorted(
            targets,
            key=lambda t: 0 if "remote" in str(t.get("name") or "").lower() else 1,
        )
        for t in ordered:
            base = str(t.get("base_url") or "").rstrip("/")
            key = str(t.get("api_key") or "").strip()
            if not base:
                continue
            data = self._api_get(
                "/api/decisions",
                "status=pending&limit=25",
                base_url=base,
                api_key=key,
            )
            for d in (data.get("decisions") or []):
                if not isinstance(d, dict):
                    continue
                if str(d.get("provider") or "") != "pragmatic":
                    continue
                did = str(d.get("decision_id") or "")
                if not did or did in recent:
                    continue
                d["_source_api_base"] = base
                d["_source_api_key"] = key
                self._last_wait_decision_id = ""
                logger.info(
                    f"[BOT] fallback pending decision: {did[:12]} "
                    f"table={d.get('table_name') or d.get('table_id')}"
                )
                self._handle_decision(d)
                if did and did != str(getattr(self, "_last_wait_decision_id", "") or ""):
                    recent[did] = now
        self._fallback_recent_decision_ids = recent

    def _preposition_poll_loop(self) -> None:
        """background thread: VPS API の preposition を受信して事前入場要求を積む。"""
        logger.info("[BOT] preposition polling thread started")
        while not getattr(self, "stop_flag", False) and not getattr(self, "_stop_decision_poll", False):
            try:
                self._check_preposition()
            except Exception as e:
                logger.warning(f"[BOT] preposition poll error: {e}")
            time.sleep(3.0)

    # ── run() override: VPS API ポーリング方式 ─────────────────────

    def run(
        self,
        duration: int | None = None,
        profile_dir: Path | None = None,
        cookies_file: Path | None = None,
    ):
        """VPS が lobby WS を監視する新アーキテクチャ。
        GUI は bet_page のみ使用し、VPS API から BET 指示を受け取る。
        """
        import json as _json  # noqa: F811

        # 新アーキテクチャ: VPS が lobby WS 監視、GUI は bet_page のみ使用
        # 決定事項トラッキング初期化
        self._pending_decisions: dict[str, dict] = {}
        self._last_preposition_key: str = ""
        self._stop_decision_poll = False

        cp.init_db()
        profile = profile_dir or cp.DEFAULT_PROFILE
        profile.mkdir(parents=True, exist_ok=True)
        is_empty = not any(profile.iterdir())
        if is_empty and cp.SOURCE_PROFILE.exists():
            logger.info(f"Cloning profile {cp.SOURCE_PROFILE} -> {profile}")
            import shutil
            try:
                profile.rmdir()
            except Exception:
                pass
            shutil.copytree(str(cp.SOURCE_PROFILE), str(profile))
        logger.info(f"DB initialized. Profile: {profile}")

        launch_opts: dict = {
            "headless": self.headless,
            "persistent_context": True,
            "user_data_dir": str(profile),
        }
        # headless モードでのブラウザプロセス安定化
        # bafather (Windows Server) では GPU ドライバ不在でクラッシュするため
        # ソフトウェアレンダリングにフォールバックさせる
        if self.headless:
            launch_opts["args"] = [
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]

        def on_signal(signum, frame):
            logger.warning(f"Signal {signum} received, stopping...")
            self.stop_flag = True

        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_signal)

        start_ts = time.time()
        report_interval = 60

        _camoufox_mgr = None  # GC による premature close を防ぐため参照を保持
        ctx = None
        bet_page = None
        try:
            _camoufox_mgr = cp.Camoufox(**launch_opts)
            ctx = _camoufox_mgr.__enter__()
            time.sleep(2)  # headless ブラウザ完全初期化待ち

            # bet_page のみ作成（lobby monitoring は VPS が担当）
            bet_page = ctx.pages[0] if ctx.pages else ctx.new_page()
            bet_page.on("websocket", self._on_ws)

            # executor setup
            if self.bet_executor.is_live and not getattr(
                self.bet_executor, "_context", None
            ):
                try:
                    self.bet_executor.setup(ctx, bet_page, bet_page)
                    logger.info("[BOT] executor context injected (run)")
                except Exception as e:
                    logger.warning(f"[BOT] executor setup failed: {e}")

            if self.bet_executor.is_live and profile_dir:
                try:
                    self.bet_executor.set_profile_dir(str(profile_dir))
                except Exception:
                    pass

            if cookies_file and cookies_file.exists():
                try:
                    with open(cookies_file) as cf:
                        cookies = _json.load(cf)
                    ctx.add_cookies(cookies)
                    logger.info(f"Restored {len(cookies)} cookies from {cookies_file}")
                except Exception as e:
                    logger.warning(f"Cookie restore failed: {e}")

            # bet_page をロビーに配置（最初の preposition/switch の準備）
            logger.info(f"Navigating bet_page to {cp.LOBBY_URL}")
            try:
                bet_page.goto(cp.LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                bet_page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"[BOT] lobby nav failed: {e}")

            _last_lobby_recover_at = 0.0
            _last_lobby_warn_at = 0.0

            def _ensure_pragmatic_lobby(force: bool = False) -> bool:
                nonlocal _last_lobby_recover_at, _last_lobby_warn_at, bet_page
                if bet_page is None:
                    return False
                try:
                    cur = str(getattr(bet_page, "url", "") or "")
                except Exception:
                    cur = ""
                ok = "pragmatic-play-live-lobby-baccarat" in cur
                if ok:
                    return True
                now = time.time()
                if (not force) and (now - _last_lobby_recover_at < 15.0):
                    return False
                _last_lobby_recover_at = now
                ex = self.bet_executor
                _pb = getattr(ex, "_pending_bet", None)
                _phase = getattr(ex, "_phase", "?")
                _pb_info = f"pending_bet=side={_pb.get('side')} table={_pb.get('table_id')} age={time.time()-float(_pb.get('queued_at',time.time())):.1f}s" if isinstance(_pb, dict) and _pb else "pending_bet=None"
                logger.warning(f"[BOT] LOBBY-GUARD TRIGGERED: not on pragmatic lobby (url={cur[:120]}) -> re-navigate | phase={_phase} | {_pb_info}")
                try:
                    bet_page.goto(cp.LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                    bet_page.wait_for_timeout(2000)
                except Exception as _e:
                    logger.warning(f"[BOT] lobby re-nav failed: {_e}")
                try:
                    cur2 = str(getattr(bet_page, "url", "") or "")
                except Exception:
                    cur2 = ""
                ok2 = "pragmatic-play-live-lobby-baccarat" in cur2
                if (not ok2) and (now - _last_lobby_warn_at >= 90.0):
                    _last_lobby_warn_at = now
                    _send_telegram(
                        "⚠️ StakeがPragmaticロビー以外のページです\n"
                        "Stakeログイン状態を確認してください（TOPページ固定時）"
                    )
                return ok2

            _ensure_pragmatic_lobby(force=True)

            # BET decision polling thread (background) - スキップ可 (--no-vps-poll)
            import threading as _threading
            if (not self.no_vps_poll) and self.bet_executor.is_live:
                poll_thread = _threading.Thread(
                    target=self._decisions_poll_loop, daemon=True, name="decision-poll"
                )
                poll_thread.start()
                prepos_thread = _threading.Thread(
                    target=self._preposition_poll_loop, daemon=True, name="preposition-poll"
                )
                prepos_thread.start()
                logger.info("[BOT] VPS API polling started (decisions + preposition)")
            elif not self.bet_executor.is_live:
                logger.info("[BOT] VPS API polling DISABLED (dry-run research mode)")
            else:
                logger.info("[BOT] VPS API polling DISABLED (--no-vps-poll / direct local mode)")
            send_log("ロビー監視中: VPSの6パターンdecisionを待機してBETを実行します")
            self._send_gui_money_status()

            last_report = time.time()
            last_executor_tick = time.time()
            last_prepos_check = time.time() - 10  # 初回即チェック
            last_result_check = time.time()
            last_result_flush_check = time.time()
            last_remote_signal_poll = time.time() - 5
            last_decision_fallback_poll = time.time() - 5
            input_stale_restart_sec = float(
                os.getenv("BACOPY_DRY_INPUT_STALE_RESTART_SEC", "180") or 180
            )
            _table_enter_at: dict[str, float] = {}  # table_id -> entered timestamp
            _TABLE_TIMEOUT = 90.0   # 90秒シグナルなし → ロビーへ戻る
            self._prepos_switch_at = 0.0             # 事前入場スイッチ開始時刻

            # executor がアイドルかどうか（switch 要求なし / pending bet なし）
            def _executor_is_idle() -> bool:
                ex = self.bet_executor
                return not (getattr(ex, "_switch_request", None)
                            or getattr(ex, "_pending_bet", None)
                            or getattr(ex, "_switch_in_progress", False))

            while not self.stop_flag:
                # ── bet_page 死活チェック ─────────────────────────
                try:
                    bet_page.wait_for_timeout(1000)
                except Exception as _page_err:
                    err_str = str(_page_err).lower()
                    if "closed" in err_str or "target" in err_str:
                        logger.warning(f"[BOT] bet_page closed unexpectedly, reinitializing: {_page_err}")
                        try:
                            bet_page = ctx.new_page()
                            bet_page.on("websocket", self._on_ws)
                            self.bet_executor.setup(ctx, bet_page, bet_page)
                            bet_page.goto(cp.LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                            bet_page.wait_for_timeout(2000)
                            logger.info("[BOT] bet_page reinit OK, resuming loop")
                        except Exception as _reinit_err:
                            logger.error(f"[BOT] reinit failed (context dead), stopping: {_reinit_err}")
                            # context が完全に死んでいる → break → finally で cleanup
                            break
                        _table_enter_at.clear()
                        continue
                    raise

                now = time.time()

                # VPSのDRY監視botでは、プロセスが生きていてもPragmatic入力だけ
                # 停止することがある。局面受信済みの接続が無音になったら再接続する。
                if (
                    not self.bet_executor.is_live
                    and self._ws_input_seen
                    and input_stale_restart_sec > 0
                    and now - self._last_collector_ws_at > input_stale_restart_sec
                ):
                    stale_for = int(now - self._last_collector_ws_at)
                    logger.error(
                        f"[INPUT-WATCHDOG] no table frames for {stale_for}s; "
                        "exiting for wrapper restart"
                    )
                    break

                # ── lobby URL ガード（TOPページ固定を自己回復） ─────
                if now - _last_lobby_recover_at >= 15.0:
                    try:
                        _ensure_pragmatic_lobby(force=False)
                    except Exception as e:
                        logger.debug(f"[BOT] lobby guard error: {e}")

                # ── executor tick ──────────────────────────────────
                if self.bet_executor.is_live:
                    # idle 時は 5 秒に 1 回のみ tick（keep-alive は lobby では不要）
                    tick_interval = 1.0 if _executor_is_idle() else 0.5
                    if now - last_executor_tick >= tick_interval:
                        try:
                            self.bet_executor.tick()
                        except Exception as e:
                            logger.debug(f"[BOT] executor tick error: {e}")
                        last_executor_tick = now

                # ── テーブル入場時刻トラッキング + タイムアウト ──
                curr_table = str(getattr(self.bet_executor, "current_table_id", "") or "")
                if curr_table and curr_table not in _table_enter_at:
                    _table_enter_at[curr_table] = now

                # タイムアウト判定: is_ready（betsopen済）OR 事前入場スイッチから90秒経過
                _prepos_elapsed = (now - self._prepos_switch_at) if self._prepos_switch_at > 0 else 0.0
                _timed_out_prepos = self._prepos_switch_at > 0 and _prepos_elapsed > _TABLE_TIMEOUT
                _timed_out_table = bool(curr_table and now - _table_enter_at.get(curr_table, now) > _TABLE_TIMEOUT)
                try:
                    bif_fn = getattr(self.bet_executor, "is_bet_in_flight", None)
                    _is_bif = bool(bif_fn()) if callable(bif_fn) else bool(bif_fn)
                except Exception:
                    _is_bif = False
                try:
                    pb_fn = getattr(self.bet_executor, "has_pending_bet", None)
                    _has_pb = bool(pb_fn()) if callable(pb_fn) else bool(pb_fn)
                except Exception:
                    _has_pb = False
                _should_return = (
                    not _is_bif
                    and not _has_pb
                    and (_timed_out_prepos or (self.bet_executor.is_ready and _timed_out_table))
                )
                if _should_return:
                    logger.info(f"[BOT] table timeout ({int(_TABLE_TIMEOUT)}s): {curr_table or '?'} → ロビーへ戻る")
                    _table_enter_at.clear()
                    self._prepos_switch_at = 0.0
                    # _last_preposition_key はリセットしない
                    # → 同じpreposition(same updated_at)を即再取得するループを防ぐ
                    # VPSが新しいupdated_atでprepositionを送った場合のみ再移動する
                    try:
                        self.bet_executor.return_to_lobby()
                    except Exception as e:
                        logger.warning(f"[BOT] return_to_lobby error: {e}")

                # ── VPS preposition ポーリング (3秒ごと) ─────────
                if now - last_prepos_check >= 3.0:
                    last_prepos_check = now
                    try:
                        self._check_preposition()
                    except Exception as e:
                        logger.debug(f"[BOT] preposition poll error: {e}")

                # ── VPS decision short-poll fallback (1秒ごと) ─────────
                if now - last_decision_fallback_poll >= 1.0:
                    last_decision_fallback_poll = now
                    try:
                        self._poll_pending_decisions_fallback()
                    except Exception as e:
                        logger.debug(f"[BOT] fallback decision poll error: {e}")

                # ── collector 停滞時の remote snapshot 補助シグナル (2秒ごと) ──
                if now - last_remote_signal_poll >= 2.0:
                    last_remote_signal_poll = now
                    try:
                        self._poll_remote_snapshots_for_signals()
                    except Exception as e:
                        logger.debug(f"[BOT] remote signal poll error: {e}")

                # ── local送信結果の反映 (2秒ごと) ─────────────────
                if now - last_result_flush_check >= 2.0:
                    last_result_flush_check = now
                    try:
                        self._flush_pending_decision_results()
                    except Exception as e:
                        logger.debug(f"[BOT] flush pending results error: {e}")

                # ── decision result ポーリング (10秒ごと) ─────────
                if now - last_result_check >= 10.0:
                    last_result_check = now
                    try:
                        self._check_decision_results()
                    except Exception as e:
                        logger.debug(f"[BOT] result poll error: {e}")

                # ── 定期ステータスレポート ───────────────────────
                if now - last_report >= report_interval:
                    elapsed = int(now - start_ts)
                    n_nt = self.wins + self.losses
                    wr = self.wins / n_nt * 100 if n_nt else 0
                    ms = self.money.status_dict()
                    mode_label = "LIVE" if self.bet_executor.is_live else "DRY"
                    logger.info(
                        f"[STATUS] elapsed={elapsed}s  signals={self.total_signals}  "
                        f"resolved={self.total_resolved}  W/L={self.wins}/{self.losses}  "
                        f"pnl=${self.virtual_pnl:+.2f}  next=${ms['next_bet']}"
                    )
                    if self.bet_executor.is_live and now - getattr(self, "_last_tg_status", 0) >= 300:
                        _send_telegram(
                            f"📊 LIVE 稼働 {elapsed//60}分\n"
                            f"通常BACCのBET: {self.wins}勝 / {self.losses}敗 / {self.ties}引分"
                            + (f" (勝率 {wr:.0f}%)" if n_nt else " (BET未実行)") + "\n"
                            f"損益: ${self.virtual_pnl:+.2f} (今回: ${ms['session_pnl']:+.2f})\n"
                            f"次BET額: ${ms['next_bet']:.2f}"
                        )
                        self._last_tg_status = now
                    last_report = now

                if duration and (now - start_ts) >= duration:
                    logger.info(f"Duration {duration}s reached, stopping.")
                    break

        except Exception as _run_err:
            # run() 全体を try-except で包み、予期しない例外でも
            # 確実に run_direct.py に戻る（ctx が死んだ場合など）
            logger.error(f"[BOT] run() fatal error: {_run_err}", exc_info=True)
            _send_telegram(f"💥 Bot crash: {_run_err}\nrun_direct.py が 10 秒後に再起動します")
            raise  # main() の except で捕捉 → exit_code=1 → run_direct.py が再起動
        finally:
            self._stop_decision_poll = True
            # camoufox_mgr の __exit__ で browser.close() + playwright stop
            if _camoufox_mgr is not None:
                try:
                    _camoufox_mgr.__exit__(None, None, None)
                except Exception:
                    pass

        logger.info(
            f"Final: signals={self.total_signals} resolved={self.total_resolved} "
            f"pnl=${self.virtual_pnl:+.2f}"
        )
        return 0


# ── エントリーポイント ───────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Dual-Line Match Pragmatic Autonomous Bot (DRY RUN / LIVE)"
    )
    ap.add_argument("--headless", action="store_true", help="Camoufox headless")
    ap.add_argument(
        "--no-v2-filter", action="store_true", help="全 24 pattern で BET (実験用)"
    )
    ap.add_argument(
        "--money-mode", type=str, default="flat",
        choices=list(MONEY_MODES), help="資金管理モード"
    )
    ap.add_argument(
        "--money-unit", type=float, default=100.0,
        help="flat/martingale の 1 unit 額 ($)"
    )
    ap.add_argument(
        "--profit-target", type=float, default=0.0, help="利確ライン ($)"
    )
    ap.add_argument(
        "--loss-cut", type=float, default=0.0, help="損切ライン ($)"
    )
    ap.add_argument(
        "--on-limit", type=str, default="stop",
        choices=["stop", "restart"], help="利確/損切後の動作"
    )
    ap.add_argument(
        "--flat-bet", type=float, default=100.0,
        help="非推奨: --money-unit を使用してください"
    )
    ap.add_argument(
        "--chip-base", type=float, default=None,
        help="GUI 互換: --money-unit の alias"
    )
    ap.add_argument(
        "--bet-mode", type=str, default="",
        help="GUI 互換: --money-mode の alias (空文字の場合は --money-mode が優先)"
    )
    ap.add_argument("--duration", type=int, default=0, help="秒 (0=無限)")
    ap.add_argument(
        "--profile", type=str, default="", help="Camoufox プロファイルディレクトリ"
    )
    ap.add_argument(
        "--profile-dir", type=str, default="",
        help="GUI 互換: --profile の alias"
    )
    ap.add_argument("--cookies", type=str, default="", help="Stake cookies JSON")
    ap.add_argument(
        "--no-resolution-notify", action="store_true",
        help="resolution Telegram 通知を抑制",
    )
    ap.add_argument(
        "--reset", action="store_true", help="state ファイルを削除して新規開始"
    )
    ap.add_argument(
        "--live", action="store_true",
        help="LIVE モード: 実 BET を発行 (未指定時は DRY RUN)",
    )
    ap.add_argument(
        "--no-vps-poll", action="store_true",
        help="VPS API polling を無効化（ローカル直接検出モード / bafather 直接実行用）",
    )
    # GUI が送るが dual-line では使わない args（parse_known_args で吸収）
    ap.add_argument("--table-name-substr", type=str, default="")
    ap.add_argument("--allow-banker", action="store_true")
    ap.add_argument("--allow-tie", action="store_true")
    ap.add_argument("--allow-switch-table", action="store_true")
    ap.add_argument("--assume-bc-012", action="store_true")
    ap.add_argument("--profit-session-limit", type=float, default=0)
    ap.add_argument("--auto-click-wait-sec", type=int, default=90)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--result-timeout-sec", type=int, default=60)
    args, _ = ap.parse_known_args(argv)

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        STATE_TMP.unlink(missing_ok=True)
        logger.info("state reset")

    # BetExecutor の選択
    bet_executor: BetExecutor
    if args.live:
        # LIVE 用 BetExecutor の読み込みを試みる
        try:
            from dual_line_live_executor import LiveBetExecutor
            bet_executor = LiveBetExecutor(notify_fn=_send_telegram)
            logger.info("LIVE mode: LiveBetExecutor loaded")
        except ImportError:
            logger.error(
                "LIVE mode 指定されたが dual_line_live_executor が見つかりません。"
                "DRY RUN にフォールバックします。"
            )
            bet_executor = DryRunBetExecutor()
    else:
        bet_executor = DryRunBetExecutor()

    money_unit = args.chip_base if args.chip_base is not None else (args.money_unit or args.flat_bet)
    # --bet-mode が MONEY_MODES の有効値であれば優先（GUI alias として機能）
    # 'dual_line' など無効値の場合は --money-mode を使う
    money_mode = args.bet_mode if (args.bet_mode and args.bet_mode in MONEY_MODES) else args.money_mode
    if money_mode not in MONEY_MODES:
        money_mode = "flat"
    profile = Path(args.profile_dir) if args.profile_dir else (Path(args.profile) if args.profile else None)
    bot = DualLinePragmaticBot(
        headless=args.headless,
        raw_log=False,
        use_v2_filter=(not args.no_v2_filter),
        money_mode=money_mode,
        money_unit=money_unit,
        profit_stop=args.profit_target,
        loss_cut=args.loss_cut,
        on_limit=args.on_limit,
        notify_signal=(not args.live),  # VPS(DryRun)のみSIGNAL通知担当、GUI botは重複排除
        notify_resolution=(not args.no_resolution_notify),
        notify_tie=False,
        bet_executor=bet_executor,
        no_vps_poll=getattr(args, "no_vps_poll", False),
    )

    mode_label = "LIVE" if bot.bet_executor.is_live else "DRY RUN"
    if bot.bet_executor.is_live:
        _send_telegram(
            "📊 LIVE 稼働 0分\n"
            "通常BACCのBET: 0勝 / 0敗 / 0引分 (BET未実行)\n"
            f"損益: $+0.00 (今回: $+0.00)\n"
            f"次BET額: ${bot.money.status_dict()['next_bet']:.2f}"
        )
        bot._last_tg_status = time.time()
    else:
        _send_telegram(
            f"🟢 dual_line_pragmatic_bot 起動 ({mode_label})\n"
            f"filter: {'v2 (6 patterns)' if not args.no_v2_filter else 'all patterns'}\n"
            f"money: {BET_MODES[bot.money.mode]} unit=${bot.money.unit}\n"
            f"stop: ${bot.money.profit_stop} cut: ${bot.money.loss_cut} on_limit: {bot.money.on_limit}\n"
            f"累計 signals: {bot.total_signals}"
        )

    cookies = Path(args.cookies) if args.cookies else None
    exit_code = 0
    try:
        bot.stop_flag = False
        bot.run(
            duration=args.duration or None,
            profile_dir=profile,
            cookies_file=cookies,
        )
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    except Exception as e:
        logger.error(f"run exception: {e}", exc_info=True)
        exit_code = 1

    bot._save_state()
    _send_telegram(
        f"🔴 dual_line_pragmatic_bot 停止\n"
        f"signals: {bot.total_signals} resolved: {bot.total_resolved}\n"
        f"W/L/T: {bot.wins}/{bot.losses}/{bot.ties}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
