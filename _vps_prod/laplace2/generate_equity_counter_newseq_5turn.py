"""テレコ逆張り — 新SEQ + 5ターン制

新SEQ [1,3,5,7,10,13,...] × 5ターン制 × 逆張り × $10k元本

Usage:
  python generate_equity_counter_newseq_5turn.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
START_CAPITAL = 10000
PROFIT_PER_WIN = 30
BANKER_COMMISSION = 0.05
MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5
STATIC_WARMUP = 30
SET_SIZE = 5

SEQ_ORIGINAL = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
                60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
                148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]

SEQ_NEW = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
           60, 70, 80, 90, 100, 110, 120, 130,
           145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
           300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]


class CounterSim:
    def __init__(self, seq, target=PROFIT_PER_WIN, set_size=7):
        self.seq = seq
        self.target = target
        self.set_size = set_size
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
        losses = self.set_size - wins
        diff = wins - losses
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
        if len(self.turns) == self.set_size:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        return None


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def simulate(shoes, start_capital, seq_table, set_size, target=PROFIT_PER_WIN):
    sim = CounterSim(seq=seq_table, target=target, set_size=set_size)
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
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'bankrupt', 'session_pnl': sim.cumulative, 'balance': 0})
                balance = 0
                bankrupt = True
                break
            if sim.cumulative >= target:
                balance += sim.cumulative
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'profit', 'session_pnl': sim.cumulative, 'balance': balance})
                session_start_ts = None
                sim.reset()
        if bankrupt:
            break
    return {'turns': turns, 'final_balance': balance, 'bankrupt_at': turns[-1]['turn'] if bankrupt else None, 'max_dd': sim.max_dd}


def load_shoes_by_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT table_name, result_sequence, started_at FROM shoes_analytics WHERE hand_count >= ? ORDER BY started_at", (MIN_HANDS_PER_SHOE,))
    sbt = defaultdict(list)
    th = 0
    for tn, seq, ts in cur.fetchall():
        sbt[tn].append((seq, ts))
        th += sum(1 for c in seq if c in ('P', 'B', 'T'))
    conn.close()
    return sbt, th


def main():
    print(f"Loading {DB_PATH}...")
    sbt, th = load_shoes_by_table()
    ts = sum(len(v) for v in sbt.values())
    eligible = [(tn, shoes) for tn, shoes in sbt.items() if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]
    print(f"Total {th:,} hands / {len(sbt)} tables / {ts} shoes\n")

    configs = [
        ("現行SEQ × 7ターン", SEQ_ORIGINAL, 7),
        ("現行SEQ × 5ターン", SEQ_ORIGINAL, 5),
        ("新SEQ × 7ターン", SEQ_NEW, 7),
        ("新SEQ × 5ターン", SEQ_NEW, 5),
    ]

    all_results = {}
    for label, seq, ss in configs:
        print(f"  {label}...")
        tbl = []
        for tn, shoes in eligible:
            r = simulate(shoes, START_CAPITAL, seq, ss)
            # セッション内DD (残高ベース) も計算
            peak_bal = START_CAPITAL
            max_bal_dd = 0
            for tr in r['turns']:
                if tr['balance'] > peak_bal:
                    peak_bal = tr['balance']
                dd = peak_bal - tr['balance']
                if dd > max_bal_dd:
                    max_bal_dd = dd
            tbl.append({'name': tn, 'pnl': r['final_balance'] - START_CAPITAL,
                        'bankrupt': r['bankrupt_at'] is not None, 'max_dd': r['max_dd'],
                        'max_bal_dd': max_bal_dd,
                        'final': r['final_balance'], 'turns': r['turns'], 'shoes': len(shoes)})
        bankrupt_n = sum(1 for t in tbl if t['bankrupt'])
        profit_n = sum(1 for t in tbl if t['pnl'] > 0)
        total_pnl = sum(t['pnl'] for t in tbl)
        worst_dd = max((t['max_dd'] for t in tbl), default=0)
        avg_dd = sum(t['max_dd'] for t in tbl) / len(tbl) if tbl else 0
        all_results[label] = {'tables': tbl, 'bankrupt': bankrupt_n, 'profit': profit_n, 'total_pnl': total_pnl, 'worst_dd': worst_dd, 'avg_dd': avg_dd}
        print(f"    破綻{bankrupt_n} / 黒字{profit_n} / PNL ${total_pnl:+,.0f}")

    # HTML
    comp_rows = ""
    for label, seq, ss in configs:
        r = all_results[label]
        pnl_c = '#4ade80' if r['total_pnl'] >= 0 else '#f87171'
        bg = "background:#1a3a1a;" if label == "新SEQ × 5ターン" else ""
        comp_rows += (f"<tr style='{bg}'><td style='font-weight:bold'>{label}</td>"
                      f"<td>{r['bankrupt']}</td><td>{r['profit']}</td>"
                      f"<td style='color:{pnl_c};font-weight:bold'>${r['total_pnl']:+,.0f}</td>"
                      f"<td style='color:#fbbf24'>${r['worst_dd']:,.0f}</td>"
                      f"<td>${r['avg_dd']:,.0f}</td></tr>")

    # 新SEQ×5ターンのテーブル別
    best = all_results["新SEQ × 5ターン"]
    best_tables = sorted(best['tables'], key=lambda x: x['final'])
    sections = ""
    for t in best_tables:
        if t['bankrupt']:
            st = f"💀 破綻"; sc = "#7c2d2d"; cls = "bankrupt"
        elif t['pnl'] > 0:
            st = f"✅ +${t['pnl']:,.0f}"; sc = "#1a4a2a"; cls = "profit"
        else:
            st = "±0"; sc = "#2a3441"; cls = ""
        rows = ""
        for r in t['turns']:
            ts_str = r['started_at'][:16].replace('T', ' ') if r.get('started_at') else '-'
            if r['outcome'] == 'profit':
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#4ade80'>WIN +${r['session_pnl']:,.0f}</td><td style='color:#4ade80;text-align:right'>${r['balance']:,.0f}</td></tr>"
            else:
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#f87171'>💀 破綻 (${r['session_pnl']:,.0f})</td><td style='color:#f87171;text-align:right'>$0</td></tr>"
        sections += f"""
