"""GUI再現 リアルタイム バックテスト (信頼できる版)

ユーザー指定仕様:
  - 入室: 直近15列のテレコ(1落ち+2落ち) 80%以上
  - 退室: 3落ち以上×2列 OR 5落ち
  - 単位: $1 start
  - 利確: $30
  - 元本: $10,000 (単一財布)
  - SEQ: 新SEQ (SEQ_COUNTER) × 5ターン
  - 実運用再現: 1財布でテーブルを跨ぎ、look-aheadなし、
    テーブル切替でもSEQ state は継続する

Usage:
  python generate_gui_realistic_backtest.py [--vps]
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter

USE_VPS = "--vps" in sys.argv
DB_PATH = "analytics_vps.sqlite3" if USE_VPS else "analytics_vps.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50

# ===== ユーザー指定 =====
START_CAPITAL = 10000.0
PROFIT_TARGET = 30.0
BASE_UNIT = 1.0

TEREKO_WINDOW = 15
TEREKO_THRESHOLD = 0.80

EXIT_3DROP_LIMIT = 2   # 3落ち以上がN回で退室
EXIT_DEEP_LIMIT = 5    # X落ちで即退室

# ===== Counter SEQ (新SEQ) =====
SEQ = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
       60, 70, 80, 90, 100, 110, 120, 130,
       145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
       300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]
SET_SIZE = 5

BANKER_COMMISSION = 0.05


class MaruBatsuBankroll:
    """単一財布で MaruBatsu を回す。破綻判定あり。"""

    def __init__(self, capital: float):
        self.capital = capital
        self.balance = capital
        self.peak = capital
        self.max_dd = 0.0

        self.cumulative = 0.0  # current session PNL
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []

        self.sessions_won = 0
        self.sessions_lost_count = 0
        self.total_completed_profit = 0.0  # profit from completed $30 sessions
        self.hands_bet = 0
        self.hands_win = 0
        self.hands_loss = 0
        self.bankrupt = False
        self.bankrupt_at = None
        # for session tracking
        self.max_unit_seen = 0
        # snapshot of balance over time
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
                    bad = dd
                    ba = s['next']
                if dd < 0 and (-dd) < bbd:
                    bbd = -dd
                    bb = s['next']
        if ba >= 0:
            return ba
        if bb >= 0:
            return min(bb + 1, len(SEQ) - 1)
        return 0

    def _complete_set(self):
        base = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        money = 0.0
        for outcome, bet_side in self.turns:
            stake = base * BASE_UNIT
            if outcome == 'O':
                if bet_side == 'B':
                    money += stake * (1.0 - BANKER_COMMISSION)
                else:
                    money += stake
            else:
                money -= stake
        wins = sum(1 for t in self.turns if t[0] == 'O')
        diff = wins - (SET_SIZE - wins)
        self.cumulative += money
        self.balance += money
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
        if self.unit_idx > self.max_unit_seen:
            self.max_unit_seen = self.unit_idx
        # update peak/DD
        if self.balance > self.peak:
            self.peak = self.balance
        dd = self.peak - self.balance
        if dd > self.max_dd:
            self.max_dd = dd

    def check_bankrupt(self) -> bool:
        """次のセットを打ち切れない残高なら破綻"""
        next_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        # 最悪ケース: 次セット全敗 = next_unit * SET_SIZE 必要
        worst_case = next_unit * SET_SIZE * BASE_UNIT
        if self.balance < worst_case:
            return True
        return False

    def add_bet(self, won: bool, bet_side: str, ts: str):
        if self.bankrupt:
            return
        self.hands_bet += 1
        if won:
            self.hands_win += 1
        else:
            self.hands_loss += 1
        self.turns.append(('O' if won else 'X', bet_side))
        if len(self.turns) == SET_SIZE:
            self._complete_set()
        if self.cumulative >= PROFIT_TARGET:
            # session complete
            self.total_completed_profit += self.cumulative
            self.sessions_won += 1
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
        # bankruptcy check after each bet
        if self.check_bankrupt():
            self.bankrupt = True
            self.bankrupt_at = ts
        # sample balance snapshot (every 500 bets)
        if self.hands_bet % 500 == 0:
            self.balance_curve.append((ts, self.balance))


class TableState:
    def __init__(self, name):
        self.name = name
        self.columns = []
        self.current_col = 0
        self.last_side = None
        self.last_nt = None
        self.is_active = False
        self.entry_col_idx = 0

    def feed(self, ch):
        if ch == 'T':
            return
        if ch == self.last_side:
            self.current_col += 1
        else:
            if self.last_side is not None:
                self.columns.append(self.current_col)
            self.current_col = 1
            self.last_side = ch
        self.last_nt = ch

    def finalize_shoe(self):
        if self.current_col > 0 and self.last_side is not None:
            self.columns.append(self.current_col)
        self.current_col = 0
        self.last_side = None

    def is_tereko(self) -> bool:
        if len(self.columns) < TEREKO_WINDOW:
            return False
        recent = self.columns[-TEREKO_WINDOW:]
        short = sum(1 for L in recent if L <= 2)
        return (short / len(recent)) >= TEREKO_THRESHOLD

    def check_exit(self):
        if not self.is_active:
            return None
        cols_since = self.columns[self.entry_col_idx:]
        check_cols = list(cols_since)
        if self.current_col >= 3:
            check_cols.append(self.current_col)
        # 5落ち即退室
        for L in check_cols:
            if L >= EXIT_DEEP_LIMIT:
                return f"{EXIT_DEEP_LIMIT}落ち発生"
        if self.current_col >= EXIT_DEEP_LIMIT:
            return f"{EXIT_DEEP_LIMIT}落ち発生(進行中)"
        # 3落ち以上×2列
        d3 = sum(1 for L in check_cols if L >= 3)
        if d3 >= EXIT_3DROP_LIMIT:
            return f"3落ち以上×{d3}"
        return None

    def enter(self):
        self.is_active = True
        self.entry_col_idx = len(self.columns)

    def exit(self):
        self.is_active = False


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
    print(f"Loaded {len(shoes):,} shoes")

    mb = MaruBatsuBankroll(START_CAPITAL)
    table_states = {}
    stats = {
        'entries': 0, 'exits': 0,
        'exit_reasons': Counter(),
    }
    session_log = []
    daily_pnl = defaultdict(lambda: {'pnl': 0.0, 'bets': 0, 'wins': 0, 'sessions': 0})

    current_table = None
    current_entry_ts = None
    current_session_bets = 0
    current_session_wins = 0
    current_session_pnl_start = 0.0
    prev_sessions_won = 0
    prev_total_profit = 0.0

    for si, (tn, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si:,}/{len(shoes):,}...  balance=${mb.balance:,.0f}  idx={mb.unit_idx}")

        if mb.bankrupt:
            break

        if tn not in table_states:
            table_states[tn] = TableState(tn)
        ts = table_states[tn]

        clean = ''.join(ch for ch in seq if ch in ('P', 'B', 'T'))
        day = started_at[:10]

        for ch in clean:
            if ch == 'T':
                ts.feed(ch)
                continue

            # Exit check (before updating)
            if current_table == tn and ts.is_active:
                reason = ts.check_exit()
                if reason:
                    ts.exit()
                    stats['exits'] += 1
                    stats['exit_reasons'][reason] += 1
                    session_pnl = mb.balance - current_session_pnl_start
                    session_log.append({
                        'table': tn,
                        'entry_ts': current_entry_ts,
                        'exit_ts': started_at,
                        'reason': reason,
                        'bets': current_session_bets,
                        'wins': current_session_wins,
                        'pnl': session_pnl,
                        'balance_after': mb.balance,
                    })
                    current_table = None

            # Entry check
            if current_table is None:
                if ts.is_tereko():
                    ts.enter()
                    current_table = tn
                    current_entry_ts = started_at
                    current_session_bets = 0
                    current_session_wins = 0
                    current_session_pnl_start = mb.balance
                    stats['entries'] += 1

            # BET
            if current_table == tn and ts.is_active and ts.last_nt is not None:
                bet_side = 'P' if ts.last_nt == 'B' else 'B'
                won = (ch == bet_side)
                current_session_bets += 1
                if won:
                    current_session_wins += 1
                mb.add_bet(won, bet_side, started_at)
                # Track daily
                daily_pnl[day]['bets'] += 1
                if won:
                    daily_pnl[day]['wins'] += 1
                # completed session bookkeeping
                if mb.sessions_won > prev_sessions_won:
                    delta = mb.total_completed_profit - prev_total_profit
                    daily_pnl[day]['pnl'] += delta
                    daily_pnl[day]['sessions'] += 1
                    prev_sessions_won = mb.sessions_won
                    prev_total_profit = mb.total_completed_profit
                if mb.bankrupt:
                    break

            ts.feed(ch)
        ts.finalize_shoe()

        if mb.bankrupt:
            session_log.append({
                'table': current_table or tn,
                'entry_ts': current_entry_ts or started_at,
                'exit_ts': mb.bankrupt_at or started_at,
                'reason': '破綻',
                'bets': current_session_bets,
                'wins': current_session_wins,
                'pnl': mb.balance - current_session_pnl_start,
                'balance_after': mb.balance,
            })
            break

    return mb, stats, session_log, daily_pnl, len(shoes)


def render_html(mb, stats, session_log, daily_pnl, total_shoes):
    # balance curve points
    bc = mb.balance_curve
    if not bc or bc[-1][1] != mb.balance:
        bc = bc + [('END', mb.balance)]

    # session log for display: last 100
    recent_sessions = session_log[-100:]

    # daily table
    daily_rows = ""
    days = sorted(daily_pnl.keys())
    cum = 0.0
    for d in days:
        row = daily_pnl[d]
        cum += row['pnl']
        wr = (row['wins'] / row['bets'] * 100) if row['bets'] > 0 else 0
        pnl_c = '#4ade80' if row['pnl'] > 0 else ('#f87171' if row['pnl'] < 0 else '#8a96a8')
        cum_c = '#4ade80' if cum > 0 else ('#f87171' if cum < 0 else '#8a96a8')
        daily_rows += (
            f"<tr><td class='ts'>{d}</td>"
            f"<td>{row['bets']:,}</td><td>{wr:.1f}%</td>"
            f"<td>{row['sessions']}</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${row['pnl']:+,.0f}</td>"
            f"<td style='color:{cum_c}'>${cum:+,.0f}</td></tr>"
        )

    # session log rows
    sess_rows = ""
    for s in recent_sessions:
        ts = (s['entry_ts'] or '')[:16].replace('T', ' ')
        pnl_c = '#4ade80' if s['pnl'] > 0 else ('#f87171' if s['pnl'] < 0 else '#8a96a8')
        wr = (s['wins'] / s['bets'] * 100) if s['bets'] > 0 else 0
        reason_color = '#f87171' if s['reason'] == '破綻' else '#fbbf24'
        sess_rows += (
            f"<tr>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tname'>{s['table']}</td>"
            f"<td style='color:{reason_color}'>{s['reason']}</td>"
            f"<td>{s['bets']}</td>"
            f"<td>{wr:.0f}%</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${s['pnl']:+,.0f}</td>"
            f"<td>${s['balance_after']:,.0f}</td>"
            f"</tr>"
        )

    # exit reasons
    exit_rows = ""
    for r, cnt in stats['exit_reasons'].most_common():
        pct = cnt / stats['exits'] * 100 if stats['exits'] > 0 else 0
        exit_rows += f"<tr><td>{r}</td><td>{cnt:,}</td><td>{pct:.1f}%</td></tr>"

    # balance curve JSON for plot
    import json
    bc_labels = [p[0][:16] if p[0] != 'END' else 'END' for p in bc]
    bc_values = [round(p[1], 2) for p in bc]
    bc_labels_j = json.dumps(bc_labels)
    bc_values_j = json.dumps(bc_values)

    status_label = "破綻" if mb.bankrupt else "生存"
    status_color = "#f87171" if mb.bankrupt else "#4ade80"
    pnl = mb.balance - mb.capital
    pnl_color = "#4ade80" if pnl >= 0 else "#f87171"
    max_unit_amount = SEQ[min(mb.max_unit_seen, len(SEQ)-1)]
    wr = (mb.hands_win / mb.hands_bet * 100) if mb.hands_bet > 0 else 0

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>GUI再現 リアルタイム バックテスト — $10,000 / $1 / $30利確</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 26px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 20px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{
  display: inline-block; margin-right: 12px; padding: 8px 16px;
  background: #1a2332; color: #6dd5ed; text-decoration: none;
  border-radius: 4px; border: 1px solid #2a3441; font-size: 13px;
}}
.banner {{
  background: #1a2a1a; border-left: 5px solid {status_color};
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: {status_color}; }}
.summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 20px 0;
}}
.card {{
  background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
.card.r .value {{ color: #f87171; }}
.card.y .value {{ color: #fbbf24; }}

table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
td.tname {{ font-weight: bold; color: #ffd700; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.spec {{
  background: #11192a; border: 1px solid #2a3441; border-radius: 6px;
  padding: 12px 16px; font-family: monospace; font-size: 12px;
  color: #8a96a8; margin: 12px 0;
}}
#chart {{ width: 100%; height: 400px; }}
.critical {{
  background: #2a1818; border: 2px solid #f87171; padding: 14px 18px;
  border-radius: 6px; margin: 16px 0;
}}
.critical strong {{ color: #f87171; }}
</style>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head>
<body>
<div class="container">
<h1>GUI再現 リアルタイム バックテスト</h1>
<div class="nav"><a href="index.html">← レポートTOP</a></div>

<div class="banner">
<strong>📊 実運用を忠実に再現（look-aheadなし・単一財布・テーブル跨ぎでSEQ継続）</strong><br>
<strong>入室:</strong> 直近15列のテレコ（1落ち+2落ち）80%以上 → 入室<br>
<strong>退室:</strong> 3落ち以上×2列 OR 5落ち → 即退室 → 次のテレコテーブルを探す<br>
<strong>資金:</strong> 元本 $10,000 / $1 base / $30利確 / 新SEQ × 5ターン<br>
<strong>破綻判定:</strong> 残高 &lt; 次セット最悪損失（next_unit × 5） で破綻<br>
データ: {DATE_FROM}〜 / {total_shoes:,}シュー
</div>

<div class="spec">
SEQ = [{', '.join(str(x) for x in SEQ[:14])}, ...]  (計{len(SEQ)}段階, 最大 ${SEQ[-1]})<br>
SET_SIZE = {SET_SIZE} turns  /  BANKER_COMMISSION = {BANKER_COMMISSION}
</div>

<div class="summary">
  <div class="card {'r' if mb.bankrupt else 'g'}"><div class="label">結果</div><div class="value">{status_label}</div></div>
  <div class="card"><div class="label">最終残高</div><div class="value">${mb.balance:,.0f}</div></div>
  <div class="card {'g' if pnl>=0 else 'r'}"><div class="label">通算損益</div><div class="value">${pnl:+,.0f}</div></div>
  <div class="card y"><div class="label">最大DD</div><div class="value">${mb.max_dd:,.0f}</div></div>
  <div class="card"><div class="label">残高ピーク</div><div class="value">${mb.peak:,.0f}</div></div>
  <div class="card"><div class="label">最大到達SEQ</div><div class="value">[{mb.max_unit_seen}]=${max_unit_amount}</div></div>
  <div class="card"><div class="label">入室回数</div><div class="value">{stats['entries']:,}</div></div>
  <div class="card"><div class="label">利確回数</div><div class="value">{mb.sessions_won:,}</div></div>
  <div class="card"><div class="label">BET数</div><div class="value">{mb.hands_bet:,}</div></div>
  <div class="card"><div class="label">勝率</div><div class="value">{wr:.2f}%</div></div>
</div>
"""

    if mb.bankrupt:
        html += f"""
<div class="critical">
<strong>⚠️ 破綻: {mb.bankrupt_at}</strong><br>
次セット最悪損失（SEQ[{mb.unit_idx}]=${SEQ[min(mb.unit_idx,len(SEQ)-1)]} × {SET_SIZE}turn = ${SEQ[min(mb.unit_idx,len(SEQ)-1)]*SET_SIZE}）を残高${mb.balance:,.0f}で賄えなくなり継続不可能と判定されました。
</div>
"""

    html += f"""
<h2>💹 残高推移</h2>
<div id="chart"></div>

<h2>📅 日次パフォーマンス</h2>
<table>
<thead><tr><th>日付</th><th>BET数</th><th>勝率</th><th>利確回数</th><th>日次PNL</th><th>累計PNL</th></tr></thead>
<tbody>{daily_rows}</tbody>
</table>

<h2>🚪 退室理由の内訳</h2>
<table style="max-width:500px">
<thead><tr><th>理由</th><th>回数</th><th>比率</th></tr></thead>
<tbody>{exit_rows}</tbody>
</table>

<h2>📝 入退室ログ（直近100件）</h2>
<table>
<thead><tr><th>入室時刻</th><th>テーブル</th><th>退室理由</th><th>BET数</th><th>勝率</th><th>セッションPNL</th><th>退室時残高</th></tr></thead>
<tbody>{sess_rows}</tbody>
</table>

<p class="ts" style="margin-top:32px;">
生成元: <code>generate_gui_realistic_backtest.py</code> /
データ: {DB_PATH}
</p>

</div>
<script>
const labels = {bc_labels_j};
const values = {bc_values_j};
Plotly.newPlot('chart', [{{
  x: labels, y: values, type: 'scatter', mode: 'lines',
  line: {{color: '#4ade80', width: 2}}, fill: 'tozeroy',
  fillcolor: 'rgba(74, 222, 128, 0.1)',
  hovertemplate: '%{{x}}<br>$%{{y:,.0f}}<extra></extra>'
}}], {{
  paper_bgcolor: '#0f1419', plot_bgcolor: '#11192a',
  font: {{color: '#e0e6ed', family: 'sans-serif'}},
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

    out = os.path.join("report", "gui_realistic_backtest.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


def main():
    mb, stats, session_log, daily_pnl, total_shoes = run()
    print(f"\n{'='*70}")
    print(f"結果: {'破綻' if mb.bankrupt else '生存'}")
    print(f"最終残高: ${mb.balance:,.2f}  (通算 ${mb.balance - mb.capital:+,.2f})")
    print(f"最大DD: ${mb.max_dd:,.2f}  ピーク: ${mb.peak:,.2f}")
    print(f"最大到達SEQ: [{mb.max_unit_seen}] = ${SEQ[min(mb.max_unit_seen,len(SEQ)-1)]}")
    print(f"入室: {stats['entries']:,}  退室: {stats['exits']:,}")
    print(f"BET: {mb.hands_bet:,}  WIN: {mb.hands_win:,} ({mb.hands_win/max(mb.hands_bet,1)*100:.2f}%)")
    print(f"利確回数: {mb.sessions_won:,}")
    if mb.bankrupt:
        print(f"破綻時刻: {mb.bankrupt_at}")
    render_html(mb, stats, session_log, daily_pnl, total_shoes)


if __name__ == "__main__":
    main()
