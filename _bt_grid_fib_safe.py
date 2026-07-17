#!/usr/bin/env python3
"""
フィボナッチ2次元グリッド「安全版」BT (2026-07-17 オーナー相談・有料サービス候補評価)
変更点 (vs _bt_grid_fib.py で却下した素のグリッド):
  - 単位 $0.2 固定 (small化)
  - 利確列を手前に: D / E / F (+参照用 G)
  - ★行キャップ=損切り行: キャップ行で負けたら損失を確定して起点(B4)へ強制リセット
指標: 中央PnL / p5 PnL(下位5%) / p99 DD / 最大BET / リセット回数 — flat・SEQ small と比較。
データ: data/seqbt/v3_seq.txt (実v3列) + MC2000試行 (tie9.5%・side分布=実データ)。
"""
import os, sys, random, statistics
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_rev53144"))

BANKER_COMMISSION = 0.95
UNIT = 0.2

COLS = ["B","C","D","E","F","G","H","I","J","K","L","M","N","O","P"]
ROW_START = 4
GRID = [[1,None,None,None,None,None,None,None,None,None,None,None,None,None,None],[1,2,None,None,None,None,None,None,None,None,None,None,None,None,None],[2,2,3,None,None,None,None,None,None,None,None,None,None,None,None],[3,4,3,4,None,None,None,None,None,None,None,None,None,None,None],[5,6,7,4,5,None,None,None,None,None,None,None,None,None,None],[8,10,10,8,5,6,None,None,None,None,None,None,None,None,None],[13,16,17,12,11,6,7,None,None,None,None,None,None,None,None],[21,26,27,20,16,16,7,8,None,None,None,None,None,None,None],[34,42,44,32,27,23,15,8,9,None,None,None,None,None,None],[55,68,71,52,43,39,22,17,9,10,None,None,None,None,None],[89,110,115,84,70,62,39,30,26,10,11,None,None,None,None],[144,178,186,136,113,101,61,56,41,36,11,12,None,None,None],[233,288,301,220,183,163,101,91,82,47,48,12,13,None,None],[377,466,487,356,296,264,163,148,138,84,60,61,13,14,None],[610,754,788,576,479,427,264,239,228,132,131,74,75,14,15],[987,1220,1275,931,775,691,427,387,371,215,206,132,89,90,15],[1597,1974,2062,1507,1254,1118,691,626,602,348,337,206,221,106,107],[2584,3194,3337,2438,2030,1809,1118,1013,973,563,543,417,310,328,114],[4181,5165,5399,3945,3284,2927,1809,1639,1575,910,880,711,638,442,443],[6765,8362,8736,6383,5314,4736,2927,2652,2548,1473,1423,1254,1349,752,770],[10946,13511,14135,10328,8598,7663,4736,4300,4123,2383,2303,1965,1987,2101,1221],[17711,21892,22871,16711,13912,12399,7663,6951,6671,3856,3726,3560,3316,3350,3471],[28657,35403,36900,27039,22510,20062,12399,11251,10794,6266,6029,5525,5540,5453,5521],[46368,57295,59771,43750,36422,32461,20062,18202,17465,10102,9755,9075,8850,8520,8630],[75025,92698,96671,70789,58932,52523,32461,29453,28259,16310,15784,14960,14410,14300,13600],[121393,150005,156442,114539,95354,84984,52523,47666,45724,26405,25539,24071,23190,22488,22370],[196418,242811,253113,185328,154286,137507,84984,77119,73983,42715,41323,39031,37600,36600,36500],[317811,393000,399000,299867,249029,222137,137507,125414,120000,69100,66810,63122,60778,59176,57500]]
TOP_ROW = {"B":4,"C":5,"D":6,"E":7,"F":8,"G":9,"H":10,"I":11,"J":12,"K":13,"L":14,"M":15,"N":16,"O":17,"P":18}

def _val(ci, r):
    ri = r - ROW_START
    if ri < 0 or ri >= len(GRID): return None
    if ci < 0 or ci >= len(COLS): return None
    return GRID[ri][ci]

