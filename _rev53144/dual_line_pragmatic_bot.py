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
import re
import signal
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
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
    LIVE_SIGNAL_PATTERNS_V4,
    decide,
    live_preposition_for_history,
    live_signal_for_history,
    score_proximity,
    chinese_road_predict,
    big_road_predict,
)
from dual_line_money import BetManager, ALLOWED_MODES as MONEY_MODES, BET_MODES, BANKER_COMMISSION

# ── Logger 設定 ──────────────────────────────────────────────────────
# collector_pragmatic.py の basicConfig と競合しないよう dedicated logger を使う
_bot_logger = logging.getLogger("dual_line.bot")
_bot_logger.setLevel(logging.INFO)
_bot_logger.propagate = False  # root logger に伝播させない
if not _bot_logger.handlers:
    _log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    # ローテーション付きファイルハンドラ。append モードで全セッション蓄積し
    # 461MB まで肥大化していたため上限を設ける。既定 25MB x 5 = 約 125MB の
    # 直近履歴を保持(調査に十分)。起動後の最初の emit で既存の巨大ログは
    # .1 へ rollover され、backupCount を超えた古い世代から自動削除される。
    # executor も同一 logger("dual_line.bot")を共有し独自ハンドラを持たないため
    # 書き手は常に 1 つで、Windows でも rotation の rename 衝突は起きない。
    try:
        from logging.handlers import RotatingFileHandler
        _log_max_mb = float(os.getenv("BACOPY_LOG_MAX_MB", "25") or 25)
        _log_backups = int(os.getenv("BACOPY_LOG_BACKUPS", "5") or 5)
        _fh = RotatingFileHandler(
            _PERSISTENT_DIR / "dual_line_pragmatic_bot.log",
            maxBytes=int(max(1.0, _log_max_mb) * 1024 * 1024),
            backupCount=max(1, _log_backups),
            encoding="utf-8",
        )
    except Exception:
        _fh = logging.FileHandler(_PERSISTENT_DIR / "dual_line_pragmatic_bot.log", encoding="utf-8")
    _fh.setFormatter(_log_fmt)
    _bot_logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_log_fmt)
    _bot_logger.addHandler(_sh)
logger = _bot_logger

# ── 定数 ─────────────────────────────────────────────────────────────

V2_PATTERNS = LIVE_SIGNAL_PATTERNS
V4_PATTERNS = LIVE_SIGNAL_PATTERNS_V4

STATE_PATH = _PERSISTENT_DIR / "dual_line_pragmatic_state.json"
STATE_TMP = _PERSISTENT_DIR / "dual_line_pragmatic_state.tmp"
MONEY_STATE_PATH = _PERSISTENT_DIR / "dual_line_money_state.json"
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


