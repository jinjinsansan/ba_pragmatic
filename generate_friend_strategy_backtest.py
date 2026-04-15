"""友人戦略 バックテスト — look-aheadなし・単一財布

戦略:
  エントリー: 大路5-10列目でカオス→「1落ちP+1落ちB」転換を検出
  シグナル1: 珠盤路6行水平読み（数の優位・T対称・3連続）
  シグナル2: 大路パターン（ニコニコ・ニコイチ）
  BET: 両シグナルがPlayer一致時のみ
  資金: 旧SEQ [1,2,3,5,7,9,11,13,...] × 7ターン / 単一$10,000

仮決定事項（Phase 1）:
  - 珠盤路優位: 行内の|P-B| >= 3 → 強、2 → 弱
  - ニコニコ: 2-P予想時は列の1手目と2手目両方に賭ける
  - ニコイチ: Pポジションのみ賭ける
  - エントリー後の退避: なし（シュー終了まで継続）

Usage:
  python generate_friend_strategy_backtest.py
"""
import sqlite3
import os
import json
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50

START_CAPITAL = 10000.0
PROFIT_TARGET = 30.0
BASE_UNIT = 1.0

# 旧SEQ × 7ターン (Player-only = コミッションなし)
SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31,
       35, 39, 43, 47, 51, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100,
       106, 112, 118, 124, 130, 136, 142, 148, 154, 160,
       170, 180, 190, 200, 210, 220, 230, 240, 250]
SET_SIZE = 7

BEAD_ROWS = 6


# ═══════════════════════════════════════════════════
# 珠盤路グリッド + 水平読み
# ═══════════════════════════════════════════════════
def build_bead_grid(hands, rows=BEAD_ROWS):
    """hands (P/B/T list or str) → 6行グリッド。列単位で上から下へ充填、右方向へ伸びる。"""
    if not hands:
        return []
    n = len(hands)
    cols = (n + rows - 1) // rows
    grid = [[None] * cols for _ in range(rows)]
    for i, ch in enumerate(hands):
        r = i % rows
        c = i // rows
        if c < cols:
            grid[r][c] = ch
    return grid


def row_signal(row):
    """1行の水平シグナル: 'P'/'B'/'unclear'。

    Rule 1: |P-B| >= 3 で強優位 → 多い側
    Rule 2: Tを中心に左右2手以上がミラー → ミラー対応側
    Rule 3: 3手以上同方向連続 → 連続延長側
    """
    cells = [c for c in row if c is not None]
    if len(cells) < 4:
        return 'unclear'
    p_count = cells.count('P')
    b_count = cells.count('B')

    # Rule 1: 数の優位
    diff = p_count - b_count
    if diff >= 3:
        return 'P'
    if diff <= -3:
        return 'B'

    # Rule 2: T対称
    t_indices = [i for i, c in enumerate(cells) if c == 'T']
    for ti in t_indices:
        for span in [3, 2]:  # 3先優先、2でも可
            if ti >= span and ti + span < len(cells):
                left = cells[ti - span:ti]
                right = cells[ti + 1:ti + 1 + span][::-1]  # 反転して対応比較
                if left == right and 'T' not in left:
                    # 対称が成立 → 左右と同じ側を予測
                    if left[-1] == 'P':
                        return 'P'
                    if left[-1] == 'B':
                        return 'B'

    # Rule 3: 3手以上連続
    # 末尾の連続を取る
    if len(cells) >= 3:
        last = cells[-1]
        if last == 'T':
            # Tは飛ばして直前を見る
            for c in reversed(cells[:-1]):
                if c != 'T':
                    last = c
                    break
        if last in ('P', 'B'):
            streak = 0
            for c in reversed(cells):
                if c == last:
                    streak += 1
                elif c == 'T':
                    continue
                else:
                    break
            if streak >= 3:
                return last  # 連続継続

    # 弱優位（|diff|==2）
    if diff == 2:
        return 'P'
    if diff == -2:
        return 'B'

    return 'unclear'


