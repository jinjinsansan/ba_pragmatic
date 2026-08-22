"""テレコ逆張り版 equity per-table — 新SEQ版

現行SEQと別のSEQ progression で破綻率を比較。

Usage:
  python generate_equity_counter_newseq.py --vps
  python generate_equity_counter_newseq.py --vps --capital 10000
  python generate_equity_counter_newseq.py --vps --capital 30000
  python generate_equity_counter_newseq.py --vps --capital 50000
"""
import sqlite3
import os
import sys
from collections import defaultdict
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"

def _parse_capital() -> int:
    for i, a in enumerate(sys.argv):
        if a == "--capital" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
        if a.startswith("--capital="):
            return int(a.split("=", 1)[1])
    return 1000

START_CAPITAL = _parse_capital()
PROFIT_PER_WIN = 30
BANKER_COMMISSION = 0.05
MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5
STATIC_WARMUP = 30

# 現行SEQ
SEQ_ORIGINAL = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
                60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
                148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]

# 新SEQ
SEQ_NEW = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
           60, 70, 80, 90, 100, 110, 120, 130,
           145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
           300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]


class CounterSim:
    def __init__(self, seq, target=PROFIT_PER_WIN):
        self.seq = seq
        self.target = target
        self.reset()

    def reset(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0.0
        self.max_dd = 0.0
        self.last_non_tie = None

    def _next_idx(self, used_idx, diff, new_os):
        if diff < 0:
            return min(used_idx + 1, len(self.seq) - 1)
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
            return min(bb + 1, len(self.seq) - 1)
        return 0

    def _complete(self):
        base_unit = self.seq[self.unit_idx] if self.unit_idx < len(self.seq) else self.seq[-1]
        money = 0.0
        for outcome, bet_side in self.turns:
            if outcome == 'O':
                if bet_side == 'B':
                    money += base_unit * (1.0 - BANKER_COMMISSION)
                else:
                    money += base_unit
            else:
                money -= base_unit
        wins = sum(1 for t in self.turns if t[0] == 'O')
        diff = wins - (7 - wins)
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
        if self.cumulative > self.peak:
            self.peak = self.cumulative
        self.max_dd = max(self.max_dd, self.peak - self.cumulative)

    def add(self, r):
        if r == 'T':
            return None
        if self.last_non_tie is None:
            self.last_non_tie = r
            return None
        bet_side = 'P' if self.last_non_tie == 'B' else 'B'
        won = (r == bet_side)
        self.turns.append(('O' if won else 'X', bet_side))
        self.last_non_tie = r
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        return None


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def simulate_no_losscut(shoes, start_capital, seq_table, target=PROFIT_PER_WIN):
    sim = CounterSim(seq=seq_table, target=target)
    balance = start_capital
    turns = []
    bankrupt = False
    session_start_ts = None

    for seq, started_at in shoes:
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue
        pattern = classify_pattern(clean[:STATIC_WARMUP], min_cols=3)
        if pattern != "テレコ+ニコ混合":
            continue

        for r in seq:
            if r not in ('P', 'B', 'T'):
                continue
            if session_start_ts is None:
                session_start_ts = started_at

            sim.add(r)

            if balance + sim.cumulative <= 0:
                turns.append({
                    'turn': len(turns) + 1,
                    'started_at': session_start_ts,
                    'outcome': 'bankrupt',
                    'session_pnl': sim.cumulative,
                    'balance': 0,
                })
                balance = 0
                bankrupt = True
                break

            if sim.cumulative >= target:
                profit = sim.cumulative
                balance += profit
                turns.append({
                    'turn': len(turns) + 1,
                    'started_at': session_start_ts,
                    'outcome': 'profit',
                    'session_pnl': profit,
                    'balance': balance,
                })
                session_start_ts = None
                sim.reset()

        if bankrupt:
            break

    return {
        'turns': turns,
        'final_balance': balance,
        'bankrupt_at': turns[-1]['turn'] if bankrupt else None,
        'max_dd': sim.max_dd,
    }


def load_shoes_by_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? ORDER BY started_at",
        (MIN_HANDS_PER_SHOE,)
    )
    shoes_by_table = defaultdict(list)
    total_hands = 0
    for tn, seq, ts in cur.fetchall():
        shoes_by_table[tn].append((seq, ts))
        total_hands += sum(1 for c in seq if c in ('P', 'B', 'T'))
    conn.close()
    return shoes_by_table, total_hands