<div style="background:#1a2332;border-left:5px solid {sc};margin:14px 0;padding:14px;border-radius:4px;">
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;margin-bottom:8px;align-items:center;">
    <div><span style="font-size:16px;font-weight:bold;color:#ffd700">{t['name']}</span> <span style="font-size:11px;color:#8a96a8">{t['shoes']}シュー</span></div>
    <div style="text-align:center">{st}</div>
    <div style="text-align:right;color:#8a96a8">最終: <strong style="color:{'#f87171' if t['bankrupt'] else '#ffd700'}">${t['final']:,.0f}</strong> / MaxDD: <strong style="color:#fbbf24">${t['max_dd']:,.0f}</strong></div>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left;padding:4px 8px;color:#c084fc">日時</th><th style="text-align:left;padding:4px 8px;color:#c084fc">結果</th><th style="text-align:right;padding:4px 8px;color:#c084fc">残高</th></tr></thead><tbody>{rows}</tbody></table>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>X. 新SEQ × 5ターン制 — $10,000スタート</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, sans-serif; background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px; background: #1a2332; color: #6dd5ed; text-decoration: none; border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.banner {{ background: #1a2a1a; border-left: 5px solid #4ade80; padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8; }}
.banner strong {{ color: #4ade80; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
.card.r .value {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.note {{ color: #8a96a8; font-size: 13px; }}
.seq-box {{ background: #11192a; border: 1px solid #2a3441; border-radius: 6px; padding: 10px 14px; font-family: monospace; font-size: 12px; color: #8a96a8; margin: 8px 0; }}
</style></head><body><div class="container">
<h1>X. 新SEQ × 5ターン制 ($10,000スタート)</h1>
<div class="nav"><a href="index.html">← レポートTOP</a><a href="equity_per_table_counter.html">Q. 現行7ターン</a><a href="equity_counter_newseq_10k.html">U. 新SEQ 7ターン</a></div>
<div class="banner">
<strong>📊 4パターンを同一条件で比較。</strong><br>
テレコ混合 × 逆張り / $1スタート / $10,000元本 / $30利確 / 損切なし。<br>
データ: 全{th:,}ハンド・{ts}シュー・{len(eligible)}テーブル。
</div>

<h2>新SEQ定義</h2>
<div class="seq-box">{', '.join(str(x) for x in SEQ_NEW[:20])} ...</div>

<div class="summary">
  <div class="card"><div class="label">対象テーブル</div><div class="value">{len(eligible)}</div></div>
  <div class="card g"><div class="label">新SEQ×5T 黒字</div><div class="value">{best['profit']}</div></div>
  <div class="card r"><div class="label">新SEQ×5T 破綻</div><div class="value">{best['bankrupt']}</div></div>
  <div class="card g"><div class="label">新SEQ×5T PNL</div><div class="value">${best['total_pnl']:+,.0f}</div></div>
</div>

<h2>💰 4パターン比較</h2>
<table><thead><tr><th>構成</th><th>破綻</th><th>黒字</th><th>通算損益</th><th>最大DD</th><th>平均DD</th></tr></thead><tbody>{comp_rows}</tbody></table>

<h2>📋 新SEQ × 5ターン テーブル別</h2>
{sections}

<p class="note" style="margin-top:32px;">生成元: <code>generate_equity_counter_newseq_5turn.py</code></p>
</div></body></html>"""

    out = os.path.join("report", "equity_counter_newseq_5turn_10k.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