def bead_road_signal(hands):
    """珠盤路全体のシグナル: 各行のシグナルの多数決。Tie時は最下位行（最新データ）優先。"""
    grid = build_bead_grid(hands)
    if not grid:
        return 'unclear'
    votes = []
    for row in grid:
        s = row_signal(row)
        if s != 'unclear':
            votes.append(s)
    if not votes:
        return 'unclear'
    p_votes = votes.count('P')
    b_votes = votes.count('B')
    if p_votes > b_votes:
        return 'P'
    if b_votes > p_votes:
        return 'B'
    # Tie → 最新の有効シグナル
    for row in reversed(grid):
        s = row_signal(row)
        if s != 'unclear':
            return s
    return 'unclear'


# ═══════════════════════════════════════════════════
# 大路 (Big Road) パターン判定
# ═══════════════════════════════════════════════════
def big_road_columns(hands):
    """hands (P/B/T list or str) → (column_lengths, column_sides)
    タイは飛ばす。Example: BBPBBP → ([2,1,2,1], ['B','P','B','P'])"""
    cols_len = []
    cols_side = []
    cur_len = 0
    cur_side = None
    for ch in hands:
        if ch == 'T' or ch is None:
            continue
        if ch == cur_side:
            cur_len += 1
        else:
            if cur_side is not None:
                cols_len.append(cur_len)
                cols_side.append(cur_side)
            cur_len = 1
            cur_side = ch
    # 現在進行中の列は含めない（完了した列のみ）
    return cols_len, cols_side, cur_len, cur_side


def is_nikoniko_like(cols_len, cols_side):
    """直近4列が長さ2中心（PP BB PP BB系）か"""
    if len(cols_len) < 4:
        return False
    recent = cols_len[-4:]
    # 全部長さ2 or 2が3個以上
    return sum(1 for L in recent if L == 2) >= 3


def is_nikoichi_like(cols_len, cols_side):
    """直近4列が2-1-2-1 交互か"""
    if len(cols_len) < 4:
        return False
    recent = cols_len[-4:]
    # 2,1,2,1 または 1,2,1,2
    return (recent in ([2, 1, 2, 1], [1, 2, 1, 2]))


def big_road_signal(cols_len, cols_side, cur_len, cur_side):
    """次の手がP/B のどちらかの予測。

    ニコニコ:
      - 直前列が1-P → 次列は2-P予想 → 次手はP
      - 直前列が2-B → 次列は1-P予想 → 次手はP

    ニコイチ:
      - PPBPPB...の時、次のPポジションを予想
      - BBPBBP...の時、次のPポジションを予想
    """
    if not cols_len or cur_side is None:
        return 'unclear'

    # パターン判定は直近のみ
    last_len = cols_len[-1] if cols_len else 0
    last_side = cols_side[-1] if cols_side else None

    # --- ニコニコ判定 ---
    if is_nikoniko_like(cols_len, cols_side):
        # 現在進行中の列の側を見る
        # 直前完成列=1-P なら次列は2-P → 現在列がPなら2手目Pを狙う
        if last_len == 1 and last_side == 'P':
            # 現在列 P 進行中なら P予想
            if cur_side == 'P' and cur_len <= 2:
                return 'P'
        if last_len == 2 and last_side == 'B':
            # 次列は1-P → 現在列 P 1手目を狙う
            if cur_side == 'P' and cur_len == 1:
                return 'P'

    # --- ニコイチ判定 ---
    if is_nikoichi_like(cols_len, cols_side):
        # PPBPPB系 or BBPBBP系
        # 現在列が短P（1手目〜2手目）なら次手P予想
        if cur_side == 'P' and cur_len <= 2:
            return 'P'
        # 現在列がBで、長さ2到達（2落ちB）→ 次列はP予想 (単独P)
        # これは列完成後の判断なので次手予想では不要
        # 今の手が終わった直後に次手を予想する場合:
        # 現在Bで長さ2なら、次手はP（次列の1手目）
        # ただし現在の手が終わった時点では cur_len はまだ更新されていない

    return 'unclear'