class GridMoneySafe:
    """行キャップつきグリッド。cap_rows=None で素のグリッド(従来挙動)。"""
    def __init__(self, unit=UNIT, loss_cut=1e12, profit_col="G", cap_rows=None):
        self.unit = unit
        self.loss_cut = loss_cut
        self.profit_col = profit_col
        # cap_rows=N: 許容行 4..(3+N)。そこで負けたら損切りリセット。
        self.cap_row = (ROW_START + cap_rows - 1) if cap_rows else None
        self.col = "B"; self.row = 4
        self.in_profit = False
        self.w = self.l = self.loss_streak = 0
        self.ever2 = False
        self.session_pnl = 0.0
        self.limit_reached = False
        self._last = 0.0
        self.max_bet = 0.0
        self.n_resets = 0          # 損切りリセット回数
        self._cycle_pnl = 0.0      # 現サイクル(起点から)の実現PnL
        self.worst_cycle = 0.0     # 最悪サイクル損失

    def next_bet(self):
        if self.limit_reached: return 0.0
        v = _val(COLS.index(self.col), self.row)
        if v is None: v = 0
        amt = v * self.unit
        if amt > (self.loss_cut + self.session_pnl):
            self.limit_reached = True
            return 0.0
        self._last = amt
        self.max_bet = max(self.max_bet, amt)
        return amt

    def _move_win_normal(self):
        ci = COLS.index(self.col); nci = ci + 1
        if nci >= len(COLS): return
        if _val(nci, self.row) is None: return
        self.col = COLS[nci]
    def _move_lose_normal(self):
        nr = self.row + 1
        if _val(COLS.index(self.col), nr) is None: return
        self.row = nr
    def _profit_shift(self):
        top = TOP_ROW[self.profit_col]
        if self.loss_streak == 2:
            self.row = top; self.ever2 = True
        elif self.loss_streak == 3:
            self.row = max(top, self.row - 2); self.ever2 = True
        elif self.loss_streak >= 4:
            self.row = max(top, self.row - 1); self.ever2 = True
    def _end_turn(self, stop_loss=False):
        if stop_loss:
            self.n_resets += 1
            self.worst_cycle = min(self.worst_cycle, self._cycle_pnl)
        self._cycle_pnl = 0.0
        self.col = "B"; self.row = 4; self.in_profit = False
        self.w = self.l = self.loss_streak = 0; self.ever2 = False

    def apply_result(self, won, side="P"):
        if self.limit_reached: return
        if won is None:
            return
        amt = self._last
        if won:
            comm = BANKER_COMMISSION if str(side).upper().startswith("B") else 1.0
            self.session_pnl += amt * comm; self._cycle_pnl += amt * comm
        else:
            self.session_pnl -= amt; self._cycle_pnl -= amt
        if self.session_pnl <= -self.loss_cut:
            self.limit_reached = True; return
        if not self.in_profit:
            if won: self._move_win_normal()
            else:   self._move_lose_normal()
            if (not self.in_profit) and self.col == self.profit_col:
                self.in_profit = True
                self.w = self.l = self.loss_streak = 0; self.ever2 = False
        else:
            if won:
                self.w += 1; self.loss_streak = 0
            else:
                self.l += 1; self.loss_streak += 1
                self._move_lose_normal(); self._profit_shift()
            if not self.ever2:
                if (self.w == 1 and self.l == 0) or (self.w == 1 and self.l == 1):
                    self._end_turn()
            else:
                if self.w > 0 and self.w == self.l and self.loss_streak == 0:
                    self._end_turn()
        # ★行キャップ: 許容行を超えたら損切りリセット
        if self.cap_row is not None and self.row > self.cap_row:
            self._end_turn(stop_loss=True)

# ── 実データ ──────────────────────────────────────────────────────────
def load_real(path):
    seq = []
    for line in open(path, encoding="utf-8"):
        p = line.split()
        if len(p) < 2: continue
        res, side = p[-2], p[-1]
        seq.append(("W" if res == "WIN" else "L", side))
    return seq

REAL = load_real("data/seqbt/v3_seq.txt")
nW = sum(1 for r,_ in REAL if r=="W")
win_sides = [s for r,s in REAL if r=="W"]
pB_win = sum(1 for s in win_sides if s.upper().startswith("B"))/max(1,len(win_sides))
print(f"[実データ] v3 {len(REAL)}件  WIN={nW}  勝率={100*nW/len(REAL):.2f}%  勝ちのうちBanker={100*pB_win:.0f}%  unit=${UNIT}")

def play_grid(seq, bankroll, profit_col="G", cap_rows=None, unit=UNIT):
    m = GridMoneySafe(unit=unit, loss_cut=bankroll, profit_col=profit_col, cap_rows=cap_rows)
    peak = maxdd = 0.0
    for r, side in seq:
        if m.limit_reached: break
        m.next_bet()
        if m.limit_reached: break
        m.apply_result(won=(r=="W") if r != "T" else None, side=side)  # TIE=押し(旧BTはここが負け扱いのバグ)
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd, m.max_bet, m.limit_reached, m.n_resets, m.worst_cycle

def play_bm(BM, mode, seq, bankroll, ss=7, unit=UNIT, **kw):
    m = BM(mode=mode, unit=unit, loss_cut=bankroll, on_limit="stop", seq_set_size=ss, **kw)
    peak = maxdd = mb = 0.0
    for r, side in seq:
        if m.limit_reached: break
        a = m.next_bet(); mb = max(mb, a)
        m.apply_result(won=(r=="W") if r!="T" else None, side=side)
        peak = max(peak, m.session_pnl); maxdd = max(maxdd, peak - m.session_pnl)
    return m.session_pnl, maxdd, mb, m.limit_reached, 0, 0.0

def mc_stream(p, hands, rng, pB=0.33):
    seq = []
    for _ in range(hands):
        if rng.random() < 0.095: seq.append(("T","P")); continue
        win = rng.random() < p
        side = "B" if rng.random() < pB else "P"
        seq.append(("W" if win else "L", side))
    return seq

