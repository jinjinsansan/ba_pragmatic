"""テレコ入室戦略 比較バックテスト

Strategy A (Classic 10-col):
  直近10列の short_rate (長さ<=2) >= 0.90 で入室

Strategy B (Post-streak):
  長さ >= 3 の長列が出現し終わった "次の列" が短列(<=2)なら入室
  (縦流れ死亡 → 直後に始まるテレコは長生きする仮説)

共通退室:
  3落ち以上(len>=3) が 2回 で退室 / 5落ち以上(len>=5) は1回で即退室

共通BET:
  前手非タイ結果の逆 (P->B, B->P)
  Banker BETは unit×1.0526 (5%手数料相殺)
  Set size 7 turns / SEQ_NEW / chip_base $10 / 利確 +$300

Usage:
  python generate_tereko_entry_comparison.py [--vps] [--date-from YYYY-MM-DD]
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter

if "--now" in sys.argv:
    DB_PATH = "analytics_now.sqlite3"
elif "--vps" in sys.argv:
    DB_PATH = "analytics_vps.sqlite3"
else:
    DB_PATH = "analytics.sqlite3"

DATE_FROM = "2026-04-06"
for i, a in enumerate(sys.argv):
    if a == "--date-from" and i + 1 < len(sys.argv):
        DATE_FROM = sys.argv[i + 1]

MIN_HANDS = 50
PROFIT_TARGET = 300.0  # $300
CHIP_BASE = 10.0       # $10 per unit
BANKER_COMMISSION = 0.05
BANKER_UNIT_MULT = 1.0 / (1.0 - BANKER_COMMISSION)  # 1.0526...

# Strategy A
A_WINDOW = 10
A_THRESHOLD = 0.90

# Strategy B
B_LONG_LEN = 3   # "long column" >= 3
B_SHORT_LEN = 2  # entering short column <= 2

# Common exit
EXIT_3DROP_LIMIT = 2     # len>=3 が 2回で退室
EXIT_5DROP_IMMEDIATE = 5 # len>=5 が 1回で即退室

SET_SIZE = 7

SEQ_NEW = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90,
           100, 110, 120, 130, 145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
           300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]


class MaruBatsuSim:
    """7ターンセットのMaruBatsu資金管理 (SEQ_NEW × CHIP_BASE)"""

    def __init__(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []           # [(O/X, bet_side)]
        self.history = []
        self.peak = 0.0
        self.max_dd = 0.0
        self.sessions_won = 0
        self.total_profit = 0.0
        self.hands_bet = 0
        self.equity_curve = []    # cumulative pnl after each completed set
        self.session_pnls = []    # PNL of each completed (won) session

    def _next_idx(self, used_idx, diff, new_os):
        if diff < 0:
            return min(used_idx + 1, len(SEQ_NEW) - 1)
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
                if dd > 0 and dd < bad:
                    bad = dd
                    ba = s['next']
                if dd < 0 and (-dd) < bbd:
                    bbd = -dd
                    bb = s['next']
        if ba >= 0:
            return ba
        if bb >= 0:
            return min(bb + 1, len(SEQ_NEW) - 1)
        return 0

    def _complete(self):
        base_unit = SEQ_NEW[self.unit_idx] if self.unit_idx < len(SEQ_NEW) else SEQ_NEW[-1]
        base_unit = base_unit * CHIP_BASE
        money = 0.0
        for outcome, bet_side in self.turns:
            if bet_side == 'B':
                actual = base_unit * BANKER_UNIT_MULT
            else:
                actual = base_unit
            if outcome == 'O':
                if bet_side == 'B':
                    money += actual * (1.0 - BANKER_COMMISSION)
                else:
                    money += actual
            else:
                money -= actual

        wins = sum(1 for t in self.turns if t[0] == 'O')
        diff = wins - (SET_SIZE - wins)
        self.cumulative += money
        new_os = max(self.prev_os - diff, 0)
        if diff > 0:
            for s in self.history:
                if not s['slashed'] and s['os'] > new_os:
                    s['slashed'] = True
        next_idx = self._next_idx(self.unit_idx, diff, new_os)
        self.history.append({'os': new_os, 'slashed': False, 'next': next_idx})
        self.prev_os = new_os
        self.unit_idx = next_idx
        self.turns = []

        total_pnl = self.total_profit + self.cumulative
        self.equity_curve.append(total_pnl)
        if total_pnl > self.peak:
            self.peak = total_pnl
        self.max_dd = max(self.max_dd, self.peak - total_pnl)

    def add_bet(self, won: bool, bet_side: str):
        self.hands_bet += 1
        self.turns.append(('O' if won else 'X', bet_side))
        if len(self.turns) == SET_SIZE:
            self._complete()
        if self.cumulative >= PROFIT_TARGET:
            self.session_pnls.append(self.cumulative)
            self.total_profit += self.cumulative
            self.sessions_won += 1
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
            # peak tracks total_profit + cumulative which now == total_profit, OK


class TableState:
    """テーブルの大路リアルタイム状態"""

    def __init__(self, name):
        self.name = name
        self.columns = []         # 確定列の長さリスト
        self.current_col = 0      # 現在進行中の列の長さ
        self.last_side = None
        self.last_nt = None       # 直前の非タイ結果
        # 戦略Aの状態
        self.a_active = False
        self.a_entry_col_idx = 0  # 入室時の確定列数
        self.a_entry_hand = 0     # 入室時のハンド番号 (テーブル内累計)
        # 戦略Bの状態
        self.b_active = False
        self.b_entry_col_idx = 0
        self.b_entry_hand = 0
        self.b_long_seen = False  # 直前の確定列が長列(>=3)だったか
        # 共通カウンタ
        self.hand_idx = 0         # テーブル内非タイハンド累計

    def _update_column(self, ch):
        """確定列があれば返す (なければNone)。current_col/last_sideを更新。"""
        finalized = None
        if ch == self.last_side:
            self.current_col += 1
        else:
            if self.last_side is not None:
                finalized = self.current_col
                self.columns.append(self.current_col)
            self.current_col = 1
            self.last_side = ch
        return finalized

    def feed(self, ch):
        """非タイ結果を feed。新しく確定した列の長さを返す (なければNone)。"""
        if ch == 'T':
            return None
        self.hand_idx += 1
        finalized = self._update_column(ch)
        self.last_nt = ch
        return finalized

    def finalize_shoe(self):
        if self.current_col > 0 and self.last_side is not None:
            self.columns.append(self.current_col)
        self.current_col = 0
        self.last_side = None
        # b_long_seen は次シューに引き継がない (シュー区切りでリセット)
        self.b_long_seen = False

    # ===== Strategy A =====
    def a_should_enter(self) -> bool:
        if self.a_active:
            return False
        if len(self.columns) < A_WINDOW:
            return False
        recent = self.columns[-A_WINDOW:]
        short = sum(1 for L in recent if L <= 2)
        return (short / len(recent)) >= A_THRESHOLD

    # ===== Strategy B =====
    def b_should_enter(self, just_finalized_len) -> bool:
        """直前のfeedで確定した列が短列で、かつそれより前に長列が来ていた場合に入室。

        実際は: just_finalized_len は今ちょうど終わった列の長さではなく、
        「新しい列が始まった瞬間」に確定した直前列の長さ。
        Strategy Bは: 長列(>=3)が確定 → 次の列(つまり今始まった列)が短列(<=2)で完結したらエントリ。
        ただし「次の列」が短列かは完結するまで分からない。
        ここでは: 「長列が確定 → 次に確定した列が短列」のタイミングで遡及的に入室する代わりに、
        運用感を出すため: 直前列が長列(>=3)なら、今始まった列=新規列に対し
        「もし2手目で側が変わったら(=長さ1で確定したら)入室確定」とすると遅延が大きい。
        簡略化: 「長列(>=3)が確定 → その直後の列が確定した時点で len<=2 ならその時点で入室」
        (= 短列確定の最初の手から次の手までは見送り、確定後の次列で BET 開始)
        """
        # caller responsibility — ここでは flag のみ判定するので b_long_seen を別管理
        return False  # 実装はメインループ側で行う

    def reset_strategy_state(self):
        self.a_active = False
        self.b_active = False
        self.b_long_seen = False


def check_exit(ts: TableState, entry_col_idx: int) -> str | None:
    """退室条件チェック (共通)。エントリ後の列のみを評価。"""
    cols_since_entry = ts.columns[entry_col_idx:]
    check_cols = list(cols_since_entry)
    if ts.current_col >= 3:
        check_cols.append(ts.current_col)

    # 5落ち以上1回で即退室
    for L in check_cols:
        if L >= EXIT_5DROP_IMMEDIATE:
            return f"{L}落ち発生"

    # 3落ち以上2回で退室 (確定列のみカウント。進行中列は3でも次手で2に戻り得るので除外)
    drop3_count = sum(1 for L in cols_since_entry if L >= 3)
    if drop3_count >= EXIT_3DROP_LIMIT:
        return f"3落ち以上×{drop3_count}"
    return None


def load_all_hands():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS, DATE_FROM)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def run_strategy(shoes, strategy_label):
    """戦略を1つ走らせる"""
    sim = MaruBatsuSim()
    table_states = {}
    table_pnl = defaultdict(lambda: {'bets': 0, 'wins': 0, 'losses': 0,
                                      'entries': 0, 'shoes': 0,
                                      'mb_pnl_start': 0.0})
    stats = {'entries': 0, 'exits': 0, 'bets': 0, 'wins': 0, 'losses': 0,
             'exit_reasons': Counter()}
    sessions = []  # one session = one entry-to-exit
    lifespans = []  # hands inside table per session

    current_table = None
    current_entry_hand = 0
    current_entry_col_idx = 0
    current_session_bets = 0
    current_session_wins = 0
    current_mb_start = 0.0

    total_shoes = len(shoes)
    for si, (table_name, seq, started_at) in enumerate(shoes):
        if si % 1000 == 0:
            print(f"  [{strategy_label}] {si}/{total_shoes}...", file=sys.stderr)

        if table_name not in table_states:
            table_states[table_name] = TableState(table_name)
        ts = table_states[table_name]
        table_pnl[table_name]['shoes'] += 1

        clean = ''.join(ch for ch in seq if ch in ('P', 'B', 'T'))

        for ch in clean:
            if ch == 'T':
                ts.feed(ch)
                continue

            # === 退室チェック (BET前) ===
            if current_table == table_name:
                exit_reason = check_exit(ts, current_entry_col_idx)
                if exit_reason:
                    mb_now = sim.total_profit + sim.cumulative
                    sessions.append({
                        'table': table_name,
                        'entry_ts': started_at,
                        'exit_ts': started_at,
                        'reason': exit_reason,
                        'bets': current_session_bets,
                        'wins': current_session_wins,
                        'flat_pnl': current_session_wins - (current_session_bets - current_session_wins),
                        'mb_pnl': mb_now - current_mb_start,
                    })
                    lifespans.append(ts.hand_idx - current_entry_hand)
                    stats['exits'] += 1
                    stats['exit_reasons'][exit_reason] += 1
                    if strategy_label == 'A':
                        ts.a_active = False
                    else:
                        ts.b_active = False
                    current_table = None

            # === BET (アクティブ中) ===
            if current_table == table_name and ts.last_nt is not None:
                bet_side = 'P' if ts.last_nt == 'B' else 'B'
                won = (ch == bet_side)
                stats['bets'] += 1
                current_session_bets += 1
                table_pnl[table_name]['bets'] += 1
                if won:
                    stats['wins'] += 1
                    current_session_wins += 1
                    table_pnl[table_name]['wins'] += 1
                else:
                    stats['losses'] += 1
                    table_pnl[table_name]['losses'] += 1
                sim.add_bet(won, bet_side)

            # === 大路更新 (この手を反映) ===
            finalized = ts.feed(ch)

            # === 入室判定 (BET中でなければ) ===
            if current_table is None:
                should_enter = False
                if strategy_label == 'A':
                    if ts.a_should_enter():
                        ts.a_active = True
                        ts.a_entry_col_idx = len(ts.columns)
                        ts.a_entry_hand = ts.hand_idx
                        should_enter = True
                        entry_col_idx = ts.a_entry_col_idx
                        entry_hand = ts.a_entry_hand
                else:  # Strategy B
                    # finalized があったとき:
                    #   - 直前列が長列だった (b_long_seen=True) かつ 今確定した列 <= 2
                    #     → "長列の次の短列" が確定 → 今始まった新列でBET開始
                    #   ただし「entered after long-broken short column」とするには
                    #   今確定した短列がまさに「長列の次の列」である必要あり
                    if finalized is not None:
                        if ts.b_long_seen and finalized <= B_SHORT_LEN:
                            # 入室 — 今始まった新列(current_col=1)からBET
                            ts.b_active = True
                            ts.b_entry_col_idx = len(ts.columns)
                            ts.b_entry_hand = ts.hand_idx
                            should_enter = True
                            entry_col_idx = ts.b_entry_col_idx
                            entry_hand = ts.b_entry_hand
                            ts.b_long_seen = False
                        else:
                            ts.b_long_seen = (finalized >= B_LONG_LEN)

                if should_enter:
                    current_table = table_name
                    current_entry_col_idx = entry_col_idx
                    current_entry_hand = entry_hand
                    current_session_bets = 0
                    current_session_wins = 0
                    current_mb_start = sim.total_profit + sim.cumulative
                    stats['entries'] += 1
                    table_pnl[table_name]['entries'] += 1
            else:
                # 他テーブルでもStrategy Bの長列フラグは更新する必要あり (バックグラウンド監視)
                if strategy_label == 'B' and finalized is not None:
                    if not ts.b_active:
                        # アクティブでないテーブルの長列フラグ更新
                        if ts.b_long_seen and finalized <= B_SHORT_LEN:
                            # 別テーブルでも入室候補が出てるが、現在BET中のため見送り
                            ts.b_long_seen = False
                        else:
                            ts.b_long_seen = (finalized >= B_LONG_LEN)

        ts.finalize_shoe()

    # 最終セッション
    if current_table:
        mb_now = sim.total_profit + sim.cumulative
        sessions.append({
            'table': current_table,
            'entry_ts': 'mid',
            'exit_ts': 'END',
            'reason': 'データ終了',
            'bets': current_session_bets,
            'wins': current_session_wins,
            'flat_pnl': current_session_wins - (current_session_bets - current_session_wins),
            'mb_pnl': mb_now - current_mb_start,
        })
        ts = table_states[current_table]
        lifespans.append(ts.hand_idx - current_entry_hand)

    return {
        'sim': sim,
        'stats': stats,
        'table_pnl': dict(table_pnl),
        'sessions': sessions,
        'lifespans': lifespans,
    }


def render_html(res_a, res_b, total_shoes, out_path):
    sim_a = res_a['sim']; sim_b = res_b['sim']
    sa = res_a['stats']; sb = res_b['stats']

    def hr(s):
        return s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0

    def fpnl(s):
        # flat $10 each bet
        return (s['wins'] - s['losses']) * CHIP_BASE

    def mb_pnl(sim):
        return sim.total_profit + sim.cumulative

    def avg_life(res):
        ls = res['lifespans']
        return sum(ls) / len(ls) if ls else 0

    # サマリーカード
    def card_set(label, res, color):
        s = res['stats']; sim = res['sim']
        fp = fpnl(s); mp = mb_pnl(sim)
        fp_c = '#4ade80' if fp >= 0 else '#f87171'
        mp_c = '#4ade80' if mp >= 0 else '#f87171'
        return f"""
        <div class="strat-block" style="border-left:6px solid {color}">
          <h3 style="color:{color}">{label}</h3>
          <div class="summary">
            <div class="card"><div class="label">入室</div><div class="value">{s['entries']:,}</div></div>
            <div class="card green"><div class="label">勝率</div><div class="value">{hr(s):.2f}%</div></div>
            <div class="card"><div class="label">BET数</div><div class="value">{s['bets']:,}</div></div>
            <div class="card" style="color:{fp_c}"><div class="label">Flat PNL ($10/bet)</div>
              <div class="value" style="color:{fp_c}">${fp:+,.0f}</div></div>
            <div class="card" style="color:{mp_c}"><div class="label">MB累計PNL</div>
              <div class="value" style="color:{mp_c}">${mp:+,.0f}</div></div>
            <div class="card"><div class="label">MB MaxDD</div><div class="value">${sim.max_dd:,.0f}</div></div>
            <div class="card"><div class="label">利確セッション</div><div class="value">{sim.sessions_won:,}</div></div>
            <div class="card"><div class="label">平均テレコ寿命(手)</div><div class="value">{avg_life(res):.1f}</div></div>
          </div>
        </div>
        """

    # Top 10 tables
    def table_top10(res):
        rows = []
        for tn, t in res['table_pnl'].items():
            fp = (t['wins'] - t['losses']) * CHIP_BASE
            wr = t['wins'] / t['bets'] * 100 if t['bets'] > 0 else 0
            rows.append((tn, t, fp, wr))
        rows.sort(key=lambda x: -x[2])
        html = "<table><thead><tr><th>テーブル</th><th>入室</th><th>BET</th><th>勝率</th><th>Flat PNL</th></tr></thead><tbody>"
        for tn, t, fp, wr in rows[:10]:
            c = '#4ade80' if fp > 0 else ('#f87171' if fp < 0 else '#888')
            html += f"<tr><td class='tname'>{tn}</td><td>{t['entries']}</td><td>{t['bets']:,}</td><td>{wr:.1f}%</td><td style='color:{c};font-weight:bold'>${fp:+,.0f}</td></tr>"
        html += "</tbody></table>"
        return html

    # Equity curves -> Chart.js datasets
    eq_a = sim_a.equity_curve
    eq_b = sim_b.equity_curve
    max_len = max(len(eq_a), len(eq_b), 1)
    # downsample for perf
    def downsample(lst, n=300):
        if len(lst) <= n:
            return lst
        step = len(lst) / n
        return [lst[int(i * step)] for i in range(n)]
    eq_a_s = downsample(eq_a)
    eq_b_s = downsample(eq_b)
    labels = list(range(max(len(eq_a_s), len(eq_b_s))))

    # Histogram of session PNLs (completed +$300 sessions only)
    def hist(values, bins=15):
        if not values:
            return [], []
        lo = min(values); hi = max(values)
        if lo == hi:
            return [f"{lo:.0f}"], [len(values)]
        w = (hi - lo) / bins
        counts = [0] * bins
        edges = [lo + i * w for i in range(bins + 1)]
        for v in values:
            idx = min(int((v - lo) / w), bins - 1)
            counts[idx] += 1
        labs = [f"${edges[i]:.0f}" for i in range(bins)]
        return labs, counts

    # Use session_log flat_pnl (per entry-exit session) for distribution
    sess_pnls_a = [s['mb_pnl'] for s in res_a['sessions']]
    sess_pnls_b = [s['mb_pnl'] for s in res_b['sessions']]
    hl_a, hc_a = hist(sess_pnls_a)
    hl_b, hc_b = hist(sess_pnls_b)

    # 結論
    diff_mb = mb_pnl(sim_b) - mb_pnl(sim_a)
    diff_life = avg_life(res_b) - avg_life(res_a)
    diff_wr = hr(sb) - hr(sa)
    winner = "Strategy B (長列死後の短列入室)" if diff_mb > 0 else "Strategy A (10列短列密度)"
    if abs(diff_mb) < 1:
        winner = "ほぼ互角"

    commentary = f"""
    <div class="highlight">
      <h3>📌 仮説検証コメンタリ</h3>
      <p>ユーザー仮説: 「縦流れが切れた直後に始まるテレコは長生きする」</p>
      <ul>
        <li>勝者: <strong>{winner}</strong> (MB PNL差: ${diff_mb:+,.0f})</li>
        <li>勝率差 (B − A): {diff_wr:+.2f} ポイント</li>
        <li>平均テレコ寿命差 (B − A): {diff_life:+.1f} 手</li>
        <li>Strategy A 入室 {sa['entries']} 回 / Strategy B 入室 {sb['entries']} 回</li>
      </ul>
      <p class="note">
        Bの寿命が長く・MB PNLも上回るなら仮説支持。逆に寿命差が小さい/負の場合は
        「長列死後の短列開始」だけでは将来のテレコ持続を予測できないことを示す。
        サンプルサイズが小さい場合は再現性に注意。
      </p>
    </div>
    """

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>テレコ入室戦略 比較バックテスト</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1500px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 22px; }}
h3 {{ color: #6dd5ed; }}
.banner {{
  background: #1a2a1a; border-left: 5px solid #4ade80;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: #4ade80; }}
.strats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.strat-block {{
  background: #131b26; border-radius: 6px; padding: 16px;
}}
.summary {{
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 12px 0;
}}
.card {{
  background: #1a2332; padding: 10px; border-radius: 4px; border-left: 3px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; }}
.card .value {{ font-size: 18px; font-weight: bold; color: #ffd700; }}
.card.green .value {{ color: #4ade80; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 12px 0; }}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
td.tname {{ font-weight: bold; color: #ffd700; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.highlight {{
  background: #11192a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 16px; margin: 16px 0;
}}
.highlight h3 {{ margin-top: 0; color: #4ade80; }}
.tables-side {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
canvas {{ background: #131b26; border-radius: 6px; padding: 8px; }}
</style>
</head>
<body>
<div class="container">
<h1>テレコ入室戦略 比較バックテスト (A vs B)</h1>

<div class="banner">
<strong>📊 同一データセット上で2つのテレコ入室戦略を比較。</strong><br>
<strong>Strategy A (Classic):</strong> 直近{A_WINDOW}列の短列(≤2)率 ≥ {A_THRESHOLD*100:.0f}% で入室<br>
<strong>Strategy B (Post-streak):</strong> 長列(≥{B_LONG_LEN})確定 → その次の列が短列(≤{B_SHORT_LEN})で確定 → 入室<br>
<strong>共通退室:</strong> 3落ち以上×{EXIT_3DROP_LIMIT} or {EXIT_5DROP_IMMEDIATE}落ち以上×1 → 即退室<br>
<strong>共通BET:</strong> 逆張り / Banker unit×{BANKER_UNIT_MULT:.4f} / SET={SET_SIZE}T / SEQ_NEW × ${CHIP_BASE:.0f} / 利確 ${PROFIT_TARGET:.0f}<br>
データ: {DATE_FROM}〜 / {total_shoes:,}シュー
</div>

<h2>1. サマリー</h2>
<div class="strats">
  {card_set('Strategy A: Classic 10-col', res_a, '#fbbf24')}
  {card_set('Strategy B: Post-streak', res_b, '#a78bfa')}
</div>

<h2>2. エクイティカーブ (MB累計PNL)</h2>
<canvas id="equityChart" height="120"></canvas>

<h2>3. セッションPNL分布</h2>
<div class="tables-side">
  <div><h3>Strategy A</h3><canvas id="histA" height="180"></canvas></div>
  <div><h3>Strategy B</h3><canvas id="histB" height="180"></canvas></div>
</div>

<h2>4. テーブル別 Top 10 (Flat PNL)</h2>
<div class="tables-side">
  <div><h3>Strategy A Top 10</h3>{table_top10(res_a)}</div>
  <div><h3>Strategy B Top 10</h3>{table_top10(res_b)}</div>
</div>

<h2>5. 結論コメンタリ</h2>
{commentary}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_tereko_entry_comparison.py</code> /
  A: 直近{A_WINDOW}列・{A_THRESHOLD*100:.0f}%閾値 /
  B: 長列≥{B_LONG_LEN} → 短列≤{B_SHORT_LEN} で入室
</p>
</div>

<script>
const labels = {labels};
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [
      {{label: 'Strategy A', data: {eq_a_s}, borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.1)', tension: 0.1, pointRadius: 0}},
      {{label: 'Strategy B', data: {eq_b_s}, borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.1)', tension: 0.1, pointRadius: 0}}
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{labels: {{color: '#e0e6ed'}}}} }},
    scales: {{
      x: {{ ticks: {{color: '#8a96a8'}}, grid: {{color: '#1a2332'}} }},
      y: {{ ticks: {{color: '#8a96a8'}}, grid: {{color: '#1a2332'}} }}
    }}
  }}
}});

new Chart(document.getElementById('histA'), {{
  type: 'bar',
  data: {{ labels: {hl_a}, datasets: [{{label: 'A: セッションPNL分布', data: {hc_a}, backgroundColor: '#fbbf24'}}] }},
  options: {{ responsive: true,
    plugins: {{ legend: {{labels: {{color: '#e0e6ed'}}}} }},
    scales: {{ x: {{ticks: {{color: '#8a96a8'}}}}, y: {{ticks: {{color: '#8a96a8'}}}} }} }}
}});
new Chart(document.getElementById('histB'), {{
  type: 'bar',
  data: {{ labels: {hl_b}, datasets: [{{label: 'B: セッションPNL分布', data: {hc_b}, backgroundColor: '#a78bfa'}}] }},
  options: {{ responsive: true,
    plugins: {{ legend: {{labels: {{color: '#e0e6ed'}}}} }},
    scales: {{ x: {{ticks: {{color: '#8a96a8'}}}}, y: {{ticks: {{color: '#8a96a8'}}}} }} }}
}});
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path}")


def main():
    print(f"Loading shoes from {DB_PATH} (date >= {DATE_FROM}, hands >= {MIN_HANDS})...", file=sys.stderr)
    shoes = load_all_hands()
    print(f"Loaded {len(shoes)} shoes", file=sys.stderr)
    if len(shoes) == 0:
        print("WARNING: 0 shoes loaded. Check DATE_FROM and DB_PATH.", file=sys.stderr)

    print("\n=== Running Strategy A (Classic 10-col) ===", file=sys.stderr)
    res_a = run_strategy(shoes, 'A')
    print("\n=== Running Strategy B (Post-streak) ===", file=sys.stderr)
    res_b = run_strategy(shoes, 'B')

    sa = res_a['stats']; sb = res_b['stats']
    sim_a = res_a['sim']; sim_b = res_b['sim']

    print(f"\n{'='*72}")
    print(f"Strategy A: entries={sa['entries']:,} bets={sa['bets']:,} "
          f"wr={sa['wins']/sa['bets']*100 if sa['bets'] else 0:.2f}% "
          f"flat=${(sa['wins']-sa['losses'])*CHIP_BASE:+,.0f} "
          f"mb=${sim_a.total_profit+sim_a.cumulative:+,.0f} "
          f"dd=${sim_a.max_dd:,.0f} won={sim_a.sessions_won}")
    print(f"Strategy B: entries={sb['entries']:,} bets={sb['bets']:,} "
          f"wr={sb['wins']/sb['bets']*100 if sb['bets'] else 0:.2f}% "
          f"flat=${(sb['wins']-sb['losses'])*CHIP_BASE:+,.0f} "
          f"mb=${sim_b.total_profit+sim_b.cumulative:+,.0f} "
          f"dd=${sim_b.max_dd:,.0f} won={sim_b.sessions_won}")

    out = os.path.join("report", "tereko_entry_comparison.html")
    render_html(res_a, res_b, len(shoes), out)
    print("Done.")


if __name__ == "__main__":
    main()