# ═══════════════════════════════════════════════════
# エントリートリガー
# ═══════════════════════════════════════════════════
def should_enter(cols_len):
    """大路5-10列目でカオス→1+1テレコ転換か

    カオス: 最初5列の max>=3 or 分散>=1.5
    1+1テレコ転換: 直近2列が [1,1]
    """
    if len(cols_len) < 7 or len(cols_len) > 12:
        return False
    first5 = cols_len[:5]
    max_first5 = max(first5)
    mean_f = sum(first5) / 5
    var_f = sum((L - mean_f) ** 2 for L in first5) / 5
    is_chaos = max_first5 >= 3 or var_f >= 1.5
    recent2 = cols_len[-2:]
    is_tereko_trigger = recent2 == [1, 1]
    return is_chaos and is_tereko_trigger


# ═══════════════════════════════════════════════════
# MaruBatsu 7ターン SEQ (Player-only)
# ═══════════════════════════════════════════════════
class MaruBatsu:
    def __init__(self, capital):
        self.capital = capital
        self.balance = capital
        self.peak = capital
        self.max_dd = 0.0
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.sessions_won = 0
        self.total_completed = 0.0
        self.hands_bet = 0
        self.hands_win = 0
        self.bankrupt = False
        self.bankrupt_at = None
        self.max_unit_seen = 0
        self.balance_curve = []

    def _next_idx(self, used_idx, diff, new_os):
        if diff < 0:
            return min(used_idx + 1, len(SEQ) - 1)
        for fi in range(len(self.history) - 1, -1, -1):
            s = self.history[fi]
            if not s['slashed'] and s['os'] == new_os:
                return s['next']
        ba, bad = -1, float("inf")
        bb, bbd = -1, float("inf")
        for fk in range(len(self.history)):
            s = self.history[fk]
            if not s['slashed']:
                dd = s['os'] - new_os
                if dd > 0 and dd < bad: bad = dd; ba = s['next']
                if dd < 0 and (-dd) < bbd: bbd = -dd; bb = s['next']
        if ba >= 0: return ba
        if bb >= 0: return min(bb + 1, len(SEQ) - 1)
        return 0

    def _complete_set(self):
        base = SEQ[min(self.unit_idx, len(SEQ) - 1)]
        money = 0.0
        for outcome in self.turns:
            stake = base * BASE_UNIT
            if outcome == 'O':
                money += stake  # Player = コミッションなし
            else:
                money -= stake
        wins = sum(1 for o in self.turns if o == 'O')
        diff = wins - (SET_SIZE - wins)
        self.cumulative += money
        self.balance += money
        new_os = max(self.prev_os - diff, 0)
        if diff > 0:
            for s in self.history:
                if not s['slashed'] and s['os'] > new_os:
                    s['slashed'] = True
        nxt = self._next_idx(self.unit_idx, diff, new_os)
        self.history.append({'os': new_os, 'slashed': False, 'next': nxt})
        self.prev_os = new_os
        self.unit_idx = nxt
        self.turns = []
        if self.unit_idx > self.max_unit_seen:
            self.max_unit_seen = self.unit_idx
        if self.balance > self.peak: self.peak = self.balance
        dd = self.peak - self.balance
        if dd > self.max_dd: self.max_dd = dd

    def check_bankrupt(self):
        nxt = SEQ[min(self.unit_idx, len(SEQ) - 1)]
        return self.balance < nxt * SET_SIZE * BASE_UNIT

    def bet(self, won, ts):
        if self.bankrupt: return
        self.hands_bet += 1
        if won: self.hands_win += 1
        self.turns.append('O' if won else 'X')
        if len(self.turns) == SET_SIZE:
            self._complete_set()
        if self.cumulative >= PROFIT_TARGET:
            self.total_completed += self.cumulative
            self.sessions_won += 1
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
        if self.check_bankrupt():
            self.bankrupt = True
            self.bankrupt_at = ts
        if self.hands_bet % 500 == 0:
            self.balance_curve.append((ts, self.balance))


