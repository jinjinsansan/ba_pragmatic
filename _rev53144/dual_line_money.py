"""Dual-Line Bet 資金管理モジュール

BetManager:
  - フラット ベット（単一ユニット）
  - SMALL SEQ シリーズ (0.2/0.6/1/1.4/2/2.4/3/6/10/30 start)
  - PLAYER SEQ シリーズ (0.2/0.4/1/2/3 start: 元祖48段 — 〇×ロジック仕様書元祖.txt の階段を開始額で比例展開・型なし)
  - 1-2-3打法 (bet123: 1回目1単位→2回目2単位→同結果連続なら3回目3単位→リセット)
  - 純粋マーチンゲール
  - 利確 / 損切
  - 利確/損切後の動作: STOP or RESTART

Usage:
  from dual_line_money import BetManager
  money = BetManager(mode="flat", unit=100.0, profit_stop=500.0, loss_cut=1000.0,
                     on_limit="stop")
  amount = money.next_bet(side="P")
  money.apply_result(won=True)
  if money.limit_reached:
      print(f"LIMIT: {money.limit_reason}")
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from marubatsu_strategy import MaruBatsuTracker, SetData
from marubatsu_strategy import SEQ as _MARUBATSU_ORIGINAL_SEQ

logger = logging.getLogger("dual_line.money")

# ── SEQ 定義 ─────────────────────────────────────────────────────────

SEQ_SMALL3 = [
    3, 5, 7, 12, 17, 23, 30,
    38, 47, 57, 68, 83, 97,
    112, 128, 145, 165, 190, 220, 255, 300, 360,
    420, 500, 600, 700, 800, 900, 1000,
]

SEQ_SMALL02 = [
    0.2, 0.4, 0.6, 0.8, 1.2, 1.6, 2.0,
    2.6, 3.2, 3.8, 4.6, 5.6, 6.4,
    7.4, 8.6, 9.6, 11.0, 12.6, 14.6, 17.0, 20.0, 24.0,
    28.0, 33.4, 40.0, 46.6, 53.4, 60.0, 66.6,
]

SEQ_SMALL1 = [
    1, 2, 4, 6, 8, 10, 13, 16, 19, 23, 28, 32, 37, 43,
    48, 55, 63, 73, 85, 100, 120, 140, 167, 200, 233, 267, 300, 333,
]

# SMALL1 のちょうど2倍($2 start・天井666x)。全段が偶数=チップ妥当額。
SEQ_SMALL2 = [
    2, 4, 8, 12, 16, 20, 26, 32, 38, 46, 56, 64, 74, 86,
    96, 110, 126, 146, 170, 200, 240, 280, 334, 400, 466, 534, 600, 666,
]

# SMALL1 のちょうど1.4倍($1.4 start・天井466.2x=333x)。全段が$0.2の倍数=チップ妥当額。
SEQ_SMALL14 = [
    1.4, 2.8, 5.6, 8.4, 11.2, 14.0, 18.2, 22.4, 26.6, 32.2, 39.2, 44.8, 51.8, 60.2,
    67.2, 77.0, 88.2, 102.2, 119.0, 140.0, 168.0, 196.0, 233.8, 280.0, 326.2, 373.8, 420.0, 466.2,
]

# SMALL1 のちょうど2.4倍($2.4 start・天井799.2x=333x)。全段が$0.2の倍数=チップ妥当額。
SEQ_SMALL24 = [
    2.4, 4.8, 9.6, 14.4, 19.2, 24.0, 31.2, 38.4, 45.6, 55.2, 67.2, 76.8, 88.8, 103.2,
    115.2, 132.0, 151.2, 175.2, 204.0, 240.0, 288.0, 336.0, 400.8, 480.0, 559.2, 640.8, 720.0, 799.2,
]

# SMALL02 のちょうど3倍(全段がチップ最小単位 $0.2 の倍数)
SEQ_SMALL06 = [
    0.6, 1.2, 1.8, 2.4, 3.6, 4.8, 6.0,
    7.8, 9.6, 11.4, 13.8, 16.8, 19.2,
    22.2, 25.8, 28.8, 33.0, 37.8, 43.8, 51.0, 60.0, 72.0,
    84.0, 100.2, 120.0, 139.8, 160.2, 180.0, 199.8,
]

SEQ_SMALL6 = [
    6, 10, 14, 24, 34, 46, 60, 76, 94, 114, 136, 166, 194, 224, 256,
    290, 330, 380, 440, 510, 600, 720, 840, 1000, 1200, 1400, 1600, 1800, 2000,
]

SEQ_SMALL10 = [
    10, 20, 30,
    50, 70, 90, 110, 130,
    160, 200, 240, 280, 320,
    370, 420, 470, 520, 570, 620,
    680, 740, 800, 860, 920, 980, 1050,
]

# 旧 NewSEQ30 (bacopy_executor_pragmatic_ws_live.py SEQ_NEW[1:]) 由来・投資家向け
SEQ_SMALL30 = [
    30, 50, 70,
    110, 150, 200, 250, 300,
    370, 450, 530, 610, 700, 810, 940,
    1090, 1260, 1440, 1640, 1870, 2150, 2450, 2800,
]

# 元祖SEQ = 大学ノート版(〇×ロジック仕様書元祖.txt)の48段・$1 start・天井$250。
# marubatsu_strategy.SEQ からimport=旧MaruBatsu系と同一値を保証(重複定義しない)。
# ★プレイヤーSEQ(player*)は元祖忠実がコンセプトのため型(shape)を適用しない=常にこの階段。
# 変種は $1 基準の元祖階段 × 開始額 で比例展開(small2=small1×2 と同流儀)。
# 元祖が全段整数なので ×(0.2の倍数) は必ず最小チップ$0.2の倍数に乗る(丸め不要)。
SEQ_PLAYER1 = list(_MARUBATSU_ORIGINAL_SEQ)
SEQ_PLAYER02 = [round(x * 0.2, 4) for x in SEQ_PLAYER1]  # $0.2 start・天井$50
SEQ_PLAYER04 = [round(x * 0.4, 4) for x in SEQ_PLAYER1]  # $0.4 start・天井$100
SEQ_PLAYER2 = [x * 2 for x in SEQ_PLAYER1]               # $2 start・天井$500
SEQ_PLAYER3 = [x * 3 for x in SEQ_PLAYER1]               # $3 start・天井$750
PLAYER_SEQ_MODES = ("player02", "player04", "player1", "player2", "player3", "playercustom")

# ── 任意開始額SEQ (2026-08-05) ────────────────────────────────────────
# GUIで「スモールSEQ / 元祖SEQ」を選び、開始額を自由入力するためのモード。
# 階段は $1基準の整数配列 × 開始額 で生成する(既存の balance/defense 型や
# player* と同じ流儀)。整数 × ($0.2の倍数) = $0.2の倍数 なので、開始額が
# チップ最小単位の倍数である限り全段が置ける額になる(丸め不要)。
#   smallcustom  : 攻撃型= SEQ_BASE_ATTACK / 型指定時は balance・defense 配列
#   playercustom : 元祖48段 (型は適用しない=元祖忠実)
# 開始額は env `BACOPY_SEQ_START` で渡す。
#
# ★攻撃型の$1基準 = SEQ_SMALL1。small2/small14/small24 が既にこの配列の
#   ×2 / ×1.4 / ×2.4 として定義されており、事実上の基準配列になっている。
#   全段が整数なのでチップ妥当性が保証される。
#   ※small02/small3/small6/small10/small30 は個別に手書きされた別形状で、
#     段数(23〜29)も勾配も揃っていない。任意開始額では $1基準に統一する。
SEQ_BASE_ATTACK = list(SEQ_SMALL1)

CHIP_MIN = 0.2  # Stake/Pragmatic の最小チップ


def _quantize_chip(v: float) -> float:
    """開始額をチップ最小単位($0.2)の倍数に丸める。"""
    return round(round(float(v) / CHIP_MIN) * CHIP_MIN, 2)


def custom_seq_start() -> float:
    """env `BACOPY_SEQ_START` から任意開始額を読む(既定 $1・$0.2刻みに丸め)。"""
    raw = (os.getenv("BACOPY_SEQ_START", "") or "").strip()
    try:
        v = float(raw) if raw else 1.0
    except ValueError:
        logger.warning(f"[SEQ-CUSTOM] invalid BACOPY_SEQ_START={raw!r}; fallback $1")
        v = 1.0
    if v <= 0:
        v = 1.0
    q = _quantize_chip(v)
    if q <= 0:
        q = CHIP_MIN
    if abs(q - v) > 1e-9:
        logger.warning(f"[SEQ-CUSTOM] start ${v} -> ${q} (チップ最小単位${CHIP_MIN}に丸め)")
    return q

# ── SEQ 型(shape) — 階段の“並び”を $1 基準で定義し開始額で比例展開する ──────
# 攻撃型(attack) = 上記 SEQ_SMALL* をそのまま使う(従来=ゼロ回帰)。
# バランス型(balance)=CAND_A / 守備型(defense)=CAND_B は $1 基準の整数配列で、
# 開始額(各versionの先頭額=0.2/0.6/1/3/6/10/30)を掛けて版を生成する。
# 整数×(0.2の倍数)=0.2の倍数 なので全段がチップ妥当額になる(丸め不要)。
# 2026-06-16 のリスク分析(`SEQ_STAIRCASE_ANALYSIS_2026-06-16.md`)に基づく:
#   守備型は必要元本ほぼ半減・破滅テール46%減(利益は約56%維持)。
SEQ_SHAPE_BALANCE = [  # CAND_A: 序盤緩め・天井250x(利益77%維持)
    1, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 27, 33, 40, 48, 58, 70, 84,
    100, 115, 130, 145, 160, 175, 190, 205, 220, 235, 250,
]
SEQ_SHAPE_DEFENSE = [  # CAND_B: 序盤最緩・天井200x(生存最優先)
    1, 1, 1, 2, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 26, 31, 37, 44, 52,
    62, 74, 88, 104, 122, 140, 158, 176, 194, 200,
]
SMALL_SEQ_MODES = ("small02", "small06", "small1", "small14", "small2", "small24", "small3", "small6", "small10", "small30", "smallcustom") + PLAYER_SEQ_MODES

BET_MODES = {
    "flat": "1 unit flat",
    "small02": "SMALL SEQ $0.20 start",
    "small06": "SMALL SEQ $0.60 start",
    "small1": "SMALL SEQ $1 start",
    "small14": "SMALL SEQ $1.40 start",
    "small2": "SMALL SEQ $2 start",
    "small24": "SMALL SEQ $2.40 start",
    "small3": "SMALL SEQ $3 start",
    "small6": "SMALL SEQ $6 start",
    "small10": "SMALL SEQ $10 start",
    "small30": "SMALL SEQ $30 start (ex-NewSEQ30)",
    "player02": "PLAYER SEQ $0.20 start (original 48-step x0.2, ceiling $50)",
    "player04": "PLAYER SEQ $0.40 start (original 48-step x0.4, ceiling $100)",
    "player1": "PLAYER SEQ $1 start (original 48-step, ceiling $250)",
    "player2": "PLAYER SEQ $2 start (original 48-step x2, ceiling $500)",
    "player3": "PLAYER SEQ $3 start (original 48-step x3, ceiling $750)",
    "smallcustom": "SMALL SEQ custom start (BACOPY_SEQ_START x $1-base ladder)",
    "playercustom": "PLAYER SEQ custom start (BACOPY_SEQ_START x original 48-step)",
    "martingale": "pure Martingale",
    "dalembert": "D'Alembert (+/-1 unit)",
    "bet123": "1-2-3 method (1/2/3 units cycle)",
    "bet123set": "1-2-3 per N-hand set (set-level 1/2/3, faithful 1-2-3 cycle)",
    "dalembertset": "D'Alembert per N-hand set (+1 unit on losing set, -1 on winning set)",
    "kelly": "Kelly proportional (bet = f x bankroll)",
    "fibgrid": "Fibonacci 2D grid (win=right/lose=down, profit col C-G + stop-loss row)",
}
ALLOWED_MODES = set(BET_MODES.keys())

BANKER_COMMISSION = 0.95

# ── Kelly(比例)モード ─────────────────────────────────────────────────
# bet = type係数 × (edge/payout) × 現在残高。残高に比例するのでゼロにならない
# (破滅しない)・複利で伸びる。SEQの真逆=負けで増やさず、エッジと残高に比例。
# 型(BACOPY_SEQ_SHAPE流用): 攻撃=フルKelly / バランス=ハーフ / 守備=クォーター。
# edge=1手あたりのEV(既定=v3実測+0.61%)。payout=0.97(60%Banker*0.95+40%Player)。
# 2026-06-20 徹底バックテスト(`_seq_pause_test/kelly_research.py`)で確定:
#   守備=~2x/年・DD中央27%・エッジ誤差に頑健。攻撃=フルはDD78%・エッジ過信で全損注意。
KELLY_PAYOUT = 0.97
KELLY_CHIP = 0.2
KELLY_SHAPE_MULT = {"attack": 1.0, "balance": 0.5, "defense": 0.25}

# ── フィボグリッド(2次元)モード ────────────────────────────────────────
# トラッカー.html(発案者システム)の忠実移植+損切り行(当社追加)。
# 起点B4・勝ち→右(列+1)/負け→下(行+1)・セル値×unit=BET額。
# 利確列到達後: 1勝目 or 1勝1敗で起点リセット(利確)。2連敗→利確列最上段/
# 3連敗→2個上/4連敗以上→1個上へ移動し、勝敗同数(トントン)まで続行。
# 損切り行: 許容行(cap_rows)を負けで超えたら損失確定して起点リセット。
# BT検証=`_bt_grid_fib_safe.py`+`FIB_GRID_SAFE_BT_2026-07-17.md`(TIE=押し)。
# 設定: env BACOPY_FIBGRID_PROFIT_COL(C-G・既定C) / BACOPY_FIBGRID_CAP_ROWS(既定6)。
# 開始額は unit(--money-unit)で比例スケール(セル値は$1基準ユニット)。
FIBGRID_COLS = ["B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"]
FIBGRID_ROW_START = 4
FIBGRID_GRID = [[1,None,None,None,None,None,None,None,None,None,None,None,None,None,None],[1,2,None,None,None,None,None,None,None,None,None,None,None,None,None],[2,2,3,None,None,None,None,None,None,None,None,None,None,None,None],[3,4,3,4,None,None,None,None,None,None,None,None,None,None,None],[5,6,7,4,5,None,None,None,None,None,None,None,None,None,None],[8,10,10,8,5,6,None,None,None,None,None,None,None,None,None],[13,16,17,12,11,6,7,None,None,None,None,None,None,None,None],[21,26,27,20,16,16,7,8,None,None,None,None,None,None,None],[34,42,44,32,27,23,15,8,9,None,None,None,None,None,None],[55,68,71,52,43,39,22,17,9,10,None,None,None,None,None],[89,110,115,84,70,62,39,30,26,10,11,None,None,None,None],[144,178,186,136,113,101,61,56,41,36,11,12,None,None,None],[233,288,301,220,183,163,101,91,82,47,48,12,13,None,None],[377,466,487,356,296,264,163,148,138,84,60,61,13,14,None],[610,754,788,576,479,427,264,239,228,132,131,74,75,14,15],[987,1220,1275,931,775,691,427,387,371,215,206,132,89,90,15],[1597,1974,2062,1507,1254,1118,691,626,602,348,337,206,221,106,107],[2584,3194,3337,2438,2030,1809,1118,1013,973,563,543,417,310,328,114],[4181,5165,5399,3945,3284,2927,1809,1639,1575,910,880,711,638,442,443],[6765,8362,8736,6383,5314,4736,2927,2652,2548,1473,1423,1254,1349,752,770],[10946,13511,14135,10328,8598,7663,4736,4300,4123,2383,2303,1965,1987,2101,1221],[17711,21892,22871,16711,13912,12399,7663,6951,6671,3856,3726,3560,3316,3350,3471],[28657,35403,36900,27039,22510,20062,12399,11251,10794,6266,6029,5525,5540,5453,5521],[46368,57295,59771,43750,36422,32461,20062,18202,17465,10102,9755,9075,8850,8520,8630],[75025,92698,96671,70789,58932,52523,32461,29453,28259,16310,15784,14960,14410,14300,13600],[121393,150005,156442,114539,95354,84984,52523,47666,45724,26405,25539,24071,23190,22488,22370],[196418,242811,253113,185328,154286,137507,84984,77119,73983,42715,41323,39031,37600,36600,36500],[317811,393000,399000,299867,249029,222137,137507,125414,120000,69100,66810,63122,60778,59176,57500]]
FIBGRID_TOP_ROW = {"B": 4, "C": 5, "D": 6, "E": 7, "F": 8, "G": 9, "H": 10, "I": 11,
                   "J": 12, "K": 13, "L": 14, "M": 15, "N": 16, "O": 17, "P": 18}
FIBGRID_PROFIT_COLS = ("C", "D", "E", "F", "G")


def _fibgrid_val(col_idx: int, row: int):
    ri = row - FIBGRID_ROW_START
    if ri < 0 or ri >= len(FIBGRID_GRID):
        return None
    if col_idx < 0 or col_idx >= len(FIBGRID_COLS):
        return None
    return FIBGRID_GRID[ri][col_idx]


def fibgrid_max_bet_units(cap_rows: int) -> int:
    """許容行内の最大セル値(unit倍率) — 起動ログ/GUI表示用。"""
    best = 0
    for ri in range(min(int(cap_rows), len(FIBGRID_GRID))):
        for v in FIBGRID_GRID[ri]:
            if v:
                best = max(best, v)
    return best


# ── BetManager ────────────────────────────────────────────────────────


class BetManager:
    """Dual-Line 用 資金管理。

    状態管理:
      - session_pnl: セッション開始時からの損益 ($)
      - seq_level: SEQ モード時の現在のインデックス
      - loss_count: Martingale 時の連続負け数
      - limit_reached: 利確 or 損切に到達
    """

    def __init__(
        self,
        *,
        mode: str = "flat",
        unit: float = 100.0,
        profit_stop: float = 0.0,
        loss_cut: float = 0.0,
        on_limit: Literal["stop", "restart"] = "stop",
        state_path: Optional[Path] = None,
        seq_set_size: int = 7,
    ):
        self.mode = str(mode or "flat").strip().lower()
        if self.mode not in ALLOWED_MODES:
            logger.warning(f"unknown mode '{self.mode}', fallback to 'flat'")
            self.mode = "flat"

        self.unit = float(unit)
        self.profit_stop = float(profit_stop)
        self.loss_cut = float(loss_cut)
        self.on_limit = "stop" if str(on_limit).lower() not in ("restart",) else "restart"
        self.state_path = state_path

        # セッション管理
        self.session_pnl: float = 0.0
        self.total_bets: int = 0
        self.total_wins: int = 0
        self.total_losses: int = 0
        self.total_ties: int = 0
        self.limit_reached: bool = False
        self.limit_reason: str = ""  # "profit" or "loss"

        # SEQ モード状態
        self.seq_level: int = 0  # SEQ 配列の index
        self.current_seq = self._resolve_seq()
        # セット長(7=標準 / 5=5ターン制)。全SMALL SEQで選択可。
        self.seq_set_size: int = 5 if int(seq_set_size or 7) == 5 else 7
        self._seq7_tracker: MaruBatsuTracker | None = None
        if self.mode in SMALL_SEQ_MODES:
            self._seq7_tracker = MaruBatsuTracker(
                chip_base=1.0,
                seq=list(self.current_seq),
                set_size=self.seq_set_size,
            )
            # 検証/運用用: どの型(階段)で動いているかをログで確認できるようにする。
            _shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "attack").strip().lower() or "attack"
            if self.mode in PLAYER_SEQ_MODES:
                _shape = "original"  # 元祖階段固定・型は適用されない
            try:
                logger.info(
                    f"[SEQ-SHAPE] mode={self.mode} shape={_shape} set_size={self.seq_set_size} "
                    f"start={self.current_seq[0]} top={self.current_seq[-1]} steps={len(self.current_seq)}"
                )
            except Exception:
                pass

        # Martingale 状態
        self.loss_count: int = 0
        self.martingale_max_bet: float = float(
            os.getenv("BACOPY_MARTINGALE_MAX_BET", "0") or 0
        )

        # 1-2-3打法 状態 (0=1単位, 1=2単位, 2=3単位)
        self.b123_step: int = 0
        self.b123_prev_won: Optional[bool] = None  # 1回目の勝敗(2回目の分岐判定用)

        # 1-2-3×セット打法 状態: 1-2-3打法の「上げ方・1への戻り方」を1手でなく
        # Nハンド(=seq_set_size, 5 or 7)=1セット単位で適用する。セット内は固定額(step単位)。
        # セット負け越し→step+1(1→2→3, 3で更に負け越したらstep1へ=1-2-3を一巡=破滅しない)。
        # セット勝ち越し→step1へ戻す。最大3単位なので絶対に破滅しない。
        self.b123set_step: int = 1  # 1/2/3 (= bet倍率)
        self.b123set_marks: list[str] = []  # 現セット内の "O"(勝)/"X"(敗)

        # ダランベール×セット打法 状態: ダランベール(+1/-1ユニット)を1手でなく
        # Nハンド(=seq_set_size)=1セット単位で適用する。セット内は固定額(level単位)。
        # セット負け越し→level+1 / セット勝ち越し→level-1(下限1)。上限なし(loss_cut併用前提)。
        self.dalembertset_level: int = 1  # 1,2,3,... (= bet倍率)
        self.dalembertset_marks: list[str] = []  # 現セット内の "O"/"X"

        # セット履歴(123×セット / ダランベール×セット共通): SEQ の seq7_sets と同じく
        # 完了セットの〇×を残し、GUI が全ストリーム・ハンド数・負け越し(セット単位の
        # 未回収負け越し)を SEQ と同じ見た目で描けるようにする。state に永続化。
        self.b123set_sets: list[dict] = []        # [{"results":"OXX...", "step_after":int}]
        self.dalembertset_sets: list[dict] = []   # [{"results":"OXX...", "level_after":int}]

        # Kelly(比例) 状態
        self.kelly_edge: float = float(os.getenv("BACOPY_KELLY_EDGE", "0.0061") or 0.0061)
        # 型は seq_shape を流用 (attack/balance/defense)
        self.kelly_shape: str = (os.getenv("BACOPY_SEQ_SHAPE", "") or "defense").strip().lower()
        if self.kelly_shape not in KELLY_SHAPE_MULT:
            self.kelly_shape = "defense"
        # フォールバック元本(ライブ残高未取得時): GUIが BACOPY_KELLY_BANKROLL で渡す
        self.kelly_bankroll_init: float = float(os.getenv("BACOPY_KELLY_BANKROLL", "1000") or 1000)
        self._kelly_live_bankroll: float = 0.0  # bot が set_bankroll() で更新
        # 安全上限: 残高の何割を超えてBETしないか(edge過大設定の暴走防止)
        self.kelly_max_frac: float = float(os.getenv("BACOPY_KELLY_MAX_FRAC", "0.05") or 0.05)
        if self.mode == "kelly":
            try:
                logger.info(
                    f"[KELLY] shape={self.kelly_shape} mult={KELLY_SHAPE_MULT[self.kelly_shape]} "
                    f"edge={self.kelly_edge*100:.3f}% f={KELLY_SHAPE_MULT[self.kelly_shape]*self.kelly_edge/KELLY_PAYOUT*100:.3f}%/残高 "
                    f"bankroll_init=${self.kelly_bankroll_init:.0f} max_frac={self.kelly_max_frac}"
                )
            except Exception:
                pass

        # フィボグリッド 状態 (mode="fibgrid")
        # 設定は env が正(GUIプルダウン→childEnv)。state からは位置のみ復元。
        _fpc = (os.getenv("BACOPY_FIBGRID_PROFIT_COL", "") or "C").strip().upper()
        self.fib_profit_col: str = _fpc if _fpc in FIBGRID_PROFIT_COLS else "C"
        try:
            _fcr = int(float(os.getenv("BACOPY_FIBGRID_CAP_ROWS", "6") or 6))
        except Exception:
            _fcr = 6
        self.fib_cap_rows: int = max(4, min(len(FIBGRID_GRID), _fcr))
        self.fib_cap_row: int = FIBGRID_ROW_START + self.fib_cap_rows - 1
        self.fib_col: str = "B"
        self.fib_row: int = FIBGRID_ROW_START
        self.fib_in_profit: bool = False
        self.fib_w: int = 0
        self.fib_l: int = 0
        self.fib_loss_streak: int = 0
        self.fib_ever2: bool = False
        self.fib_cycle_pnl: float = 0.0
        self.fib_takes: int = 0   # 利確リセット回数(1勝目/1勝1敗トントン)
        self.fib_stops: int = 0   # 損切りリセット回数(損切り行超え)
        if self.mode == "fibgrid":
            try:
                logger.info(
                    f"[FIBGRID] profit_col={self.fib_profit_col} cap_rows={self.fib_cap_rows} "
                    f"unit=${self.unit} max_bet=${fibgrid_max_bet_units(self.fib_cap_rows) * self.unit:.2f}"
                )
            except Exception:
                pass

        # 前回のベット額（結果反映まで保持）
        self._last_bet_amount: float = 0.0

        if self.state_path:
            self._load_state()

    def _resolve_seq(self) -> list[float]:
        m = self.mode
        # ── 任意開始額 (2026-08-05): $1基準配列 × 開始額 ──────────────
        if m in ("smallcustom", "playercustom"):
            start = custom_seq_start()
            if m == "playercustom":
                # 元祖SEQは型を適用しない(元祖忠実)
                return [round(x * start, 4) for x in SEQ_PLAYER1]
            shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "").strip().lower()
            if shape in ("balance", "a", "bal", "cand_a"):
                base = SEQ_SHAPE_BALANCE
            elif shape in ("defense", "defence", "b", "def", "cand_b"):
                base = SEQ_SHAPE_DEFENSE
            else:
                base = SEQ_BASE_ATTACK
            return [round(x * start, 4) for x in base]
        # 攻撃型(=従来)の基準配列
        attack = {
            "small02": SEQ_SMALL02, "small06": SEQ_SMALL06, "small1": SEQ_SMALL1,
            "small14": SEQ_SMALL14, "small2": SEQ_SMALL2, "small24": SEQ_SMALL24,
            "small3": SEQ_SMALL3, "small6": SEQ_SMALL6,
            "small10": SEQ_SMALL10, "small30": SEQ_SMALL30,
            "player02": SEQ_PLAYER02, "player04": SEQ_PLAYER04,
            "player1": SEQ_PLAYER1, "player2": SEQ_PLAYER2, "player3": SEQ_PLAYER3,
        }.get(m)
        if attack is None:
            return [1.0]
        attack = list(attack)
        # ── 型(shape)切替: balance=CAND_A / defense=CAND_B を開始額で比例展開 ──
        # 既定 attack は従来挙動のまま(ゼロ回帰)。env `BACOPY_SEQ_SHAPE` で切替。
        shape = (os.getenv("BACOPY_SEQ_SHAPE", "") or "").strip().lower()
        # player*(プレイヤーSEQ=元祖忠実)は型を適用しない=常に元祖48段(×開始額)のまま
        if m in PLAYER_SEQ_MODES:
            return attack
        if m in SMALL_SEQ_MODES and shape in ("balance", "a", "bal", "cand_a"):
            start = attack[0]
            return [round(x * start, 4) for x in SEQ_SHAPE_BALANCE]
        if m in SMALL_SEQ_MODES and shape in ("defense", "defence", "b", "def", "cand_b"):
            start = attack[0]
            return [round(x * start, 4) for x in SEQ_SHAPE_DEFENSE]
        return attack

    # ── ベット計算 ──────────────────────────────────────────────

    def _compute_next_bet(self) -> float:
        """次のベット額を計算する（状態を変更しない純粋な計算）。"""
        if self.limit_reached:
            return 0.0
        if self.mode == "flat":
            return self.unit
        elif self.mode == "martingale":
            amount = self.unit * (2 ** self.loss_count)
            if self.martingale_max_bet > 0:
                amount = min(amount, self.martingale_max_bet)
            if self.loss_cut > 0:
                # 損切り残額を超えるベットは行わない
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "dalembert":
            # D'Alembert: 負けで +1 unit, 勝ちで -1 unit。loss_count を段数に使う。
            # 例 unit=2: 2 -> 4 -> 6 -> 8 -> (勝) 6 -> 4 ...
            amount = self.unit * (self.loss_count + 1)
            if self.loss_cut > 0:
                # 損切り残額を超えるベットは行わない
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "bet123":
            # 1-2-3打法: 段数は apply_result の状態機械が管理
            amount = self.unit * (self.b123_step + 1)
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "bet123set":
            # 1-2-3×セット打法: セット内は固定額(step単位)。stepは apply_result が
            # セット確定(Nハンド)ごとに 1-2-3打法の規則で更新する。最大3単位=破滅しない。
            amount = self.unit * self.b123set_step
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "dalembertset":
            # ダランベール×セット打法: セット内は固定額(level単位)。levelは apply_result が
            # セット確定ごとに ±1 する(負け越し+1/勝ち越し-1, 下限1)。上限なし=loss_cut推奨。
            amount = self.unit * self.dalembertset_level
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "kelly":
            # Kelly(比例): bet = 型係数 × (edge/payout) × 現在残高。
            # 残高 = ライブ残高(bot が set_bankroll)優先, 無ければ 設定元本+session_pnl。
            bank = self._kelly_live_bankroll if self._kelly_live_bankroll > 0 else (
                self.kelly_bankroll_init + self.session_pnl
            )
            if bank <= 0:
                return 0.0
            mult = KELLY_SHAPE_MULT.get(self.kelly_shape, 0.25)
            f = mult * (self.kelly_edge / KELLY_PAYOUT)
            amount = f * bank
            amount = min(amount, self.kelly_max_frac * bank)  # 安全上限
            # チップ最小単位($0.2)に丸める
            amount = round(amount / KELLY_CHIP) * KELLY_CHIP
            if amount < KELLY_CHIP:
                amount = KELLY_CHIP
            if amount > bank:
                amount = bank
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        elif self.mode == "fibgrid":
            # フィボグリッド: 現在セル値 × unit。loss_cut残額を超えるBETは行わない。
            v = _fibgrid_val(FIBGRID_COLS.index(self.fib_col), self.fib_row)
            amount = (v or 0) * self.unit
            if self.loss_cut > 0:
                remaining_loss = self.loss_cut + self.session_pnl
                if remaining_loss <= 0:
                    return 0.0
                amount = min(amount, remaining_loss)
            return max(amount, 0.0)
        else:
            seq = self.current_seq
            if self._seq7_tracker is not None:
                level = min(self._seq7_tracker.current_unit_idx, len(seq) - 1)
                self.seq_level = level
            else:
                level = min(self.seq_level, len(seq) - 1)
            return seq[level]

    def set_bankroll(self, balance) -> None:
        """Kelly用: bot が読んだライブ残高を渡す。0以下/不正は無視(フォールバック維持)。"""
        try:
            b = float(balance)
            if b > 0:
                self._kelly_live_bankroll = b
        except Exception:
            pass

    def next_bet(self, side: str = "P") -> float:
        """次のベット額を計算し _last_bet_amount を更新する。"""
        amount = self._compute_next_bet()
        if amount > 0:
            self._last_bet_amount = amount
        return amount

    def apply_result(self, won: bool | None, side: str = "P") -> None:
        """ベット結果を反映する。

        Args:
            won: True=勝ち, False=負け, None=TIE
            side: "P" or "B" — BANKER 勝利時は 5% コミッションを適用
        """
        if self.limit_reached:
            return

        amount = self._last_bet_amount
        self.total_bets += 1

        if won is None:
            # TIE → PnL 変動なし、SEQ/Martingale は現状維持
            self.total_ties += 1
            return

        if won:
            commission = BANKER_COMMISSION if str(side).upper() in ("B", "BANKER") else 1.0
            self.total_wins += 1
            self.session_pnl += amount * commission
            # SMALL系は受け子GUIと同じ7ターン管理で進行
            if self._seq7_tracker is not None:
                self._seq7_tracker.add_result("player")
                self.seq_level = self._seq7_tracker.current_unit_idx
            # 従来SEQ: 勝ったら先頭に戻る
            elif self.mode not in ("flat", "martingale", "dalembert", "bet123", "bet123set", "dalembertset", "kelly"):
                self.seq_level = 0
            # Martingale: リセット / D'Alembert: 1段下げる(下限0)
            if self.mode == "dalembert":
                self.loss_count = max(0, self.loss_count - 1)
            else:
                self.loss_count = 0
        else:
            self.total_losses += 1
            self.session_pnl -= amount
            # SMALL系は受け子GUIと同じ7ターン管理で進行
            if self._seq7_tracker is not None:
                self._seq7_tracker.add_result("banker")
                self.seq_level = self._seq7_tracker.current_unit_idx
            # 従来SEQ: レベル進行
            elif self.mode not in ("flat", "martingale", "dalembert", "bet123", "bet123set", "dalembertset", "kelly"):
                self.seq_level = min(self.seq_level + 1, len(self.current_seq) - 1)
            # Martingale / D'Alembert: 1段上げる
            if self.mode in ("martingale", "dalembert"):
                self.loss_count += 1

        # 1-2-3打法: 1回目→必ず2単位 / 2回目→1回目と同結果なら3単位・割れたらリセット / 3回目→必ずリセット
        if self.mode == "bet123":
            if self.b123_step == 0:
                self.b123_prev_won = won
                self.b123_step = 1
            elif self.b123_step == 1:
                self.b123_step = 2 if self.b123_prev_won == won else 0
            else:
                self.b123_step = 0
            self.seq_level = self.b123_step  # 表示用ミラー

        # 1-2-3×セット打法: 現セットに勝敗を積み、Nハンド揃ったらセット純結果で step 更新。
        #   負け越し → step+1 (1→2→3, step3で更に負け越し→step1=1-2-3を一巡)
        #   勝ち越し → step1 へリセット
        # (TIE は won is None で上で return 済み=セットにカウントしない=7ハンド=7決着)
        if self.mode == "bet123set":
            self.b123set_marks.append("O" if won else "X")
            if len(self.b123set_marks) >= self.seq_set_size:
                wins = self.b123set_marks.count("O")
                losses = len(self.b123set_marks) - wins
                if wins - losses < 0:  # セット負け越し
                    self.b123set_step = self.b123set_step + 1 if self.b123set_step < 3 else 1
                else:                  # セット勝ち越し
                    self.b123set_step = 1
                self.b123set_sets.append({"results": "".join(self.b123set_marks),
                                          "step_after": self.b123set_step})
                self.b123set_sets = self.b123set_sets[-500:]  # 肥大保険
                self.b123set_marks = []
            self.seq_level = self.b123set_step - 1  # 表示用ミラー(0基準)

        # ダランベール×セット打法: 現セットに勝敗を積み、Nハンドでセット純結果で level ±1。
        #   負け越し → level+1 / 勝ち越し → level-1 (下限1)。上限なし(loss_cut推奨)。
        if self.mode == "dalembertset":
            self.dalembertset_marks.append("O" if won else "X")
            if len(self.dalembertset_marks) >= self.seq_set_size:
                wins = self.dalembertset_marks.count("O")
                losses = len(self.dalembertset_marks) - wins
                if wins - losses < 0:  # セット負け越し
                    self.dalembertset_level += 1
                else:                  # セット勝ち越し
                    self.dalembertset_level = max(1, self.dalembertset_level - 1)
                self.dalembertset_sets.append({"results": "".join(self.dalembertset_marks),
                                               "level_after": self.dalembertset_level})
                self.dalembertset_sets = self.dalembertset_sets[-500:]  # 肥大保険
                self.dalembertset_marks = []
            self.seq_level = self.dalembertset_level - 1  # 表示用ミラー(0基準)

        # フィボグリッド: 位置更新(トラッカーHTML仕様・BT `_bt_grid_fib_safe.py` と同一ロジック)
        if self.mode == "fibgrid":
            _comm = BANKER_COMMISSION if str(side).upper() in ("B", "BANKER") else 1.0
            self.fib_cycle_pnl += (amount * _comm) if won else (-amount)
            if not self.fib_in_profit:
                if won:
                    _ci = FIBGRID_COLS.index(self.fib_col)
                    if _fibgrid_val(_ci + 1, self.fib_row) is not None:
                        self.fib_col = FIBGRID_COLS[_ci + 1]
                else:
                    if _fibgrid_val(FIBGRID_COLS.index(self.fib_col), self.fib_row + 1) is not None:
                        self.fib_row += 1
                if (not self.fib_in_profit) and self.fib_col == self.fib_profit_col:
                    self.fib_in_profit = True
                    self.fib_w = self.fib_l = self.fib_loss_streak = 0
                    self.fib_ever2 = False
            else:
                if won:
                    self.fib_w += 1
                    self.fib_loss_streak = 0
                else:
                    self.fib_l += 1
                    self.fib_loss_streak += 1
                    if _fibgrid_val(FIBGRID_COLS.index(self.fib_col), self.fib_row + 1) is not None:
                        self.fib_row += 1
                    _top = FIBGRID_TOP_ROW[self.fib_profit_col]
                    if self.fib_loss_streak == 2:
                        self.fib_row = _top
                        self.fib_ever2 = True
                    elif self.fib_loss_streak == 3:
                        self.fib_row = max(_top, self.fib_row - 2)
                        self.fib_ever2 = True
                    elif self.fib_loss_streak >= 4:
                        self.fib_row = max(_top, self.fib_row - 1)
                        self.fib_ever2 = True
                # ターン終了(利確)判定: 2連敗未経験=1勝目 or 1勝1敗 / 経験後=勝敗同数(最後が勝ち)
                if not self.fib_ever2:
                    if self.fib_w == 1 and self.fib_l in (0, 1):
                        self._fibgrid_end_turn(stop_loss=False)
                else:
                    if self.fib_w > 0 and self.fib_w == self.fib_l and self.fib_loss_streak == 0:
                        self._fibgrid_end_turn(stop_loss=False)
            # 損切り行: 許容行を(負けで)超えたら損失確定して起点リセット
            if self.fib_row > self.fib_cap_row:
                self._fibgrid_end_turn(stop_loss=True)

        # 利確 / 損切判定
        self._check_limits()

        # state 保存
        if self.state_path:
            self._save_state()

    def _fibgrid_wins_to_take(self) -> int:
        """利確確定までの残り最短勝ち数(GUI進行表示用)。
        通常時 = 利確列までの残り列数 + 利確モードでの1勝。
        利確モード中 = 1勝(2連敗経験後は勝敗同数に戻すまでの必要勝ち数)。"""
        if not self.fib_in_profit:
            return (FIBGRID_COLS.index(self.fib_profit_col) - FIBGRID_COLS.index(self.fib_col)) + 1
        if not self.fib_ever2:
            return 1
        return max(1, self.fib_l - self.fib_w)

    def _fibgrid_end_turn(self, stop_loss: bool = False) -> None:
        """フィボグリッド: サイクル終了(利確 or 損切り)→起点B4へリセット。"""
        try:
            if stop_loss:
                self.fib_stops += 1
                logger.info(
                    f"[FIBGRID] STOP-LOSS reset: cycle_pnl=${self.fib_cycle_pnl:+.2f} "
                    f"(stops={self.fib_stops} takes={self.fib_takes})"
                )
            else:
                self.fib_takes += 1
                logger.info(
                    f"[FIBGRID] TAKE-PROFIT reset: cycle_pnl=${self.fib_cycle_pnl:+.2f} "
                    f"(takes={self.fib_takes} stops={self.fib_stops})"
                )
        except Exception:
            pass
        self.fib_cycle_pnl = 0.0
        self.fib_col = "B"
        self.fib_row = FIBGRID_ROW_START
        self.fib_in_profit = False
        self.fib_w = self.fib_l = self.fib_loss_streak = 0
        self.fib_ever2 = False

    def _check_limits(self) -> None:
        if self.profit_stop > 0 and self.session_pnl >= self.profit_stop:
            self.limit_reached = True
            self.limit_reason = "profit"
            logger.info(
                f"[money] profit_stop ${self.profit_stop} reached "
                f"(session_pnl=${self.session_pnl:+.2f})"
            )
            if self.on_limit == "restart":
                self._reset_session()

        elif self.loss_cut > 0 and self.session_pnl <= -self.loss_cut:
            self.limit_reached = True
            self.limit_reason = "loss"
            logger.info(
                f"[money] loss_cut ${self.loss_cut} reached "
                f"(session_pnl=${self.session_pnl:+.2f})"
            )
            if self.on_limit == "restart":
                self._reset_session()

    def _reset_session(self) -> None:
        """セッションリセット（on_limit="restart" 時）。"""
        self.session_pnl = 0.0
        self.seq_level = 0
        self.loss_count = 0
        self.b123_step = 0
        self.b123_prev_won = None
        self.fib_cycle_pnl = 0.0
        self.fib_col = "B"
        self.fib_row = FIBGRID_ROW_START
        self.fib_in_profit = False
        self.fib_w = self.fib_l = self.fib_loss_streak = 0
        self.fib_ever2 = False
        self.limit_reached = False
        self.limit_reason = ""
        logger.info("[money] session reset (restart mode)")

    # ── 状態取得 ────────────────────────────────────────────────

    @property
    def win_rate(self) -> float:
        nt = self.total_wins + self.total_losses
        return self.total_wins / nt * 100 if nt else 0.0

    def status_dict(self) -> dict:
        seq_turn = (len(self._seq7_tracker.current_turns) + 1) if self._seq7_tracker else None
        seq_overshoot = self._seq7_tracker.prev_overshoot if self._seq7_tracker else None
        seq7_sets = [s.__dict__ for s in self._seq7_tracker.sets] if self._seq7_tracker else []
        seq7_current_turns = list(self._seq7_tracker.current_turns) if self._seq7_tracker else []
        return {
            "mode": self.mode,
            "unit": self.unit,
            "profit_stop": self.profit_stop,
            "loss_cut": self.loss_cut,
            "on_limit": self.on_limit,
            "session_pnl": round(self.session_pnl, 2),
            "total_bets": self.total_bets,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_ties": self.total_ties,
            "win_rate": round(self.win_rate, 1),
            "seq_level": self.seq_level,
            "seq_turn": seq_turn,
            "seq_set_size": self.seq_set_size,
            "seq_overshoot": seq_overshoot,
            "seq7_sets": seq7_sets,
            "seq7_current_turns": seq7_current_turns,
            "loss_count": self.loss_count,
            "martingale_max_bet": self.martingale_max_bet,
            "b123set_step": self.b123set_step if self.mode == "bet123set" else None,
            "b123set_marks": list(self.b123set_marks) if self.mode == "bet123set" else None,
            "b123set_sets": list(self.b123set_sets) if self.mode == "bet123set" else None,
            "b123set_set_size": self.seq_set_size if self.mode == "bet123set" else None,
            "dalembertset_level": self.dalembertset_level if self.mode == "dalembertset" else None,
            "dalembertset_marks": list(self.dalembertset_marks) if self.mode == "dalembertset" else None,
            "dalembertset_sets": list(self.dalembertset_sets) if self.mode == "dalembertset" else None,
            "dalembertset_set_size": self.seq_set_size if self.mode == "dalembertset" else None,
            "limit_reached": self.limit_reached,
            "limit_reason": self.limit_reason,
            "next_bet": round(self._compute_next_bet(), 2),
            "kelly_shape": self.kelly_shape if self.mode == "kelly" else None,
            "kelly_bankroll": (round(self._kelly_live_bankroll, 2) if self._kelly_live_bankroll > 0
                               else round(self.kelly_bankroll_init + self.session_pnl, 2)) if self.mode == "kelly" else None,
            "kelly_edge": self.kelly_edge if self.mode == "kelly" else None,
            "fibgrid": {
                "profit_col": self.fib_profit_col,
                "cap_rows": self.fib_cap_rows,
                "col": self.fib_col,
                "row": self.fib_row,
                "row_depth": self.fib_row - FIBGRID_ROW_START + 1,
                "rows_left": max(0, self.fib_cap_row - self.fib_row),
                "in_profit": self.fib_in_profit,
                "cycle_pnl": round(self.fib_cycle_pnl, 2),
                "takes": self.fib_takes,
                "stops": self.fib_stops,
                "max_bet": round(fibgrid_max_bet_units(self.fib_cap_rows) * self.unit, 2),
                "wins_to_take": self._fibgrid_wins_to_take(),
            } if self.mode == "fibgrid" else None,
        }

    # ── 状態保存 ────────────────────────────────────────────────

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.write_text(
                json.dumps(
                    {
                        "mode": self.mode,
                        "unit": self.unit,
                        "profit_stop": self.profit_stop,
                        "loss_cut": self.loss_cut,
                        "on_limit": self.on_limit,
                        "session_pnl": round(self.session_pnl, 2),
                        "total_bets": self.total_bets,
                        "total_wins": self.total_wins,
                        "total_losses": self.total_losses,
                        "total_ties": self.total_ties,
                        "seq_level": self.seq_level,
                        "seq_set_size": self.seq_set_size,
                        "seq7_sets": [s.__dict__ for s in (self._seq7_tracker.sets if self._seq7_tracker else [])],
                        "seq7_current_turns": list(self._seq7_tracker.current_turns) if self._seq7_tracker else [],
                        "loss_count": self.loss_count,
                        "b123_step": self.b123_step,
                        "b123_prev_won": self.b123_prev_won,
                        "b123set_step": self.b123set_step,
                        "b123set_marks": list(self.b123set_marks),
                        "b123set_sets": list(self.b123set_sets),
                        "dalembertset_level": self.dalembertset_level,
                        "dalembertset_marks": list(self.dalembertset_marks),
                        "dalembertset_sets": list(self.dalembertset_sets),
                        "fib_col": self.fib_col,
                        "fib_row": self.fib_row,
                        "fib_in_profit": self.fib_in_profit,
                        "fib_w": self.fib_w,
                        "fib_l": self.fib_l,
                        "fib_loss_streak": self.fib_loss_streak,
                        "fib_ever2": self.fib_ever2,
                        "fib_cycle_pnl": round(self.fib_cycle_pnl, 4),
                        "fib_takes": self.fib_takes,
                        "fib_stops": self.fib_stops,
                        "fib_profit_col": self.fib_profit_col,
                        "fib_cap_rows": self.fib_cap_rows,
                        "limit_reached": self.limit_reached,
                        "limit_reason": self.limit_reason,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"money state save failed: {e}")

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
            self.session_pnl = float(s.get("session_pnl", 0.0))
            self.total_bets = int(s.get("total_bets", 0))
            self.total_wins = int(s.get("total_wins", 0))
            self.total_losses = int(s.get("total_losses", 0))
            self.total_ties = int(s.get("total_ties", 0))
            self.seq_level = int(s.get("seq_level", 0))
            if self._seq7_tracker is not None:
                raw_sets = s.get("seq7_sets", []) or []
                parsed_sets: list[SetData] = []
                if isinstance(raw_sets, list):
                    for item in raw_sets:
                        if isinstance(item, dict):
                            try:
                                parsed_sets.append(SetData(**item))
                            except Exception:
                                continue
                self._seq7_tracker.sets = parsed_sets
                # セット長が前回保存時と違う場合は進行中セットを破棄して仕切り直し
                # (確定済みセット=overshoot/段位置は数値で保存されているため引き継がれる)
                saved_set_size = int(s.get("seq_set_size", 7) or 7)
                raw_turns = s.get("seq7_current_turns", []) or []
                if saved_set_size == self.seq_set_size and isinstance(raw_turns, list):
                    self._seq7_tracker.current_turns = [t for t in raw_turns if t in ("O", "X")]
                elif raw_turns:
                    logger.info(
                        f"[money] seq set size changed {saved_set_size}->{self.seq_set_size}: "
                        f"discard in-progress turns ({len(raw_turns)})"
                    )
                self.seq_level = self._seq7_tracker.current_unit_idx
            self.loss_count = int(s.get("loss_count", 0))
            self.b123_step = max(0, min(2, int(s.get("b123_step", 0))))
            _bpw = s.get("b123_prev_won", None)
            self.b123_prev_won = _bpw if isinstance(_bpw, bool) else None
            self.b123set_step = max(1, min(3, int(s.get("b123set_step", 1) or 1)))
            _bm = s.get("b123set_marks", []) or []
            self.b123set_marks = [m for m in _bm if m in ("O", "X")] if isinstance(_bm, list) else []
            _bs = s.get("b123set_sets", []) or []
            self.b123set_sets = [x for x in _bs if isinstance(x, dict) and x.get("results")] if isinstance(_bs, list) else []
            self.dalembertset_level = max(1, int(s.get("dalembertset_level", 1) or 1))
            _dm = s.get("dalembertset_marks", []) or []
            self.dalembertset_marks = [m for m in _dm if m in ("O", "X")] if isinstance(_dm, list) else []
            _ds = s.get("dalembertset_sets", []) or []
            self.dalembertset_sets = [x for x in _ds if isinstance(x, dict) and x.get("results")] if isinstance(_ds, list) else []
            # フィボグリッド: 設定(利確列/損切り行)が前回保存時と同じ場合のみ位置を復元。
            # 変わっていたら進行中サイクルを破棄して起点から(累計 takes/stops は引き継ぐ)。
            self.fib_takes = max(0, int(s.get("fib_takes", 0) or 0))
            self.fib_stops = max(0, int(s.get("fib_stops", 0) or 0))
            _saved_pc = str(s.get("fib_profit_col", "") or "")
            _saved_cap = int(s.get("fib_cap_rows", 0) or 0)
            if _saved_pc == self.fib_profit_col and _saved_cap == self.fib_cap_rows:
                _fc = str(s.get("fib_col", "B") or "B")
                _fr = int(s.get("fib_row", FIBGRID_ROW_START) or FIBGRID_ROW_START)
                if (_fc in FIBGRID_COLS
                        and FIBGRID_COLS.index(_fc) <= FIBGRID_COLS.index(self.fib_profit_col)
                        and FIBGRID_ROW_START <= _fr <= self.fib_cap_row
                        and _fibgrid_val(FIBGRID_COLS.index(_fc), _fr) is not None):
                    self.fib_col = _fc
                    self.fib_row = _fr
                    self.fib_in_profit = bool(s.get("fib_in_profit", False))
                    self.fib_w = max(0, int(s.get("fib_w", 0) or 0))
                    self.fib_l = max(0, int(s.get("fib_l", 0) or 0))
                    self.fib_loss_streak = max(0, int(s.get("fib_loss_streak", 0) or 0))
                    self.fib_ever2 = bool(s.get("fib_ever2", False))
                    self.fib_cycle_pnl = float(s.get("fib_cycle_pnl", 0.0) or 0.0)
            elif self.mode == "fibgrid" and _saved_pc:
                logger.info(
                    f"[FIBGRID] config changed ({_saved_pc}/cap{_saved_cap} -> "
                    f"{self.fib_profit_col}/cap{self.fib_cap_rows}): discard in-progress cycle"
                )
            self.limit_reached = bool(s.get("limit_reached", False))
            self.limit_reason = str(s.get("limit_reason", ""))
            # 設定は復元しない（GUI の値が正）
        except Exception as e:
            logger.debug(f"money state load failed: {e}")
