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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

# このスクリプトは VPS の /opt/laplace2/ に置く想定
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# .env を load (= TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 等)
# 複数候補を試し、TELEGRAM credentials が含まれているものを優先
try:
    from dotenv import load_dotenv
    _env_candidates = [
        HERE / ".env",                            # /opt/laplace2/.env
        Path("/opt/laplace/.env"),                # production の場所
        HERE.parent / "bacopy" / ".env",          # /opt/bacopy/.env
        Path("/opt/bacopy/.env"),                 # 同上 (絶対パス)
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

from dual_line_match import decide, score_proximity
from dual_line_money import BetManager, ALLOWED_MODES as MONEY_MODES, BET_MODES

# ── Logger 設定 ──────────────────────────────────────────────────────
# collector_pragmatic.py の basicConfig と競合しないよう dedicated logger を使う
_bot_logger = logging.getLogger("dual_line.bot")
_bot_logger.setLevel(logging.INFO)
_bot_logger.propagate = False  # root logger に伝播させない
if not _bot_logger.handlers:
    _fh = logging.FileHandler(HERE / "dual_line_pragmatic_bot.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _bot_logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    _bot_logger.addHandler(_sh)
logger = _bot_logger

# ── 定数 ─────────────────────────────────────────────────────────────

V2_PATTERNS = {
    "niconico|dragon|P",
    "niconico|niconico|B",
    "niconico|nikoichi|B",
    "telecho|telecho|B",
}

STATE_PATH = HERE / "dual_line_pragmatic_state.json"
STATE_TMP = HERE / "dual_line_pragmatic_state.tmp"
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


def _send_telegram(text: str) -> None:
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
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        logger.debug(f"telegram send failed: {e}")


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

    def place_bet(self, table_id: str, side: str, amount: float, metadata: dict) -> None:
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
    ):
        super().__init__(headless=headless, raw_log=raw_log)
        self.use_v2_filter = use_v2_filter
        self.notify_signal = notify_signal
        self.notify_resolution = notify_resolution
        self.notify_tie = notify_tie
        self.bet_executor: BetExecutor = bet_executor or DryRunBetExecutor()
        self.money = BetManager(
            mode=money_mode, unit=money_unit,
            profit_stop=profit_stop, loss_cut=loss_cut,
            on_limit=on_limit,
            state_path=HERE / "dual_line_money_state.json",
        )

        # per-table 状態
        self.last_hand_count: dict[str, int] = defaultdict(int)
        self.last_fresh_start: dict[str, bool] = defaultdict(bool)
        self.shoe_active: dict[str, bool] = defaultdict(bool)  # 完全観測中フラグ
        self.pending: dict[str, dict] = {}  # table_id -> pending prediction
        self.table_scores: dict[str, int] = defaultdict(int)  # table_id -> score (0-2)


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
                if ctx is not None and not getattr(
                    self.bet_executor, "_context", None
                ):
                    self.bet_executor.setup(ctx, ws_page)
                    logger.info("[BOT] executor context injected")
            except Exception as e:
                logger.debug(f"[BOT] executor context injection failed: {e}")

        if cp.PRAGMATIC_WS_PATTERN not in url:
            return

        self._ws_alive = True

        def on_bot_close():
            logger.warning(f"[BOT] WS closed: {url}")
            self._ws_alive = False
            self._ws_disconnected_at = time.time()

        ws.on("close", on_bot_close)

    # ── ハンドオブザーバ ──────────────────────────────────────────

    def on_ws_frame(self, payload):  # type: ignore[override]
        super().on_ws_frame(payload)
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            msg = json.loads(payload)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        table_id = msg.get("tableId")
        if not table_id:
            # tableId がないフレームはハンド情報を持たないのでスキップ。
            # shuffle=true が tableId なしで来る可能性は極めて低いが、
            # 万が一到達した場合は警告。
            if msg.get("shuffle") is True:
                logger.warning(
                    f"shuffle=true but no tableId in msg: keys={list(msg.keys())[:10]}"
                )
            return
        buf = self.buffers.get(table_id)
        if not buf:
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
            self.last_hand_count[table_id] = current_count
            return

        # ── 新規ハンド検知 ──
        if current_count <= prev_count:
            return
        new_hands = (buf.hands or [])[prev_count:current_count]
        self.last_hand_count[table_id] = current_count

        for new_hand in new_hands:
            self._on_new_hand(table_id, buf, new_hand)

        # 手処理後、全テーブルのスコアを更新（LOOP でも BET でも）
        self._update_table_score(table_id, buf)

    def _on_shoe_change(self, table_id: str, buf):
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

    def _on_new_hand(self, table_id: str, buf, new_hand: dict):
        outcome_char = _winner_to_char(new_hand.get("winner"))
        if not outcome_char:
            return

        # 1) 前回の予想を resolve
        if table_id in self.pending:
            pending = self.pending.pop(table_id)
            self._resolve_prediction(table_id, buf, pending, outcome_char, new_hand)

        # 2) observed_sequence を構築
        seq_chars = []
        for h in (buf.hands or []):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        observed_sequence = "".join(seq_chars)

        # 3) 次手の予想
        next_n = len(observed_sequence) + 1
        d = decide(observed_sequence, next_n=next_n)
        if d.action == "LOOK":
            return

        bet_side = "P" if d.action == "BET_P" else "B"
        pattern_key = f"{d.china_pattern}|{d.big_pattern}|{bet_side}"

        if self.use_v2_filter and pattern_key not in V2_PATTERNS:
            return

        # 4) ベット発行
        # LIVE モード: 同時BETはしない（1件ずつ処理）
        if self.bet_executor.is_live:
            if self.pending:
                return
            if getattr(self.bet_executor, "has_pending_bet", False):
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
        }
        bet_id = self.bet_executor.place_bet(
            table_id=table_id,
            side=bet_side,
            amount=bet_amount,
            metadata=bet_metadata,
        )

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
            "predicted_at": _utc_now_iso(),
            "predicting_n": next_n,
            "bet_amount": bet_amount,
        }
        if bet_id:
            pending_entry["bet_id"] = bet_id
        self.pending[table_id] = pending_entry

        self.total_signals += 1
        send_phase("predicting", f"{bet_side} via {pattern_key}")
        send_action(f"🎯 #{self.total_signals} {pattern_key} → {bet_side} on {buf.table_name or table_id}")
        if self.notify_signal:
            self._notify_signal(table_id, buf, pattern_key, bet_side, observed_sequence, bet_amount)
        self._save_state()

        # スコア更新: signal(=2) が確定
        self.table_scores[table_id] = 2

    def _update_table_score(self, table_id: str, buf) -> None:
        """dga hand 後、そのテーブルの現在の近接度スコアを計算。"""
        seq_chars = []
        for h in (buf.hands or []):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        seq = "".join(seq_chars)
        next_n = len(seq) + 1
        score, _direction = score_proximity(seq, next_n)
        self.table_scores[table_id] = score

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
                            f"Pattern: `{pkey}`\n"
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
                f"Pattern: `{pkey}`\n"
                f"Pred: {side} → Got: {outcome}\n"
                f"PnL: ${pnl:+.2f} | 累計: ${self.virtual_pnl:+.2f}\n"
                f"W/L/T: {self.wins}/{self.losses}/{self.ties} ({wr:.1f}%)\n"
                f"signals: {self.total_signals} resolved: {self.total_resolved}"
            )

        # resolution 後も即座に state 保存（クラッシュ時のデータ消失防止）
        self._save_state()

    def _notify_signal(
        self, table_id: str, buf, pattern_key: str, side: str, seq: str,
        bet_amount: float = 0.0,
    ):
        side_name = "BANKER" if side == "B" else "PLAYER"
        mode_label = "[LIVE]" if self.bet_executor.is_live else "[DRY]"
        bet_amt = bet_amount or self.money._last_bet_amount
        _send_telegram(
            f"🎯 v2 SIGNAL #{self.total_signals} {mode_label}\n"
            f"Table: {buf.table_name or table_id}\n"
            f"Pattern: `{pattern_key}`\n"
            f"Predict: {side} ({side_name}) | Bet: ${bet_amt:.2f}\n"
            f"Sequence: ...{seq[-20:]}\n"
            f"PnL: ${self.virtual_pnl:+.2f} | session: ${self.money.session_pnl:+.2f}"
        )

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
            s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.total_signals = s.get("total_signals", 0)
            self.total_resolved = s.get("total_resolved", 0)
            self.wins = s.get("wins", 0)
            self.losses = s.get("losses", 0)
            self.ties = s.get("ties", 0)
            self.virtual_pnl = float(s.get("virtual_pnl", 0.0))
            self.pending = s.get("pending", {}) or {}
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

    def _api_get(self, path: str, params: str = "") -> dict:
        """GET https://master.bafather.uk/api/<path>"""
        import urllib.request as _ur
        url = (os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk") + path
        if params:
            url += "?" + params
        key = os.getenv("BACOPY_API_KEY", "").strip()
        try:
            req = _ur.Request(url, headers={"Authorization": f"Bearer {key}"})
            with _ur.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.debug(f"[API] GET {path} failed: {e}")
            return {}

    def _api_post(self, path: str, data: dict) -> dict:
        """POST https://master.bafather.uk/api/<path>"""
        import urllib.request as _ur
        url = (os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk") + path
        key = os.getenv("BACOPY_API_KEY", "").strip()
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

    def _check_preposition(self) -> None:
        """VPS の事前入場指示をポーリング。WARM テーブルへ事前入場。"""
        if not self.bet_executor.is_live:
            return
        data = self._api_get("/api/preposition")
        table_id = str(data.get("table_id") or "").strip()
        if not table_id:
            return
        # 同じテーブルへの重複リクエストはスキップ
        if table_id == getattr(self, "_last_preposition_id", ""):
            return
        self._last_preposition_id = table_id
        table_name = str(data.get("table_name") or "")
        score = int(data.get("score") or 1)
        if self.bet_executor.can_switch():
            logger.info(f"[BOT] pre-positioning → {table_name} (score={score})")
            try:
                self.bet_executor._request_switch(table_id, table_name, table_id)
            except Exception as e:
                logger.warning(f"[BOT] pre-position switch failed: {e}")

    def _handle_decision(self, decision: dict) -> None:
        """VPS からの BET decision を受け取り、executor 経由で BET 実行。"""
        did = str(decision.get("decision_id") or "")
        fa = decision.get("friend_action") or {}
        if not isinstance(fa, dict):
            return
        side = str(fa.get("side") or "").upper()
        if side not in ("P", "B"):
            return
        table_id = str(decision.get("table_id") or "")
        table_name = str(decision.get("table_name") or "")

        bet_amount = self.money.next_bet()
        logger.info(f"[BOT] decision received: {did} side={side} table={table_name} amount=${bet_amount}")

        metadata = {"table_name": table_name, "qpid_table_id": table_id}
        bet_id = self.bet_executor.place_bet(table_id, side, bet_amount, metadata)

        self._pending_decisions[did] = {
            "side": side, "amount": bet_amount,
            "table_id": table_id, "table_name": table_name,
            "bet_id": str(bet_id or ""), "placed_at": time.time(),
        }
        self.total_signals += 1

        # ACK
        self._api_post(f"/api/decisions/{did}/ack", {
            "ack": {"executor_id": "gui-1", "placed_at": _utc_now_iso()},
            "status": "processing",
        })
        _send_telegram(
            f"🎯 BET 実行\n{table_name}\nSide: {side} ${bet_amount:.2f}\ndecision: {did[:12]}"
        )

    def _check_decision_results(self) -> None:
        """status=done の decision を取得して BetManager を更新。"""
        if not self._pending_decisions:
            return
        data = self._api_get("/api/decisions/pending", "status=done&limit=50")
        for d in (data.get("decisions") or []):
            did = str(d.get("decision_id") or "")
            if did not in self._pending_decisions:
                continue
            pending = self._pending_decisions.pop(did)
            result = d.get("result") or {}
            won = bool(result.get("won"))
            tie = bool(result.get("tie"))
            outcome = str(result.get("outcome") or "?")
            self.money.apply_result(won)
            if tie:
                self.ties += 1
            elif won:
                self.wins += 1
            else:
                self.losses += 1
            self.total_resolved += 1
            pnl_delta = (pending["amount"] * COMMISSION_BANKER if won and pending["side"] == "B"
                         else pending["amount"] if won else -pending["amount"]) if not tie else 0.0
            self.virtual_pnl += pnl_delta
            self._save_state()
            icon = "✅" if won else ("🔵" if tie else "❌")
            ms = self.money.status_dict()
            _send_telegram(
                f"{icon} {'WIN' if won else ('TIE' if tie else 'LOSE')}\n"
                f"outcome: {outcome}\n"
                f"pnl: ${pnl_delta:+.2f} | cum: ${self.virtual_pnl:+.2f}\n"
                f"W/L/T: {self.wins}/{self.losses}/{self.ties}\n"
                f"next: ${ms['next_bet']}"
            )

    def _decisions_poll_loop(self) -> None:
        """background thread: VPS API を long-poll して BET decision を受信。"""
        api_url = (os.getenv("BACOPY_API_URL", "").rstrip("/") or "https://master.bafather.uk")
        key = os.getenv("BACOPY_API_KEY", "").strip()
        import urllib.request as _ur
        while not getattr(self, "_stop_decision_poll", False):
            try:
                url = f"{api_url}/api/decisions/wait?provider=pragmatic&wait_sec=20"
                req = _ur.Request(url, headers={"Authorization": f"Bearer {key}"})
                with _ur.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                for d in (data.get("decisions") or []):
                    if not getattr(self, "_stop_decision_poll", False):
                        try:
                            self._handle_decision(d)
                        except Exception as e:
                            logger.warning(f"[BOT] handle_decision error: {e}")
            except Exception as e:
                logger.debug(f"[BOT] decision poll error: {e}")
                time.sleep(3)

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
        self._last_preposition_id: str = ""
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

        launch_opts = {
            "headless": self.headless,
            "persistent_context": True,
            "user_data_dir": str(profile),
        }

        def on_signal(signum, frame):
            logger.warning(f"Signal {signum} received, stopping...")
            self.stop_flag = True

        signal.signal(signal.SIGINT, on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, on_signal)

        start_ts = time.time()
        report_interval = 60

        with cp.Camoufox(**launch_opts) as ctx:
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

            # BET decision polling thread (background)
            import threading as _threading
            poll_thread = _threading.Thread(
                target=self._decisions_poll_loop, daemon=True, name="decision-poll"
            )
            poll_thread.start()
            logger.info("[BOT] VPS API polling started (decisions + preposition)")
            _send_telegram(
                f"📡 ロビー監視中\n"
                f"VPS監視: hakudasama@gmail.com\n"
                f"シグナル発生時に自動テーブル入場・BETします\n"
                f"手動操作は不要です"
            )

            last_report = time.time()
            last_executor_tick = time.time()
            last_prepos_check = time.time() - 10  # 初回即チェック
            last_result_check = time.time()

            while not self.stop_flag:
                bet_page.wait_for_timeout(1000)
                now = time.time()

                # executor tick (bet_page の WS 処理)
                if self.bet_executor.is_live:
                    if now - last_executor_tick >= 0.5:
                        try:
                            self.bet_executor.tick()
                        except Exception as e:
                            logger.debug(f"[BOT] executor tick error: {e}")
                        last_executor_tick = now

                # VPS preposition ポーリング (10秒ごと)
                if now - last_prepos_check >= 10.0:
                    last_prepos_check = now
                    try:
                        self._check_preposition()
                    except Exception as e:
                        logger.debug(f"[BOT] preposition poll error: {e}")

                # decision result ポーリング (10秒ごと)
                if now - last_result_check >= 10.0:
                    last_result_check = now
                    try:
                        self._check_decision_results()
                    except Exception as e:
                        logger.debug(f"[BOT] result poll error: {e}")

                # 定期ステータスレポート
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
                    if now - getattr(self, "_last_tg_status", 0) >= 300:
                        _send_telegram(
                            f"📊 定期ステータス [{mode_label}]\n"
                            f"稼働: {elapsed//60}分\n"
                            f"signals: {self.total_signals} / resolved: {self.total_resolved}\n"
                            f"W/L/T: {self.wins}/{self.losses}/{self.ties} ({wr:.1f}%)\n"
                            f"cumPnL: ${self.virtual_pnl:+.2f}\n"
                            f"session: ${ms['session_pnl']:+.2f} next: ${ms['next_bet']}"
                        )
                        self._last_tg_status = now
                    last_report = now

                if duration and (now - start_ts) >= duration:
                    logger.info(f"Duration {duration}s reached, stopping.")
                    break

        self._stop_decision_poll = True
        logger.info(f"Final: signals={self.total_signals} resolved={self.total_resolved} pnl=${self.virtual_pnl:+.2f}")
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
        notify_signal=True,
        notify_resolution=(not args.no_resolution_notify),
        notify_tie=False,
        bet_executor=bet_executor,
    )

    mode_label = "LIVE" if bot.bet_executor.is_live else "DRY RUN"
    _send_telegram(
        f"🟢 dual_line_pragmatic_bot 起動 ({mode_label})\n"
        f"filter: {'v2 (4 patterns)' if not args.no_v2_filter else 'all 24 patterns'}\n"
        f"money: {BET_MODES[bot.money.mode]} unit=${bot.money.unit}\n"
        f"stop: ${bot.money.profit_stop} cut: ${bot.money.loss_cut} on_limit: {bot.money.on_limit}\n"
        f"累計 signals: {bot.total_signals} PnL: ${bot.virtual_pnl:+.2f}"
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
        f"W/L/T: {bot.wins}/{bot.losses}/{bot.ties}\n"
        f"累計 PnL: ${bot.virtual_pnl:+.2f}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