# ═══════════════════════════════════════════════════
# Shoe シミュレーション
# ═══════════════════════════════════════════════════
def simulate_shoe(seq, mb, ts, stats):
    """1シューの進行をリアルタイム再生。look-aheadなし。"""
    if mb.bankrupt: return
    observed = []  # 見てきたハンド (T含む)
    entered = False
    for i, ch in enumerate(seq):
        if ch not in ('P', 'B', 'T'):
            continue
        observed.append(ch)

        # エントリー判定 (未入室時のみ)
        if not entered:
            cols_len, _, _, _ = big_road_columns(observed)
            if should_enter(cols_len):
                entered = True
                stats['entries'] += 1

        # 入室後、次のハンド（未来）を予測して現在のハンドでBET判定
        # ただし look-ahead なしなので "これからベット" → "次のハンド結果で判定"
        if entered and i + 1 < len(seq):
            # 次のハンドを予測: 両シグナル計算
            cols_len, cols_side, cur_len, cur_side = big_road_columns(observed)
            bead_sig = bead_road_signal(observed)
            big_sig = big_road_signal(cols_len, cols_side, cur_len, cur_side)

            # デュアル合致 (Player)
            if bead_sig == 'P' and big_sig == 'P':
                # 次の非タイハンドを見つける
                next_ch = None
                for j in range(i + 1, len(seq)):
                    if seq[j] in ('P', 'B'):
                        next_ch = seq[j]
                        break
                if next_ch is not None:
                    won = (next_ch == 'P')
                    mb.bet(won, ts)
                    stats['bets'] += 1
                    if won: stats['wins'] += 1
                    if mb.bankrupt:
                        return


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS, DATE_FROM)
    )
    shoes = cur.fetchall()
    conn.close()
    print(f"Loaded {len(shoes):,} shoes")

    mb = MaruBatsu(START_CAPITAL)
    stats = {'entries': 0, 'bets': 0, 'wins': 0}
    daily = defaultdict(lambda: {'bets': 0, 'wins': 0, 'pnl': 0.0, 'sessions': 0})
    prev_sessions = 0
    prev_completed = 0.0
    table_stats = defaultdict(lambda: {'shoes': 0, 'entries': 0, 'bets': 0, 'wins': 0})

    for si, (tn, seq, ts) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si:,}/{len(shoes):,}... balance=${mb.balance:,.0f} idx={mb.unit_idx} bets={stats['bets']:,}")
        if mb.bankrupt:
            break
        table_stats[tn]['shoes'] += 1
        prev_entries = stats['entries']
        prev_bets = stats['bets']
        prev_wins = stats['wins']
        simulate_shoe(seq, mb, ts, stats)
        table_stats[tn]['entries'] += stats['entries'] - prev_entries
        table_stats[tn]['bets'] += stats['bets'] - prev_bets
        table_stats[tn]['wins'] += stats['wins'] - prev_wins
        day = ts[:10]
        daily[day]['bets'] += stats['bets'] - prev_bets
        daily[day]['wins'] += stats['wins'] - prev_wins
        if mb.sessions_won > prev_sessions:
            delta = mb.total_completed - prev_completed
            daily[day]['pnl'] += delta
            daily[day]['sessions'] += mb.sessions_won - prev_sessions
            prev_sessions = mb.sessions_won
            prev_completed = mb.total_completed

    # Print summary
    print(f"\n{'='*70}")
    print(f"Result: {'BANKRUPT' if mb.bankrupt else 'SURVIVED'}")
    print(f"Final balance: ${mb.balance:,.2f} ({mb.balance - mb.capital:+,.2f})")
    print(f"Peak: ${mb.peak:,.2f}  MaxDD: ${mb.max_dd:,.2f}")
    print(f"Max SEQ idx: {mb.max_unit_seen} = ${SEQ[min(mb.max_unit_seen, len(SEQ)-1)]}")
    print(f"Entries: {stats['entries']:,}")
    print(f"Bets: {stats['bets']:,} / Wins: {stats['wins']:,}")
    if stats['bets'] > 0:
        print(f"Win rate: {stats['wins']/stats['bets']*100:.2f}%")
    print(f"Sessions won ($30 target): {mb.sessions_won:,}")
    if mb.bankrupt:
        print(f"Bankrupt at: {mb.bankrupt_at}")

    render_html(mb, stats, daily, table_stats, len(shoes))