class _DgaHandBuf:
    """_settle_confirmed_decision_from_hand / _decision_matches_hand が参照する
    最小の buf 互換オブジェクト。dga 勝者から VPS-NOW 確定BETを決済する際に、
    game-WS の本物の buf の代わりに渡す(table_name / qpid_table_id / table_id)。"""

    __slots__ = ("table_name", "qpid_table_id", "table_id")

    def __init__(self, table_name: str = "", qpid_table_id: str = "", table_id: str = ""):
        self.table_name = table_name
        self.qpid_table_id = qpid_table_id
        self.table_id = table_id


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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


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
    """Telegram 通知を送る。

    非ブロッキング: 実送信はデーモンスレッドで行い、bot のメインループ/ポーリング
    スレッドを絶対に止めない（過去、Telegram が一時的に詰まると通知が「止まった」
    まま復帰しなかった）。スレッド内で 3 回までリトライし、429(レート制限)は
    retry_after を尊重。成否は必ずログに残す(これまで成功時は無ログで原因不明だった)。
    """
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

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    attempts = max(1, int(os.getenv("BACOPY_TELEGRAM_RETRIES", "3") or 3))
    timeout_s = float(os.getenv("BACOPY_TELEGRAM_TIMEOUT_SEC", "8") or 8)

    def _worker() -> None:
        import time as _t
        try:
            import requests
        except Exception as e:
            logger.warning(f"[TELEGRAM] send skipped: requests import failed: {e}")
            return
        for attempt in range(1, attempts + 1):
            try:
                r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout_s)
                if r.ok:
                    logger.info(f"[TELEGRAM] sent ok (attempt={attempt})")
                    return
                if r.status_code == 429:
                    retry_after = 1.0
                    try:
                        retry_after = float(r.json().get("parameters", {}).get("retry_after", 1.0))
                    except Exception:
                        pass
                    retry_after = min(max(0.5, retry_after), 6.0)
                    logger.warning(f"[TELEGRAM] rate-limited 429 retry_after={retry_after}s (attempt={attempt})")
                    _t.sleep(retry_after)
                    continue
                logger.warning(
                    f"[TELEGRAM] send failed status={r.status_code} "
                    f"response={r.text[:160]!r} (attempt={attempt})"
                )
            except Exception as e:
                logger.warning(f"[TELEGRAM] send error (attempt={attempt}): {e}")
            if attempt < attempts:
                _t.sleep(min(1.0 * attempt, 3.0))
        logger.warning("[TELEGRAM] send giving up after retries")

    try:
        import threading
        threading.Thread(target=_worker, daemon=True, name="tg-send").start()
        return True
    except Exception as e:
        logger.warning(f"[TELEGRAM] thread spawn failed, sending inline: {e}")
        try:
            _worker()
        except Exception:
            pass
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
        manual_assist: bool = False,
        seq_set_size: int = 7,
    ):
        super().__init__(headless=headless, raw_log=raw_log)
        self.use_v2_filter = use_v2_filter
        # dual-line モード: "v3"(6パターン, 既定) / "v4"(10パターン)。GUIプルダウンで切替。
        self.dual_mode = (os.getenv("BACOPY_DUAL_MODE", "v3") or "v3").strip().lower()
        if self.dual_mode not in ("v3", "v4"):
            self.dual_mode = "v3"
        # ── 安全モード(GUI トグル): エッジ劣化(warn-edge)ゲート ─────────────────
        # ON のとき _handle_decision で「選択中の系統(6P/6P追従/10P/10P追従)の
        # 長期累計勝率 >= 50.0% なら BET / < 50.0%(=warn-edge=エッジ劣化) なら停止」。
        # ★短期の調子(好調/軟調/低調/悪調 badge)では判定しない(自己相関≈0で先行性ゼロ・
        #   勝率チャートのコード自身が「短期は見るだけ・増減判断に使うな」と明記)。
        #   死んだパターンを退役させる長期累計でのみゲート(2026-06-28 改訂)。
        # 長期累計は _safety_stats_poll_loop が /api/winrate-trend を ~60s 毎に取得。
        # データ無/古い時はフェイルセーフ=賭ける(warn-edge は確証がある時だけ停止)。
        # OFF のとき本メソッドの判定は一切働かず従来挙動。
        self._safety_enabled = _env_bool("BACOPY_SAFETY_MODE", False)
        # 4系統の長期累計勝率%(None=データ無): v3=6P/v4=10P/follow_v3=6P追従/follow_v4=10P追従
        self._safety_trend = {"v3": None, "v4": None, "follow_v3": None, "follow_v4": None}
        self._safety_stats_at = 0.0  # 最後に winrate-trend を取得できた時刻(鮮度ガード)
        self.no_vps_poll = no_vps_poll
        self.manual_assist = bool(manual_assist)
        self.manual_assist_auto_click = bool(
            self.manual_assist
            and (
                _env_bool("BACOPY_MANUAL_ASSIST_AUTO_CLICK", False)
                or _env_bool("BACOPY_CHROME_ASSIST_AUTO_CLICK", False)
            )
        )
        # HARD OVERRIDE → PURE manual-assist (no auto-click) regardless of GUI
        # config or other envs. The bot must NOT click bets: it only shows the NOW
        # signal + amount and tracks SEQ/D'Alembert via the operator's WIN/LOSE
        # taps. (Auto-click was silently ON, making the bot auto-bet with the
        # unreliable geometry clicker AND breaking the manual overlay/WIN flow.)
        if _env_bool("BACOPY_MANUAL_NO_AUTOCLICK", False):
            self.manual_assist_auto_click = False
        # ── デュアルラインオート追従 (BACOPY_DUAL_FOLLOW=1) ──────────────
        # 勝った時だけ「追従」: 大路(pattern_keyの真ん中)が telecho/dragon の signal で
        # 勝つと、同じ卓で負けるまで連続自動BETする。telecho=逆張り(毎手反対側)、
        # dragon=順張り(同じ側)。それ以外の大路は追従しない。額は SEQ 継続(money model)。
        # TIE はプッシュ(同じ側で再BET・反転しない)。追従中は他卓のNOWを無視。
        self._follow_enabled = _env_bool("BACOPY_DUAL_FOLLOW", False)
        self._follow_active = False
        self._follow_table_id = ""        # 追従中の卓 qpid
        self._follow_table_name = ""
        self._follow_kind = ""            # "telecho"(逆張り) | "dragon"(順張り)
        self._follow_next_side = ""       # 次の追従BETの側 "B"/"P"
        self._follow_pattern_key = ""
        self._follow_chain = 0            # この追従での連勝数
        self._follow_last_bet_at = 0.0    # 追従BET最終時刻(タイムアウト監視用)
        self._follow_bet_id = ""          # 進行中の追従BETのbet_id(失敗即検知用)
        self._follow_did = ""             # 進行中の追従BETのdecision_id(pending掃除用)
        self._follow_reason = ""          # 直近追従の根拠表示(GUIアシストパネル用)
        # ── 逆張り(reverse)モード ────────────────────────────────────────
        # 正味NOW(v3/v4)の side を入口で B<->P 反転するだけ(T はそのまま)。賭け・
        # 決済・光り・勝率カウントは反転後 side で一貫。タイミング/センタリング/決済
        # 経路には一切触れない=安全。★env から読まない=再起動で必ず OFF に戻る
        # (付けっぱなし事故防止)。stdin {"type":"set_reverse","on":bool} で即時切替。
        # ON 中は追従(WIN追跡)を自動停止する(_follow_on_settled でゲート)。
        self._reverse_bet = False
        # 利確(profit_stop)到達でGUIへ1回だけ停止通知を出す用。
        self._profit_stop_sent = False
        self.notify_signal = notify_signal
        self.notify_resolution = notify_resolution
        self.notify_tie = notify_tie
        self.bet_executor: BetExecutor = bet_executor or DryRunBetExecutor()
        # 追従の方向再計算リゾルバを executor に注入(送信直前に最新出目から再導出)。
        try:
            setattr(self.bet_executor, "_follow_side_resolver", self._resolve_follow_side_at_send)
        except Exception:
            pass
        self.money = BetManager(
            mode=money_mode, unit=money_unit,
            profit_stop=profit_stop, loss_cut=loss_cut,
            on_limit=on_limit,
            state_path=MONEY_STATE_PATH,
            seq_set_size=seq_set_size,
        )
        # 利確で停止→再起動(再開)時: 新セッションとして session_pnl=0 + limit解除で
        # 再アーム(利確額は据置)。SEQ進行(seq7/seq_level)はそのまま継続(=リセットしない)。
        # 利確分は既にStake残高/デイリーに計上済みなので session_pnl は0スタートでよい。
        if self.money.limit_reached and self.money.limit_reason == "profit":
            self.money.session_pnl = 0.0
            self.money.limit_reached = False
            self.money.limit_reason = ""
            try:
                self.money._save_state()
            except Exception:
                pass
            logger.info("[PROFIT] resume after profit-stop: session_pnl reset to 0, profit target re-armed, SEQ kept")

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
        self._manual_assist_items: dict[str, dict] = {}
        self._manual_command_reader_started = False
        self._now_lock: dict[str, object] = {}
        # 機能①(HOLD): この卓を固定して他卓のシグナルでスクロールさせない。
        self._pinned = False
        self._pinned_lock: dict[str, object] = {}
        # 機能①拡張(HOLD): 赤/青/黄いずれの枠も無い時でもHOLDで画面スクロールを凍結する。
        self._scroll_frozen = False
        # VPS-driven NOW: GUIのNOWを VPS decision(=テレグラム配信と同一) だけで駆動し、
        # ローカル独自signalのNOWは抑止する（テレグラムと1対1・同速にする）。
        self._vps_driven_now = os.getenv("BACOPY_VPS_DRIVEN_NOW", "0").strip().lower() in ("1", "true", "yes", "on")


        # 累計統計
        self.total_signals = 0
        self.total_resolved = 0
        self.wins = 0
        self.losses = 0
        self.ties = 0
        self.virtual_pnl = 0.0
        # 拾ったNOW(チャンネルから受信した初回シグナル)の影の勝率。賭けたか否かに
        # 関係なく、その台の次の結果で勝敗を付ける(=取りこぼし込み)。追従は対象外
        # (追従は受信シグナルでなく賭け戦略)。実BET勝率(self.wins)とは別物。
        self.caught_wins = 0      # 追従込み(全拾NOW=初回+追従)
        self.caught_losses = 0
        self.caught_ties = 0
        self.caught_now_wins = 0  # 追従なし(初回NOWのみ)
        self.caught_now_losses = 0
        self.caught_now_ties = 0
        self._caught_pending: list = []  # [{"ids": set, "side": "P"/"B", "follow": bool}]
        self._caught_seen_count: dict = {}  # table_id -> 最後に見たbuf.hands長(拾NOW影決済用)
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
        browser_mode = (
            os.getenv("BACOPY_BROWSER", "")
            or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
            or "camoufox"
        ).strip()
        cdp_url = (
            os.getenv("BACOPY_CHROME_CDP_URL", "")
            or os.getenv("BACOPY_CHROME_DEBUG_URL", "")
            or "http://127.0.0.1:9222"
        ).strip()
        logger.info(
            f"[AUTO-PROBE] init browser={browser_mode or 'camoufox'} "
            f"cdp={cdp_url if browser_mode.lower() in ('chrome_attach', 'chrome-cdp', 'cdp') else '-'} "
            f"manual_assist={self.manual_assist} "
            f"auto_bet_enabled={not self.manual_assist or self.manual_assist_auto_click} "
            f"manual_assist_auto_click={self.manual_assist_auto_click} "
            f"live_executor={self.bet_executor.is_live}"
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
                    self._maybe_register_dga_callback()
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

    def _register_caught_now(self, *ids, side=None, is_follow=False) -> None:
        """拾ったNOWを影の勝率追跡に登録する。実際にBETするか否かに関係なく、その台の
        次の完了ハンドで勝敗が付く(取りこぼし込み)。LIVE受け子のみ。2系統で集計する:
        ・追従込み(全NOW=初回+追従)  ・追従なし(初回NOWのみ)。is_follow で振り分け。
        ★捕捉経路で台IDが異なる(table_id/qpid/target)ため全IDをエイリアス集合で保持し、
        結果観測側はbufの全IDで照合する(=ID不一致でも当たる)。"""
        try:
            if not getattr(self.bet_executor, "is_live", False):
                return  # 監視bot(dry-run)では集計しない
            sc = str(side or "").strip().upper()[:1]
            if sc not in ("P", "B"):
                return
            alias = set(str(x) for x in ids if x)
            if not alias:
                return
            self._caught_pending.append({"ids": alias, "side": sc, "follow": bool(is_follow)})
            if len(self._caught_pending) > 300:  # 無限増殖の保険
                self._caught_pending = self._caught_pending[-300:]
            logger.info(
                f"[CAUGHT-NOW] registered ids={sorted(alias)} side={sc} "
                f"follow={bool(is_follow)} pending={len(self._caught_pending)}"
            )
        except Exception:
            pass

    def _resolve_caught_now(self, table_id: str, outcome_char: str, buf=None, extra_keys=None) -> None:
        """この台で今完了したハンド(P/B/T)に対し、最古の保留中・拾NOWを決済し集計。
        BETの有無と無関係(取りこぼしも当たれば+1)。台IDはbuf/extra_keysの全IDで照合する。
        追従込み(caught_*)は常時、追従なし(caught_now_*)は初回NOWのみ加算。"""
        try:
            if not self._caught_pending:
                return
            keys = {str(table_id)}
            if buf is not None:
                for a in (getattr(buf, "qpid_table_id", ""), getattr(buf, "table_name", "")):
                    if a:
                        keys.add(str(a))
            if extra_keys:
                for a in extra_keys:
                    if a:
                        keys.add(str(a))
            idx = next((i for i, e in enumerate(self._caught_pending) if e["ids"] & keys), None)
            if idx is None:
                return
            e = self._caught_pending.pop(idx)
            sc = e["side"]
            foll = bool(e.get("follow"))
            oc = str(outcome_char).strip().upper()[:1]
            if oc == "T":
                _r = "T"
            elif oc == sc:
                _r = "W"
            elif oc in ("P", "B"):
                _r = "L"
            else:
                self._caught_pending.insert(idx, e)  # 未知の結果は戻して次ハンド待ち
                return
            # 追従込み(全NOW)
            if _r == "T":
                self.caught_ties += 1
            elif _r == "W":
                self.caught_wins += 1
            else:
                self.caught_losses += 1
            # 追従なし(初回NOWのみ)
            if not foll:
                if _r == "T":
                    self.caught_now_ties += 1
                elif _r == "W":
                    self.caught_now_wins += 1
                else:
                    self.caught_now_losses += 1
            nA = self.caught_wins + self.caught_losses
            nN = self.caught_now_wins + self.caught_now_losses
            # カウンタは常時更新済み。I/O(ログ+GUI送信)は >=2s にスロットルして、結果処理
            # ループ上の stdout 書き込み圧を最小化する(GUIカードは periodic status でも更新)。
            _emt = time.monotonic()
            if _emt - getattr(self, "_last_caught_emit", 0.0) >= 2.0:
                self._last_caught_emit = _emt
                logger.info(
                    f"[CAUGHT-NOW] resolved table={table_id} pred={sc} outcome={oc} -> {_r} follow={foll}  "
                    f"込み={self.caught_wins}W/{self.caught_losses}L "
                    f"なし={self.caught_now_wins}W/{self.caught_now_losses}L (signals={self.total_signals})"
                )
                send_msg({
                    "type": "caught_stats",
                    "caught_wins": self.caught_wins,
                    "caught_losses": self.caught_losses,
                    "caught_ties": self.caught_ties,
                    "caught_win_rate": round((self.caught_wins / nA * 100) if nA else 0.0, 1),
                    "caught_now_wins": self.caught_now_wins,
                    "caught_now_losses": self.caught_now_losses,
                    "caught_now_ties": self.caught_now_ties,
                    "caught_now_win_rate": round((self.caught_now_wins / nN * 100) if nN else 0.0, 1),
                    "total_signals": self.total_signals,
                })
        except Exception:
            pass

    def _process_table_frame(self, table_id: str) -> None:
        buf = self.buffers.get(table_id)
        if not buf:
            return
        # 拾NOW影決済(取りこぼし込み): no_vps_poll等の分岐に依らず、全卓の新ハンドを
        # 自前カウンタで追って判定する(BETの有無に関係なく)。bufの全IDで照合。
        try:
            _cur = len(buf.hands or [])
            _prev = self._caught_seen_count.get(table_id)
            self._caught_seen_count[table_id] = _cur
            if _prev is not None and _cur > _prev and self._caught_pending:
                for _nh in (buf.hands or [])[_prev:_cur]:
                    _oc = _winner_to_char(_nh.get("winner"))
                    if _oc:
                        self._resolve_caught_now(table_id, _oc, buf)
        except Exception:
            pass
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
                    # (拾NOW影決済は関数冒頭の自前カウンタ側で実施=分岐に依存しない)
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
            logger.info(
                f"[AUTO-PROBE] money snapshot source=status "
                f"mode={ms.get('mode')} next=${float(ms.get('next_bet') or 0.0):.2f} "
                f"seq_turn={ms.get('seq_turn')} overshoot={ms.get('seq_overshoot')} "
                f"turns={''.join(turns) if isinstance(turns, list) else ''} "
                f"pnl=${float(ms.get('session_pnl') or 0.0):+.2f}"
            )
            send_msg(
                {
                    "type": "status",
                    "wins": self.wins,
                    "losses": self.losses,
                    "ties": self.ties,
                    "caught_wins": self.caught_wins,
                    "caught_losses": self.caught_losses,
                    "caught_ties": self.caught_ties,
                    "caught_win_rate": round(
                        (self.caught_wins / (self.caught_wins + self.caught_losses) * 100)
                        if (self.caught_wins + self.caught_losses) else 0.0, 1),
                    "caught_now_wins": self.caught_now_wins,
                    "caught_now_losses": self.caught_now_losses,
                    "caught_now_ties": self.caught_now_ties,
                    "caught_now_win_rate": round(
                        (self.caught_now_wins / (self.caught_now_wins + self.caught_now_losses) * 100)
                        if (self.caught_now_wins + self.caught_now_losses) else 0.0, 1),
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

    def _send_manual_assist_mode(self) -> None:
        """Tell the GUI whether dual-line is running in manual assist mode."""
        try:
            send_msg(
                {
                    "type": "manual_assist_mode",
                    "enabled": bool(self.manual_assist),
                    "auto_bet_enabled": (not bool(self.manual_assist)) or bool(self.manual_assist_auto_click),
                    "manual_assist_auto_click": bool(self.manual_assist_auto_click),
                    "money_status": self.money.status_dict(),
                    "ts": time.time(),
                }
            )
            self._last_manual_assist_mode_sent_at = time.time()
        except Exception:
            pass

    def _maintain_manual_assist_mode_heartbeat(self) -> None:
        """Re-publish manual_assist_mode periodically so the GUI panel does not vanish."""
        if not self.manual_assist:
            return
        try:
            interval = float(os.getenv("BACOPY_MANUAL_ASSIST_MODE_HEARTBEAT_SEC", "30") or 30)
        except Exception:
            interval = 30.0
        last = float(getattr(self, "_last_manual_assist_mode_sent_at", 0.0) or 0.0)
        if time.time() - last >= max(5.0, interval):
            self._send_manual_assist_mode()

    def _send_manual_assist_item(
        self,
        *,
        status: str,
        table_id: str,
        table_name: str = "",
        qpid: str = "",
        side: str = "",
        amount: float | None = None,
        pattern_key: str = "",
        decision_id: str = "",
        item_id: str = "",
        signal_game_id: str = "",
        score: int | None = None,
        steps_before: int | None = None,
        source: str = "",
        expires_sec: float = 30.0,
        result: str = "",
        pnl: float | None = None,
        follow_reason: str = "",
    ) -> None:
        """Emit the manual operator queue item without touching auto-bet state."""
        if not self.manual_assist:
            return
        now = time.time()
        try:
            amt = float(amount if amount is not None else self.money.next_bet())
        except Exception:
            amt = 0.0
        ms = self.money.status_dict()
        turns = ms.get("seq7_current_turns") or []
        if item_id:
            item_id = str(item_id)
        elif decision_id:
            item_id = str(decision_id)
        elif str(status).upper() == "READY":
            item_id = f"READY:{qpid or table_id}"
        else:
            item_id = f"{status}:{qpid or table_id}:{pattern_key}:{int(now)}"
        payload = {
            "type": "manual_assist_item",
            "id": item_id,
            "decision_id": decision_id,
            "table_id": table_id,
            "qpid": qpid or table_id,
            "table_name": table_name or table_id,
            "side": side,
            "amount": amt,
            "pattern_key": pattern_key,
            "status": status,
            "signal_game_id": signal_game_id,
            "score": score,
            "steps_before": steps_before,
            "source": source,
            "created_at": _utc_now_iso(),
            "expires_at": now + float(expires_sec or 0.0),
            "money_status": ms,
            "seq_turn": ms.get("seq_turn"),
            "seq_overshoot": ms.get("seq_overshoot"),
            "seq7_current_turns": turns if isinstance(turns, list) else [],
            "gui_next_bet": ms.get("next_bet"),
            "result": str(result or ""),
            "pnl": pnl,
            "follow_reason": str(follow_reason or ""),
        }
        logger.info(
            f"[AUTO-PROBE] manual_item status={status} id={item_id} "
            f"table={table_name or table_id} qpid={qpid or table_id} side={side or '-'} "
            f"planned=${amt:.2f} gui_next=${float(ms.get('next_bet') or 0.0):.2f} "
            f"seq_turn={ms.get('seq_turn')} overshoot={ms.get('seq_overshoot')} "
            f"turns={''.join(turns) if isinstance(turns, list) else ''}"
        )
        self._manual_assist_items[item_id] = dict(payload)
        try:
            send_msg(payload)
        except Exception:
            pass

    def _active_now_lock(self) -> dict[str, object]:
        lock = self._now_lock if isinstance(self._now_lock, dict) else {}
        if not lock:
            return {}
        until = float(lock.get("until") or 0.0)
        if until and time.time() >= until:
            logger.info(
                f"[NOW-LOCK] expired did={str(lock.get('decision_id') or '-')[:12]} "
                f"table={lock.get('table_id') or '-'}"
            )
            self._now_lock = {}
            return {}
        return lock

    def _start_now_lock(
        self,
        *,
        decision_id: str,
        table_id: str,
        table_name: str = "",
        side: str = "",
        bet_id: str = "",
        hold_sec: float | None = None,
        match_table_id: str = "",
        signal_game_id: str = "",
    ) -> None:
        tid = str(table_id or "").strip()
        did = str(decision_id or "").strip()
        if not tid and not did:
            return
        # 機能①(HOLD): 固定中は固定卓以外のNOWでフォーカスを奪わない（スクロール抑止）。
        if self._pinned:
            ptid = str((self._pinned_lock or {}).get("table_id") or "")
            if ptid and tid and tid != ptid:
                logger.info(f"[NOW-LOCK] suppressed (pinned to {ptid[:12]}) for table={tid[:12]}")
                return
        # match_table_id = the COLLECTOR table_id that _on_new_hand fires with
        # (can differ from `tid`, which is the QPID used for executor overlay /
        # focus-hold keying in multi-area lobby). signal_game_id = the gameId of
        # the signal hand; the NEXT new hand on match_table_id is the bet hand,
        # so its arrival == hand-end. Preserve both across re-stamp calls (the
        # auto path calls _start_now_lock twice: pre-bet then with bet_id).
        prev = self._now_lock if isinstance(self._now_lock, dict) else {}
        mtid = str(match_table_id or "").strip()
        sgid = str(signal_game_id or "").strip()
        if (not mtid or not sgid) and prev and str(prev.get("decision_id") or "") == did:
            mtid = mtid or str(prev.get("match_table_id") or "")
            sgid = sgid or str(prev.get("signal_game_id") or "")
        sec = float(hold_sec if hold_sec is not None else os.getenv("BACOPY_NOW_LOCK_MAX_SEC", "65") or 65)
        until_ts = time.time() + max(30.0, sec)
        self._now_lock = {
            "decision_id": did,
            "table_id": tid,
            "table_name": str(table_name or tid),
            "side": str(side or "").upper(),
            "bet_id": str(bet_id or ""),
            "started_at": time.time(),
            "until": until_ts,
            "match_table_id": mtid,
            "signal_game_id": sgid,
        }
        logger.info(
            f"[NOW-LOCK] start did={did[:12] or '-'} table={tid or '-'} "
            f"name={table_name or tid or '-'} side={side or '-'} max_sec={max(30.0, sec):.0f}"
        )
        try:
            hold_fn = getattr(self.bet_executor, "_start_assist_focus_hold", None)
            if callable(hold_fn) and tid:
                hold_fn(tid, table_name or tid, intent="now_lock")
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] focus hold start failed: {ex}")
        try:
            mirror_fn = getattr(self.bet_executor, "set_bot_now_lock", None)
            if callable(mirror_fn):
                mirror_fn(
                    decision_id=did,
                    table_id=tid,
                    table_name=table_name or tid,
                    side=side,
                    until=until_ts,
                )
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] executor mirror failed: {ex}")
        # NOW が確定した瞬間に一度だけ即時センタリングを実行する。
        # _maintain_active_now_bet_hold のポーリング (0.5s) を待たずに
        # タイルを画面中央へ移動するため。
        try:
            center_fn = getattr(self.bet_executor, "_center_multi_tile", None)
            if callable(center_fn) and tid:
                center_fn(tid, table_name or tid, click=False, source="now_lock")
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] immediate center failed: {ex}")

    def _release_now_lock(
        self,
        *,
        decision_id: str = "",
        table_id: str = "",
        bet_id: str = "",
        reason: str = "",
    ) -> None:
        lock = self._now_lock if isinstance(self._now_lock, dict) else {}
        if not lock:
            return
        did = str(decision_id or "").strip()
        tid = str(table_id or "").strip()
        bid = str(bet_id or "").strip()
        lock_did = str(lock.get("decision_id") or "")
        lock_tid = str(lock.get("table_id") or "")
        lock_bid = str(lock.get("bet_id") or "")
        if did and lock_did and did != lock_did:
            return
        if tid and lock_tid and tid != lock_tid:
            return
        if bid and lock_bid and bid != lock_bid:
            return
        logger.info(
            f"[NOW-LOCK] release did={lock_did[:12] or '-'} table={lock_tid or '-'} "
            f"reason={reason or '-'}"
        )
        self._now_lock = {}
        try:
            mirror_fn = getattr(self.bet_executor, "clear_bot_now_lock", None)
            if callable(mirror_fn):
                mirror_fn(decision_id=lock_did, table_id=lock_tid, reason=reason or "")
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] executor mirror clear failed: {ex}")
        # Visually remove the red/blue NOW box and stop the focus-hold so the
        # view resumes scanning (overlay/hold are keyed by QPID = lock_tid).
        try:
            assist_fn = getattr(self.bet_executor, "release_assist_now", None)
            if callable(assist_fn) and lock_tid:
                assist_fn(lock_tid)
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] assist overlay release failed: {ex}")

    def _on_executor_hand_end(self, decision_id: str = "", qpid: str = "", gid: str = "") -> None:
        """Executor detected (via betsopen gameId advance) that the NOW-locked
        hand ended. Drop the red/blue overlay so the view resumes scanning. This
        is the reliable hand-end path for Speed/multiplay tables, where the
        winner-bearing hand never reaches _on_new_hand. Visual only — the money
        progression stays operator-driven via WIN/LOSE. Pure-manual mode only."""
        if not (self.manual_assist and not self.manual_assist_auto_click):
            return
        try:
            lock = self._now_lock if isinstance(self._now_lock, dict) else {}
            if not lock:
                return
            lk_did = str(lock.get("decision_id") or "")
            lk_tid = str(lock.get("table_id") or "")
            if decision_id and lk_did and decision_id != lk_did:
                return
            if qpid and lk_tid and qpid != lk_tid:
                return
            # 機能①(HOLD): 固定中はハンド終了で解除せず、同卓に赤/青枠を再描画して保持。
            if self._pinned and self._pinned_lock:
                logger.info(
                    f"[NOW-LOCK] hand-end: pinned -> keep box (re-arm) qpid={qpid or lk_tid}"
                )
                self._rearm_pinned_box()
                return
            logger.info(
                f"[NOW-LOCK] hand-end clear (betsopen) qpid={qpid or lk_tid} gid={gid or '-'}"
            )
            self._release_now_lock(decision_id=lk_did, reason="hand_ended")
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] executor hand-end clear failed: {ex}")

    def _is_pinned_item(self, item: dict | None) -> bool:
        """この item が現在HOLD固定中の卓のものかを判定。"""
        if not self._pinned or not item:
            return False
        pl = self._pinned_lock or {}
        ptbl = str(pl.get("table_id") or pl.get("qpid") or "")
        if not ptbl:
            return True
        itbl = str(item.get("qpid") or item.get("table_id") or "")
        return bool(itbl) and itbl == ptbl

    def _rearm_manual_now(self, key: str, item: dict, *, reason: str = "") -> None:
        """同じ卓・同方向で赤/青枠(NOW)を再表示し、再BET可能な状態に戻す。
        機能②(TIEプッシュ) と 機能①(HOLD固定中の継続BET) で共用。"""
        if not item:
            return
        side = str(item.get("side") or "P").upper()
        tid = str(item.get("table_id") or "")
        qpid = str(item.get("qpid") or item.get("table_id") or "")
        tname = str(item.get("table_name") or tid)
        try:
            amount = float(self.money.next_bet() or 0.0)
        except Exception:
            amount = float(item.get("amount") or 0.0)
        did = str(item.get("decision_id") or item.get("id") or key or "")
        item["status"] = "NOW"
        item["amount"] = amount
        item.pop("manual_result", None)
        item.pop("settled_at", None)
        self._send_manual_assist_item(
            status="NOW",
            table_id=tid,
            table_name=tname,
            qpid=qpid,
            side=side,
            amount=amount,
            pattern_key=str(item.get("pattern_key") or ""),
            decision_id=str(item.get("decision_id") or ""),
            item_id=str(key or item.get("id") or ""),
            signal_game_id=str(item.get("signal_game_id") or ""),
            source=str(item.get("source") or "rearm"),
            expires_sec=30.0,
        )
        self._start_now_lock(
            decision_id=did,
            table_id=qpid or tid,
            table_name=tname,
            side=side,
            match_table_id=tid,
            signal_game_id=str(item.get("signal_game_id") or ""),
        )
        logger.info(
            f"[MANUAL-ASSIST] re-arm NOW ({reason}) table={tname} side={side} amount=${amount:.2f}"
        )

    def _rearm_pinned_box(self) -> None:
        """HOLD固定中、ハンド終了後も同卓に赤/青枠を再描画して保持する。"""
        pl = self._pinned_lock or {}
        tid = str(pl.get("table_id") or "")
        if not tid:
            return
        self._start_now_lock(
            decision_id=str(pl.get("decision_id") or ""),
            table_id=tid,
            table_name=str(pl.get("table_name") or tid),
            side=str(pl.get("side") or ""),
            match_table_id=tid,
        )

    def _handle_hold_command(self, want: bool, *, item: dict | None = None, key: str = "") -> None:
        """機能①: GUIのHOLDボタン。固定中はその卓に留まり他卓のシグナルでスクロールしない。
        解除はボタン再押下のみ（自動解除なし）。"""
        lock = self._now_lock if isinstance(self._now_lock, dict) else {}
        if want:
            ptid = str(
                (lock.get("table_id") if lock else "")
                or (item or {}).get("qpid")
                or (item or {}).get("table_id")
                or ""
            )
            # NOW(赤/青枠)が無い時は、エンジンが今センタリングしている黄色枠(予告)の
            # 卓を固定対象にする。これで予告段階の卓も任意でHOLDできる。
            fh_name = ""
            if not ptid:
                try:
                    fh = getattr(self.bet_executor, "_assist_focus_hold", {}) or {}
                    ptid = str(fh.get("table_id") or "")
                    fh_name = str(fh.get("table_name") or "")
                except Exception:
                    pass
            if not ptid:
                # 機能①拡張: 枠が無くてもHOLDでスクロール凍結。現在の表示位置で停止し、
                # 他卓の予告が来ても画面を動かさない(卓固定ではなく純粋なスクロール停止)。
                self._scroll_frozen = True
                self._pinned = False
                self._pinned_lock = {}
                try:
                    fz = getattr(self.bet_executor, "freeze_scroll", None)
                    if callable(fz):
                        fz(True)
                except Exception as ex:
                    logger.debug(f"[HOLD] freeze_scroll(True) failed: {ex}")
                logger.info("[MANUAL-ASSIST] HOLD scroll-freeze ON (no active/centered table)")
                try:
                    send_msg({
                        "type": "manual_assist_pin", "pinned": True, "table_id": "",
                        "scroll_frozen": True, "ts": time.time(),
                    })
                except Exception:
                    pass
                return
            self._pinned = True
            self._pinned_lock = {
                "table_id": ptid,
                "qpid": ptid,
                "table_name": str(
                    (item or {}).get("table_name")
                    or (lock.get("table_name") if lock else "")
                    or fh_name
                    or ptid
                ),
                "side": str((item or {}).get("side") or (lock.get("side") if lock else "") or ""),
                "decision_id": str(
                    (lock.get("decision_id") if lock else "") or (item or {}).get("decision_id") or ""
                ),
                "item_key": str(key or (item or {}).get("id") or ""),
            }
            if lock:
                self._now_lock["pinned"] = True
                self._now_lock["until"] = time.time() + 36000.0
            try:
                pin_fn = getattr(self.bet_executor, "set_pinned_table", None)
                if callable(pin_fn) and ptid:
                    pin_fn(ptid)
            except Exception as ex:
                logger.debug(f"[HOLD] executor set_pinned_table failed: {ex}")
            # 機能①拡張: HOLDは枠の有無に関わらず常に画面を停止する。卓固定(PIN)中も
            # スクロール凍結をONにして、固定卓の再センタリング(=スクロールに見える)も止める。
            self._scroll_frozen = True
            try:
                fz = getattr(self.bet_executor, "freeze_scroll", None)
                if callable(fz):
                    fz(True)
            except Exception as ex:
                logger.debug(f"[HOLD] freeze_scroll(True) failed: {ex}")
            if item:
                self._rearm_manual_now(key, item, reason="pin_on")
            logger.info(f"[MANUAL-ASSIST] HOLD pin+freeze ON table={ptid or '-'}")
        else:
            ptid = str((self._pinned_lock or {}).get("table_id") or "")
            pdid = str(
                (self._pinned_lock or {}).get("decision_id")
                or (lock.get("decision_id") if lock else "")
                or ""
            )
            self._pinned = False
            self._pinned_lock = {}
            # 機能①拡張: スクロール凍結も解除して通常スキャンへ復帰。
            was_frozen = self._scroll_frozen
            self._scroll_frozen = False
            try:
                fz = getattr(self.bet_executor, "freeze_scroll", None)
                if callable(fz):
                    fz(False)
            except Exception:
                pass
            try:
                pin_fn = getattr(self.bet_executor, "clear_pinned_table", None)
                if callable(pin_fn):
                    pin_fn()
            except Exception as ex:
                logger.debug(f"[HOLD] executor clear_pinned_table failed: {ex}")
            self._release_now_lock(decision_id=pdid, reason="unpin")
            logger.info(
                f"[MANUAL-ASSIST] HOLD {'scroll-freeze OFF' if was_frozen else 'pin OFF'} table={ptid or '-'}"
            )
        try:
            send_msg({
                "type": "manual_assist_pin",
                "pinned": bool(self._pinned),
                "table_id": str((self._pinned_lock or {}).get("table_id") or ""),
                "ts": time.time(),
            })
        except Exception:
            pass

    def _expire_manual_assist_ready(
        self,
        *,
        reason: str,
        table_id: str = "",
        qpid: str = "",
        keep_table_id: str = "",
        keep_qpid: str = "",
    ) -> int:
        """Expire READY assist items when the pre-alert candidate collapses."""
        if not self.manual_assist:
            return 0
        target_table = str(table_id or "").strip()
        target_qpid = str(qpid or "").strip()
        keep_table = str(keep_table_id or "").strip()
        keep_target_qpid = str(keep_qpid or "").strip()
        expired = 0
        for key, item in list(self._manual_assist_items.items()):
            try:
                if str(item.get("status") or "").upper() != "READY":
                    continue
                item_table = str(item.get("table_id") or "").strip()
                item_qpid = str(item.get("qpid") or "").strip()
                if keep_table and item_table == keep_table:
                    continue
                if keep_target_qpid and item_qpid == keep_target_qpid:
                    continue
                if target_table or target_qpid:
                    if target_table and item_table != target_table:
                        continue
                    if target_qpid and item_qpid != target_qpid:
                        continue
                self._send_manual_assist_item(
                    status="EXPIRED",
                    table_id=item_table,
                    table_name=str(item.get("table_name") or ""),
                    qpid=item_qpid,
                    side=str(item.get("side") or ""),
                    amount=float(item.get("amount") or 0.0),
                    pattern_key=str(item.get("pattern_key") or ""),
                    decision_id=str(item.get("decision_id") or ""),
                    item_id=key,
                    signal_game_id=str(item.get("signal_game_id") or ""),
                    score=item.get("score"),
                    steps_before=item.get("steps_before"),
                    source=str(item.get("source") or ""),
                    expires_sec=0.0,
                )
                clear_fn = getattr(self.bet_executor, "clear_manual_assist_overlay", None)
                if callable(clear_fn):
                    try:
                        clear_fn(item_qpid or item_table)
                    except Exception as ex:
                        logger.debug(f"[MANUAL-ASSIST] overlay clear failed target={item_qpid or item_table}: {ex}")
                expired += 1
            except Exception as ex:
                logger.debug(f"[MANUAL-ASSIST] READY expire failed id={key}: {ex}")
        if expired:
            logger.info(f"[MANUAL-ASSIST] READY expired count={expired} reason={reason}")
        return expired

    def start_manual_assist_command_reader(self) -> None:
        """Read GUI manual-assist commands from stdin without touching auto-bet."""
        if self._manual_command_reader_started:
            return
        self._manual_command_reader_started = True

        def _loop() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                except Exception:
                    return
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "manual_assist_command":
                    self._handle_manual_assist_command(msg)
                elif isinstance(msg, dict) and msg.get("type") == "set_dual_mode":
                    self._set_dual_mode(str(msg.get("mode") or ""))
                elif isinstance(msg, dict) and msg.get("type") == "safety_mode":
                    self._set_safety_mode(bool(msg.get("enabled")))
                elif isinstance(msg, dict) and msg.get("type") == "set_reverse":
                    self._set_reverse(bool(msg.get("on")))

        threading.Thread(target=_loop, name="manual-assist-stdin", daemon=True).start()

    def _find_manual_assist_item(self, item_id: str = "", decision_id: str = "") -> tuple[str, dict] | tuple[str, None]:
        if item_id and item_id in self._manual_assist_items:
            return item_id, self._manual_assist_items[item_id]
        if decision_id:
            for key, item in self._manual_assist_items.items():
                if str(item.get("decision_id") or "") == decision_id:
                    return key, item
        return "", None

    def _set_dual_mode(self, mode: str) -> None:
        """GUI から dual-line モード(v3=6 / v4=10)を切替。既定 v3。
        v4 は forward 検証後に使う想定（既定 v3 なので未検証 v4 は暴発しない）。"""
        m = (mode or "").strip().lower()
        if m not in ("v3", "v4"):
            logger.warning(f"[DUAL-MODE] invalid mode ignored: {mode!r}")
            return
        if m == getattr(self, "dual_mode", "v3"):
            return
        self.dual_mode = m
        n = len(V4_PATTERNS) if m == "v4" else len(V2_PATTERNS)
        logger.info(f"[DUAL-MODE] switched to {m} ({n} patterns)")
        try:
            send_log(f"デュアルライン モード切替: {m} ({n}パターン)")
            _send_telegram(f"⚙️ モード切替: {m.upper()} ({n}パターン)")
        except Exception:
            pass

    def _set_safety_mode(self, enabled: bool) -> None:
        """GUI から安全モード ON/OFF を即時切替(再起動不要)。ON=当日累計勝率ゲート適用。"""
        prev = bool(getattr(self, "_safety_enabled", False))
        self._safety_enabled = bool(enabled)
        if prev != self._safety_enabled:
            logger.info(
                f"[SAFETY-MODE] {'ENABLED' if self._safety_enabled else 'DISABLED'} "
                f"{self._safety_wr_str()}"
            )
            try:
                _send_telegram(f"🛡 安全モード: {'ON' if self._safety_enabled else 'OFF'}")
            except Exception:
                pass
        # トグル直後に GUI 表示を即更新(20s ポーラを待たない)。
        self._send_safety_status()

    def _maybe_reverse(self, side: str) -> str:
        """逆張りON時のみ side を B<->P 反転して返す(T/不正値はそのまま)。
        正味NOW(VPS/ローカル/DGA)の入口で1回だけ呼ぶ。place_bet 等の低レベル経路や
        追従/TIEプッシュの再BET側には適用しない(二重反転を避けるため)。"""
        if not getattr(self, "_reverse_bet", False):
            return side
        s = str(side or "").upper()
        return {"B": "P", "P": "B"}.get(s, side)

    def _set_reverse(self, on: bool) -> None:
        """GUI から逆張り ON/OFF を即時切替(再起動不要)。ON=正味NOWの side を反転。
        ON 中は追従(WIN追跡)を自動停止(_follow_on_settled でゲート)。素の6P/10P勝率を
        見たい時は OFF。★再起動では env を読まず常に OFF=付けっぱなし事故を防ぐ。"""
        prev = bool(getattr(self, "_reverse_bet", False))
        self._reverse_bet = bool(on)
        if prev != self._reverse_bet:
            logger.info(f"[REVERSE] {'ENABLED' if self._reverse_bet else 'DISABLED'}")
            # ON にした瞬間に進行中の追従チェーンは打ち切る(順方向前提のため)。
            if self._reverse_bet:
                try:
                    self._follow_reset(reason="reverse_on")
                except Exception:
                    pass
            try:
                _send_telegram(
                    "🔄 逆張りモード: ON（追従は自動停止）" if self._reverse_bet
                    else "🔄 逆張りモード: OFF"
                )
            except Exception:
                pass
        self._send_reverse_status()

    def _send_reverse_status(self) -> None:
        """GUI へ逆張りの現在状態を通知(ON 中は赤い常時バナーを出すため)。"""
        try:
            send_msg({
                "type": "reverse_status",
                "enabled": bool(getattr(self, "_reverse_bet", False)),
                "ts": time.time(),
            })
        except Exception:
            pass

    @staticmethod
    def _safety_label(key: str) -> str:
        """系統キー → 表示名。"""
        return {"v3": "6P", "v4": "10P", "follow_v3": "6P追従", "follow_v4": "10P追従"}.get(key, key)

    def _safety_selected_key(self) -> str:
        """GUIで選択中の系統キー: 6P=v3 / 10P=v4 / 6P追従=follow_v3 / 10P追従=follow_v4。"""
        base = "v4" if str(getattr(self, "dual_mode", "v3")) == "v4" else "v3"
        return ("follow_" + base) if bool(getattr(self, "_follow_enabled", False)) else base

    def _safety_selected_ok(self) -> bool:
        """選択系統の長期累計勝率ゲート: >=50% で賭ける(True) / <50%(=warn-edge=エッジ劣化)で停止(False)。
        データ無/古い(>300s)はフェイルセーフ=True(賭ける。warn-edge は確証がある時だけ停止)。"""
        if time.time() - float(getattr(self, "_safety_stats_at", 0.0) or 0.0) > 300.0:
            return True
        cur = (getattr(self, "_safety_trend", {}) or {}).get(self._safety_selected_key())
        if cur is None:
            return True
        return float(cur) >= 50.0

    def _safety_wr_str(self) -> str:
        """ログ用: 選択系統名と長期累計勝率・取得経過秒。"""
        key = self._safety_selected_key()
        cur = (getattr(self, "_safety_trend", {}) or {}).get(key)
        age = time.time() - float(getattr(self, "_safety_stats_at", 0.0) or 0.0)
        wr = f"{cur:.1f}%" if cur is not None else "-/-"
        return f"sel={self._safety_label(key)} cumWR={wr} age={age:.0f}s"

    def _send_safety_status(self) -> None:
        """GUI へ安全モードの現在状態を通知(選択系統・長期累計勝率・warn-edgeで停止中か)。
        GUI はこれを受けて ON 中は常時バナー表示(賭け中=緑 / エッジ劣化で停止中=黄)。"""
        try:
            enabled = bool(getattr(self, "_safety_enabled", False))
            key = self._safety_selected_key()
            cur = (getattr(self, "_safety_trend", {}) or {}).get(key)
            fresh = (time.time() - float(getattr(self, "_safety_stats_at", 0.0) or 0.0)) <= 300.0
            ok = self._safety_selected_ok()
            send_msg({
                "type": "safety_status",
                "enabled": enabled,
                # ON かつ warn-edge(選択系統の長期累計<50%)で停止中 = holding。
                "holding": enabled and not ok,
                "system": key,                       # v3/v4/follow_v3/follow_v4
                "label": self._safety_label(key),    # 6P/10P/6P追従/10P追従
                "wr": (float(cur) if cur is not None else None),  # 長期累計勝率%
                "fresh": fresh,                      # 勝率データが取得できているか
                "ts": time.time(),
            })
        except Exception:
            pass

    def _safety_stats_poll_loop(self) -> None:
        """/api/winrate-trend を ~60s 毎に取得して4系統の長期累計勝率(cur)を _safety_trend に格納。
        安全モードの ON/OFF に関わらず常時更新(切替直後から正しい値で判定できるよう)。"""
        def _cur(d):
            try:
                c = (d or {}).get("cur")
                return float(c) if c is not None else None
            except Exception:
                return None
        while True:
            try:
                got = False
                for t in self._decision_poll_targets():
                    base = str(t.get("base_url") or "").rstrip("/")
                    key = str(t.get("api_key") or "").strip()
                    if not base:
                        continue
                    data = self._api_get("/api/winrate-trend", "", base_url=base, api_key=key)
                    if isinstance(data, dict) and (data.get("v3") or data.get("v4")):
                        fol = data.get("follow") or {}
                        self._safety_trend = {
                            "v3": _cur(data.get("v3")),
                            "v4": _cur(data.get("v4")),
                            "follow_v3": _cur(fol.get("v3")),
                            "follow_v4": _cur(fol.get("v4")),
                        }
                        self._safety_stats_at = time.time()
                        got = True
                        break
                if not got:
                    logger.debug("[SAFETY-STATS] no winrate-trend from any target")
            except Exception as e:
                logger.debug(f"[SAFETY-STATS] poll error: {e}")
            # 取得の成否に関わらず GUI へ現在状態を通知(常時バナー表示の鮮度維持)。
            self._send_safety_status()
            time.sleep(60)

    def _handle_manual_assist_command(self, msg: dict) -> None:
        if not self.manual_assist:
            logger.warning("[MANUAL-ASSIST] command ignored: mode disabled")
            return
        action = str(msg.get("action") or "").strip().lower()
        # デュアルラインオート(自動WS BET)中は決済が自動(dga勝者駆動)。手動 WIN/LOSE/TIE は
        # 二重決済になり SEQ/ダランベールが壊れるため拒否する(GUIでも非表示だが保険)。
        # HOLD 等のスクロール制御は許可。
        if action == "result" and getattr(self, "manual_assist_auto_click", False):
            logger.info("[MANUAL-ASSIST] result ignored: auto-bet mode (settlement is automatic)")
            return
        item_id = str(msg.get("id") or "").strip()
        decision_id = str(msg.get("decision_id") or "").strip()
        key, item = self._find_manual_assist_item(item_id=item_id, decision_id=decision_id)
        if action == "hold":
            self._handle_hold_command(bool(msg.get("hold")), item=item, key=key)
            return
        if not item and action == "result":
            key = f"manual-result-{int(time.time() * 1000)}"
            try:
                amount = float(self.money.next_bet() or 0.0)
            except Exception:
                amount = 0.0
            item = {
                "status": "NOW",
                "table_id": "",
                "table_name": "Manual Result",
                "qpid": "",
                "side": str(msg.get("side") or "P").upper(),
                "amount": amount,
                "pattern_key": "manual",
                "decision_id": "",
                "signal_game_id": "",
                "source": "manual_button",
            }
            self._manual_assist_items[key] = dict(item)
            logger.info(
                f"[MANUAL-ASSIST] result fallback item created id={key} "
                f"amount=${amount:.2f}"
            )
        elif not item:
            logger.warning(f"[MANUAL-ASSIST] command ignored: item not found id={item_id or decision_id or '-'}")
            return
        status = str(item.get("status") or "").upper()
        if action == "take":
            if status != "NOW":
                logger.warning(f"[MANUAL-ASSIST] take ignored: status={status} id={key}")
                return
            item["status"] = "TAKEN"
            item["taken_at"] = _utc_now_iso()
            self._send_manual_assist_item(
                status="TAKEN",
                table_id=str(item.get("table_id") or ""),
                table_name=str(item.get("table_name") or ""),
                qpid=str(item.get("qpid") or ""),
                side=str(item.get("side") or ""),
                amount=float(item.get("amount") or 0.0),
                pattern_key=str(item.get("pattern_key") or ""),
                decision_id=str(item.get("decision_id") or ""),
                item_id=key,
                signal_game_id=str(item.get("signal_game_id") or ""),
                expires_sec=120.0,
            )
            logger.info(f"[MANUAL-ASSIST] TAKEN id={key} table={item.get('table_name') or item.get('table_id')}")
            return

        if action != "result":
            logger.warning(f"[MANUAL-ASSIST] unknown command action={action!r}")
            return
        if status == "SETTLED":
            logger.warning(f"[MANUAL-ASSIST] result ignored: already settled id={key}")
            return

        result = str(msg.get("result") or "").strip().upper()
        if result not in ("WIN", "LOSE", "TIE"):
            logger.warning(f"[MANUAL-ASSIST] invalid result={result!r} id={key}")
            return

        side = str(item.get("side") or "P").upper()
        amount = float(item.get("amount") or 0.0)
        if amount <= 0:
            try:
                amount = float(self.money.next_bet() or 0.0)
            except Exception:
                amount = 0.0
        before_pnl = float(getattr(self.money, "session_pnl", 0.0) or 0.0)
        try:
            self.money._last_bet_amount = amount
        except Exception:
            pass
        won = True if result == "WIN" else False if result == "LOSE" else None
        self.money.apply_result(won, side=side)
        try:
            if getattr(self.money, "state_path", None):
                self.money._save_state()
        except Exception:
            pass
        after_pnl = float(getattr(self.money, "session_pnl", 0.0) or 0.0)
        pnl = after_pnl - before_pnl

        if result == "WIN":
            self.wins += 1
        elif result == "LOSE":
            self.losses += 1
        else:
            self.ties += 1
        self.total_resolved += 1
        prediction = "BANKER" if side.startswith("B") else "PLAYER"
        if result == "TIE":
            outcome = "TIE"
        elif result == "WIN":
            outcome = prediction
        else:
            outcome = "PLAYER" if prediction == "BANKER" else "BANKER"

        ms = self.money.status_dict()
        send_msg(
            {
                "type": "resolution",
                "decision_id": str(item.get("decision_id") or item.get("id") or ""),
                "result": result,
                "prediction": prediction,
                "outcome": outcome,
                "table_id": item.get("table_id"),
                "table_name": item.get("table_name") or item.get("table_id"),
                "pattern_key": item.get("pattern_key") or "",
                "bet_amount": amount,
                "pnl": pnl,
                "cumulative_pnl": after_pnl,
                "wins": self.wins,
                "losses": self.losses,
                "ties": self.ties,
                "win_rate": round((self.wins / max(1, self.wins + self.losses)) * 100, 1),
                "money_status": ms,
            }
        )

        # 機能②: TIE は常にプッシュ → 枠を維持して同方向で再BET（SETTLEDにしない）。
        # 機能①: HOLD固定中は WIN/LOSE でも同卓に留まり再BET（解除はボタンのみ）。
        keep_betting = (result == "TIE") or self._is_pinned_item(item)
        if keep_betting:
            self._send_gui_money_status()
            self._rearm_manual_now(
                key, item, reason=("tie_push" if result == "TIE" else "pinned")
            )
            self._save_state()
            logger.info(
                f"[MANUAL-ASSIST] {'TIE push' if result == 'TIE' else 'pinned'} re-bet "
                f"id={key} side={side} next=${float(ms.get('next_bet') or 0.0):.2f}"
            )
            return

        # WIN/LOSE（固定なし）: 通常通り確定 → NOW-lock解除 → SETTLED。
        item["status"] = "SETTLED"
        item["manual_result"] = result
        item["settled_at"] = _utc_now_iso()
        try:
            lock = self._now_lock if isinstance(self._now_lock, dict) else {}
            iid = str(item.get("decision_id") or item.get("id") or "")
            itbl = str(item.get("qpid") or item.get("table_id") or "")
            if lock and (
                (iid and str(lock.get("decision_id") or "") == iid)
                or (itbl and str(lock.get("table_id") or "") == itbl)
            ):
                self._release_now_lock(
                    decision_id=str(lock.get("decision_id") or ""),
                    reason="manual_result",
                )
                logger.info("[MANUAL-ASSIST] NOW-lock released on result")
        except Exception:
            pass
        self._send_manual_assist_item(
            status="SETTLED",
            table_id=str(item.get("table_id") or ""),
            table_name=str(item.get("table_name") or ""),
            qpid=str(item.get("qpid") or ""),
            side=side,
            amount=amount,
            pattern_key=str(item.get("pattern_key") or ""),
            decision_id=str(item.get("decision_id") or ""),
            item_id=key,
            signal_game_id=str(item.get("signal_game_id") or ""),
            expires_sec=0.0,
        )
        self._send_gui_money_status()
        self._save_state()
        logger.info(
            f"[MANUAL-ASSIST] SETTLED id={key} result={result} "
            f"amount=${amount:.2f} pnl=${pnl:+.2f} session=${after_pnl:+.2f}"
        )

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

    def _on_dga_frame(self, payload) -> None:
        """SHADOW (BACOPY_DGA_LOCAL_SIGNAL): build per-table P/B sequences from the
        dga lobby WS gameResult feed and LOG the live signal that WOULD fire
        locally. No bets, no settlement, no shared bot state — a pure, isolated
        validation of local-signal correctness and timing vs the VPS NOW path.
        The existing v3 VPS-decision path is completely untouched.
        """
        try:
            if isinstance(payload, (dict, list)):
                msg = payload
            elif isinstance(payload, (bytes, bytearray)):
                msg = json.loads(payload.decode("utf-8", "replace"))
            else:
                msg = json.loads(payload)
        except Exception:
            return
        frames = msg if isinstance(msg, list) else [msg]
        if not hasattr(self, "_dga_seq"):
            self._dga_seq: dict[str, str] = {}
            self._dga_gids: dict[str, set] = {}
            self._dga_names: dict[str, str] = {}
            self._dga_clean: set[str] = set()  # tables seen shuffle => true shoe-start
            self._dga_rx = 0
            self._dga_grf = 0
            self._dga_added = 0
            self._dga_stat_at = 0.0
            self._dga_qpid: dict[str, str] = {}
            self._dga_mode = os.getenv("BACOPY_DGA_LOCAL_SIGNAL", "").strip().lower()
            # Phase 2b live execution: the dga source runs on a BACKGROUND thread,
            # so it cannot call place_bet (Playwright = main loop only). It enqueues
            # bet/settle requests here; the main loop drains and executes them.
            self._dga_bet_q: deque = deque()     # pending bet requests (bg -> main)
            self._dga_settle_q: deque = deque()  # pending settle requests (bg -> main)
            self._dga_vps_settle_q: deque = deque()  # VPS-NOW confirmed-bet settle (bg->main)
            self._dga_bets: dict[str, dict] = {} # tid -> open bet {bet_id, side, ...}
            self._dga_lock = threading.Lock()
        self._dga_rx += 1
        wl = LIVE_SIGNAL_PATTERNS_V4 if getattr(self, "dual_mode", "v3") == "v4" else LIVE_SIGNAL_PATTERNS
        for m in frames:
            if not isinstance(m, dict):
                continue
            tid = str(m.get("tableId") or "").strip()
            if not tid:
                continue
            nm = m.get("tableName")
            if nm:
                self._dga_names[tid] = str(nm)
            img = m.get("tableImage")
            if img and tid not in self._dga_qpid:
                _mm = re.search(r"/snaps/([^/]+)/", str(img))
                if _mm:
                    self._dga_qpid[tid] = str(_mm.group(1))
            if m.get("shuffle") is True:
                self._dga_seq[tid] = ""
                self._dga_gids[tid] = set()
                # A shuffle marks a TRUE shoe-start.
                self._dga_clean.add(tid)
            # SNAPSHOT-TRUST: the direct dga source opens a FRESH subscribe, whose
            # first per-table frame is the current shoe from hand 1 (the same feed
            # the VPS collector builds full shoes from, and the same basis VPS NOW
            # uses — it does NOT wait for a shuffle). So the first time we see a
            # table we trust its seeded sequence as a correct shoe-start. Without
            # this, only freshly-shuffled (short) tables are "clean" and the long
            # mid-shoe sequences that actually hit patterns get gated out (= the
            # cause of 0 signals observed 2026-06-01). Validated vs VPS NOW in
            # shadow before any real betting.
            if tid not in self._dga_clean and tid not in self._dga_seq:
                self._dga_clean.add(tid)
            gr = m.get("gameResult")
            if not isinstance(gr, list) or not gr:
                continue
            self._dga_grf += 1
            gids = self._dga_gids.setdefault(tid, set())
            added = False
            for h in gr:
                if not isinstance(h, dict):
                    continue
                gid = str(h.get("gameId") or "")
                if not gid or gid in gids:
                    continue
                gids.add(gid)
                c = _winner_to_char(h.get("winner"))
                # Phase 2b settlement: the first NEW result on a table that has an
                # open dga bet IS that bet's outcome. Hand the outcome to the main
                # loop (which owns the money model + executor confirmation check).
                if c in ("P", "B", "T"):
                    with self._dga_lock:
                        open_bet = self._dga_bets.pop(tid, None)
                    if open_bet is not None:
                        self._dga_settle_q.append(
                            {"tid": tid, "outcome": c, "bet": open_bet, "gid": gid}
                        )
                    # VPS-NOW 確定BETの決済: この勝者(gid)を main loop に渡し、
                    # _settle_confirmed_decision_from_hand が gid 厳密一致で決済する。
                    # マルチプレイ/Speed卓で winner が _on_new_hand に届かない問題の解。
                    if getattr(self, "_pending_decisions", None):
                        vq = getattr(self, "_dga_vps_settle_q", None)
                        if vq is not None:
                            vq.append({
                                "tid": tid, "qpid": self._dga_qpid.get(tid, ""),
                                "gid": gid, "outcome": c,
                                "name": self._dga_names.get(tid, ""),
                            })
                # 拾NOW影決済は決済enqueueの「後」で実行(統計のみ・取りこぼし込み)。
                # ★決済/光り/追従のクリティカルパスを絶対に遅らせないため末尾に置く
                #   (2026-06-23: 決済の前に置いていてfollow窓を1つ外す回帰を修正)。
                if c in ("P", "B", "T"):
                    self._resolve_caught_now(
                        tid, c, extra_keys=(self._dga_qpid.get(tid, ""), self._dga_names.get(tid, ""))
                    )
                if c in ("P", "B"):
                    self._dga_seq[tid] = self._dga_seq.get(tid, "") + c
                    added = True
                elif c == "T":
                    added = True
            # 決済専用フィード(自動BETのSEQ進行用): signal計算/dga発注はせず、
            # 勝者→VPS決済(上の enqueue)だけ行う。
            if getattr(self, "_dga_settle_only", False):
                continue
            if not added:
                continue
            self._dga_added += 1
            seq = self._dga_seq.get(tid, "")
            if len(seq) < 3:
                continue
            # SAFETY: never compute a signal on a sequence that started mid-shoe.
            # Only tables seen shuffling this session have a correct shoe-start.
            if tid not in self._dga_clean:
                continue
            try:
                sig = live_signal_for_history(seq, wl)
            except Exception:
                continue
            if sig:
                logger.info(
                    f"[DGA-SIGNAL] tid={tid} name={self._dga_names.get(tid, '?')} "
                    f"qpid={self._dga_qpid.get(tid, '?')} "
                    f"side={sig.get('side')} pattern={sig.get('pattern_key')} "
                    f"seq_len={len(seq)} tail={seq[-12:]}"
                )
                if getattr(self, "_dga_mode", "") in ("livecompare", "live"):
                    try:
                        self._dga_consider_bet(tid, sig)
                    except Exception as _e:
                        logger.debug(f"[DGA-BET] consider error: {_e}")
        _now = time.time()
        if _now - getattr(self, "_dga_stat_at", 0.0) >= 20.0:
            self._dga_stat_at = _now
            logger.info(
                f"[DGA-STAT] frames_rx={self._dga_rx} gr_frames={self._dga_grf} "
                f"results_added={self._dga_added} tables={len(self._dga_seq)} "
                f"clean={len(self._dga_clean)}"
            )

    def _dga_consider_bet(self, tid: str, sig: dict) -> None:
        """Runs on the BACKGROUND dga thread. In 'live' it ENQUEUES the bet for the
        main loop (Playwright owns place_bet + the money model). In 'livecompare'
        it only logs the would-bet. Never touches money or the executor here."""
        name = self._dga_names.get(tid, "")
        qpid = self._dga_qpid.get(tid, "")
        side = str(sig.get("side") or "")
        pattern = str(sig.get("pattern_key") or "")
        if side not in ("P", "B"):
            return
        # 逆張りON: DGA正味NOWの side も反転(pattern は順方向のまま=系統判定用)。
        if getattr(self, "_reverse_bet", False):
            _orig_side = side
            side = self._maybe_reverse(side)
            logger.info(f"[REVERSE] dga side {_orig_side}->{side} pattern={pattern}")
        if _is_unsupported_table_name(f"{name} {qpid} {tid}"):
            return  # not a dual-line betting table (Privé / unsupported)
        # Regular-tables-only toggle (BACOPY_DGA_REGULAR_ONLY=1): skip Speed/Turbo
        # (fast ~13s window) and bet only regular tables (~60s window in existing
        # window logic) where the cold focus + multi-chip click sequence (~19s
        # worst case) fits comfortably. Edge is table-type independent (verified),
        # so this trades signal volume for reliable multi-chip landing. Reuses the
        # existing _is_fast_table_name().
        if os.getenv("BACOPY_DGA_REGULAR_ONLY", "0").strip().lower() in ("1", "true", "on", "yes") \
                and _is_fast_table_name(f"{name} {tid}"):
            logger.info(f"[DGA-BET-Q] skip fast table (regular-only): {name or tid}")
            return
        target = qpid or tid
        mode = getattr(self, "_dga_mode", "")
        if mode == "live":
            # One bet at a time: the single BetManager (SEQ/D'Alembert) progression
            # requires it, and the NOW-lock enforces it at place time. Skip if this
            # table already has an open bet or is already queued.
            with self._dga_lock:
                if tid in self._dga_bets:
                    return
                if any(r.get("tid") == tid for r in self._dga_bet_q):
                    return
                self._dga_bet_q.append({
                    "tid": tid, "qpid": qpid, "side": side, "pattern": pattern,
                    "name": name, "ts": time.time(),
                })
            logger.info(
                f"[DGA-BET-Q] table={name or tid} qpid={target} side={side} pattern={pattern}"
            )
            return
        # livecompare: read-only would-bet log (no money mutation)
        lock = self._active_now_lock()
        lock_note = f"locked({lock.get('table_id') or '-'})" if lock else "free"
        try:
            amount = float(self.money._compute_next_bet())
        except Exception:
            amount = 0.0
        logger.info(
            f"[DGA-WOULD-BET] table={name or tid} qpid={target} side={side} "
            f"amount=${amount:.2f} pattern={pattern} lock={lock_note} mode={mode}"
        )

    def _dga_main_pump(self) -> None:
        """Main-loop (Playwright thread) drain of the dga bet/settle queues. Places
        real bets and settles the money model. Only active in 'live' mode."""
        if getattr(self, "_dga_mode", "") != "live":
            return
        while True:
            try:
                req = self._dga_bet_q.popleft()
            except IndexError:
                break
            try:
                self._dga_place_one(req)
            except Exception as e:
                logger.warning(f"[DGA-BET] place error: {e}")
        while True:
            try:
                s = self._dga_settle_q.popleft()
            except IndexError:
                break
            try:
                self._dga_settle_one(s)
            except Exception as e:
                logger.warning(f"[DGA-SETTLE] error: {e}")

    def _dga_vps_settle_pump(self) -> None:
        """Main-loop drain: dga 勝者で VPS-NOW 確定BETを決済する。dga signal mode に
        依存せず常に走る(=自動WS BET時にマルチプレイ/Speed卓の○×/SEQ/ダランベールが
        進む)。bg(dga)スレッドが enqueue し、ここ(Playwright main loop)で money model を
        触る。"""
        q = getattr(self, "_dga_vps_settle_q", None)
        if not q:
            return
        drained = 0
        while drained < 200:
            try:
                s = q.popleft()
            except IndexError:
                break
            drained += 1
            try:
                self._settle_vps_from_dga(s)
            except Exception as e:
                logger.debug(f"[DGA-VPS-SETTLE] drain error: {e}")

    def _settle_vps_from_dga(self, s: dict) -> None:
        if not self._pending_decisions:
            return
        gid = str(s.get("gid") or "").strip()
        outcome = str(s.get("outcome") or "").strip().upper()
        if outcome not in ("P", "B", "T") or not gid:
            return
        qpid = str(s.get("qpid") or "").strip()
        tid = str(s.get("tid") or "").strip()
        name = str(s.get("name") or "").strip()
        # _decision_matches_hand は confirmed_bet + gid 厳密一致を要求する。確定BET情報が
        # まだ pending に付いていなければ executor の非消費 getter で補完する(タイミング保険)。
        getter = getattr(self.bet_executor, "get_confirmed_bet", None)
        if callable(getter):
            for did, p in list(self._pending_decisions.items()):
                if not isinstance(p, dict) or p.get("settlement_posted"):
                    continue
                if isinstance(p.get("confirmed_bet"), dict) and p.get("confirmed_bet"):
                    continue
                bid = str(p.get("bet_id") or "")
                if not bid:
                    continue
                try:
                    ci = getter(bid)
                except Exception:
                    ci = None
                if isinstance(ci, dict) and ci:
                    p["confirmed_bet"] = ci
        buf = _DgaHandBuf(table_name=name, qpid_table_id=qpid, table_id=tid)
        new_hand = {"winner": outcome, "gameId": gid}
        try:
            settled = self._settle_confirmed_decision_from_hand(qpid or tid, buf, new_hand, outcome)
        except Exception as e:
            logger.warning(f"[DGA-VPS-SETTLE] settle error gid={gid}: {e}")
            return
        if settled:
            logger.info(
                f"[DGA-VPS-SETTLE] settled via dga winner table={name or qpid or tid} "
                f"gid={gid} outcome={outcome}"
            )

    def _dga_place_one(self, req: dict) -> None:
        tid = str(req.get("tid") or "")
        qpid = str(req.get("qpid") or "")
        side = str(req.get("side") or "")
        name = str(req.get("name") or "")
        pattern = str(req.get("pattern") or "")
        target = qpid or tid
        with self._dga_lock:
            if tid in self._dga_bets:
                return
        # One bet at a time (money model + NOW-lock). Hold off while any lock is up.
        lock = self._active_now_lock()
        if lock:
            logger.info(f"[DGA-BET] skip place table={name or target}: NOW-lock held by {lock.get('table_id') or '-'}")
            return
        # Drop a stale signal (don't bet a hand that already resolved).
        age = time.time() - float(req.get("ts") or 0.0)
        max_age = float(os.getenv("BACOPY_DGA_MAX_AGE_SEC", "12") or 12)
        if age > max_age:
            logger.info(f"[DGA-BET] skip place table={name or target}: signal stale age={age:.1f}s")
            return
        # Fast-table warm gate (mode B): a Speed/Turbo table (~13s window) only lands
        # the multi-chip bet if its tile is already prepared (in view). If cold, the
        # scroll + multi-chip clicks exceed the window → skip instead of wasting a
        # miss/partial. Regular tables (~60s window) are always placed. Reuses the
        # executor's _prepared_table_id (the warm tile) + recent betsopen window.
        if os.getenv("BACOPY_DGA_FAST_REQUIRE_WARM", "1").strip().lower() in ("1", "true", "on", "yes") \
                and _is_fast_table_name(f"{name} {tid}"):
            ex = self.bet_executor
            prepared = str(getattr(ex, "_prepared_table_id", "") or "")
            warm = (prepared == target)
            if not warm:
                try:
                    st = (getattr(ex, "_table_states", {}) or {}).get(target) or {}
                    last_open = float(st.get("last_bets_open_at") or 0.0)
                    warm = bool(last_open) and (time.time() - last_open) < float(
                        os.getenv("BACOPY_DGA_FAST_WARM_WINDOW_SEC", "9") or 9
                    )
                except Exception:
                    warm = False
            if not warm:
                logger.info(
                    f"[DGA-BET] skip cold fast table (not warm): {name or target} "
                    f"prepared={prepared or '-'}"
                )
                return
        try:
            amount = float(self.money.next_bet(side=side))
        except Exception:
            amount = 0.0
        if amount <= 0:
            logger.info(f"[DGA-BET] skip place table={name or target}: amount<=0 (limit reached?)")
            return
        self._start_now_lock(decision_id=f"dga_{tid}", table_id=target, table_name=name or target, side=side)
        md = {"table_name": name, "qpid_table_id": qpid}
        bet_id = self.bet_executor.place_bet(target, side, amount, md)
        with self._dga_lock:
            self._dga_bets[tid] = {
                "bet_id": str(bet_id or ""), "side": side, "amount": amount,
                "qpid": target, "name": name, "pattern": pattern, "placed_at": time.time(),
            }
        logger.info(
            f"[DGA-BET-PLACE] table={name or target} qpid={target} side={side} "
            f"amount=${amount:.2f} bet_id={bet_id}"
        )
        # Feed the operator panel so the dga NOW bet shows like a normal signal
        # (dga is the single source of truth for panel + bets in live mode).
        self.total_signals += 1
        self._register_caught_now(tid, target, side=side)  # 拾NOW勝率(取りこぼし込み)
        try:
            self._send_manual_assist_item(
                status="NOW", table_id=tid, table_name=name, qpid=target,
                side=side, amount=amount, pattern_key=pattern,
                decision_id=f"dga_{tid}", source="dga", expires_sec=30.0,
            )
            self._send_gui_money_status()
        except Exception as e:
            logger.debug(f"[DGA-BET] panel update error: {e}")

    def _dga_settle_one(self, s: dict) -> None:
        tid = str(s.get("tid") or "")
        outcome = str(s.get("outcome") or "")
        bet = s.get("bet") or {}
        bet_id = str(bet.get("bet_id") or "")
        side = str(bet.get("side") or "")
        name = str(bet.get("name") or tid)
        # Only settle the money model if the wager actually LANDED (confirmed via
        # the lpbet WS). Settling a bet that never clicked would corrupt SEQ.
        confirmed = None
        consume = getattr(self.bet_executor, "consume_confirmed_bet", None)
        if callable(consume) and bet_id:
            confirmed = consume(bet_id)
        if not confirmed:
            logger.info(
                f"[DGA-SETTLE] table={name} bet_id={bet_id} NOT confirmed (did not land) "
                f"→ no money change; lock released"
            )
            self._release_dga_lock(tid)
            return
        amount = float(bet.get("amount") or 0.0)
        pattern = str(bet.get("pattern") or "")
        if outcome == "T":
            self.money.apply_result(None, side=side)
            res, pnl = "TIE", 0.0
            self.ties += 1
        else:
            won = (outcome == side)
            self.money.apply_result(won, side=side)
            if won:
                res = "WIN"
                pnl = amount * (BANKER_COMMISSION if side.upper() in ("B", "BANKER") else 1.0)
                self.wins += 1
            else:
                res = "LOSE"
                pnl = -amount
                self.losses += 1
        self.total_resolved += 1
        self.virtual_pnl += pnl
        try:
            nxt = float(self.money._compute_next_bet())
        except Exception:
            nxt = 0.0
        logger.info(
            f"[DGA-SETTLE] table={name} side={side} outcome={outcome} {res} "
            f"pnl=${self.money.session_pnl:+.2f} next=${nxt:.2f} "
            f"W/L/T={self.money.total_wins}/{self.money.total_losses}/{self.money.total_ties}"
        )
        # Drive the GUI LIVE FEED (WLT + win/loss flash). app.js matches the queue
        # item by decision_id (dga_<tid>) and flashes green/red on result; the
        # signal panel/SEQ update unconditionally from money_status.
        n_nt = self.wins + self.losses
        wr = self.wins / n_nt * 100 if n_nt else 0.0
        try:
            send_msg({
                "type": "resolution",
                "decision_id": f"dga_{tid}",
                "table_id": tid,
                "table_name": name,
                "prediction": side,
                "outcome": outcome,
                "result": res,
                "pattern_key": pattern,
                "pnl": pnl,
                "cumulative_pnl": self.virtual_pnl,
                "wins": self.wins,
                "losses": self.losses,
                "ties": self.ties,
                "win_rate": round(wr, 1),
                "total_signals": self.total_signals,
                "total_resolved": self.total_resolved,
                "money_status": self.money.status_dict(),
                "bet_amount": amount,
                "planned_bet_amount": amount,
            })
            icon = "✅" if res == "WIN" else ("🔵" if res == "TIE" else "❌")
            send_action(
                f"{icon} {res} {name}: {side}→{outcome} pnl=${pnl:+.2f} "
                f"cum=${self.virtual_pnl:+.2f} ({self.wins}W/{self.losses}L/{self.ties}T {wr:.1f}%)"
            )
        except Exception as e:
            logger.debug(f"[DGA-SETTLE] resolution emit error: {e}")
        try:
            self._send_gui_money_status()
        except Exception:
            pass
        self._save_state()
        self._release_dga_lock(tid)

    def _release_dga_lock(self, tid: str) -> None:
        with self._dga_lock:
            self._dga_bets.pop(tid, None)
        lock = self._now_lock if isinstance(self._now_lock, dict) else {}
        if lock and str(lock.get("decision_id") or "") == f"dga_{tid}":
            self._now_lock = {}
            logger.info(f"[DGA-SETTLE] NOW-lock released (dga_{tid})")

    def _maybe_register_dga_callback(self) -> None:
        """Idempotently register the dga local-signal callback when enabled via
        BACOPY_DGA_LOCAL_SIGNAL. Safe to call from any executor-setup site."""
        if getattr(self, "_dga_cb_registered", False):
            return
        dga_on = os.getenv("BACOPY_DGA_LOCAL_SIGNAL", "").strip().lower() in (
            "1", "shadow", "livecompare", "live", "true", "on",
        )
        # 自動BET(manual_assist_auto_click)時は勝者(gameResult)フィードが
        # マルチプレイ/Speed卓の決済に必須(これらの卓は winner が game-WS の
        # _on_new_hand に届かず、betsopen の hand-end しか取れない)。dga signal を
        # 使わなくても「決済専用」で直結 dga gameResult フィードを起こす。
        settle_feed = bool(
            getattr(self, "manual_assist_auto_click", False)
            or os.getenv("BACOPY_DGA_SETTLE_FEED", "").strip().lower() in ("1", "true", "on", "yes")
        )
        if not (dga_on or settle_feed):
            return
        # settle-only: 勝者→VPS決済のみ。signal計算/dga発注はしない。
        self._dga_settle_only = bool(settle_feed and not dga_on)
        setter = getattr(self.bet_executor, "set_dga_result_callback", None)
        if callable(setter):
            setter(self._on_dga_frame)
            self._dga_cb_registered = True
            logger.info(
                f"[DGA-LOCAL] dga result callback registered "
                f"(settle_only={self._dga_settle_only} dga_on={dga_on})"
            )

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

        # ── Manual-assist: auto-clear the NOW (red/blue) box on hand-end ──
        # This very callback means the locked table just produced a NEW hand
        # result. The signal hand was already consumed before the NOW was
        # issued, so any later new hand on the locked table IS the hand the
        # operator bet on → it just resolved. Clear the visual lock so the view
        # resumes scanning. We do NOT touch the money progression: that stays
        # operator-driven via WIN/LOSE (we never know if they actually wagered).
        # Gated to pure-manual mode (no auto-click) to leave auto/VPS settlement
        # untouched. Matched on the collector table_id; the signal_game_id guard
        # rejects a re-delivery of the signal hand itself.
        if self.manual_assist and not self.manual_assist_auto_click:
            try:
                lock = self._active_now_lock()
                if lock:
                    # now-lock の table_id は両経路とも QPID。VPS 経路は
                    # table_id=qpid、local 経路は table_id=target_id(=qpid)。
                    # _on_new_hand の buf.qpid_table_id と照合する(match_table_id
                    # 方式は VPS 経路で未設定→不成立だった)。collector key の
                    # table_id 引数とも一致し得るので両方を許容する。
                    lock_qpid = str(lock.get("table_id") or "")
                    buf_qpid = str(getattr(buf, "qpid_table_id", "") or "")
                    this_gid = str(new_hand.get("gameId") or new_hand.get("game_id") or "")
                    lk_sgid = str(lock.get("signal_game_id") or "")
                    started = float(lock.get("started_at") or 0.0)
                    same_table = bool(lock_qpid) and (
                        lock_qpid == buf_qpid or lock_qpid == str(table_id)
                    )
                    # started_at>1s: 同一 _on_new_hand 呼び出しで lock 開始と同時に
                    # クリアするのを防ぐ。signal_game_id!=: シグナルハンド自身の
                    # 再配信を弾く保険。
                    if (
                        same_table
                        and this_gid
                        and this_gid != lk_sgid
                        and (time.time() - started) > 1.0
                    ):
                        logger.info(
                            f"[NOW-LOCK] hand-end clear qpid={lock_qpid} "
                            f"gid={this_gid} sig_gid={lk_sgid or '-'} "
                            f"side={lock.get('side') or '-'}"
                        )
                        self._release_now_lock(
                            decision_id=str(lock.get("decision_id") or ""),
                            reason="hand_ended",
                        )
            except Exception as ex:
                logger.debug(f"[NOW-LOCK] hand-end clear failed: {ex}")

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

        # Phase 2b: in dga-live mode the local-signal PLACEMENT path stands down so
        # the same hand is not bet twice (the direct-dga path places the identical
        # signal, earlier). Settlement/resolve above still runs harmlessly (dga bets
        # live in self._dga_bets, not self.pending). VPS path is stopped in
        # _handle_decision; these are the only two place_bet-initiating paths.
        if getattr(self, "_dga_mode", "") == "live" or \
                os.getenv("BACOPY_DGA_LOCAL_SIGNAL", "").strip().lower() == "live":
            return

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

        # 逆張りON: 系統ゲート(pattern_key)通過後に side を反転。以降の bet/caught_now/
        # 決済は反転後 bet_side で一貫する(pattern_key は順方向のまま=表示/系統判定用)。
        if getattr(self, "_reverse_bet", False):
            _orig_bet_side = bet_side
            bet_side = self._maybe_reverse(bet_side)
            logger.info(f"[REVERSE] local-signal side {_orig_bet_side}->{bet_side} pattern={pattern_key}")

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
        ms_probe = self.money.status_dict()
        turns_probe = ms_probe.get("seq7_current_turns") or []
        logger.info(
            f"[AUTO-PROBE] bet plan source=local_signal table={buf.table_name or table_id} "
            f"qpid={str(getattr(buf, 'qpid_table_id', '') or table_id)} side={bet_side} "
            f"planned=${float(bet_amount):.2f} gui_next=${float(ms_probe.get('next_bet') or 0.0):.2f} "
            f"seq_turn={ms_probe.get('seq_turn')} overshoot={ms_probe.get('seq_overshoot')} "
            f"turns={''.join(turns_probe) if isinstance(turns_probe, list) else ''} "
            f"manual_assist={self.manual_assist}"
        )
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
        if self.manual_assist:
            if self._vps_driven_now:
                logger.info(
                    f"[LOCAL-SIGNAL] suppressed local NOW (VPS-driven mode): "
                    f"table={buf.table_name or table_id} side={bet_side} pattern={pattern_key}"
                )
                return
            qpid = str(getattr(buf, "qpid_table_id", "") or "").strip()
            target_id = qpid or table_id
            local_id = f"local-{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[AUTO-PROBE] final_click allowed={str(bool(self.manual_assist_auto_click)).lower()} "
                f"reason={'manual_assist_auto_click' if self.manual_assist_auto_click else 'manual_assist_autoclick_disabled'} "
                f"id={local_id} table={buf.table_name or table_id} "
                f"qpid={target_id or '-'} side={bet_side} planned=${bet_amount:.2f}"
            )
            try:
                focus_fn = getattr(self.bet_executor, "_request_switch", None)
                if callable(focus_fn):
                    focus_fn(
                        table_id,
                        buf.table_name or "",
                        target_id,
                        intent="manual_assist",
                        side=bet_side,
                        preselect_amount=bet_amount,
                    )
                    logger.info(
                        f"[MANUAL-ASSIST] local focus queued id={local_id} "
                        f"table={buf.table_name or table_id} side={bet_side} amount=${bet_amount:.2f}"
                    )
            except Exception as ex:
                logger.warning(f"[MANUAL-ASSIST] local focus queue failed id={local_id}: {ex}")
            self.total_signals += 1
            self._register_caught_now(table_id, target_id, getattr(buf, "qpid_table_id", ""), side=bet_side)  # 拾NOW勝率
            self._send_manual_assist_item(
                status="NOW",
                table_id=table_id,
                table_name=buf.table_name or "",
                qpid=target_id,
                side=bet_side,
                amount=bet_amount,
                pattern_key=pattern_key,
                decision_id=local_id,
                signal_game_id=str(bet_metadata.get("source_last_game_id") or ""),
                source="local",
                expires_sec=30.0,
            )
            send_phase("manual_assist", f"{bet_side} via {pattern_key}")
            send_action(
                f"MANUAL NOW #{self.total_signals} {pattern_key} "
                f"{bet_side} ${bet_amount:.2f} on {buf.table_name or table_id}"
            )
            self._send_gui_money_status()
            self.table_scores[table_id] = 2
            self._save_state()
            if self.manual_assist_auto_click:
                bet_metadata["decision_id"] = local_id
                self._start_now_lock(
                    decision_id=local_id,
                    table_id=table_id,
                    table_name=buf.table_name or "",
                    side=bet_side,
                )
                bet_id = self.bet_executor.place_bet(
                    table_id=table_id,
                    side=bet_side,
                    amount=bet_amount,
                    metadata=bet_metadata,
                )
                self._start_now_lock(
                    decision_id=local_id,
                    table_id=table_id,
                    table_name=buf.table_name or "",
                    side=bet_side,
                    bet_id=str(bet_id or ""),
                )
                self.pending[table_id] = {
                    "side": bet_side,
                    "pattern_key": pattern_key,
                    "china_pattern": d.china_pattern,
                    "china_pred": d.china_pred,
                    "big_pattern": d.big_pattern,
                    "big_pred": d.big_pred,
                    "seq_at_predict": observed_sequence,
                    "table_id": table_id,
                    "table_name": buf.table_name or "",
                    "qpid_table_id": target_id,
                    "predicted_at": _utc_now_iso(),
                    "predicting_n": next_n,
                    "bet_amount": bet_amount,
                    "bet_id": str(bet_id or ""),
                    "decision_id": local_id,
                }
                logger.info(
                    f"[MANUAL-ASSIST] local NOW id={local_id} table={buf.table_name or table_id} "
                    f"side={bet_side} amount=${bet_amount:.2f} auto_bet=enabled bet_id={bet_id or '-'}"
                )
                self._save_state()
                return
            # Manual (no auto-click): hold a NOW lock so (a) the assist overlay is
            # actively maintained + centered on the tile for the hand (not just the
            # 65s passive TTL), and (b) preposition/focus is suppressed so the view
            # does NOT scroll away to other forecasts mid-hand. Released on the
            # operator's WIN/LOSE in _handle_manual_assist_command; auto-expires
            # after BACOPY_NOW_LOCK_MAX_SEC if they walk away.
            self._start_now_lock(
                decision_id=local_id,
                table_id=target_id,
                table_name=buf.table_name or "",
                side=bet_side,
                match_table_id=table_id,
                signal_game_id=str(bet_metadata.get("source_last_game_id") or ""),
            )
            logger.info(
                f"[MANUAL-ASSIST] local NOW id={local_id} table={buf.table_name or table_id} "
                f"side={bet_side} amount=${bet_amount:.2f} auto_bet=disabled "
                f"(NOW-lock held: overlay maintained, no scroll until result)"
            )
            return
        logger.info(
            f"[AUTO-PROBE] final_click allowed=true reason=auto_mode "
            f"table={buf.table_name or table_id} qpid={str(getattr(buf, 'qpid_table_id', '') or table_id)} "
            f"side={bet_side} planned=${bet_amount:.2f}"
        )
        local_decision_id_pre = f"local-{uuid.uuid4().hex[:12]}"
        bet_metadata["decision_id"] = local_decision_id_pre
        self._start_now_lock(
            decision_id=local_decision_id_pre,
            table_id=table_id,
            table_name=buf.table_name or "",
            side=bet_side,
        )
        bet_id = self.bet_executor.place_bet(
            table_id=table_id,
            side=bet_side,
            amount=bet_amount,
            metadata=bet_metadata,
        )
        self._start_now_lock(
            decision_id=local_decision_id_pre,
            table_id=table_id,
            table_name=buf.table_name or "",
            side=bet_side,
            bet_id=str(bet_id or ""),
        )
        decision_id = local_decision_id_pre
        if not self.bet_executor.is_live:
            decision_id = self._publish_live_decision(table_id, buf, bet_side, bet_amount, bet_metadata) or local_decision_id_pre
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
        self._register_caught_now(table_id, getattr(buf, "qpid_table_id", ""), side=bet_side)  # 拾NOW勝率
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
            self._expire_manual_assist_ready(reason="local_score_zero", table_id=table_id)
            return
        if _is_unsupported_table_name(str(buf.table_name or table_id)):
            return
        pattern_keys = [str(x) for x in (preview.get("pattern_keys") or []) if str(x) in V2_PATTERNS]
        if not pattern_keys:
            self._expire_manual_assist_ready(reason="local_pattern_cleared", table_id=table_id)
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
        self._release_now_lock(
            decision_id=str(pending.get("decision_id") or ""),
            table_id=table_id,
            bet_id=bet_id,
            reason="prediction_resolved",
        )

        # LIVE: 実BET送信が確認できないシグナルは資金管理/勝敗に反映しない
        if self.bet_executor.is_live and bet_id:
            confirmed_bet = None
            consume_confirmed = getattr(self.bet_executor, "consume_confirmed_bet", None)
            if callable(consume_confirmed):
                try:
                    confirmed_bet = consume_confirmed(bet_id)
                except Exception:
                    confirmed_bet = None
            if isinstance(confirmed_bet, dict) and confirmed_bet:
                pending["confirmed_bet"] = confirmed_bet
                try:
                    confirmed_amount = float(confirmed_bet.get("confirmed_amount") or 0.0)
                except Exception:
                    confirmed_amount = 0.0
                if confirmed_amount > 0:
                    pending["actual_amount"] = confirmed_amount
                    try:
                        self.money._last_bet_amount = confirmed_amount
                    except Exception:
                        pass
            else:
                consume_sent = getattr(self.bet_executor, "consume_sent_bet", None)
                try:
                    sent = bool(consume_sent(bet_id)) if callable(consume_sent) else False
                except Exception:
                    sent = False
                # Chrome attach 等で trusted_confirm が取れず _failed_bet_ids に入っている
                # 場合でも、lpbet が観測されていれば実BETは成立している。
                # Stake画面で実際にチップが置かれているのに GUI 7-turn / 〇× / WLT が
                # 全く更新されない症状の主因なので、その状況だけ救済する。
                if not sent:
                    failed_info: dict | None = None
                    consume_failed = getattr(self.bet_executor, "consume_failed_bet", None)
                    if callable(consume_failed):
                        try:
                            failed_info = consume_failed(bet_id)
                        except Exception:
                            failed_info = None
                    failed_reason = str((failed_info or {}).get("reason") or "")
                    lpbet_observed = bool((failed_info or {}).get("lpbet_observed"))
                    soft_confirm_ok = (
                        isinstance(failed_info, dict)
                        and (
                            failed_reason
                            in (
                                "trusted_bet_not_confirmed",
                                "lpbet_not_confirmed",
                            )
                        )
                        and (
                            lpbet_observed
                            or _env_bool(
                                "BACOPY_DUAL_SOFT_CONFIRM_ON_LPBET_MISS", False
                            )
                        )
                    )
                    if soft_confirm_ok:
                        logger.warning(
                            f"[SOFT-CONFIRM] resolve continues despite missing trusted confirm: "
                            f"bet_id={bet_id} reason={failed_reason} "
                            f"lpbet_observed={lpbet_observed}"
                        )
                        sent = True
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
        actual_amount = float(pending.get("actual_amount") or pending.get("bet_amount") or 0.0)
        planned_amount = float(pending.get("bet_amount") or actual_amount or 0.0)
        if actual_amount > 0:
            try:
                self.money._last_bet_amount = actual_amount
            except Exception:
                pass

        if outcome == "T":
            self.ties += 1
            pstats["ties"] += 1
            pnl = 0.0
            result = "TIE"
            self.money.apply_result(won=None, side=side)
        elif outcome == side:
            self.wins += 1
            pstats["wins"] += 1
            pnl = actual_amount * (COMMISSION_BANKER if side == "B" else 1.0)
            result = "WIN"
            self.money.apply_result(won=True, side=side)
        else:
            self.losses += 1
            pstats["losses"] += 1
            pnl = -actual_amount
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
            # decision_id を含めないと GUI が manual_assist キュー項目を
            # SETTLED に遷移できない (app.js の resolution ハンドラは
            # r.decision_id で item を照合する)。signal panel/SEQ は
            # money_status から無条件更新されるが、キュー項目の確定には必須。
            "decision_id": decision_id,
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
            "bet_amount": actual_amount,
            "planned_bet_amount": planned_amount,
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
        if src.get("is_follow"):
            return  # 追従BETは合成decision。マスターのdecision行は無いので投げない。
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
        if hand_table_ids and confirmed_table_ids and hand_table_ids.intersection(confirmed_table_ids):
            return True

        # Multi-lobby result feed uses numeric DGA table ids, while the confirmed
        # click bet is keyed by Pragmatic qpid.  Once the exact game id has
        # matched, allow the stable table name to bridge those id namespaces.
        def _norm_name(value: object) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

        hand_name = _norm_name(getattr(buf, "table_name", "") or "")
        confirmed_names = {
            _norm_name(pending.get("table_name") or ""),
            _norm_name(confirmed.get("table_name") or ""),
            _norm_name(detail.get("tableName") or detail.get("table_name") or ""),
        }
        confirmed_names.discard("")
        if hand_gid and confirmed_gid and hand_name and hand_name in confirmed_names:
            logger.info(
                f"[DECISION] match by exact game_id + table_name: "
                f"game={hand_gid} table={getattr(buf, 'table_name', '') or table_id} "
                f"ids={sorted(hand_table_ids)} confirmed_ids={sorted(confirmed_table_ids)}"
            )
            return True
        return False

    # ── デュアルラインオート追従 ────────────────────────────────────────
    def _follow_big_kind(self, pattern_key: str) -> str:
        """大路(pattern_key の真ん中 china|BIG|side)が telecho/dragon なら追従種別を返す。
        それ以外(niconico/nikoichi/sansan...)は '' = 追従しない。"""
        try:
            big = str(pattern_key or "").split("|")[1].strip().lower()
        except Exception:
            return ""
        if big == "telecho":
            return "telecho"   # 逆張り(毎手反対側)
        if big == "dragon":
            return "dragon"    # 順張り(同じ側)
        return ""

    def _follow_compute_next(self, kind: str, last_side: str) -> str:
        last = "B" if str(last_side or "").upper().startswith("B") else "P"
        if kind == "telecho":
            return "P" if last == "B" else "B"   # 逆張り
        return last                              # dragon = 順張り(同じ側)

    def _latest_table_outcome(self, qpid: str) -> str:
        """その卓の最新確定出目('P'/'B'、TIEは遡ってスキップ)。不明なら ''。"""
        try:
            for buf in self.buffers.values():
                if getattr(buf, "qpid_table_id", "") == qpid:
                    for h in reversed(buf.hands or []):
                        c = _winner_to_char(h.get("winner"))
                        if c in ("P", "B"):
                            return c
                    return ""
        except Exception:
            pass
        return ""

    def _resolve_follow_side_at_send(self, bet: dict) -> str:
        """executor が送信直前に呼ぶ: 追従の側を最新出目から再計算して返す(''=変更なし)。
        キュー後にハンドが進む(窓落ちで1ハンドスキップ)と「前ベット結果の逆」は
        テレコでちょうど逆向きになるため、送信時点の最新出目を正とする。"""
        try:
            kind = str(bet.get("follow_kind") or "")
            qpid = str(bet.get("follow_qpid") or bet.get("table_id") or "")
            cur = str(bet.get("side") or "").upper()
            if kind not in ("telecho", "dragon") or not qpid:
                return ""
            latest = self._latest_table_outcome(qpid)
            if latest not in ("P", "B"):
                return ""
            new_side = self._follow_compute_next(kind, latest)
            if new_side == cur:
                return ""
            did = str(bet.get("decision_id") or "")
            pd = self._pending_decisions.get(did)
            if isinstance(pd, dict):
                pd["side"] = new_side
            self._follow_next_side = new_side
            logger.info(
                f"[FOLLOW] side recomputed at send: {cur} -> {new_side} "
                f"(latest={latest} kind={kind} table={qpid})"
            )
            return new_side
        except Exception as ex:
            logger.debug(f"[FOLLOW] side resolver failed: {ex}")
            return ""

    def _follow_reset(self, reason: str = "") -> None:
        if self._follow_active:
            logger.info(f"[FOLLOW] END reason={reason or '-'} chain={self._follow_chain} table={self._follow_table_name or self._follow_table_id}")
        self._follow_active = False
        self._follow_table_id = ""
        self._follow_table_name = ""
        self._follow_kind = ""
        self._follow_next_side = ""
        self._follow_pattern_key = ""
        self._follow_chain = 0
        self._follow_bet_id = ""
        self._follow_did = ""
        self._follow_reason = ""

    def _follow_on_settled(self, *, side: str, result: str, pattern_key: str,
                           qpid: str, table_name: str) -> None:
        """BET決済後フック。
        ・TIE=プッシュ: 全モード共通(オート/オート追従どちらでも・どのパターンでも)。
          同じ側でW/Lが出るまで何度でも再BET。
        ・WIN=追従chase: オート追従 かつ 大路 telecho(逆張り)/dragon(順張り) の時のみ。
          それ以外(平常オート/非対象パターン)はチェーン終了。
        ・LOSE=終了(待機)。"""
        side = "B" if str(side or "").upper().startswith("B") else "P"
        result = str(result or "").upper()
        if not qpid:
            return
        # チェーン中(追従/プッシュ)なら、その卓の決済のみ扱う(別卓は無視)
        if self._follow_active and self._follow_table_id and qpid != self._follow_table_id:
            return

        # ── TIE = プッシュ(全モード共通・同じ側で再BET) ──
        if result == "TIE":
            self._follow_active = True
            self._follow_table_id = qpid
            self._follow_table_name = table_name or qpid
            self._follow_pattern_key = pattern_key
            if not self._follow_kind:
                self._follow_kind = self._follow_big_kind(pattern_key)
            self._follow_next_side = side  # 反転しない・同じ側
            self._follow_reason = f"TIEプッシュ: 同側{side}を再BET"
            logger.info(f"[CHAIN] TIE push table={self._follow_table_name} reBET={side}")
            self._follow_place_next()
            return

        # ── WIN ──
        if result == "WIN":
            kind = self._follow_kind or self._follow_big_kind(pattern_key)
            # 逆張りON中は追従(WIN追跡)を自動停止。追従は順方向(本来side)前提の論理で、
            # 反転BETに追従すると論理破綻するため。GUIの追従選択(_follow_enabled)は保持し、
            # 逆張りOFFで復帰する。TIEプッシュ(上)は実BET側の繰り返しなので反転と一貫=継続。
            if self._follow_enabled and not getattr(self, "_reverse_bet", False) \
                    and kind in ("telecho", "dragon"):
                self._follow_active = True
                self._follow_table_id = qpid
                self._follow_table_name = table_name or qpid
                self._follow_kind = kind
                self._follow_pattern_key = pattern_key
                self._follow_chain += 1
                self._follow_next_side = self._follow_compute_next(kind, side)
                _kind_label = "テレコ継続" if kind == "telecho" else "ドラゴン同側"
                self._follow_reason = (
                    f"追従#{self._follow_chain}: 前結果{side}→{self._follow_next_side}（{_kind_label}）"
                )
                logger.info(
                    f"[FOLLOW] WIN chase kind={kind} table={self._follow_table_name} "
                    f"won={side} next={self._follow_next_side} chain={self._follow_chain}"
                )
                self._follow_place_next()
            else:
                # 平常オート or 非対象パターンの勝ち → チェーン終了(次のNOW待ち)
                self._follow_reset(reason="win_no_follow")
            return

        # ── LOSE ──
        self._follow_reset(reason="lose")

    def _follow_place_next(self) -> None:
        """追従の次手を同じ卓へ自動BET(合成decisionとして既存の決済経路に乗せる)。"""
        if not self._follow_active or not self._follow_table_id:
            return
        side = self._follow_next_side
        if side not in ("B", "P"):
            self._follow_reset(reason="bad_side"); return
        try:
            amount = float(self.money.next_bet(side=side))
        except Exception:
            amount = 0.0
        if amount <= 0:
            self._follow_reset(reason="amount<=0"); return
        qpid = self._follow_table_id
        name = self._follow_table_name
        did = f"follow_{qpid}_{int(time.time() * 1000)}"
        md = {
            "table_name": name, "qpid_table_id": qpid,
            "pattern_key": self._follow_pattern_key, "decision_id": did, "source": "follow",
            "follow_kind": self._follow_kind,
        }
        self._start_now_lock(decision_id=did, table_id=qpid, table_name=name, side=side)
        try:
            bet_id = self.bet_executor.place_bet(qpid, side, amount, md)
        except Exception as ex:
            logger.warning(f"[FOLLOW] place_bet failed: {ex}")
            self._follow_reset(reason="place_error"); return
        self._start_now_lock(decision_id=did, table_id=qpid, table_name=name, side=side, bet_id=str(bet_id or ""))
        self._pending_decisions[did] = {
            "side": side, "amount": amount, "table_id": qpid, "table_name": name,
            "pattern_key": self._follow_pattern_key, "bet_id": str(bet_id or ""),
            "placed_at": time.time(), "result_posted": False, "settlement_posted": False,
            "bet_sent_posted": False, "local_bet_sent": False, "local_bet_failed": False,
            "bet_sent_notified": False, "source_base_url": "", "source_api_key": "",
            "is_follow": True,
        }
        self._follow_last_bet_at = time.time()
        self._follow_bet_id = str(bet_id or "")
        self._follow_did = did
        self.total_signals += 1
        self._register_caught_now(qpid, name, side=side, is_follow=True)  # 拾NOW率(追従)
        try:
            self._send_manual_assist_item(
                status="NOW", table_id=qpid, table_name=name, qpid=qpid, side=side,
                amount=amount, pattern_key=self._follow_pattern_key,
                decision_id=did, source="follow", expires_sec=30.0,
                follow_reason=self._follow_reason,
            )
            self._send_gui_money_status()
        except Exception:
            pass
        logger.info(
            f"[FOLLOW] place next side={side} amount=${amount:.2f} table={name} "
            f"chain={self._follow_chain} did={did} bet_id={bet_id or '-'}"
        )

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

        # ── 幻決済ガード(2026-06-12): lpbet_fast(自分の送信エコーのみ)で確証した
        # BETは、決済前にサーバ受理の後追い証拠(GAME-BET-CONFIRM/残高変動)を要求。
        # 証拠が無ければ実際には賭かっていない(ターボ卓の短窓サイレントドロップ)
        # ため、決済せず error 破棄して SEQ/PnL/追従を進めない。
        _sv_check = getattr(self.bet_executor, "bet_server_validated", None)
        if bet_id and callable(_sv_check):
            try:
                _sv_ok = bool(_sv_check(bet_id))
            except Exception:
                _sv_ok = True  # fail-open: ガード故障で実BETを落とさない
            # hh88: 受理確証 {"win":{nwb}} は決済とほぼ同時に届くため、ドロップ判定前に
            # 短時間だけ win 到着を待つ(レース回避)。win 照会は pop しないので安全。
            if (not _sv_ok) and (os.getenv("BACOPY_PLATFORM", "").strip().lower() == "hh88"):
                _win_check = getattr(self.bet_executor, "has_win_for_game", None)
                _gid = str(pending.get("game_id") or "")
                if _gid and callable(_win_check):
                    for _ in range(8):  # 最大 ~4s
                        try:
                            if bool(_win_check(_gid)):
                                _sv_ok = True
                                logger.info(f"[PHANTOM-GUARD] hh88 win arrived during grace: game={_gid}")
                                break
                        except Exception:
                            _sv_ok = True
                            break
                        time.sleep(0.5)
            if not _sv_ok:
                _pg_table = str(pending.get("table_name") or getattr(buf, "table_name", "") or table_id)
                _pg_amount = float(pending.get("amount") or 0.0)
                logger.warning(
                    f"[PHANTOM-GUARD] drop unconfirmed bet at settle: did={did} "
                    f"bet_id={bet_id} table={_pg_table} side={side} amount=${_pg_amount:.2f}"
                )
                pending["local_bet_failed"] = True
                pending["settlement_posted"] = True
                try:
                    self._api_post(
                        f"/api/decisions/{did}/result",
                        {
                            "result": {
                                "error": "phantom_no_server_confirm",
                                "phase": "bet_unconfirmed",
                                "table_id": str(pending.get("table_id") or table_id or ""),
                                "table_name": _pg_table,
                                "bet": {"amount": _pg_amount, "side": side, "bet_id": bet_id},
                            },
                            "status": "error",
                        },
                        base_url=str(pending.get("source_base_url") or ""),
                        api_key=str(pending.get("source_api_key") or ""),
                    )
                except Exception:
                    pass
                try:
                    if hasattr(self.bet_executor, "consume_sent_bet"):
                        self.bet_executor.consume_sent_bet(bet_id)
                except Exception:
                    pass
                self._release_now_lock(
                    decision_id=did,
                    table_id=str(pending.get("table_id") or table_id or ""),
                    bet_id=bet_id,
                    reason="phantom_bet",
                )
                self._pending_decisions.pop(did, None)
                self.pending.pop(str(pending.get("table_id") or ""), None)
                self.pending.pop(str(table_id or ""), None)
                self._follow_reset(reason="phantom_bet")
                _send_telegram(
                    f"⚠️ BET未受理を検出(幻決済ガード)\n{_pg_table}\n"
                    f"{side} ${_pg_amount:.2f}\nSEQ/PnLは進めません"
                )
                send_action(f"⚠️ PHANTOM-GUARD {_pg_table}: {side} ${_pg_amount:.2f} not accepted — dropped")
                return True

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
        self._release_now_lock(
            decision_id=did,
            table_id=str(pending.get("table_id") or table_id or ""),
            bet_id=bet_id,
            reason="decision_settled",
        )

        table_name = str(pending.get("table_name") or getattr(buf, "table_name", "") or table_id)
        # 課金ガード: 実際に賭けて決済した卓名を登録(以後この卓のベット履歴のみ課金)。
        try:
            _ng = self._billing_norm_game(table_name)
            if _ng:
                self._billing_my_games.add(_ng)
        except Exception:
            pass
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
                "type": "resolution",
                "decision_id": str(pending.get("decision_id") or ""),
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
                "planned_bet_amount": planned_amount,
            }
        )
        self._send_gui_money_status()
        send_action(
            f"{icon} {result} {table_name}: {side}→{outcome} "
            f"pnl=${pnl_delta:+.2f} cum=${self.virtual_pnl:+.2f}"
        )
        # 追従(BACOPY_DUAL_FOLLOW): 勝てば同卓で次手、TIEはプッシュ、負ければ終了。
        try:
            self._follow_on_settled(
                side=side, result=result,
                pattern_key=str(pending.get("pattern_key") or ""),
                qpid=str(pending.get("table_id") or table_id or ""),
                table_name=table_name,
            )
        except Exception as ex:
            logger.debug(f"[FOLLOW] on_settled error: {ex}")
        # 機能(A): 決済が確定した瞬間に NOW-LOCK/黄色枠も解除する。従来は枠の解除が
        # betsopen hand-end(=次ハンド開始≒26秒後)頼みで、しかも auto_click 時は
        # _on_executor_hand_end が pure-manual 限定で発火せず、枠が長く残っていた。
        # ここで解除すれば「枠が残る→消えてから光る」が解消し、枠解除と勝敗フラッシュ
        # が同時になる。WIN→追従で _follow_place_next が既に新しい NOW-LOCK を張って
        # いる場合は、_release_now_lock が decision_id 不一致で no-op になる(=追従の
        # 枠は誤って消さない)ので安全。
        try:
            self._release_now_lock(
                decision_id=str(pending.get("decision_id") or ""),
                table_id=str(pending.get("table_id") or table_id or ""),
                reason="settled",
            )
        except Exception as ex:
            logger.debug(f"[NOW-LOCK] settle-time release failed: {ex}")
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

    # ── 課金: Stake「ベット履歴」DOMから realized PnL を集計 (Step 1: log-only) ──
    # 口座の全バカラBETの純損益 net=stake*(mult-1) を UUID 重複排除で日次集計。
    # WIN/LOSEボタン非依存・入出金除外。後で daily_pnl を /api/session-state へ送る。
    @staticmethod
    def _billing_num(s):
        try:
            m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(",", ""))
            return float(m.group(0)) if m else None
        except Exception:
            return None

    @staticmethod
    def _billing_is_baccarat(game) -> bool:
        g = str(game or "")
        return ("baccarat" in g.lower()) or ("バカラ" in g)

    @staticmethod
    def _billing_norm_game(name) -> str:
        """卓名を照合用に正規化(小文字・空白/記号除去)。ベット履歴DOMの表示名と
        bot内部の table_name の軽微な表記揺れを吸収する。"""
        s = str(name or "").lower()
        return re.sub(r"[^a-z0-9぀-ヿ一-鿿]+", "", s)

    @staticmethod
    def _billing_jst_date() -> str:
        # JST = UTC+9; avoid timedelta import by shifting the epoch.
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))

    def _load_billing_state(self) -> None:
        self._billing_seen: set = set()
        self._billing_seen_order: list = []
        self._billing_daily_pnl: float = 0.0
        self._billing_daily_date: str = self._billing_jst_date()
        self._billing_prev_pnl: float = 0.0
        self._billing_prev_date: str = ""
        self._billing_count: int = 0
        self._billing_seeded: bool = False
        # 課金=「自分が実際に賭けた卓」のベットのみ受理する(2026-06-12)。Stakeベット
        # 履歴DOMが「My Bets」でなく全プレイヤーフィードを表示する事故(user05で+$1,871
        # 幻)対策。決済成功時に卓名を追加し、poll はこの集合の卓のみ課金。集合が空の
        # 間(起動直後/未ベット)は従来どおり倍率ガードのみ(後方互換・過小側=安全)。
        self._billing_my_games: set = set()
        # 残高(リアルタイム表示用) — Stake口座WSの捕捉値から取得して送る
        self._billing_currency: str = ""
        self._billing_current_balance = None      # float | None
        self._billing_last_balance_at: float = 0.0
        self._billing_balance_open = None          # float | None (JST日初の残高)
        self._billing_balance_open_date: str = ""
        self._billing_balance_seen: bool = False
        p = getattr(self, "_billing_state_path", None)
        try:
            if p and Path(p).exists():
                d = json.loads(Path(p).read_text(encoding="utf-8"))
                self._billing_seen_order = list(d.get("seen") or [])
                self._billing_seen = set(self._billing_seen_order)
                self._billing_daily_pnl = float(d.get("daily_pnl") or 0.0)
                self._billing_daily_date = str(d.get("daily_date") or self._billing_jst_date())
                self._billing_prev_pnl = float(d.get("prev_pnl") or 0.0)
                self._billing_prev_date = str(d.get("prev_date") or "")
                self._billing_count = int(d.get("count") or 0)
                if d.get("balance_open") is not None:
                    self._billing_balance_open = float(d.get("balance_open"))
                self._billing_balance_open_date = str(d.get("balance_open_date") or "")
                self._billing_seeded = True  # prior state exists → do NOT reseed
                logger.info(
                    f"[BILLING] state loaded: daily_pnl={self._billing_daily_pnl:+.4f} "
                    f"date={self._billing_daily_date} seen={len(self._billing_seen)} n={self._billing_count}"
                )
        except Exception as ex:
            logger.warning(f"[BILLING] state load failed: {ex}")

    def _save_billing_state(self) -> None:
        p = getattr(self, "_billing_state_path", None)
        if not p:
            return
        try:
            Path(p).write_text(json.dumps({
                "seen": self._billing_seen_order[-3000:],
                "daily_pnl": round(self._billing_daily_pnl, 8),
                "daily_date": self._billing_daily_date,
                "prev_pnl": round(self._billing_prev_pnl, 8),
                "prev_date": self._billing_prev_date,
                "count": self._billing_count,
                "balance_open": (round(self._billing_balance_open, 8)
                                 if self._billing_balance_open is not None else None),
                "balance_open_date": self._billing_balance_open_date,
            }), encoding="utf-8")
        except Exception as ex:
            logger.debug(f"[BILLING] state save failed: {ex}")

    def _capture_stake_balance(self) -> None:
        """リアルタイム残高表示用: executor が Stake口座WS(availableBalances)から
        捕捉した残高を読み、current_balance / daily_open(JST日初) を更新する。
        WSが無音(捕捉0)なら何もしない。送信は _sync_billing_session_state。"""
        try:
            bal = None
            cur = "USDT"
            # 1) Pragmatic ゲームフレームの DOM から残高を読む(主経路・確実)。
            #    口座WSは chrome_attach(OOPIF)で残高フレーム不達のため使えない。
            try:
                reader = getattr(self.bet_executor, "read_stake_balance", None)
                if callable(reader):
                    r = reader()
                    if r is not None:
                        bal = float(r)
            except Exception:
                bal = None
            # 2) フォールバック: executor の WS 捕捉(通常 chrome_attach では無音)。
            if bal is None:
                bals = getattr(self.bet_executor, "_stake_balance_by_currency", {}) or {}
                if bals:
                    _c = ""
                    for c in ("USDT", "USD", "USDC"):
                        if c in bals:
                            _c = c
                            break
                    if not _c:
                        _c = max(bals, key=lambda k: float(bals.get(k) or 0.0))
                    cur = _c
                    bal = float(bals.get(_c) or 0.0)
            if bal is None:
                return
            self._billing_currency = cur
            self._billing_current_balance = bal
            self._billing_last_balance_at = time.time()
            # Kelly(比例)モード: ライブ残高を money に渡す(bet = f × 残高)。
            try:
                self.money.set_bankroll(bal)
            except Exception:
                pass
            today = self._billing_jst_date()
            if self._billing_balance_open_date != today or self._billing_balance_open is None:
                self._billing_balance_open_date = today
                self._billing_balance_open = bal
            if not self._billing_balance_seen:
                self._billing_balance_seen = True
                logger.info(f"[BILLING-BAL] first balance captured {cur}={bal:.4f}")
        except Exception as ex:
            logger.debug(f"[BILLING-BAL] capture failed: {ex}")

    def _poll_billing_hh88(self) -> None:
        """hh88: PnL を win.nwb(実HKD決済額)の合計で算出し、実FX(HKD/USD)で USD へ換算して
        daily_pnl($) を出す。GUI/bafather.uk/ユーザーページを Stake と同じ $ 建てで統一する。
        Stake用ベット履歴DOM/残高は hh88で使わない。executor が自BETの win.nwb を積む。"""
        try:
            _fx = float(os.getenv("BACOPY_FX_HKD_USD", "7.8") or 7.8) or 7.8
            today = self._billing_jst_date()
            if self._billing_daily_date and today != self._billing_daily_date:
                self._billing_prev_pnl = self._billing_daily_pnl
                self._billing_prev_date = self._billing_daily_date
                self._billing_daily_pnl = 0.0
                self._billing_daily_date = today
                logger.info(f"[BILLING] JST rollover (hh88) -> {today} prev_pnl=${self._billing_prev_pnl:+.2f}")
            if not self._billing_daily_date:
                self._billing_daily_date = today
            _popf = getattr(self.bet_executor, "pop_hh88_nwb_delta", None)
            _delta_hkd = float(_popf() or 0.0) if callable(_popf) else 0.0
            if _delta_hkd != 0.0:
                _delta_usd = _delta_hkd / _fx  # HKD → USD($)
                self._billing_daily_pnl += _delta_usd
                self._billing_count += 1
                logger.info(
                    f"[BILLING-HH88] nwb +={_delta_hkd:+.2f} HKD (={_delta_usd:+.4f}$ @fx{_fx}) "
                    f"-> daily_pnl=${self._billing_daily_pnl:+.4f} n={self._billing_count}"
                )
                self._save_billing_state()
            # Stake と同じ通貨ラベルにし、bafather.uk/ユーザーページが $ 建てで表示する。
            self._billing_currency = "USDT"
        except Exception as ex:
            logger.debug(f"[BILLING-HH88] poll error: {ex}")
        # GUI DAILY TOTAL: hh88 は別通貨残高を信頼できないので balance=None。
        # pnl_only=True で「残高差分でなく daily_pnl($) を DAILY TOTAL に表示」を指示。
        try:
            send_msg({
                "type": "daily_total",
                "daily_pnl": round(float(self._billing_daily_pnl or 0.0), 2),
                "daily_date": str(self._billing_daily_date or ""),
                "balance": None,
                "daily_open_balance": None,
                "currency": "USDT",
                "pnl_only": True,
                "count": int(self._billing_count or 0),
            })
        except Exception:
            pass

    def _poll_bet_history_billing(self) -> None:
        # hh88 は PnL を win.nwb(実HKD決済額)で算出する専用経路へ(Stake用DOM/残高は
        # hh88で読めない/別通貨)。Stake は以降の従来ロジックのまま(無変更)。
        if (os.getenv("BACOPY_PLATFORM", "stake") or "stake").strip().lower() == "hh88":
            self._poll_billing_hh88()
            return
        try:
            self._capture_stake_balance()
            reader = getattr(self.bet_executor, "read_bet_history", None)
            if not callable(reader):
                return
            rows = reader() or []
            if not rows:
                return
            # JST 深夜ロールオーバー
            today = self._billing_jst_date()
            if self._billing_daily_date and today != self._billing_daily_date:
                self._billing_prev_pnl = self._billing_daily_pnl
                self._billing_prev_date = self._billing_daily_date
                self._billing_daily_pnl = 0.0
                self._billing_daily_date = today
                logger.info(
                    f"[BILLING] JST rollover -> {today} (prev {self._billing_prev_date} "
                    f"pnl={self._billing_prev_pnl:+.4f})"
                )
            # 初回のみ: 既存(開始前)のBETを seen に入れて課金対象外にする
            # (開始後の新規BETから課金。連続稼働前提なので実害は初回のみ)。
            if not self._billing_seeded:
                for r in rows:
                    u = str(r.get("uuid") or "")
                    if u and u not in self._billing_seen:
                        self._billing_seen.add(u)
                        self._billing_seen_order.append(u)
                self._billing_seeded = True
                self._save_billing_state()
                logger.info(
                    f"[BILLING] seeded {len(self._billing_seen)} pre-existing bets "
                    f"(not billed); counting NEW bets from now"
                )
                return
            added = 0
            for r in rows:
                u = str(r.get("uuid") or "")
                c = r.get("cells") or []
                if not u or u in self._billing_seen or len(c) < 4:
                    continue
                self._billing_seen.add(u)
                self._billing_seen_order.append(u)
                game = c[0] if len(c) > 0 else ""
                stake = self._billing_num(c[2]) if len(c) > 2 else None
                mult = self._billing_num(c[3]) if len(c) > 3 else None
                if stake is None or mult is None or not self._billing_is_baccarat(game):
                    continue
                # 「自分が賭けた卓」のみ課金(他人フィード混入ガード)。卓名を正規化
                # 照合。集合が空の間は適用しない(後方互換)。賭けてない卓はseen化のみ。
                if self._billing_my_games:
                    if self._billing_norm_game(game) not in self._billing_my_games:
                        continue
                # 倍率サニティガード: バカラ配当倍率は小さい(P/B~2, Tie~9,
                # サイドベット<~30)。これを大きく超える/負の値は、ベット履歴DOMの
                # 誤読(実測 mult=1501.50→+$13504 の幻ベットで daily_bet_pnl が
                # +$14k に膨張し課金/表示を破壊)。誤読は課金しない(uuidはseen済みで
                # 再評価されない)。課金ユーザーの過剰請求も恒久防止。
                if mult > 30.0 or mult < 0.0:
                    logger.warning(
                        f"[BILLING] skip implausible mult={mult} game={game!r} "
                        f"stake={stake} (bet-history DOM parse error?)"
                    )
                    continue
                net = stake * (mult - 1.0)
                self._billing_daily_pnl += net
                self._billing_count += 1
                added += 1
                logger.info(
                    f"[BILLING] new bet game={game!r} stake={stake:.4f} mult={mult:.2f} "
                    f"net={net:+.4f} -> daily_pnl={self._billing_daily_pnl:+.4f} (n={self._billing_count})"
                )
            if added:
                if len(self._billing_seen_order) > 6000:
                    self._billing_seen_order = self._billing_seen_order[-3000:]
                    self._billing_seen = set(self._billing_seen_order)
                self._save_billing_state()
        except Exception as ex:
            logger.debug(f"[BILLING] poll error: {ex}")
        # GUI の DAILY TOTAL に正確な日次PnL(課金=/me/realtime と同値)を送る。
        # renderer のローカル残高差分が不安定で 0 のままになる問題の対策。
        try:
            send_msg({
                "type": "daily_total",
                "daily_pnl": round(float(self._billing_daily_pnl or 0.0), 2),
                "daily_date": str(self._billing_daily_date or ""),
                "balance": self._billing_current_balance,
                # bafather.uk リアルタイム監視(admin/users)と同じ残高差分
                # (current_balance - daily_open.balance)を GUI でも出せるよう
                # 当日始値残高を同梱。/api/session-state の daily_open.balance と同値。
                "daily_open_balance": (round(float(self._billing_balance_open), 4)
                                       if self._billing_balance_open is not None else None),
                "currency": str(self._billing_currency or ""),
                "count": int(self._billing_count or 0),
            })
        except Exception:
            pass

    def _sync_billing_session_state(self) -> None:
        """Step 2: realized daily_bet_pnl を bafather /api/session-state へ POST。
        settle cron の最優先課金ソース(daily_bet_pnl)で、サーバが管理者設定の
        profit_share_rate を適用。email+api_key 未設定なら送らずペイロードをログ
        のみ(配布前/未プロビジョニング機での誤送信防止)。"""
        try:
            state = {
                "daily_bet_pnl": round(float(self._billing_daily_pnl), 4),
                "daily_bet_pnl_date": self._billing_daily_date,
                "prev_daily_bet_pnl": round(float(self._billing_prev_pnl), 4),
                "prev_daily_bet_pnl_date": self._billing_prev_date,
                "total_bets": int(self._billing_count),
                "last_updated_at": _utc_now_iso(),
            }
            # リアルタイム残高(/me/realtime 表示用)。Stake口座WSの捕捉値がある時のみ付与。
            if self._billing_current_balance is not None and self._billing_last_balance_at:
                try:
                    _bal_iso = datetime.fromtimestamp(
                        float(self._billing_last_balance_at), tz=timezone.utc
                    ).isoformat()
                except Exception:
                    _bal_iso = _utc_now_iso()
                state["current_balance"] = round(float(self._billing_current_balance), 4)
                state["last_balance_at"] = _bal_iso
                state["currency"] = self._billing_currency
                if self._billing_balance_open is not None:
                    state["daily_open"] = {
                        "date": self._billing_balance_open_date,
                        "balance": round(float(self._billing_balance_open), 4),
                    }
            # ── フリート監視用 bot_status (admin/users 表示・後方互換の追加)。値は
            # エンジンが既に保持(新規計算なし)。課金フィールドとは別オブジェクトで
            # settle cron に無影響。失敗しても billing 本体に波及させない。
            try:
                ms = self.money.status_dict()
                auto = (not self.manual_assist) or bool(self.manual_assist_auto_click)
                _platform = (os.getenv("BACOPY_PLATFORM", "stake") or "stake").strip().lower()
                _seq_shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "attack").strip().lower() or "attack"
                state["bot_status"] = {
                    "running": True,
                    "mode": "auto" if auto else "manual",
                    "platform": _platform,                        # stake / hh88
                    "dual_mode": getattr(self, "dual_mode", "v3"),  # v3=6パターン / v4=10パターン
                    "seq_shape": _seq_shape,                       # attack / balance / defense
                    "follow": bool(self._follow_enabled),
                    "follow_active": bool(self._follow_active),
                    "follow_chain": int(self._follow_chain or 0),
                    "money_mode": ms.get("mode"),
                    "unit": ms.get("unit"),
                    "next_bet": round(float(self.money.next_bet() or 0.0), 2),
                    "seq_level": ms.get("seq_level"),
                    "seq_turns": ms.get("seq_set_size"),  # 5/7ターン制
                    "seq_overshoot": ms.get("seq_overshoot"),  # 負け越し(推奨指標)
                    "loss_cut": ms.get("loss_cut"),
                    "wins": int(self.wins or 0),
                    "losses": int(self.losses or 0),
                    "win_rate": ms.get("win_rate"),
                    "updated_at": _utc_now_iso(),
                }
            except Exception as _bs_e:
                logger.debug(f"[BILLING-SYNC] bot_status build failed: {_bs_e}")
            email = getattr(self, "_billing_email", "")
            key = getattr(self, "_billing_api_key", "")
            if not email or not key:
                logger.info(f"[BILLING-SYNC] not sent (no email/api_key); payload={json.dumps(state)}")
                return
            import urllib.request as _ur
            url = f"{getattr(self, '_billing_site', 'https://www.bafather.uk').rstrip('/')}/api/session-state"
            body = json.dumps({"email": email, "api_key": key, "session_state": state}).encode("utf-8")
            req = _ur.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "LAPLACE-dualline/1.0"},
                method="POST",
            )
            with _ur.urlopen(req, timeout=10) as resp:
                ok = (getattr(resp, "status", 200) == 200)
            _bal_s = (f"{state.get('currency','')}{state['current_balance']:.4f}"
                      if 'current_balance' in state else "NONE(ws-silent)")
            logger.info(
                f"[BILLING-SYNC] posted daily_bet_pnl={state['daily_bet_pnl']:+.4f} "
                f"date={state['daily_bet_pnl_date']} n={state['total_bets']} "
                f"current_balance={_bal_s} email={email[:6]}... ok={ok}"
            )
        except Exception as ex:
            logger.warning(f"[BILLING-SYNC] post failed: {ex}")

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
                        "caught_wins": self.caught_wins,
                        "caught_losses": self.caught_losses,
                        "caught_ties": self.caught_ties,
                        "caught_now_wins": self.caught_now_wins,
                        "caught_now_losses": self.caught_now_losses,
                        "caught_now_ties": self.caught_now_ties,
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
            self.caught_wins = s.get("caught_wins", 0)
            self.caught_losses = s.get("caught_losses", 0)
            self.caught_ties = s.get("caught_ties", 0)
            self.caught_now_wins = s.get("caught_now_wins", 0)
            self.caught_now_losses = s.get("caught_now_losses", 0)
            self.caught_now_ties = s.get("caught_now_ties", 0)
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
        # VPS-driven NOW(複数受け子 fan-out): 受け子が decision の status を書き換える
        # (/ack や /result) と、その decision が共有 pending から外れ、他の受け子が
        # 取れなくなる(master は単一executor前提)。VPS駆動時は decision のライフサイクル
        # (受付→解決) を配信元 VPS に一任し、受け子側からは一切書き込まない。これにより
        # 全受け子が同じ pending(=テレグラム配信)を取得できる。
        if self._vps_driven_now and "/api/decisions/" in path and (
            path.endswith("/ack") or path.endswith("/result")
        ):
            logger.debug(f"[VPS-DRIVEN] skip decision write {path} (VPS owns lifecycle)")
            return {"ok": True, "skipped_vps_driven": True}
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
        pending_settlement = any(
            isinstance(p, dict)
            and not p.get("settlement_posted")
            and (
                p.get("local_bet_sent")
                or p.get("bet_sent_posted")
                or (isinstance(p.get("confirmed_bet"), dict) and bool(p.get("confirmed_bet")))
            )
            for p in list(getattr(self, "_pending_decisions", {}) or {}).values()
        )
        if not self.no_vps_poll and not pending_settlement:
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
        if not pending_settlement and last_local > 0 and (now - last_local) <= stale_sec:
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
            if last_hand:
                # The sequence string only carries P/B/T.  Preserve the snapshot's
                # exact latest game id so confirmed live bets can settle by hand id.
                last = dict(hands[-1])
                winner = last_hand.get("winner")
                if winner:
                    last["winner"] = winner
                gid = last_hand.get("gameId") or last_hand.get("game_id")
                if gid:
                    last["gameId"] = str(gid)
                hands[-1] = last

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
        """VPS の事前入場指示をポーリング。score=1 遷移時に対象テーブルへ事前入場して待機。

        Phase 2b note: preposition does NOT place bets — it only positions (scroll)
        and shows the yellow forecast overlay. It is KEPT ON in dga-live mode: the
        pre-positioning actually WIDENS the placement window (the candidate tile is
        already in view when its NOW signal fires), which helps multi-chip landing,
        and it restores the operator's normal yellow-box display.
        """
        now_ts = time.time()
        if not self.bet_executor.is_live:
            last = float(getattr(self, "_last_preposition_skip_log_at", 0.0) or 0.0)
            if now_ts - last >= 30.0:
                logger.info("[PREPOS] skip: executor is not live")
                self._last_preposition_skip_log_at = now_ts
            return
        # NOW-lock active → the operator is on the NOW tile (red/blue box) for the
        # current hand. Do NOT scroll/yellow-box other forecasts mid-hand: that
        # would move the view away from the tile being bet. Resume once the lock is
        # released (WIN/LOSE result) or expires.
        if self._active_now_lock():
            last = float(getattr(self, "_last_preposition_skip_log_at", 0.0) or 0.0)
            if now_ts - last >= 30.0:
                logger.info("[PREPOS] skip: NOW-lock active (no scroll mid-hand)")
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
            send_busy = bool(getattr(self.bet_executor, "_bet_send_in_progress", False))
        except Exception:
            send_busy = False
        if has_pb or send_busy:
            last = float(getattr(self, "_last_preposition_skip_log_at", 0.0) or 0.0)
            if now_ts - last >= 15.0:
                try:
                    sent_count = len(getattr(self.bet_executor, "_sent_bet_ids", set()) or set())
                except Exception:
                    sent_count = -1
                logger.info(
                    f"[PREPOS] skip: pending_bet={has_pb} "
                    f"send_busy={send_busy} sent_waiting={sent_count}"
                )
                self._last_preposition_skip_log_at = now_ts
            return

        targets = self._decision_poll_targets()
        if not targets:
            targets = [{"name": "default", "base_url": "", "api_key": ""}]

        data: dict = {}
        src_name = "default"
        saw_stale_candidate = False
        # 選択中モードの予告を要求する。v3(既定)は params 無し=従来と同一リクエスト。
        # v4 のときだけ ?mode=v4 を付け、master が v4(10パターン)で予告計算する。
        active_mode = getattr(self, "dual_mode", "v3")
        active_whitelist = V4_PATTERNS if active_mode == "v4" else V2_PATTERNS
        prepos_params = "mode=v4" if active_mode == "v4" else ""
        for t in targets:
            base = str(t.get("base_url") or "").rstrip("/")
            key = str(t.get("api_key") or "").strip()
            candidate = self._api_get("/api/preposition", params=prepos_params, base_url=base, api_key=key)
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
                pattern_key in active_whitelist for pattern_key in candidate_patterns
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
                self._expire_manual_assist_ready(reason="vps_no_active_preposition")
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
        # Regular-only: when dga betting is restricted to regular tables, don't
        # pre-position / yellow-box Speed/Turbo tables either. Otherwise the yellow
        # box appears on tables dga will never bet (confusing), AND positioning to a
        # Speed tile leaves the next regular NOW cold. Keep preposition on regular
        # tables so it warms the tiles dga actually bets.
        if os.getenv("BACOPY_DGA_REGULAR_ONLY", "0").strip().lower() in ("1", "true", "on", "yes") \
                and _is_fast_table_name(f"{table_name} {table_id}"):
            logger.info(f"[PREPOS] skip fast table (regular-only): {table_name or table_id}")
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
            switch_active = getattr(self.bet_executor, "_active_switch_request", None) or {}
            req_target = str(switch_req.get("qpid") or switch_req.get("table_id") or "").strip()
            req_age = now_retry - float(switch_req.get("requested_at") or 0.0) if switch_req else 999.0
            active_target = str(switch_active.get("qpid") or switch_active.get("table_id") or "").strip()
            last_req_age = now_retry - float(getattr(self, "_prepos_switch_at", 0.0) or 0.0)
            already_handling = (
                (prepared == target)
                or (req_target == target and req_age < 8.0)
                or (active_target == target)
            )
            if already_handling:
                logger.info(f"[PREPOS] dedup skip (same key): {current_key[:80]}")
                return
            logger.info(
                f"[PREPOS] retry same key after focus miss: target={target or '-'} "
                f"last_req_age={last_req_age:.1f}s prepared={prepared or '-'} "
                f"req_target={req_target or '-'} active={active_target or '-'}"
            )
        lock = self._active_now_lock()
        if lock:
            logger.info(
                f"[PREPOS] skip: NOW lock active "
                f"lock_did={str(lock.get('decision_id') or '-')[:12]} "
                f"lock_table={lock.get('table_id') or '-'} incoming={table_id or '-'}"
            )
            return
        self._last_preposition_key = current_key
        logger.info(
            f"[PREPOS] requesting switch → {table_name!r} qpid={qpid!r} score={score} "
            f"direction={direction or '?'} source={src_name}"
        )
        preselect_amount = float(self.money.next_bet() or 0.0)
        steps_before = int(data.get("steps_before") or max(0, 3 - score))
        if self.manual_assist:
            self._expire_manual_assist_ready(
                reason="vps_ready_replaced",
                keep_table_id=table_id,
                keep_qpid=qpid or table_id,
            )
            self._send_manual_assist_item(
                status="READY",
                table_id=table_id,
                table_name=table_name,
                qpid=qpid,
                side=direction,
                amount=preselect_amount,
                pattern_key="/".join(pattern_keys),
                score=score,
                steps_before=steps_before,
                source=src_name,
                expires_sec=90.0,
            )
            logger.info(
                f"[MANUAL-ASSIST] READY table={table_name or table_id} "
                f"qpid={qpid or '-'} side={direction or '?'} amount=${preselect_amount:.2f}"
            )
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
        # Phase 2b: when the local dga signal drives betting ('live'), the VPS NOW
        # path MUST stand down — otherwise the same hand is bet twice. The dga path
        # places the identical signal locally (validated) and earlier.
        if getattr(self, "_dga_mode", "") == "live" or \
                os.getenv("BACOPY_DGA_LOCAL_SIGNAL", "").strip().lower() == "live":
            return
        # 追従中(オート追従)は他卓の新規NOWを無視し、追従卓に専念する。
        if getattr(self, "_follow_active", False):
            logger.info(f"[FOLLOW] skip VPS decision while following table={self._follow_table_name or self._follow_table_id}")
            return
        did = str(decision.get("decision_id") or "")
        fa = decision.get("friend_action") or {}
        if not isinstance(fa, dict):
            return
        side = str(fa.get("side") or "").upper()
        if side not in ("P", "B"):
            logger.warning(f"[DECISION] invalid side={side!r} in {did}")
            return
        # 逆張りON: 正味NOWの side をここで反転。以降の caught_now/NOW-LOCK/place_bet/
        # 決済まで反転後 side で一貫する。pattern_key(=系統ゲート)は元のまま=正味NOWでのみ逆張り。
        if getattr(self, "_reverse_bet", False):
            _orig_side = side
            side = self._maybe_reverse(side)
            logger.info(f"[REVERSE] VPS decision side {_orig_side}->{side} did={did[:12]}")
        pattern_key = str(fa.get("pattern_key") or decision.get("pattern_key") or "")
        # ── モードフィルタ: 選択中モードの decision のみ BET (既定 v3) ──
        # decision の mode 未指定は v3 扱い(後方互換)。選択モードと不一致なら無視。
        # これにより未検証 v4 は GUI が v4 を選ばない限り暴発しない。
        dmode = str(decision.get("mode") or fa.get("mode") or "v3").strip().lower()
        sysname = "v4" if dmode == "v4" else "v3"
        active_mode = getattr(self, "dual_mode", "v3")
        # 選択中モードの decision のみ(安全モードON/OFFに関わらず・選択系統で判定)。
        if dmode != active_mode:
            return
        active_whitelist = V4_PATTERNS if active_mode == "v4" else V2_PATTERNS
        if self.use_v2_filter and pattern_key not in active_whitelist:
            logger.warning(
                f"[DECISION] rejected non-whitelist signal: did={did[:12]} "
                f"mode={active_mode} pattern={pattern_key or '-'}"
            )
            return
        # ── 安全モード(warn-edge): ON かつ 選択系統(6P/6P追従/10P/10P追従)の長期累計
        #    勝率 < 50.0% = エッジ劣化 なら BET 停止。>=50% / データ無/古いは通す。
        #    ★短期の調子(badge)では止めない(先行性ゼロ)。死んだパターンの退役のみ。
        if getattr(self, "_safety_enabled", False) and not self._safety_selected_ok():
            logger.info(
                f"[SAFETY-GATE] skip (warn-edge) did={did[:12]} {self._safety_wr_str()}"
            )
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
                    logger.warning(
                        f"[AUTO-PROBE] final_click allowed=false reason=decision_stale "
                        f"did={did[:12]} age={decision_age:.1f}s max={max_age:.1f}s"
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
            logger.warning(
                f"[AUTO-PROBE] final_click allowed=false reason=unsupported_table "
                f"did={did[:12]} table={table_name or table_id}"
            )
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

        now_lock = self._active_now_lock()
        if now_lock and str(now_lock.get("decision_id") or "") != did:
            lock_table = str(now_lock.get("table_id") or "").strip()
            logger.info(
                f"[DECISION] SKIP: NOW lock active for {did} "
                f"lock_did={str(now_lock.get('decision_id') or '-')[:12]} "
                f"lock_table={lock_table or '-'} incoming_table={table_id or '-'}"
            )
            logger.info(
                f"[AUTO-PROBE] final_click allowed=false reason=now_lock_active "
                f"did={did[:12]} table={table_name or table_id} "
                f"lock_table={lock_table or '-'}"
            )
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "skipped_at": _utc_now_iso(),
                        "reason": "now_lock_active",
                        "lock_decision_id": str(now_lock.get("decision_id") or ""),
                        "lock_table": lock_table,
                    },
                    "status": "skipped_busy",
                },
                base_url=str(decision.get("_source_api_base") or ""),
                api_key=str(decision.get("_source_api_key") or ""),
            )
            return

        # ── Fix B: 同一 decision の二重配信を入口で弾く(idempotent intake) ──────
        # VPS マスターは未消費 decision を poll の度に再配信し得る。1回目で実BETを
        # 置いた後に同じ did が再到達すると executor の _pending_bet を「再武装」し、
        # その時にはシグナルが古く _try_execute_bet が stale 破棄→_mark_bet_failed
        # を登録する。これが「確定済みの本物のBET」を失敗扱いに上書きし、決済も追従も
        # 止める(実機 2026-06-07 のドラゴン不追従の真因)。既に①pending_decisions に
        # 在庫がある or ②同 did の NOW-lock が bet_id 付き(=BET発注済み)なら、再配信は
        # 無視して元のBETの決済に委ねる。bet_id 無しの NOW-lock(=受付のみ未発注)は
        # 従来通り再試行を許す(リトライ動作は維持)。
        if did and did in self._pending_decisions:
            logger.info(f"[DECISION] SKIP duplicate (pending bet exists) for {did[:12]}")
            return
        if now_lock and str(now_lock.get("decision_id") or "") == did and str(now_lock.get("bet_id") or ""):
            logger.info(f"[DECISION] SKIP duplicate (bet already placed via NOW-lock) for {did[:12]}")
            return

        ex = self.bet_executor

        # ── Manual-assist reachability gate ──────────────────────────────────
        # The betting Chrome only carries the ~45 tables of its multiplay channel
        # group. The VPS collector monitors the FULL lobby, so it can issue a NOW
        # for a table that is simply not present in this betting session's grid
        # (has_ws=False / last_bets_open='never'). Such a tile can never be found,
        # centered or bet — the red box flashes then is lost, which is exactly the
        # "couldn't bet" symptom. The 'prepared' flag is NOT trustworthy here (a
        # text-fallback click can mark an absent tile prepared), so gate on the
        # only hard signal: whether this grid ever saw a betsopen for the table.
        # Skip cleanly (no lock, no box) so the panel keeps scanning for a NOW on
        # a table the operator can actually bet.
        if self.manual_assist and getattr(ex, "_multi_lobby_mode", False):
            _st_reach = (getattr(ex, "_table_states", {}) or {}).get(table_id) or {}
            _reach_open_at = float(_st_reach.get("last_bets_open_at") or 0.0)
            if _reach_open_at <= 0.0:
                logger.warning(
                    f"[MANUAL-ASSIST] SKIP unreachable table (not in betting grid, "
                    f"no WS/betsopen): did={did[:12]} table={table_name or table_id} "
                    f"qpid={table_id}"
                )
                self._api_post(
                    f"/api/decisions/{did}/ack",
                    {
                        "ack": {
                            "executor_id": "gui-1",
                            "skipped_at": _utc_now_iso(),
                            "reason": "table_not_in_betting_grid",
                        },
                        "status": "skipped_unreachable",
                    },
                    base_url=str(decision.get("_source_api_base") or ""),
                    api_key=str(decision.get("_source_api_key") or ""),
                )
                return

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

        visible_hold = getattr(ex, "_visible_bet_hold", {}) or {}
        hold_target = str(visible_hold.get("table_id") or "").strip()
        hold_until = float(visible_hold.get("until") or 0.0)
        hold_remaining = hold_until - time.time()
        if hold_target and hold_remaining > 0:
            logger.info(
                f"[DECISION] SKIP: visible result hold active for {did} "
                f"hold_table={hold_target} incoming_table={table_id} remaining={hold_remaining:.1f}s"
            )
            logger.info(
                f"[AUTO-PROBE] final_click allowed=false reason=result_hold_active "
                f"did={did[:12]} table={table_name or table_id} "
                f"hold_table={hold_target} remaining={hold_remaining:.1f}s"
            )
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "skipped_at": _utc_now_iso(),
                        "reason": "result_hold_active",
                        "hold_table": hold_target,
                    },
                    "status": "skipped_busy",
                },
                base_url=str(decision.get("_source_api_base") or ""),
                api_key=str(decision.get("_source_api_key") or ""),
            )
            return

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
            logger.info(
                f"[AUTO-PROBE] final_click allowed=false reason=executor_busy_bet_in_flight "
                f"did={did[:12]} table={table_name or table_id}"
            )
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
                logger.info(
                    f"[AUTO-PROBE] now_probe wait reason=switch_request "
                    f"did={did[:12]} table={table_name or table_id} "
                    f"request_intent={req_intent or '-'} request_target={req_target or '-'}"
                )
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
                logger.info(
                    f"[AUTO-PROBE] now_probe wait reason=switch_in_progress "
                    f"did={did[:12]} table={table_name or table_id} "
                    f"active_intent={active_intent or '-'} active_target={active_target or '-'}"
                )
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
                logger.info(
                    f"[AUTO-PROBE] final_click allowed=false reason=executor_busy_pending_bet "
                    f"did={did[:12]} table={table_name or table_id} "
                    f"pending_table={pending_tid or '-'} pending_age={p_age:.1f}s"
                )
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
            # Speed/Turbo の賭け窓は ~8-15 秒と短い。通常卓の 60s 許容のままだと
            # 「閉じた窓」や「次の(予想外)ハンド」を active 扱いして best-effort BET
            # してしまうため、fast 卓は短い窓許容で active 判定を厳格化する。
            is_fast_table = _is_fast_table_name(table_name) or _is_fast_table_name(table_id)
            if is_fast_table:
                window_max = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC_SPEED", "13") or 13)
            else:
                window_max = float(os.getenv("BACOPY_BET_WINDOW_MAX_SEC", "60") or 60)
            active_window = bool(open_gid and open_gid != closed_gid and open_age < window_max)
            # ── fast 卓の遅延内訳計測 (王道: まず原因を定量化) ──
            # open_age          = 窓open → NOW (botが動いた時点)
            # window_left       = 窓の残り (負 = 到達時に既に締切)
            # vps_publish_to_now= VPS captured_at → NOW (network + bot poll 遅延)
            # open_to_vps_cap   = 窓open → VPS が信号生成 (VPS観測+計算遅延)
            #   → open_to_vps_cap が既に窓超なら VPS 自体が遅い(Lever3/ローカル信号必須)
            #   → vps_publish_to_now が大きいなら bot poll/network がボトルネック
            if is_fast_table:
                try:
                    now_ts = time.time()
                    cap_ts = 0.0
                    if captured_at:
                        try:
                            cap_ts = datetime.fromisoformat(
                                captured_at.replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            cap_ts = 0.0
                    vps_to_now = (now_ts - cap_ts) if cap_ts else -1.0
                    open_to_cap = (cap_ts - last_open_at) if (cap_ts and last_open_at) else -1.0
                    window_left = (window_max - open_age) if last_open_at else -1.0
                    logger.warning(
                        f"[FAST-LATENCY] table={table_name or table_id} did={did[:12]} "
                        f"open_age={open_age:.1f}s window_max={window_max:.0f}s window_left={window_left:.1f}s "
                        f"vps_publish_to_now={vps_to_now:.1f}s open_to_vps_cap={open_to_cap:.1f}s "
                        f"active_window={active_window} prepared_match={prepared == table_id}"
                    )
                except Exception as _lat_ex:
                    logger.debug(f"[FAST-LATENCY] calc failed: {_lat_ex}")
            antenna_ok = False
            try:
                antenna_fn = getattr(ex, "is_table_in_antenna_zone", None)
                if callable(antenna_fn):
                    antenna_ok = bool(antenna_fn(table_id, side))
            except Exception as ant_ex:
                logger.debug(f"[DECISION] antenna check failed: {ant_ex}")
            if prepared != table_id and not active_window and not antenna_ok:
                # Fast(Speed/Turbo)卓は窓が短く、ここで best-effort BET しても
                # 予想ハンドの窓は既に過ぎている。次の窓で賭けると別の(予想して
                # いない)ハンドに賭けることになり戦略が壊れるため、未準備かつ窓
                # 非アクティブなら signal_guard の有無に関わらず必ずスキップする。
                if is_fast_table:
                    logger.warning(
                        f"[DECISION] SKIP fast-table window not active: did={did[:12]} "
                        f"table={table_name or table_id} prepared={prepared or '-'} "
                        f"open_gid={open_gid or '-'} open_age={open_age:.1f}s window_max={window_max:.0f}s"
                    )
                    logger.warning(
                        f"[AUTO-PROBE] final_click allowed=false reason=fast_window_closed "
                        f"did={did[:12]} table={table_name or table_id} "
                        f"open_age={open_age:.1f}s prepared={prepared or '-'}"
                    )
                    self._api_post(
                        f"/api/decisions/{did}/ack",
                        {
                            "ack": {
                                "executor_id": "gui-1",
                                "skipped_at": _utc_now_iso(),
                                "reason": "fast_table_window_closed",
                                "prepared_table_id": prepared,
                                "open_age_sec": round(open_age, 2),
                            },
                            "status": "skipped_not_prepared",
                        },
                        base_url=str(decision.get("_source_api_base") or ""),
                        api_key=str(decision.get("_source_api_key") or ""),
                    )
                    return
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
                    logger.warning(
                        f"[AUTO-PROBE] final_click allowed=false reason=target_not_prepared "
                        f"did={did[:12]} table={table_name or table_id} prepared={prepared or '-'} "
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
        ms_probe = self.money.status_dict()
        turns_probe = ms_probe.get("seq7_current_turns") or []
        logger.info(
            f"[AUTO-PROBE] bet plan source=vps_decision did={did[:12]} "
            f"table={table_name or table_id} qpid={table_id or '-'} side={side} "
            f"planned=${float(bet_amount):.2f} gui_next=${float(ms_probe.get('next_bet') or 0.0):.2f} "
            f"seq_turn={ms_probe.get('seq_turn')} overshoot={ms_probe.get('seq_overshoot')} "
            f"turns={''.join(turns_probe) if isinstance(turns_probe, list) else ''} "
            f"manual_assist={self.manual_assist} manual_assist_auto_click={self.manual_assist_auto_click}"
        )

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
        self._start_now_lock(
            decision_id=did,
            table_id=table_id,
            table_name=table_name,
            side=side,
            signal_game_id=str(metadata.get("signal_game_id") or ""),
        )
        if self.manual_assist:
            logger.info(
                f"[AUTO-PROBE] final_click allowed={str(bool(self.manual_assist_auto_click)).lower()} "
                f"reason={'manual_assist_auto_click' if self.manual_assist_auto_click else 'manual_assist_autoclick_disabled'} "
                f"did={did[:12]} table={table_name or table_id} qpid={table_id or '-'} "
                f"side={side} planned=${bet_amount:.2f}"
            )
            try:
                focus_fn = getattr(self.bet_executor, "_request_switch", None)
                if callable(focus_fn):
                    focus_fn(
                        table_id,
                        table_name,
                        table_id,
                        intent="manual_assist",
                        side=side,
                        preselect_amount=bet_amount,
                    )
                    logger.info(
                        f"[MANUAL-ASSIST] focus queued did={did[:12]} "
                        f"table={table_name or table_id} side={side} amount=${bet_amount:.2f}"
                    )
            except Exception as ex:
                logger.warning(f"[MANUAL-ASSIST] focus queue failed did={did[:12]}: {ex}")
            self.total_signals += 1
            self._register_caught_now(table_id, table_name, side=side)  # 拾NOW勝率(取りこぼし込み)
            self._send_manual_assist_item(
                status="NOW",
                table_id=table_id,
                table_name=table_name,
                qpid=table_id,
                side=side,
                amount=bet_amount,
                pattern_key=pattern_key,
                decision_id=did,
                signal_game_id=str(metadata.get("signal_game_id") or ""),
                source=str(decision.get("_source_name") or "vps"),
                expires_sec=30.0,
            )
            send_phase("manual_assist", f"{side} via VPS")
            send_action(
                f"MANUAL NOW #{self.total_signals} {pattern_key or 'VPS'} "
                f"{side} ${bet_amount:.2f} on {table_name or table_id}"
            )
            self._send_gui_money_status()
            source_base = str(decision.get("_source_api_base") or "")
            source_key = str(decision.get("_source_api_key") or "")
            if self.manual_assist_auto_click:
                # NOW-LOCK を place_bet の前にもう一段固める。
                # 直前の self._start_now_lock(...) は decision 受付時の lock 始動だが、
                # place_bet -> send_bet が同期で _try_execute_bet を発火し得るので、
                # その前に最新の table_id/side で再確定する。
                self._start_now_lock(
                    decision_id=did,
                    table_id=table_id,
                    table_name=table_name,
                    side=side,
                )
                bet_id = self.bet_executor.place_bet(table_id, side, bet_amount, metadata)
                self._start_now_lock(
                    decision_id=did,
                    table_id=table_id,
                    table_name=table_name,
                    side=side,
                    bet_id=str(bet_id or ""),
                )
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
                    "source_base_url": source_base,
                    "source_api_key": source_key,
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
                self._api_post(
                    f"/api/decisions/{did}/ack",
                    {
                        "ack": {
                            "executor_id": "gui-1",
                            "manual_assist_at": _utc_now_iso(),
                            "placed_at": _utc_now_iso(),
                            "auto_bet": True,
                            "bet_id": str(bet_id or ""),
                        },
                        "status": "processing",
                    },
                    base_url=source_base,
                    api_key=source_key,
                )
                logger.info(
                    f"[MANUAL-ASSIST] NOW did={did[:12]} table={table_name or table_id} "
                    f"side={side} amount=${bet_amount:.2f} auto_bet=enabled bet_id={bet_id or '-'}"
                )
                self._save_state()
                return
            self._api_post(
                f"/api/decisions/{did}/ack",
                {
                    "ack": {
                        "executor_id": "gui-1",
                        "manual_assist_at": _utc_now_iso(),
                        "auto_bet": False,
                    },
                    "status": "processing",
                },
                base_url=source_base,
                api_key=source_key,
            )
            logger.info(
                f"[MANUAL-ASSIST] NOW did={did[:12]} table={table_name or table_id} "
                f"side={side} amount=${bet_amount:.2f} auto_bet=disabled"
            )
            return
        logger.info(
            f"[AUTO-PROBE] final_click allowed=true reason=auto_mode "
            f"did={did[:12]} table={table_name or table_id} qpid={table_id or '-'} "
            f"side={side} planned=${bet_amount:.2f}"
        )
        # NOW-LOCK は place_bet の前に張る。place_bet -> send_bet -> _try_execute_bet が
        # bet window が開いている場合 owner thread から同期発火するため、後付けでは
        # executor 側の _request_switch / _perform_switch.auto_fire 等が他卓へ
        # focus を奪う可能性がある。
        self._start_now_lock(
            decision_id=did,
            table_id=table_id,
            table_name=table_name,
            side=side,
        )
        bet_id = self.bet_executor.place_bet(table_id, side, bet_amount, metadata)
        # bet_id を反映するため lock を再設定（既存 lock と同一 decision_id なので上書き OK）
        self._start_now_lock(
            decision_id=did,
            table_id=table_id,
            table_name=table_name,
            side=side,
            bet_id=str(bet_id or ""),
        )

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
        self._register_caught_now(table_id, table_name, side=side)  # 拾NOW勝率(取りこぼし込み)
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

    def _settle_pending_from_master(self) -> None:
        """VPS結果駆動の決済フォールバック (2026-05-31)。

        bafather はSEQをローカルのハンド観測で決済するが、マルチロビーでは賭けた卓の
        結果(winner+game_id)を取りこぼすことがあり、その decision が settlement_timeout
        (900s) まで processing で凍結 → SEQ停止 → 未追跡実BET累積 という事故が起きる。
        一方 VPS は全卓を観測し、_v4_settle_patch で master に status=done + outcome を
        ~30秒で確実に post している。そこで在庫BET(送信済・未決済)の did について master の
        done 結果を読み、VPS の outcome と **ローカルの実着弾額(actual_amount)** で SEQ を
        進める。master への再post はしない (VPS が既に done)。settlement_posted でローカル
        決済経路と二重適用を防ぐ。"""
        if os.getenv("BACOPY_MASTER_SETTLE_ENABLE", "1").strip() == "0":
            return
        if not self._pending_decisions:
            return
        now = time.time()
        poll_sec = float(os.getenv("BACOPY_MASTER_SETTLE_POLL_SEC", "5") or 5)
        if now - float(getattr(self, "_last_master_settle_poll_at", 0.0) or 0.0) < poll_sec:
            return
        self._last_master_settle_poll_at = now
        waiting = [
            (did, p) for did, p in list(self._pending_decisions.items())
            if isinstance(p, dict)
            and not p.get("settlement_posted")
            and not p.get("local_bet_failed")
            and (
                p.get("bet_sent_posted")
                or p.get("local_bet_sent")
                or (isinstance(p.get("confirmed_bet"), dict) and bool(p.get("confirmed_bet")))
            )
        ]
        if not waiting:
            return
        done_map: dict[str, dict] = {}
        seen_sources: set[tuple[str, str]] = set()
        for _did, p in waiting:
            base = str(p.get("source_base_url") or "").rstrip("/")
            key = str(p.get("source_api_key") or "")
            sig = (base, key)
            # 空 base は _api_get が BACOPY_API_URL/master へフォールバックする。
            # スキップせず最低1回は fetch する (source 未設定でも done を読む)。
            if sig in seen_sources:
                continue
            seen_sources.add(sig)
            try:
                data = self._api_get(
                    "/api/decisions", "status=done&limit=80", base_url=base, api_key=key
                )
            except Exception as ex:
                logger.debug(f"[DECISION] master done fetch failed: {ex}")
                continue
            for d in (data.get("decisions") or []):
                if isinstance(d, dict):
                    dd = str(d.get("decision_id") or "")
                    if dd:
                        done_map[dd] = d
        for did, p in waiting:
            rec = done_map.get(did)
            if not isinstance(rec, dict):
                continue
            result_obj = rec.get("result") if isinstance(rec.get("result"), dict) else {}
            outcome = str(result_obj.get("outcome") or "").upper()
            if outcome not in ("P", "B", "T"):
                continue
            try:
                self._apply_master_settlement(did, p, outcome, result_obj)
            except Exception as ex:
                logger.warning(f"[DECISION] master-driven settle failed did={did[:12]}: {ex}")

    def _apply_master_settlement(self, did: str, p: dict, outcome: str, result_obj: dict) -> None:
        """master の done outcome + ローカル actual_amount で SEQ/GUI を決済 (master 再postなし)。"""
        side = str(p.get("side") or "").upper()
        if side not in ("P", "B"):
            return
        bet_id = str(p.get("bet_id") or "")
        confirmed_info = p.get("confirmed_bet") if isinstance(p.get("confirmed_bet"), dict) else {}
        if bet_id and hasattr(self.bet_executor, "consume_confirmed_bet"):
            try:
                consumed = self.bet_executor.consume_confirmed_bet(bet_id)
                if isinstance(consumed, dict) and consumed:
                    confirmed_info = consumed
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
        actual_amount = float(confirmed_amount or p.get("actual_amount") or p.get("amount") or 0.0)
        planned_amount = float(p.get("amount") or actual_amount or 0.0)
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
                mark_resolved(bet_id=bet_id, table_id=str(p.get("table_id") or ""))
            except Exception:
                pass
        self._release_now_lock(
            decision_id=did,
            table_id=str(p.get("table_id") or ""),
            bet_id=bet_id,
            reason="master_settled",
        )
        p["settlement_posted"] = True
        p["actual_amount"] = actual_amount
        self._pending_decisions.pop(did, None)
        self.pending.pop(str(p.get("table_id") or ""), None)
        self._save_state()
        ms = self.money.status_dict()
        table_name = str(p.get("table_name") or p.get("table_id") or "")
        icon = "✅" if result == "WIN" else ("🔵" if tie else "❌")
        logger.info(
            f"[DECISION] master-driven settle: {did} table={table_name} side={side} "
            f"outcome={outcome} result={result} amount=${actual_amount:.2f} (VPS done)"
        )
        try:
            send_msg(
                {
                    "type": "resolution",
                    "decision_id": str(p.get("decision_id") or did),
                    "table_id": str(p.get("table_id") or ""),
                    "table_name": table_name,
                    "prediction": side,
                    "outcome": outcome,
                    "result": result,
                    "pattern_key": str(p.get("pattern_key") or ""),
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
                    "planned_bet_amount": planned_amount,
                }
            )
        except Exception:
            pass
        self._send_gui_money_status()
        try:
            send_action(
                f"{icon} {result} {table_name}: {side}→{outcome} "
                f"pnl=${pnl_delta:+.2f} cum=${self.virtual_pnl:+.2f} (VPS)"
            )
            _send_telegram(
                f"{icon} {result} (VPS決済)\n{table_name}\nPred: {side} → Got: {outcome}\n"
                f"bet: ${actual_amount:.2f}"
                + (f" (planned ${planned_amount:.2f})" if abs(actual_amount - planned_amount) > 0.01 else "")
                + f"\npnl: ${pnl_delta:+.2f} | cum: ${self.virtual_pnl:+.2f}\n"
                f"W/L/T: {self.wins}/{self.losses}/{self.ties}\nnext: ${ms['next_bet']}"
            )
        except Exception:
            pass

    def _flush_pending_decision_results(self) -> None:
        """送信済み/失敗を API に反映して processing 滞留を防ぐ。"""
        if not self._pending_decisions:
            return
        # VPS結果駆動の決済フォールバック: ローカル観測が取りこぼした在庫BETを
        # master の done outcome で決済し、SEQ凍結→未追跡BET累積を防ぐ。
        try:
            self._settle_pending_from_master()
        except Exception as _mse:
            logger.debug(f"[DECISION] master-driven settle sweep error: {_mse}")
        if not self._pending_decisions:
            return
        post_timeout = float(os.getenv("BACOPY_DECISION_POST_TIMEOUT_SEC", "180") or 180)
        settlement_timeout = float(os.getenv("BACOPY_DECISION_SETTLEMENT_TIMEOUT_SEC", "900") or 900)
        now = time.time()
        confirmed_changed = False
        for did, p in list(self._pending_decisions.items()):
            if not isinstance(p, dict) or p.get("settlement_posted"):
                continue
            if p.get("is_follow"):
                continue  # 追従BETは合成decision。マスターへ結果POSTしない(404防止)。
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
            # ── Fix A: 発注/約定が SOFT失敗(stale/skip)に勝つ ────────────────────
            # 二重配信の stale 破棄は、実際に発注され直後に確定する本物のBETと同じ
            # bet_id に "failed" を載せ得る(失敗:13s→確定:15s の順序が実機で発生)。
            # これを適用すると local_bet_failed が立ち決済(=追従)が止まる。失敗理由が
            # SOFT(signal_too_old / bet_skipped*)で、かつ既に確定 or ブックへ送信済み
            # (has_sent_bet)なら、その失敗は無視し決済継続に委ねる。HARD失敗(reject等)
            # と「未送信の正当な skip」は従来通り格下げする。
            if isinstance(failed_info, dict) and failed_info:
                _freason = str(failed_info.get("reason") or "")
                if _freason in ("signal_too_old", "bet_skipped_stale", "bet_skipped", "bet_skip"):
                    _already = bool(confirmed_info) or bool(p.get("confirmed_bet")) or bool(p.get("local_bet_sent"))
                    if not _already and bet_id:
                        try:
                            _hsb = getattr(self.bet_executor, "has_sent_bet", None)
                            if callable(_hsb) and bool(_hsb(bet_id)):
                                _already = True
                        except Exception:
                            pass
                    if _already:
                        logger.warning(
                            f"[DECISION] ignore SOFT failed-bet ({_freason}) for already-sent/confirmed "
                            f"{did[:12]}; settlement continues"
                        )
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
                        partial_release_sec = float(os.getenv("BACOPY_PARTIAL_BET_BIF_RELEASE_SEC", "20") or 20)
                        if (
                            age >= max(1.0, partial_release_sec)
                            and not p.get("executor_bif_released")
                            and isinstance(confirmed_info, dict)
                            and confirmed_info.get("partial_bet")
                        ):
                            released = False
                            try:
                                if bet_id and hasattr(self.bet_executor, "consume_confirmed_bet"):
                                    consumed = self.bet_executor.consume_confirmed_bet(bet_id)
                                    released = bool(consumed)
                                elif bet_id and hasattr(self.bet_executor, "consume_sent_bet"):
                                    released = bool(self.bet_executor.consume_sent_bet(bet_id))
                            except Exception as ex:
                                logger.warning(f"[DECISION] partial BIF release failed: {did} err={ex}")
                            p["executor_bif_released"] = True
                            p["confirmed_bet"] = confirmed_info or {}
                            logger.warning(
                                f"[DECISION] partial bet released executor BIF: {did} "
                                f"released={released} age={age:.1f}s "
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
        # 継続再スキャン: confirmed 済みだが未決済の pending がある限り毎回 buffer を
        # 走査する。結果ハンドは BET の約30秒後に届くため、confirmed_changed が True に
        # なる「BET確認の瞬間」だけ走らせる旧実装では再スキャンが毎回空振りしていた。
        # _decision_matches_hand は game_id 完全一致を要求するので、誤卓・誤ハンドへの
        # 着弾は構造的に起きない (settlement_posted で二重決済もガード済み)。
        has_unsettled_confirmed = any(
            isinstance(p, dict)
            and not p.get("settlement_posted")
            and not p.get("local_bet_failed")
            and (
                p.get("local_bet_sent")
                or p.get("bet_sent_posted")
                or (isinstance(p.get("confirmed_bet"), dict) and p.get("confirmed_bet"))
            )
            for p in self._pending_decisions.values()
        )
        if confirmed_changed or has_unsettled_confirmed:
            self._settle_confirmed_decisions_from_buffers()
        if has_unsettled_confirmed:
            try:
                self._diag_unsettled_pending()
            except Exception as e:
                logger.debug(f"[DIAG-UNSETTLED] scan error: {e}")

    def _diag_unsettled_pending(self) -> None:
        """未決済の confirmed bet が残る理由を診断ログ出力する。

        RC1 (matcher が拒否: gid は buffer にあるのにマッチ失敗) と
        RC2 (結果ハンドがどの buffer にも届いていない) を切り分ける。読み取り専用。
        """
        if not self._pending_decisions:
            return
        now = time.time()
        try:
            min_age = float(os.getenv("BACOPY_DIAG_UNSETTLED_MIN_AGE_SEC", "45") or 45)
            repeat_sec = float(os.getenv("BACOPY_DIAG_UNSETTLED_REPEAT_SEC", "30") or 30)
        except Exception:
            min_age, repeat_sec = 45.0, 30.0
        logged_at = getattr(self, "_diag_unsettled_logged_at", None)
        if logged_at is None:
            logged_at = {}
            self._diag_unsettled_logged_at = logged_at
        for did, p in list(self._pending_decisions.items()):
            if not isinstance(p, dict) or p.get("settlement_posted") or p.get("local_bet_failed"):
                continue
            confirmed = p.get("confirmed_bet") if isinstance(p.get("confirmed_bet"), dict) else {}
            if not (p.get("local_bet_sent") or p.get("bet_sent_posted") or confirmed):
                continue
            age = now - float(p.get("placed_at") or now)
            if age < min_age:
                continue
            if now - float(logged_at.get(did, 0.0)) < repeat_sec:
                continue
            logged_at[did] = now
            confirmed_gid = str(
                (confirmed or {}).get("game_id") or (confirmed or {}).get("lpbet_game_id") or ""
            ).strip()
            p_tid = str(p.get("table_id") or "").strip()
            p_name = str(p.get("table_name") or "").strip()
            name_norm = re.sub(r"[^a-z0-9]+", "", p_name.lower())
            gid_found_in = ""
            bet_buf_info = "NONE"
            for tid, buf in list(getattr(self, "buffers", {}) or {}).items():
                hands = list(getattr(buf, "hands", None) or [])
                gids = [str(h.get("gameId") or "") for h in hands[-40:]]
                if confirmed_gid and confirmed_gid in gids:
                    gid_found_in = f"{tid}({getattr(buf, 'table_name', '')})"
                bnorm = re.sub(r"[^a-z0-9]+", "", str(getattr(buf, "table_name", "") or "").lower())
                qpid = str(getattr(buf, "qpid_table_id", "") or "")
                if (name_norm and bnorm == name_norm) or (p_tid and (tid == p_tid or qpid == p_tid)):
                    bet_buf_info = f"tid={tid} qpid={qpid} hands={len(hands)} last_gids={gids[-3:]}"
            if not confirmed_gid:
                verdict = "NO_CONFIRMED_GID(table_id/name match only)"
            elif gid_found_in:
                verdict = "RC1_matcher_reject(gid IS in buffer but not matched)"
            else:
                verdict = "RC2_result_hand_not_in_any_buffer"
            logger.warning(
                f"[DIAG-UNSETTLED] did={did[:12]} age={age:.0f}s side={p.get('side')} "
                f"table={p_name}({p_tid}) confirmed_gid={confirmed_gid or '-'} "
                f"gid_found_in={gid_found_in or 'NONE'} bet_buf=[{bet_buf_info}] "
                f"verdict={verdict}"
            )

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
                    "type": "resolution",
                    "decision_id": did,
                    "table_id": str(pending.get("table_id") or d.get("table_id") or ""),
                    "table_name": str(pending.get("table_name") or d.get("table_name") or ""),
                    "prediction": str(pending.get("side") or "").upper(),
                    "outcome": str(outcome or ""),
                    "result": normalized_result,
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
                    "planned_bet_amount": float(pending.get("amount") or 0),
                }
            )
            self._send_manual_assist_item(
                status="SETTLED",
                table_id=str(pending.get("table_id") or d.get("table_id") or ""),
                table_name=str(pending.get("table_name") or d.get("table_name") or ""),
                qpid=str(pending.get("table_id") or d.get("table_id") or ""),
                side=str(pending.get("side") or ""),
                amount=actual_amount,
                pattern_key=str(pending.get("pattern_key") or ""),
                decision_id=did,
                item_id=did,
                expires_sec=0.0,
                result=normalized_result,
                pnl=pnl_delta,
            )
            self._send_gui_money_status()

    def _decisions_poll_loop(self) -> None:
        """background thread: VPS API を long-poll して BET decision を受信。"""
        import urllib.request as _ur
        recent_ids: dict[str, float] = {}
        last_target_log_at = 0.0
        last_probe_log_at = 0.0
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
                    decisions = data.get("decisions") or []
                    if (decisions or now - last_probe_log_at >= 30.0):
                        logger.info(
                            f"[AUTO-PROBE] now_probe source=wait target={t.get('name') or 'target'} "
                            f"count={len(decisions) if isinstance(decisions, list) else 0} "
                            f"recent={len(recent_ids)}"
                        )
                        last_probe_log_at = now
                    for d in decisions:
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
            decisions = data.get("decisions") or []
            last_fallback_probe = float(getattr(self, "_last_fallback_probe_log_at", 0.0) or 0.0)
            if decisions or now - last_fallback_probe >= 30.0:
                logger.info(
                    f"[AUTO-PROBE] now_probe source=pending_fallback target={t.get('name') or 'target'} "
                    f"count={len(decisions) if isinstance(decisions, list) else 0} recent={len(recent)}"
                )
                self._last_fallback_probe_log_at = now
            for d in decisions:
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

        # Manual-assist: let the executor tell us when the NOW-locked hand ended
        # (detected via betsopen gameId advance — Speed/multiplay tables never
        # deliver a winner-bearing hand to _on_new_hand). Clears the red/blue
        # overlay only; SEQ stays operator-driven via WIN/LOSE.
        try:
            _rel = getattr(self.bet_executor, "set_now_lock_release_cb", None)
            if callable(_rel):
                _rel(self._on_executor_hand_end)
                logger.info("[NOW-LOCK] executor hand-end release callback registered")
        except Exception as _e:
            logger.debug(f"[NOW-LOCK] release cb registration failed: {_e}")

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

        # 課金: ベット履歴集計の状態ファイル(プロファイル配下) を準備して復元。
        self._billing_state_path = Path(profile) / "dual_line_billing_state.json"
        self._load_billing_state()
        # 課金送信(Step 2)の認証情報。per-user .env の BACOPY_USER_EMAIL を優先、
        # 無ければ BACOPY_SUPPORT_USER_EMAIL。両方無ければ送らずログのみ。
        self._billing_email = (
            os.getenv("BACOPY_USER_EMAIL", "").strip()
            or os.getenv("BACOPY_SUPPORT_USER_EMAIL", "").strip()
        )
        self._billing_api_key = (
            os.getenv("LAPLACE_SITE_API_KEY", "").strip()
            or os.getenv("LAPLACE_API_KEY", "").strip()
        )
        self._billing_site = (
            os.getenv("LAPLACE_SITE_URL", "").strip()
            or os.getenv("BACOPY_SESSION_SITE_URL", "").strip()
            or "https://www.bafather.uk"
        )
        logger.info(
            f"[BILLING-SYNC] config email={'set' if self._billing_email else 'MISSING'} "
            f"api_key={'set' if self._billing_api_key else 'MISSING'} site={self._billing_site}"
        )

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
        _playwright_mgr = None
        _chrome_browser = None
        ctx = None
        bet_page = None
        try:
            browser_mode = (
                os.getenv("BACOPY_BROWSER", "")
                or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
                or ""
            ).strip().lower()
            chrome_attach = browser_mode in ("chrome_attach", "chrome-cdp", "cdp")
            if chrome_attach:
                from playwright.sync_api import sync_playwright

                cdp_url = (
                    os.getenv("BACOPY_CHROME_CDP_URL", "")
                    or os.getenv("BACOPY_CHROME_DEBUG_URL", "")
                    or "http://127.0.0.1:9222"
                ).strip()
                logger.info(f"[BROWSER] chrome_attach enabled cdp={cdp_url}")
                # 起動時バナー: context死からの自己復旧(full CDP reconnect)が入った engine か
                # 一目で分かるようにする。これが出ていれば 2026-06-21 の修正が効いている。
                logger.info("[BOT] ★CONTEXT-DEATH AUTO-RECOVERY: ENABLED v2 (full CDP reconnect + retry/backoff up to 8x — 2026-06-21). recovery proof = '[BOT] full CDP reconnect OK on attempt N'")
                _playwright_mgr = sync_playwright().start()
                _chrome_browser = _playwright_mgr.chromium.connect_over_cdp(cdp_url)
                contexts = list(getattr(_chrome_browser, "contexts", []) or [])
                ctx = contexts[0] if contexts else _chrome_browser.new_context()
                try:
                    all_pages = list(getattr(ctx, "pages", []) or [])
                    page_urls = [
                        str(getattr(p, "url", "") or "")[:160]
                        for p in all_pages[:8]
                    ]
                    logger.info(
                        f"[CHROME-ATTACH] connected contexts={len(contexts)} "
                        f"pages={len(all_pages)} urls={page_urls}"
                    )
                except Exception as ex:
                    logger.warning(f"[CHROME-ATTACH] page inventory failed: {ex}")
                time.sleep(0.5)
            else:
                _camoufox_mgr = cp.Camoufox(**launch_opts)
                ctx = _camoufox_mgr.__enter__()
                time.sleep(2)  # headless ブラウザ完全初期化待ち

            # プラットフォーム設定 (stake|hh88)。ページ選択/ナビで使うので早めに確定する。
            _plat = (os.getenv("BACOPY_PLATFORM", "stake") or "stake").strip().lower()
            # ★hh88 は reload すると起動WS(pusher)が壊れ Pragmatic が再launchされない
            #   → no-reload を既定ON(executor の _HH88_WS_BRIDGE_INIT が既存ソケットを掴む)。
            #   Stake は従来どおり既定OFF(reload 方式・後方互換)。
            _no_reload = (os.getenv("BACOPY_PLATFORM_NO_RELOAD", "1" if _plat == "hh88" else "0") or "0").strip() not in ("0", "", "false", "no")
            _host = (os.getenv("BACOPY_PLATFORM_HOST", "hh88vip5.com" if _plat == "hh88" else "stake.com") or "").strip().lower()
            _lobby_match = (os.getenv("BACOPY_PLATFORM_LOBBY_MATCH", "game-iframe-v2" if _plat == "hh88" else "pragmatic-play-live-lobby-baccarat") or "").strip().lower()
            _lobby_url = (os.getenv("BACOPY_PLATFORM_LOBBY_URL", "").strip() or cp.LOBBY_URL)

            # bet_page のみ作成（lobby monitoring は VPS が担当）
            if chrome_attach:
                pages = list(getattr(ctx, "pages", []) or [])
                stake_pages = [
                    p for p in pages
                    if _host in str(getattr(p, "url", "") or "").lower()
                ]
                lobby_pages = [
                    p for p in stake_pages
                    if _lobby_match in str(getattr(p, "url", "") or "").lower()
                ]
                bet_page = lobby_pages[0] if lobby_pages else (stake_pages[0] if stake_pages else (pages[0] if pages else ctx.new_page()))
                logger.info(
                    f"[BROWSER] chrome_attach page selected url={str(getattr(bet_page, 'url', '') or '')[:160]}"
                )
                logger.info(
                    f"[CHROME-ATTACH] selected stake_pages={len(stake_pages)} "
                    f"lobby_pages={len(lobby_pages)} selected_url={str(getattr(bet_page, 'url', '') or '')[:200]}"
                )
            else:
                bet_page = ctx.pages[0] if ctx.pages else ctx.new_page()
            bet_page.on("websocket", self._on_ws)

            # executor setup
            if self.bet_executor.is_live and not getattr(
                self.bet_executor, "_context", None
            ):
                try:
                    self.bet_executor.setup(ctx, bet_page, bet_page)
                    logger.info("[BOT] executor context injected (run)")
                    # Local-signal (dga) shadow: forward the dga gameResult feed to
                    # the bot for local signal computation. Default OFF → v3 path
                    # is unchanged. Set BACOPY_DGA_LOCAL_SIGNAL=shadow to enable.
                    self._maybe_register_dga_callback()
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

            # (_plat/_no_reload/_host/_lobby_match/_lobby_url は上のページ選択前で定義済み)

            # bet_page をロビーに配置（最初の preposition/switch の準備）
            if _no_reload:
                logger.info("[BOT] no-reload platform: skip lobby goto (attach to manually-opened multibaccarat)")
            else:
                try:
                    _cur0 = str(getattr(bet_page, "url", "") or "").lower()
                except Exception:
                    _cur0 = ""
                try:
                    # hh88(非stake): 起動トークンが都度発行のため固定URLへ goto できない。
                    # 既にプラットフォーム host に居れば reload(トークン再取得→Pragmatic再launch)する。
                    # ★Stake は従来どおり goto(cp.LOBBY_URL)。挙動を変えない(後方互換)。
                    if _plat != "stake" and _host and _host in _cur0:
                        logger.info(f"[BOT] already on platform host ({_host}); reload to re-hook WS (platform={_plat})")
                        bet_page.reload(wait_until="domcontentloaded", timeout=60000)
                    else:
                        logger.info(f"Navigating bet_page to {_lobby_url} (platform={_plat})")
                        bet_page.goto(_lobby_url, wait_until="domcontentloaded", timeout=60000)
                    bet_page.wait_for_timeout(3000)
                except Exception as e:
                    logger.warning(f"[BOT] lobby nav failed: {e}")

            _last_lobby_recover_at = 0.0
            _last_lobby_warn_at = 0.0

            def _ensure_pragmatic_lobby(force: bool = False) -> bool:
                nonlocal _last_lobby_recover_at, _last_lobby_warn_at, bet_page
                if bet_page is None:
                    return False
                # no-reload: ナビゲーションしない。現在ページをそのまま「OK」とみなす
                # (multibaccarat に居るかは executor の multi-area 検出が担う)。
                if _no_reload:
                    return True
                try:
                    cur = str(getattr(bet_page, "url", "") or "")
                except Exception:
                    cur = ""
                ok = _lobby_match in cur
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
                    bet_page.goto(_lobby_url, wait_until="domcontentloaded", timeout=60000)
                    bet_page.wait_for_timeout(2000)
                except Exception as _e:
                    logger.warning(f"[BOT] lobby re-nav failed: {_e}")
                try:
                    cur2 = str(getattr(bet_page, "url", "") or "")
                except Exception:
                    cur2 = ""
                ok2 = _lobby_match in cur2
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
                # 安全モード用 当日累計勝率ポーラ(ON/OFF に関わらず常時更新)。
                safety_thread = _threading.Thread(
                    target=self._safety_stats_poll_loop, daemon=True, name="safety-stats-poll"
                )
                safety_thread.start()
                logger.info(
                    f"[BOT] VPS API polling started (decisions + preposition + safety-stats) "
                    f"safety_mode={'ON' if getattr(self, '_safety_enabled', False) else 'OFF'}"
                )
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
            last_billing_poll = 0.0
            last_billing_sync = 0.0
            last_remote_signal_poll = time.time() - 5
            last_decision_fallback_poll = time.time() - 5
            last_tick_diag = 0.0
            last_loop_wait_diag = 0.0
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
                    _wait_start = time.time()
                    _wait_switch_req = getattr(self.bet_executor, "_switch_request", None)
                    if _wait_switch_req and _wait_start - last_loop_wait_diag >= 5.0:
                        logger.warning(
                            f"[BOT-LOOP-DIAG] before page wait "
                            f"switch_in_progress={getattr(self.bet_executor, '_switch_in_progress', None)} "
                            f"switch_request={_wait_switch_req}"
                        )
                        last_loop_wait_diag = _wait_start
                    bet_page.wait_for_timeout(1000)
                    _wait_elapsed = time.time() - _wait_start
                    if _wait_elapsed > 2.0:
                        logger.warning(
                            f"[BOT-LOOP-DIAG] page wait slow elapsed={_wait_elapsed:.1f}s "
                            f"switch_request={getattr(self.bet_executor, '_switch_request', None)}"
                        )
                except Exception as _page_err:
                    err_str = str(_page_err).lower()
                    if "closed" in err_str or "target" in err_str:
                        logger.warning(f"[BOT] bet_page closed unexpectedly, reinitializing: {_page_err}")
                        if _no_reload:
                            # hh88: トークン都度発行のため新規ページへの goto で復旧不可。
                            # 自動ナビせず、ユーザーが手動でマルチバカラを開き直すのを待つ。
                            logger.error("[BOT] no-reload platform: cannot auto-recover closed page — please re-open multibaccarat manually. stopping loop.")
                            break
                        try:
                            bet_page = ctx.new_page()
                            bet_page.on("websocket", self._on_ws)
                            self.bet_executor.setup(ctx, bet_page, bet_page)
                            bet_page.goto(cp.LOBBY_URL, wait_until="domcontentloaded", timeout=60000)
                            bet_page.wait_for_timeout(2000)
                            logger.info("[BOT] bet_page reinit OK, resuming loop")
                        except Exception as _reinit_err:
                            # ── context ごと死亡時の自己復旧 (2026-06-21, retry強化) ────────
                            # 旧: ctx.new_page() 失敗で即 stopping → エンジン終了。:9222 Chrome
                            # 自体は生きている(or CDPウォッチドッグが再起動中)ので connect_over_cdp で
                            # 完全再接続して自己復帰させる。★クラッシュ直後は :9222 が瞬間的に CDP 接続を
                            # 受けない(Chrome再起動中)ため、1回だけだと connect が失敗して停止していた
                            # (2026-06-21 実機で確認)→ バックオフ付きで最大~60s リトライする。
                            if chrome_attach:
                                logger.warning(
                                    f"[BOT] reinit failed (context dead: {_reinit_err}); "
                                    f"full CDP reconnect to {cdp_url} (retry w/ backoff)"
                                )
                                _reconnected = False
                                for _att in range(1, 9):  # 最大8回(~2,4,6,8,10,10,10,10 = ~60s)
                                    try:
                                        time.sleep(min(2.0 * _att, 10.0))  # :9222 復帰待ち(Chrome再起動)
                                        try:
                                            _chrome_browser.close()
                                        except Exception:
                                            pass
                                        _chrome_browser = _playwright_mgr.chromium.connect_over_cdp(cdp_url)
                                        _rc = list(getattr(_chrome_browser, "contexts", []) or [])
                                        ctx = _rc[0] if _rc else _chrome_browser.new_context()
                                        _rp = list(getattr(ctx, "pages", []) or [])
                                        _rsp = [p for p in _rp if _host in str(getattr(p, "url", "") or "").lower()]
                                        _rlp = [p for p in _rsp if _lobby_match in str(getattr(p, "url", "") or "").lower()]
                                        bet_page = _rlp[0] if _rlp else (_rsp[0] if _rsp else (_rp[0] if _rp else ctx.new_page()))
                                        bet_page.on("websocket", self._on_ws)
                                        self.bet_executor.setup(ctx, bet_page, bet_page)
                                        logger.info(
                                            f"[BOT] full CDP reconnect OK on attempt {_att} "
                                            f"(url={str(getattr(bet_page, 'url', '') or '')[:120]}), resuming loop"
                                        )
                                        _reconnected = True
                                        break
                                    except Exception as _recon_err:
                                        logger.warning(f"[BOT] reconnect attempt {_att}/8 failed: {_recon_err}")
                                if _reconnected:
                                    _table_enter_at.clear()
                                    continue
                                logger.error("[BOT] full CDP reconnect exhausted (8 tries), stopping")
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

                # ── executor tick ──────────────────────────────────
                if self.bet_executor.is_live:
                    # idle 時は 5 秒に 1 回のみ tick（keep-alive は lobby では不要）
                    tick_interval = 1.0 if _executor_is_idle() else 0.5
                    switch_req = getattr(self.bet_executor, "_switch_request", None)
                    if switch_req and now - last_tick_diag >= 5.0:
                        logger.warning(
                            f"[BOT-TICK-DIAG] before executor.tick "
                            f"is_live={self.bet_executor.is_live} idle={_executor_is_idle()} "
                            f"dt={now - last_executor_tick:.1f}s interval={tick_interval:.1f}s "
                            f"switch_in_progress={getattr(self.bet_executor, '_switch_in_progress', None)} "
                            f"switch_request={switch_req}"
                        )
                        last_tick_diag = now
                    if now - last_executor_tick >= tick_interval:
                        try:
                            _tk0 = time.time()  # 計装: tick間欠ブロック検出(yellow-stuck調査)
                            self.bet_executor.tick()
                            _tkdt = time.time() - _tk0
                            if _tkdt >= 2.0:
                                logger.warning(f"[LOOP-SLOW] executor.tick {_tkdt:.1f}s (main-loop block)")
                        except Exception as e:
                            logger.warning(f"[BOT] executor tick error: {e!r}")
                        last_executor_tick = now

                # ── lobby URL ガード（TOPページ固定を自己回復） ─────
                if now - _last_lobby_recover_at >= 15.0:
                    try:
                        _ensure_pragmatic_lobby(force=False)
                    except Exception as e:
                        logger.debug(f"[BOT] lobby guard error: {e}")

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
                    # dga local-signal: keep the passive observer page alive
                    # (reopen if the user/Stake closed it). Playwright main thread.
                    try:
                        ensure = getattr(self.bet_executor, "_ensure_dga_observer", None)
                        if callable(ensure):
                            ensure()
                    except Exception as e:
                        logger.debug(f"[BOT] dga observer ensure error: {e}")
                    # READ-ONLY DOM probe (BACOPY_DOM_RESULT_PROBE=1, default off):
                    # capture the betting-page tile roadmap structure to design a
                    # single-session ordered-result source. No betting impact.
                    try:
                        probe = getattr(self.bet_executor, "_maybe_dom_result_probe", None)
                        if callable(probe):
                            probe()
                    except Exception as e:
                        logger.debug(f"[BOT] dom probe error: {e}")

                # ── VPS decision short-poll fallback (1秒ごと) ─────────
                if now - last_decision_fallback_poll >= 1.0:
                    last_decision_fallback_poll = now
                    try:
                        self._poll_pending_decisions_fallback()
                    except Exception as e:
                        logger.debug(f"[BOT] fallback decision poll error: {e}")

                # ── Phase 2b: dga local-signal bet/settle pump (every loop, low
                # latency so the multi-chip placement gets the full window). Only
                # active in BACOPY_DGA_LOCAL_SIGNAL=live; no-op otherwise. ──
                try:
                    self._dga_main_pump()
                except Exception as e:
                    logger.debug(f"[BOT] dga pump error: {e}")
                # VPS-NOW 確定BETを dga 勝者で決済(自動WS BET時の○×/SEQ/ダランベール進行)。
                # dga signal mode off でも走る(settle-only feed)。
                try:
                    self._dga_vps_settle_pump()
                except Exception as e:
                    logger.debug(f"[BOT] dga vps settle pump error: {e}")
                # 追従BET失敗(pending_signal_stale 等=窓に間に合わず未着弾)を即検知して
                # 追従終了。181秒も待たずに他卓NOWへ復帰させる(機会損失/ブロック防止)。
                if getattr(self, "_follow_active", False) and getattr(self, "_follow_bet_id", ""):
                    _cf = getattr(self.bet_executor, "consume_failed_bet", None)
                    if callable(_cf):
                        try:
                            _fi = _cf(self._follow_bet_id)
                        except Exception:
                            _fi = None
                        if isinstance(_fi, dict) and _fi:
                            logger.warning(f"[FOLLOW] bet failed ({_fi.get('reason')}) -> END chain (resume NOW)")
                            if self._follow_did:
                                self._pending_decisions.pop(self._follow_did, None)
                            self._follow_reset(reason="bet_failed")
                # 追従ウォッチドッグ(保険): 決済も失敗も来ずスタックしたら短時間でリセット＋
                # 取りこぼした追従pendingを掃除(残留防止)。
                _ft = float(os.getenv("BACOPY_FOLLOW_TIMEOUT_SEC", "75") or 75)
                if getattr(self, "_follow_active", False) and self._follow_last_bet_at \
                        and (now - self._follow_last_bet_at) > _ft:
                    logger.warning(f"[FOLLOW] timeout {now - self._follow_last_bet_at:.0f}s no settle -> reset")
                    self._follow_reset(reason="timeout")
                if self._pending_decisions:
                    for _fdid, _fv in list(self._pending_decisions.items()):
                        if isinstance(_fv, dict) and _fv.get("is_follow") \
                                and not _fv.get("settlement_posted") \
                                and (now - float(_fv.get("placed_at") or now)) > _ft:
                            self._pending_decisions.pop(_fdid, None)

                # 利確(profit_stop)到達: GUIへ1回だけ停止通知。SEQはリセットせず保持し、
                # GUIが利確表示を出してbotを停止する(app.js側でstopBot)。次回起動時に
                # 上の resume クリアで新セッション再開できる。追従中なら止める。
                if (self.money.limit_reached and self.money.limit_reason == "profit"
                        and not self._profit_stop_sent):
                    self._profit_stop_sent = True
                    if getattr(self, "_follow_active", False):
                        self._follow_reset(reason="profit_stop")
                    ms = self.money.status_dict()
                    try:
                        send_msg({
                            "type": "profit_target_reached",
                            "session_pnl": round(float(self.money.session_pnl), 2),
                            "profit_stop": float(self.money.profit_stop),
                            "money_status": ms,
                        })
                    except Exception:
                        pass
                    logger.info(
                        f"[PROFIT] target reached session_pnl=${self.money.session_pnl:+.2f} "
                        f"stop=${self.money.profit_stop} -> notify GUI to stop (SEQ kept)"
                    )
                    try:
                        _send_telegram(
                            f"🎯 利確達成 ${self.money.session_pnl:+.2f}\n"
                            f"目標 ${self.money.profit_stop} 到達 — 停止(SEQ保持)"
                        )
                    except Exception:
                        pass

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
                    try:
                        self._maintain_manual_assist_mode_heartbeat()
                    except Exception as e:
                        logger.debug(f"[BOT] manual assist heartbeat error: {e}")

                # ── decision result ポーリング (10秒ごと) ─────────
                if now - last_result_check >= 10.0:
                    last_result_check = now
                    try:
                        self._check_decision_results()
                    except Exception as e:
                        logger.debug(f"[BOT] result poll error: {e}")

                # ── 課金: ベット履歴 realized PnL 集計 (6秒ごと) ──
                if self.bet_executor.is_live and now - last_billing_poll >= 6.0:
                    last_billing_poll = now
                    self._poll_bet_history_billing()

                # ── 課金: session-state を bafather へ同期 (60秒ごと) ──
                if self.bet_executor.is_live and now - last_billing_sync >= 60.0:
                    last_billing_sync = now
                    self._sync_billing_session_state()

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
            # chrome_attach mode connects to a user-owned Chrome. Do not close
            # the browser process; just stop Playwright's client side.
            if _playwright_mgr is not None:
                try:
                    _playwright_mgr.stop()
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
        "--seq-turns", type=int, default=7, choices=[5, 7],
        help="SEQ のセット長 (7=標準 / 5=5ターン制)"
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
        "--manual-assist",
        action="store_true",
        help="manual assist mode: scroll/focus/preselect only; never auto-click BET",
    )
    ap.add_argument(
        "--browser",
        type=str,
        default="",
        help="experimental browser backend: default/camoufox or chrome_attach",
    )
    ap.add_argument(
        "--chrome-cdp-url",
        type=str,
        default="",
        help="experimental: existing Chrome DevTools URL for --browser chrome_attach",
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

    if args.browser:
        os.environ["BACOPY_BROWSER"] = str(args.browser).strip()
    if args.chrome_cdp_url:
        os.environ["BACOPY_CHROME_CDP_URL"] = str(args.chrome_cdp_url).strip()
    browser_mode_for_log = (
        os.getenv("BACOPY_BROWSER", "")
        or os.getenv("BACOPY_DUAL_LINE_BROWSER", "")
        or "camoufox"
    ).strip()
    chrome_cdp_for_log = (
        os.getenv("BACOPY_CHROME_CDP_URL", "")
        or os.getenv("BACOPY_CHROME_DEBUG_URL", "")
        or "http://127.0.0.1:9222"
    ).strip()
    logger.info(
        f"[AUTO-PROBE] argv live={args.live} manual_assist={args.manual_assist} "
        f"browser={browser_mode_for_log or 'camoufox'} "
        f"cdp={chrome_cdp_for_log if browser_mode_for_log.lower() in ('chrome_attach', 'chrome-cdp', 'cdp') else '-'} "
        f"money_mode_arg={args.money_mode} bet_mode_arg={args.bet_mode or '-'}"
    )

    if args.reset:
        removed = []
        for path in (STATE_PATH, STATE_TMP, MONEY_STATE_PATH):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(path.name)
            except Exception as ex:
                logger.warning(f"state reset failed for {path.name}: {ex}")
        logger.info(f"state reset removed={removed or '-'}")

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
        manual_assist=bool(getattr(args, "manual_assist", False)),
        seq_set_size=int(getattr(args, "seq_turns", 7) or 7),
    )

    bot._send_manual_assist_mode()
    if bot.manual_assist:
        bot.start_manual_assist_command_reader()
    mode_label = "MANUAL ASSIST" if bot.manual_assist else ("LIVE" if bot.bet_executor.is_live else "DRY RUN")
    logger.info(
        f"dual-line mode={mode_label} live_executor={bot.bet_executor.is_live} "
        f"auto_bet_enabled={not bot.manual_assist}"
    )
    if bot.manual_assist:
        _send_telegram(
            f"🟢 dual_line_pragmatic_bot 起動 ({mode_label})\n"
            "auto-bet: disabled\n"
            f"filter: {'v2 (6 patterns)' if not args.no_v2_filter else 'all patterns'}\n"
            f"money: {BET_MODES[bot.money.mode]} unit=${bot.money.unit}\n"
            f"stop: ${bot.money.profit_stop} cut: ${bot.money.loss_cut} on_limit: {bot.money.on_limit}"
        )
    elif bot.bet_executor.is_live:
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