def run_comparison(shoes_by_table):
    eligible = [(tn, shoes) for tn, shoes in shoes_by_table.items()
                if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]

    results = {}
    for label, seq_table in [("現行SEQ", SEQ_ORIGINAL), ("新SEQ", SEQ_NEW)]:
        table_results = []
        for tn, shoes in eligible:
            r = simulate_no_losscut(shoes, START_CAPITAL, seq_table)
            table_results.append({
                'name': tn, 'final_balance': r['final_balance'],
                'pnl': r['final_balance'] - START_CAPITAL,
                'bankrupt_at': r['bankrupt_at'],
                'turns': r['turns'],
                'max_dd': r['max_dd'],
                'shoes': len(shoes),
            })
        results[label] = table_results

    # 元本別
    capital_levels = [1000, 10000, 30000, 50000, 100000]
    cap_results = {}
    for label, seq_table in [("現行SEQ", SEQ_ORIGINAL), ("新SEQ", SEQ_NEW)]:
        cap_data = []
        for cap in capital_levels:
            bankrupt_n = 0
            profit_n = 0
            total_bal = 0
            for tn, shoes in eligible:
                r = simulate_no_losscut(shoes, cap, seq_table)
                if r['bankrupt_at']:
                    bankrupt_n += 1
                else:
                    profit_n += 1
                total_bal += r['final_balance']
            cap_data.append({
                'capital': cap, 'bankrupt': bankrupt_n, 'survived': profit_n,
                'total_profit': total_bal - cap * len(eligible),
                'roi': (total_bal - cap * len(eligible)) / (cap * len(eligible)) * 100 if len(eligible) > 0 else 0,
            })
        cap_results[label] = cap_data

    return results, cap_results, len(eligible)


