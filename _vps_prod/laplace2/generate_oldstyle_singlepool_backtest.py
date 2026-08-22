"""旧レポート同条件 × 単一財布 バックテスト

目的:
  旧レポート equity_counter_newseq_5turn_10k.html と全く同じ条件で
  「1財布$10,000を62テーブル全体で共有」した場合を検証。

  旧レポート:
    - 各テーブル独立$10,000 × 62テーブル = $620,000
    - shoe冒頭30ハンドで classify_pattern → テレコ+ニコ混合なら入室
    - 退避なし・shoe終わりまで粘る
    - $30利確でSEQリセット
    - 結果: 破綻 0/62

  本シミュレーション:
    - 上記と同じエントリーフィルタ
    - 退避なし
    - しかし財布は1つ$10,000
    - SEQ は全テーブル跨ぎで継続

Usage:
  python generate_oldstyle_singlepool_backtest.py
"""
import sqlite3
import os
import sys
from collections import defaultdict
from pattern_classifier import classify_pattern

USE_VPS = True
DB_PATH = "analytics_vps.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
STATIC_WARMUP = 30
NO_LOOKAHEAD = ("--no-lookahead" in sys.argv)

# ===== 旧レポートと同条件 =====
START_CAPITAL = 10000.0
PROFIT_TARGET = 30.0
BASE_UNIT = 1.0

SEQ = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
       60, 70, 80, 90, 100, 110, 120, 130,
       145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
       300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]
SET_SIZE = 5
BANKER_COMMISSION = 0.05


class MaruBatsuBankroll:
    def __init__(self, capital: float):
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
        self.total_completed_profit = 0.0
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
                if dd > 0 and dd < bad:
                    bad = dd; ba = s['next']
                if dd < 0 and (-dd) < bbd:
                    bbd = -dd; bb = s['next']
        if ba >= 0: return ba
        if bb >= 0: return min(bb + 1, len(SEQ) - 1)
        return 0

    def _complete_set(self):
        base = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        money = 0.0
        for outcome, side in self.turns:
            stake = base * BASE_UNIT
            if outcome == 'O':
                money += stake * (1.0 - BANKER_COMMISSION) if side == 'B' else stake
            else:
                money -= stake
        wins = sum(1 for t in self.turns if t[0] == 'O')
        diff = wins - (SET_SIZE - wins)
        self.cumulative += money
        self.balance += money
        new_os = max(self.prev_os - diff, 0)
        if diff > 0:
            for s in self.history:
                if not s['slashed'] and s['os'] > new_os: s['slashed'] = True
        next_idx = self._next_idx(self.unit_idx, diff, new_os)
        self.history.append({'os': new_os, 'slashed': False, 'next': next_idx})
        self.prev_os = new_os
        self.unit_idx = next_idx
        self.turns = []
        if self.unit_idx > self.max_unit_seen:
            self.max_unit_seen = self.unit_idx
        if self.balance > self.peak: self.peak = self.balance
        dd = self.peak - self.balance
        if dd > self.max_dd: self.max_dd = dd

    def check_bankrupt(self):
        next_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        return self.balance < next_unit * SET_SIZE * BASE_UNIT

    def add_bet(self, won, bet_side, ts):
        if self.bankrupt: return
        self.hands_bet += 1
        if won: self.hands_win += 1
        self.turns.append(('O' if won else 'X', bet_side))
        if len(self.turns) == SET_SIZE:
            self._complete_set()
        if self.cumulative >= PROFIT_TARGET:
            self.total_completed_profit += self.cumulative
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


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def load_shoes():
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