def run_mc(playfn, p, hands, n=2000, seed=7, **kw):
    rng = random.Random(seed)
    dds, pnls, mb, resets = [], [], 0.0, []
    for _ in range(n):
        seq = mc_stream(p, hands, rng, pB=pB_win)
        pnl, dd, b, ru, nr, wc = playfn(seq, 1e12, **kw)
        dds.append(dd); pnls.append(pnl); mb = max(mb, b); resets.append(nr)
    dds.sort(); pnls.sort()
    return {"p99_dd": dds[min(len(dds)-1, int(0.99*len(dds)))],
            "med_pnl": statistics.median(pnls),
            "p5_pnl": pnls[int(0.05*len(pnls))],
            "mean_pnl": statistics.mean(pnls),
            "max_bet": mb,
            "med_resets": statistics.median(resets)}

import dual_line_money as DM
import importlib

VARIANTS = [(pc, cap) for pc in ("D","E","F","G") for cap in (6, 8, 10, None)]

print("\n================ ① 実データ1パス (v3 4469件・$0.2) ================")
print(f"{'variant':<24}{'最終PnL$':>10}{'最大DD$':>9}{'最大BET$':>9}{'損切回':>7}{'最悪ｻｲｸﾙ$':>10}")
for pc, cap in VARIANTS:
    pnl, dd, b, ru, nr, wc = play_grid(REAL, 1e12, profit_col=pc, cap_rows=cap)
    lbl = f"Grid 利確{pc}/cap{cap if cap else '∞'}"
    print(f"{lbl:<24}{pnl:>+10.2f}{dd:>9.2f}{b:>9.2f}{nr:>7}{wc:>+10.2f}")
os.environ["BACOPY_SEQ_SHAPE"] = "attack"; importlib.reload(DM)
pnl, dd, b, ru, _, _ = play_bm(DM.BetManager, "small1", REAL, 1e12)
print(f"{'SEQ攻撃(small1)':<24}{pnl:>+10.2f}{dd:>9.2f}{b:>9.2f}")
os.environ["BACOPY_SEQ_SHAPE"] = "defense"; importlib.reload(DM)
pnl, dd, b, ru, _, _ = play_bm(DM.BetManager, "small1", REAL, 1e12)
print(f"{'SEQ守備':<24}{pnl:>+10.2f}{dd:>9.2f}{b:>9.2f}")
importlib.reload(DM)
pnl, dd, b, ru, _, _ = play_bm(DM.BetManager, "flat", REAL, 1e12)
print(f"{'flat($0.2)':<24}{pnl:>+10.2f}{dd:>9.2f}{b:>9.2f}")

print("\n================ ② モンテカルロ (2000試行・$0.2) ================")
for p, plabel in ((0.5127, "通常51.3%"), (0.490, "不調49.0%")):
    for hands, hlabel in ((1250, "1週間相当"), (5000, "1ヶ月相当")):
        print(f"\n--- 勝率{plabel} × {hlabel}({hands}手) ---")
        print(f"{'variant':<24}{'中央PnL$':>9}{'p5PnL$':>9}{'平均PnL$':>9}{'p99DD$':>8}{'最大BET$':>9}{'損切回(中央)':>12}")
        for pc, cap in VARIANTS:
            r = run_mc(lambda s,b,**k: play_grid(s,b,**k), p, hands, n=2000, profit_col=pc, cap_rows=cap)
            lbl = f"Grid 利確{pc}/cap{cap if cap else '∞'}"
            print(f"{lbl:<24}{r['med_pnl']:>+9.2f}{r['p5_pnl']:>+9.2f}{r['mean_pnl']:>+9.2f}{r['p99_dd']:>8.2f}{r['max_bet']:>9.2f}{r['med_resets']:>12.0f}")
        os.environ["BACOPY_SEQ_SHAPE"]="attack"; importlib.reload(DM); BM=DM.BetManager
        r = run_mc(lambda s,b,**k: play_bm(BM,"small1",s,b), p, hands, n=2000)
        print(f"{'SEQ攻撃(small1)':<24}{r['med_pnl']:>+9.2f}{r['p5_pnl']:>+9.2f}{r['mean_pnl']:>+9.2f}{r['p99_dd']:>8.2f}{r['max_bet']:>9.2f}")
        os.environ["BACOPY_SEQ_SHAPE"]="defense"; importlib.reload(DM); BM=DM.BetManager
        r = run_mc(lambda s,b,**k: play_bm(BM,"small1",s,b), p, hands, n=2000)
        print(f"{'SEQ守備':<24}{r['med_pnl']:>+9.2f}{r['p5_pnl']:>+9.2f}{r['mean_pnl']:>+9.2f}{r['p99_dd']:>8.2f}{r['max_bet']:>9.2f}")
        importlib.reload(DM); BM=DM.BetManager
        r = run_mc(lambda s,b,**k: play_bm(BM,"flat",s,b), p, hands, n=2000)
        print(f"{'flat($0.2)':<24}{r['med_pnl']:>+9.2f}{r['p5_pnl']:>+9.2f}{r['mean_pnl']:>+9.2f}{r['p99_dd']:>8.2f}{r['max_bet']:>9.2f}")