def render_html(results, cap_results, total_tables, total_hands, total_shoes):
    # 比較テーブル
    comp_html = ""
    for cap_idx in range(len(cap_results["現行SEQ"])):
        orig = cap_results["現行SEQ"][cap_idx]
        new = cap_results["新SEQ"][cap_idx]
        diff_b = new['bankrupt'] - orig['bankrupt']
        diff_p = new['total_profit'] - orig['total_profit']
        diff_c = '#4ade80' if diff_b <= 0 else '#f87171'
        diff_pc = '#4ade80' if diff_p >= 0 else '#f87171'
        comp_html += (
            f"<tr>"
            f"<td><strong>${orig['capital']:,}</strong></td>"
            f"<td>{orig['bankrupt']}</td><td>{orig['survived']}</td>"
            f"<td style='color:{'#4ade80' if orig['total_profit']>=0 else '#f87171'}'>${orig['total_profit']:+,.0f}</td>"
            f"<td>{new['bankrupt']}</td><td>{new['survived']}</td>"
            f"<td style='color:{'#4ade80' if new['total_profit']>=0 else '#f87171'}'>${new['total_profit']:+,.0f}</td>"
            f"<td style='color:{diff_c}'>{diff_b:+d}</td>"
            f"<td style='color:{diff_pc}'>${diff_p:+,.0f}</td>"
            f"</tr>"
        )

    # テーブル別 (新SEQ)
    new_tables = sorted(results["新SEQ"], key=lambda x: x['final_balance'])
    sections_html = ""
    for t in new_tables:
        if t['bankrupt_at']:
            status = f"💀 {t['bankrupt_at']}ターン目で破綻"
            sc = "#7c2d2d"; cls = "bankrupt"
        elif t['pnl'] > 0:
            status = f"✅ +${t['pnl']:,.0f} 利益"
            sc = "#1a4a2a"; cls = "profit"
        else:
            status = "±0 / 未参加"
            sc = "#2a3441"; cls = "neutral"

        rows_html = ""
        for r in t['turns']:
            ts = r['started_at'][:16].replace('T', ' ') if r.get('started_at') else '-'
            if r['outcome'] == 'profit':
                rows_html += f"<tr class='profit'><td class='ts'>{ts}</td><td class='oc' style='color:#4ade80'>WIN +${r['session_pnl']:,.0f}</td><td class='bl' style='color:#4ade80'>${r['balance']:,.0f}</td></tr>"
            else:
                rows_html += f"<tr class='loss'><td class='ts'>{ts}</td><td class='oc' style='color:#f87171'>💀 破綻 (${r['session_pnl']:,.0f})</td><td class='bl' style='color:#f87171'>$0</td></tr>"

        sections_html += f"""
<div class="table-section {cls}" style="border-left-color:{sc};">
  <div class="table-header">
    <div><span class="tname">{t['name']}</span> <span class="tmeta">{t['shoes']}シュー</span></div>
    <div class="status">{status}</div>
    <div class="balance">最終: <strong>${t['final_balance']:,.0f}</strong></div>
  </div>
  <table class="ledger"><thead><tr><th>日時</th><th>結果</th><th>残高</th></tr></thead><tbody>{rows_html}</tbody></table>
</div>"""

    bankrupt_orig = sum(1 for t in results["現行SEQ"] if t['bankrupt_at'])
    bankrupt_new = sum(1 for t in results["新SEQ"] if t['bankrupt_at'])
    profit_new = sum(1 for t in results["新SEQ"] if t['pnl'] > 0)
    neutral_new = total_tables - bankrupt_new - profit_new

    # SEQ比較表示
    seq_orig_str = ", ".join(str(x) for x in SEQ_ORIGINAL[:15]) + " ..."
    seq_new_str = ", ".join(str(x) for x in SEQ_NEW[:15]) + " ..."

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>U. SEQ比較 テレコ逆張り — ${START_CAPITAL:,}スタート</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px; background: #1a2332; color: #6dd5ed; text-decoration: none; border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.banner {{ background: #2a1a3a; border-left: 5px solid #c084fc; padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8; }}
.banner strong {{ color: #c084fc; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.profit .value {{ color: #4ade80; }}
.card.bankrupt .value {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
.table-section {{ background: #1a2332; border-left: 5px solid #6dd5ed; margin: 16px 0; padding: 14px; border-radius: 4px; }}
.table-section.bankrupt {{ background: #2a1a1a; }}
.table-header {{ display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 10px; margin-bottom: 10px; align-items: center; }}
.tname {{ font-size: 16px; font-weight: bold; color: #ffd700; }}
.tmeta {{ font-size: 11px; color: #8a96a8; margin-left: 8px; }}
.status {{ font-size: 13px; text-align: center; }}
.balance {{ font-size: 12px; color: #8a96a8; text-align: right; }}
.balance strong {{ color: #ffd700; font-size: 15px; }}
.bankrupt .balance strong {{ color: #f87171; }}
.ledger {{ font-size: 12px; }}
.ledger td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.ledger td.bl {{ font-weight: bold; text-align: right; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.seq-box {{ background: #11192a; border: 1px solid #2a3441; border-radius: 6px; padding: 10px 14px; font-family: monospace; font-size: 12px; color: #8a96a8; margin: 8px 0; word-break: break-all; }}
</style>
</head>
<body>
<div class="container">
<h1>U. SEQ比較 テレコ逆張り (元本 ${START_CAPITAL:,})</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="equity_per_table_counter.html">Q. 現行SEQ版</a>
</div>

<div class="banner">
<strong>📊 SEQ (チップ progression) を変えた場合の破綻率比較。</strong><br>
テレコ混合 × 逆張り × 7ターン制 × $30利確 × 損切なし。<br>
データ: 全{total_hands:,}ハンド・{total_shoes}シュー・{total_tables}テーブル。
</div>

<h2>SEQ定義</h2>
<p class="note"><strong>現行SEQ:</strong></p>
<div class="seq-box">{seq_orig_str}</div>
<p class="note"><strong>新SEQ:</strong></p>
<div class="seq-box">{seq_new_str}</div>

<div class="summary">
  <div class="card"><div class="label">対象テーブル数</div><div class="value">{total_tables}</div></div>
  <div class="card profit"><div class="label">新SEQ プラス</div><div class="value">{profit_new}</div></div>
  <div class="card"><div class="label">新SEQ ±0</div><div class="value">{neutral_new}</div></div>
  <div class="card bankrupt"><div class="label">新SEQ 破綻</div><div class="value">{bankrupt_new}</div></div>
</div>

<h2>💰 元本別 現行SEQ vs 新SEQ</h2>
<table>
<thead><tr>
  <th rowspan="2">元本</th>
  <th colspan="3" style="text-align:center;color:#6dd5ed">現行SEQ</th>
  <th colspan="3" style="text-align:center;color:#fbbf24">新SEQ</th>
  <th colspan="2" style="text-align:center;color:#c084fc">差分</th>
</tr>
<tr>
  <th>破綻</th><th>生存</th><th>損益</th>
  <th>破綻</th><th>生存</th><th>損益</th>
  <th>破綻差</th><th>損益差</th>
</tr></thead>
<tbody>{comp_html}</tbody>
</table>

<h2>📋 新SEQ テーブル別 (元本${START_CAPITAL:,})</h2>
{sections_html}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_equity_counter_newseq.py</code> / テレコ逆張り / 7ターン / $30利確
</p>
</div>
</body>
</html>
"""
    suffix = f"_{START_CAPITAL // 1000}k" if START_CAPITAL >= 1000 else f"_{START_CAPITAL}"
    out_path = os.path.join("report", f"equity_counter_newseq{suffix}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")
    print(f"  新SEQ: {profit_new} profit, {neutral_new} neutral, {bankrupt_new} bankrupt")


def main():
    print(f"Loading {DB_PATH} (capital=${START_CAPITAL:,})...")
    shoes_by_table, total_hands = load_shoes_by_table()
    total_shoes = sum(len(v) for v in shoes_by_table.values())
    print(f"Total {total_hands:,} hands / {len(shoes_by_table)} tables / {total_shoes} shoes\n")
    results, cap_results, total_tables = run_comparison(shoes_by_table)
    render_html(results, cap_results, total_tables, total_hands, total_shoes)


if __name__ == "__main__":
    main()