def run():
    shoes = load_shoes()
    print(f"Loaded {len(shoes):,} shoes (time-ordered across ALL tables)")
    if NO_LOOKAHEAD:
        print(f"Mode: NO_LOOKAHEAD (observe first {STATIC_WARMUP} non-tie hands, then start betting)")
    else:
        print("Mode: LOOKAHEAD (WARNING: uses shoe filter but bets from shoe start)")

    mb = MaruBatsuBankroll(START_CAPITAL)
    stats = {
        'shoes_entered': 0,
        'shoes_skipped': 0,
        'shoes_by_table': defaultdict(int),
    }
    daily = defaultdict(lambda: {'bets': 0, 'wins': 0, 'pnl': 0.0, 'sessions': 0})
    prev_sessions = 0
    current_day = None
    day_open_balance = None

    for si, (tn, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si:,}/{len(shoes):,}...  balance=${mb.balance:,.0f}  idx={mb.unit_idx}")

        if mb.bankrupt:
            break

        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            stats['shoes_skipped'] += 1
            continue

        # 旧レポートと同じゲート: shoe冒頭がテレコ+ニコ混合か
        pattern = classify_pattern(clean[:STATIC_WARMUP], min_cols=3)
        if pattern != "テレコ+ニコ混合":
            stats['shoes_skipped'] += 1
            continue

        stats['shoes_entered'] += 1
        stats['shoes_by_table'][tn] += 1
        day = started_at[:10]

        # 日次PNLは「その日の開始残高→終了残高」の差で計算（未確定の途中損益も含める）
        if current_day is None:
            current_day = day
            day_open_balance = mb.balance
        elif day != current_day:
            daily[current_day]['pnl'] = mb.balance - (day_open_balance or mb.balance)
            current_day = day
            day_open_balance = mb.balance

        # shoe全ハンドを逆張り (退避なし)
        # NO_LOOKAHEAD: 冒頭STATIC_WARMUP手（非タイ）を観測してからBET開始
        last_nt = None
        nt_seen = 0
        for ch in seq:
            if ch not in ('P', 'B', 'T'):
                continue
            if ch == 'T':
                continue

            if NO_LOOKAHEAD and nt_seen < STATIC_WARMUP:
                last_nt = ch
                nt_seen += 1
                continue

            if last_nt is not None:
                bet_side = 'P' if last_nt == 'B' else 'B'
                won = (ch == bet_side)
                mb.add_bet(won, bet_side, started_at)
                daily[day]['bets'] += 1
                if won:
                    daily[day]['wins'] += 1
                if mb.sessions_won > prev_sessions:
                    daily[day]['sessions'] += 1
                    prev_sessions = mb.sessions_won
                if mb.bankrupt:
                    break
            last_nt = ch
            nt_seen += 1
        if mb.bankrupt:
            break

    # 最終日の締め
    if current_day is not None and day_open_balance is not None:
        daily[current_day]['pnl'] = mb.balance - day_open_balance

    return mb, stats, daily, len(shoes)


def render_html(mb, stats, daily, total_shoes):
    import json
    bc = mb.balance_curve
    if not bc or bc[-1][1] != mb.balance:
        bc = bc + [('END', mb.balance)]
    bc_labels = [p[0][:16] if p[0] != 'END' else 'END' for p in bc]
    bc_values = [round(p[1], 2) for p in bc]

    days = sorted(daily.keys())
    daily_rows = ""
    cum = 0.0
    for d in days:
        r = daily[d]
        cum += r['pnl']
        wr = (r['wins'] / r['bets'] * 100) if r['bets'] > 0 else 0
        pnl_c = '#4ade80' if r['pnl'] > 0 else ('#f87171' if r['pnl'] < 0 else '#8a96a8')
        cum_c = '#4ade80' if cum > 0 else ('#f87171' if cum < 0 else '#8a96a8')
        daily_rows += (
            f"<tr><td class='ts'>{d}</td>"
            f"<td>{r['bets']:,}</td><td>{wr:.1f}%</td>"
            f"<td>{r['sessions']}</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${r['pnl']:+,.0f}</td>"
            f"<td style='color:{cum_c}'>${cum:+,.0f}</td></tr>"
        )

    status = "破綻" if mb.bankrupt else "生存"
    status_c = "#f87171" if mb.bankrupt else "#4ade80"
    pnl = mb.balance - mb.capital
    pnl_c = "#4ade80" if pnl >= 0 else "#f87171"
    max_unit = SEQ[min(mb.max_unit_seen, len(SEQ)-1)]
    wr = (mb.hands_win / mb.hands_bet * 100) if mb.hands_bet > 0 else 0

    title_suffix = "（漏洩なし: 30手観測後に入室）" if NO_LOOKAHEAD else ""
    entry_desc = "shoe冒頭30ハンド観測→ classify_pattern → テレコ+ニコ混合 → 31手目からBET" if NO_LOOKAHEAD else "shoe冒頭30ハンドで classify_pattern → テレコ+ニコ混合"

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>旧レポート同条件 × 単一財布 バックテスト{title_suffix}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", "Yu Gothic UI", sans-serif;
       background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 26px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 20px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px;
         background: #1a2332; color: #6dd5ed; text-decoration: none;
         border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.banner {{ background: #1a2a1a; border-left: 5px solid {status_c};
          padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8; }}
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
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
#chart {{ width: 100%; height: 400px; }}
.critical {{ background: #2a1818; border: 2px solid #f87171; padding: 14px 18px;
            border-radius: 6px; margin: 16px 0; }}