def render_html(mb, stats, daily, table_stats, total_shoes):
    bc = mb.balance_curve
    if not bc or (bc and bc[-1][1] != mb.balance):
        bc = bc + [('END', mb.balance)]
    bc_labels = [p[0][:16] if p[0] != 'END' else 'END' for p in bc]
    bc_values = [round(p[1], 2) for p in bc]

    wr = (stats['wins'] / stats['bets'] * 100) if stats['bets'] > 0 else 0
    pnl = mb.balance - mb.capital
    status = "破綻" if mb.bankrupt else "生存"
    status_c = "#f87171" if mb.bankrupt else "#4ade80"
    pnl_c = "#4ade80" if pnl >= 0 else "#f87171"
    max_unit = SEQ[min(mb.max_unit_seen, len(SEQ)-1)]

    # Daily
    daily_rows = ""
    cum = 0.0
    for d in sorted(daily.keys()):
        r = daily[d]
        cum += r['pnl']
        dwr = (r['wins'] / r['bets'] * 100) if r['bets'] > 0 else 0
        pc = '#4ade80' if r['pnl'] > 0 else ('#f87171' if r['pnl'] < 0 else '#8a96a8')
        cc = '#4ade80' if cum > 0 else ('#f87171' if cum < 0 else '#8a96a8')
        daily_rows += (
            f"<tr><td class='ts'>{d}</td><td>{r['bets']:,}</td>"
            f"<td>{dwr:.1f}%</td><td>{r['sessions']}</td>"
            f"<td style='color:{pc};font-weight:bold'>${r['pnl']:+,.0f}</td>"
            f"<td style='color:{cc}'>${cum:+,.0f}</td></tr>"
        )

    # Table stats
    tbl_list = []
    for tn, s in table_stats.items():
        if s['bets'] < 10: continue
        w = s['wins'] / s['bets'] * 100
        tbl_list.append((tn, s['shoes'], s['entries'], s['bets'], w))
    tbl_list.sort(key=lambda x: -x[4])
    tbl_rows = ""
    for tn, sh, en, bt, w in tbl_list[:30]:
        wc = '#4ade80' if w >= 52 else ('#fbbf24' if w >= 50 else '#f87171')
        tbl_rows += (
            f"<tr><td class='tname'>{tn}</td><td>{sh}</td>"
            f"<td>{en}</td><td>{bt:,}</td>"
            f"<td style='color:{wc};font-weight:bold'>{w:.2f}%</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>友人戦略バックテスト — Player-only / 7ターンSEQ / look-aheadなし</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", "Yu Gothic UI", sans-serif;
       background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 26px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 20px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px;
         background: #1a2332; color: #6dd5ed; text-decoration: none;
         border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.banner {{ background: #1a2a1a; border-left: 5px solid {status_c};
          padding: 14px 18px; margin: 16px 0; font-size: 14px;
          border-radius: 4px; line-height: 1.8; }}
.banner strong {{ color: {status_c}; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
.card.r .value {{ color: #f87171; }}
.card.y .value {{ color: #fbbf24; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
           border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
td.tname {{ font-weight: bold; color: #ffd700; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
#chart {{ width: 100%; height: 400px; }}
</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head><body><div class="container">
<h1>友人戦略バックテスト — Player-only / 7ターンSEQ / look-aheadなし</h1>
<div class="nav"><a href="index.html">← レポートTOP</a></div>

<div class="banner">
<strong>🎯 戦略仕様（Phase 1実装）</strong><br>
<strong>エントリー:</strong> 大路5-10列で「カオス→1落ちP+1落ちB転換」検出時<br>
<strong>シグナル1 (珠盤路):</strong> 6行グリッド水平読み（|P-B|≥3 / T対称 / 3連続）<br>
<strong>シグナル2 (大路):</strong> ニコニコ継続予測 / ニコイチPポジション予測<br>
<strong>BET:</strong> 両シグナルが Player一致時のみ / Banker予測はLOOK<br>
<strong>資金:</strong> $10,000 / 旧SEQ [1,2,3,5,7,9,11,...] × 7ターン / $30利確<br>
<strong>データ:</strong> {DATE_FROM}〜 / {total_shoes:,}シュー / look-aheadなし
</div>

<div class="summary">
  <div class="card {'r' if mb.bankrupt else 'g'}"><div class="label">結果</div><div class="value">{status}</div></div>
  <div class="card"><div class="label">最終残高</div><div class="value">${mb.balance:,.0f}</div></div>
  <div class="card {'g' if pnl>=0 else 'r'}"><div class="label">通算損益</div><div class="value">${pnl:+,.0f}</div></div>
  <div class="card y"><div class="label">最大DD</div><div class="value">${mb.max_dd:,.0f}</div></div>
  <div class="card"><div class="label">残高ピーク</div><div class="value">${mb.peak:,.0f}</div></div>
  <div class="card"><div class="label">入室回数</div><div class="value">{stats['entries']:,}</div></div>
  <div class="card"><div class="label">BET数</div><div class="value">{stats['bets']:,}</div></div>
  <div class="card {'g' if wr>=51 else ('y' if wr>=50 else 'r')}"><div class="label">勝率</div><div class="value">{wr:.2f}%</div></div>
  <div class="card"><div class="label">利確回数</div><div class="value">{mb.sessions_won:,}</div></div>
  <div class="card"><div class="label">最大SEQ</div><div class="value">[{mb.max_unit_seen}]=${max_unit}</div></div>
</div>

<h2>💹 残高推移</h2>
<div id="chart"></div>

<h2>📅 日次パフォーマンス</h2>
<table>
<thead><tr><th>日付</th><th>BET数</th><th>勝率</th><th>利確</th><th>日次PNL</th><th>累計PNL</th></tr></thead>
<tbody>{daily_rows}</tbody></table>

<h2>🏆 テーブル別 勝率 TOP30（BET10回以上）</h2>
<table>
<thead><tr><th>テーブル</th><th>シュー</th><th>入室</th><th>BET数</th><th>勝率</th></tr></thead>
<tbody>{tbl_rows}</tbody></table>

<p style="color:#8a96a8;font-size:11px;margin-top:32px;">
生成元: <code>generate_friend_strategy_backtest.py</code> / データ: {DB_PATH}
</p>
</div>
<script>
const labels = {json.dumps(bc_labels)};
const values = {json.dumps(bc_values)};
Plotly.newPlot('chart', [{{
  x: labels, y: values, type: 'scatter', mode: 'lines',
  line: {{color: '{status_c}', width: 2}}, fill: 'tozeroy',
  fillcolor: 'rgba(74, 222, 128, 0.08)',
  hovertemplate: '%{{x}}<br>$%{{y:,.0f}}<extra></extra>'
}}], {{
  paper_bgcolor: '#0f1419', plot_bgcolor: '#11192a',
  font: {{color: '#e0e6ed'}},
  xaxis: {{gridcolor: '#2a3441', showticklabels: false}},
  yaxis: {{gridcolor: '#2a3441', title: 'Balance ($)', tickformat: ',.0f'}},
  margin: {{l: 80, r: 20, t: 20, b: 20}},
  shapes: [{{type: 'line', x0: 0, x1: 1, xref: 'paper',
    y0: {START_CAPITAL}, y1: {START_CAPITAL},
    line: {{color: '#8a96a8', width: 1, dash: 'dash'}} }}]
}}, {{displayModeBar: false, responsive: true}});
</script>
</body></html>
"""
    out = os.path.join("report", "friend_strategy_backtest.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