.critical strong {{ color: #f87171; }}
</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head><body><div class="container">
<h1>旧レポート同条件 × 単一財布 バックテスト{title_suffix}</h1>
<div class="nav"><a href="index.html">← レポートTOP</a><a href="gui_realistic_backtest.html">GUI再現版</a><a href="equity_counter_newseq_5turn_10k.html">旧レポート (62財布)</a></div>

<div class="banner">
<strong>🔬 旧レポートと全く同じエントリー条件 + 退避なし。違いは「財布が1つ」だけ。</strong><br>
<strong>エントリー:</strong> {entry_desc}<br>
<strong>退避:</strong> なし (shoe終わりまで全ハンド逆張り)<br>
<strong>資金:</strong> 元本 $10,000 (単一財布) / $1 base / $30利確 / 新SEQ × 5ターン<br>
データ: {DATE_FROM}〜 / {total_shoes:,}シュー時系列ソート / 入室{stats['shoes_entered']:,}シュー
</div>

<div class="summary">
  <div class="card {'r' if mb.bankrupt else 'g'}"><div class="label">結果</div><div class="value">{status}</div></div>
  <div class="card"><div class="label">最終残高</div><div class="value">${mb.balance:,.0f}</div></div>
  <div class="card {'g' if pnl>=0 else 'r'}"><div class="label">通算損益</div><div class="value">${pnl:+,.0f}</div></div>
  <div class="card y"><div class="label">最大DD</div><div class="value">${mb.max_dd:,.0f}</div></div>
  <div class="card"><div class="label">残高ピーク</div><div class="value">${mb.peak:,.0f}</div></div>
  <div class="card"><div class="label">最大到達SEQ</div><div class="value">[{mb.max_unit_seen}]=${max_unit}</div></div>
  <div class="card"><div class="label">BET数</div><div class="value">{mb.hands_bet:,}</div></div>
  <div class="card"><div class="label">勝率</div><div class="value">{wr:.2f}%</div></div>
  <div class="card"><div class="label">利確回数</div><div class="value">{mb.sessions_won:,}</div></div>
  <div class="card"><div class="label">入室シュー</div><div class="value">{stats['shoes_entered']:,}</div></div>
</div>
"""
    if mb.bankrupt:
        html += f"""
<div class="critical">
<strong>⚠️ 破綻: {mb.bankrupt_at}</strong><br>
SEQ[{mb.unit_idx}]=${SEQ[min(mb.unit_idx,len(SEQ)-1)]} × {SET_SIZE}turn = ${SEQ[min(mb.unit_idx,len(SEQ)-1)]*SET_SIZE} を残高${mb.balance:,.0f}で賄えなくなった。
</div>
"""
    html += f"""
<h2>💹 残高推移</h2>
<div id="chart"></div>

<h2>📅 日次パフォーマンス</h2>
<table>
<thead><tr><th>日付</th><th>BET数</th><th>勝率</th><th>利確回数</th><th>日次PNL</th><th>累計PNL</th></tr></thead>
<tbody>{daily_rows}</tbody></table>

<h2>🔬 この実験の意味</h2>
<div style="background:#11192a;border-left:4px solid #fbbf24;padding:14px 18px;border-radius:4px;">
<p>旧レポート <code>equity_counter_newseq_5turn_10k.html</code> は「破綻 0/62」を報告したが、
それは各テーブルに独立$10,000 (計$620,000) を配った結果。</p>
<p><strong>同じ戦略・同じエントリー・同じ退避なし</strong>を、単一$10,000 1財布に変えて時系列で回したのがこのレポート。</p>
<p>結果が <strong>{status}</strong> なら、{'真因は「1財布 vs 62財布」のスケール差' if mb.bankrupt else '退避ルール・リアルタイム判定が破綻の主因'}だと結論できる。</p>
</div>

<p class="ts" style="margin-top:32px;">
生成元: <code>generate_oldstyle_singlepool_backtest.py</code> / データ: {DB_PATH} / mode: {"NO_LOOKAHEAD" if NO_LOOKAHEAD else "LOOKAHEAD"}
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
  shapes: [{{
    type: 'line', x0: 0, x1: 1, xref: 'paper',
    y0: {START_CAPITAL}, y1: {START_CAPITAL},
    line: {{color: '#8a96a8', width: 1, dash: 'dash'}}
  }}]
}}, {{displayModeBar: false, responsive: true}});
</script>
</body></html>
"""
    out = os.path.join("report", "oldstyle_singlepool_backtest_nolookahead.html" if NO_LOOKAHEAD else "oldstyle_singlepool_backtest.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


def main():
    mb, stats, daily, total_shoes = run()
    print(f"\n{'='*70}")
    print(f"結果: {'破綻' if mb.bankrupt else '生存'}")
    print(f"最終残高: ${mb.balance:,.2f}  (通算 ${mb.balance - mb.capital:+,.2f})")
    print(f"最大DD: ${mb.max_dd:,.2f}  ピーク: ${mb.peak:,.2f}")
    print(f"最大到達SEQ: [{mb.max_unit_seen}] = ${SEQ[min(mb.max_unit_seen,len(SEQ)-1)]}")
    print(f"入室シュー: {stats['shoes_entered']:,} / スキップ: {stats['shoes_skipped']:,}")
    print(f"BET: {mb.hands_bet:,}  WIN: {mb.hands_win:,} ({mb.hands_win/max(mb.hands_bet,1)*100:.2f}%)")
    print(f"利確回数: {mb.sessions_won:,}")
    if mb.bankrupt:
        print(f"破綻時刻: {mb.bankrupt_at}")
    render_html(mb, stats, daily, total_shoes)


if __name__ == "__main__":
    main()
