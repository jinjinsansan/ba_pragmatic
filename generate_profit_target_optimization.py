"""利確ターゲット最適化バックテスト

テレコ混合 × 逆張り × 〇✖ で、利確額を変えた場合の
DD・ROI・セッション完走率・資金効率を比較。

利確設定: $20 / $30 / $40 / $50 / $60 / $80 / $100

Usage:
  python generate_profit_target_optimization.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
STATIC_WARMUP = 30
BANKER_COMMISSION = 0.05
START_CAPITAL = 10000

TARGETS = [20, 30, 40, 50, 60, 80, 100]

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class CounterMaruBatsuSim:
    def __init__(self, target=50):
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

    def _complete(self):
        base_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
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


def simulate_all_tables(shoes, target, start_capital):
    """全テーブル独立シミュレーション"""
    shoes_by_table = defaultdict(list)
    for tn, seq, ts in shoes:
        shoes_by_table[tn].append((seq, ts))

    table_results = []
    for tn, table_shoes in shoes_by_table.items():
        if len(table_shoes) < 30:
            continue
        sim = CounterMaruBatsuSim(target=target)
        balance = start_capital
        sessions_won = 0
        max_dd_from_start = 0.0
        min_balance = start_capital
        peak_balance = start_capital
        bankrupt = False
        total_bets = 0
        total_wins = 0
        tereko_shoes = 0
        equity_curve = [start_capital]

        for seq, started_at in table_shoes:
            clean = strip_ties(seq)
            if len(clean) < STATIC_WARMUP:
                continue
            pattern = classify_pattern(clean[:STATIC_WARMUP], min_cols=3)
            if pattern != "テレコ+ニコ混合":
                continue
            tereko_shoes += 1

            for r in seq:
                if r not in ('P', 'B', 'T'):
                    continue
                if r != 'T' and sim.last_non_tie is not None:
                    total_bets += 1
                    bet_side = 'P' if sim.last_non_tie == 'B' else 'B'
                    if r == bet_side:
                        total_wins += 1

                result = sim.add(r)
                current_bal = balance + sim.cumulative

                if current_bal <= 0:
                    bankrupt = True
                    balance = 0
                    equity_curve.append(0)
                    break

                if result == 'profit':
                    balance += sim.cumulative
                    sessions_won += 1
                    sim.reset()
                    equity_curve.append(balance)

                if balance + sim.cumulative < min_balance:
                    min_balance = balance + sim.cumulative
                if balance > peak_balance:
                    peak_balance = balance
                dd = peak_balance - (balance + sim.cumulative)
                if dd > max_dd_from_start:
                    max_dd_from_start = dd

            if bankrupt:
                break

        final_balance = balance + sim.cumulative if not bankrupt else 0
        pnl = final_balance - start_capital

        table_results.append({
            'name': tn,
            'final_balance': final_balance,
            'pnl': pnl,
            'sessions_won': sessions_won,
            'max_dd': max_dd_from_start,
            'min_balance': min_balance,
            'bankrupt': bankrupt,
            'total_bets': total_bets,
            'total_wins': total_wins,
            'tereko_shoes': tereko_shoes,
            'shoes': len(table_shoes),
            'hit_rate': total_wins / total_bets * 100 if total_bets > 0 else 0,
        })

    return table_results


def run_backtest(shoes):
    results = {}
    for target in TARGETS:
        print(f"  Target=${target}...")
        table_results = simulate_all_tables(shoes, target, START_CAPITAL)

        total_tables = len(table_results)
        bankrupt_count = sum(1 for t in table_results if t['bankrupt'])
        profit_count = sum(1 for t in table_results if t['pnl'] > 0)
        neutral_count = total_tables - bankrupt_count - profit_count

        total_pnl = sum(t['pnl'] for t in table_results)
        total_sessions = sum(t['sessions_won'] for t in table_results)
        total_bets = sum(t['total_bets'] for t in table_results)
        total_wins = sum(t['total_wins'] for t in table_results)
        avg_hr = total_wins / total_bets * 100 if total_bets > 0 else 0

        max_dd_worst = max((t['max_dd'] for t in table_results), default=0)
        avg_dd = sum(t['max_dd'] for t in table_results) / total_tables if total_tables > 0 else 0
        max_dd_median = sorted(t['max_dd'] for t in table_results)[total_tables // 2] if total_tables > 0 else 0

        # 資金効率 = 利益 / 最大DD (高いほど効率的)
        efficiency = total_pnl / max_dd_worst if max_dd_worst > 0 else float('inf')
        # 利益/BET回数
        profit_per_bet = total_pnl / total_bets if total_bets > 0 else 0

        results[target] = {
            'target': target,
            'total_tables': total_tables,
            'bankrupt': bankrupt_count,
            'profit': profit_count,
            'neutral': neutral_count,
            'total_pnl': total_pnl,
            'total_sessions': total_sessions,
            'total_bets': total_bets,
            'avg_hr': avg_hr,
            'max_dd_worst': max_dd_worst,
            'avg_dd': avg_dd,
            'max_dd_median': max_dd_median,
            'efficiency': efficiency,
            'profit_per_bet': profit_per_bet,
            'roi': total_pnl / (START_CAPITAL * total_tables) * 100 if total_tables > 0 else 0,
            'table_results': table_results,
        }

    return results


def render_html(results, total_shoes):
    # メイン比較表
    comp_rows = ""
    best_efficiency = max(r['efficiency'] for r in results.values())
    best_pnl = max(r['total_pnl'] for r in results.values())
    best_dd = min(r['max_dd_worst'] for r in results.values() if r['max_dd_worst'] > 0)

    for target in TARGETS:
        r = results[target]
        is_best_eff = r['efficiency'] == best_efficiency
        is_best_pnl = r['total_pnl'] == best_pnl
        is_best_dd = r['max_dd_worst'] == best_dd

        pnl_c = '#4ade80' if r['total_pnl'] >= 0 else '#f87171'
        eff_c = '#4ade80' if r['efficiency'] > 0 else '#f87171'
        row_bg = "background:#1a3a1a;" if is_best_eff else ""

        badges = ""
        if is_best_eff:
            badges += " 🏆効率"
        if is_best_pnl:
            badges += " 💰利益"
        if is_best_dd:
            badges += " 🛡️DD"

        comp_rows += (
            f"<tr style='{row_bg}'>"
            f"<td style='font-weight:bold;font-size:16px'>${target}{badges}</td>"
            f"<td>{r['bankrupt']}</td>"
            f"<td>{r['profit']}</td>"
            f"<td>{r['total_sessions']:,}</td>"
            f"<td>{r['total_bets']:,}</td>"
            f"<td>{r['avg_hr']:.2f}%</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${r['total_pnl']:+,.0f}</td>"
            f"<td>{r['roi']:+.1f}%</td>"
            f"<td style='color:#f87171'>${r['max_dd_worst']:,.0f}</td>"
            f"<td>${r['avg_dd']:,.0f}</td>"
            f"<td>${r['max_dd_median']:,.0f}</td>"
            f"<td style='color:{eff_c};font-weight:bold'>{r['efficiency']:.2f}</td>"
            f"<td>${r['profit_per_bet']:.4f}</td>"
            f"</tr>"
        )

    # 各利確額のテーブル別DD分布
    dd_detail_html = ""
    for target in TARGETS:
        r = results[target]
        tables = sorted(r['table_results'], key=lambda x: -x['max_dd'])
        rows = ""
        for t in tables[:15]:
            pnl_c = '#4ade80' if t['pnl'] > 0 else ('#f87171' if t['pnl'] < 0 else '#555')
            dd_pct = t['max_dd'] / START_CAPITAL * 100
            dd_c = '#4ade80' if dd_pct < 5 else ('#fbbf24' if dd_pct < 20 else '#f87171')
            rows += (
                f"<tr>"
                f"<td class='tname'>{t['name']}</td>"
                f"<td>{t['tereko_shoes']}/{t['shoes']}</td>"
                f"<td>{t['sessions_won']}</td>"
                f"<td>{t['total_bets']:,}</td>"
                f"<td>{t['hit_rate']:.1f}%</td>"
                f"<td style='color:{pnl_c}'>${t['pnl']:+,.0f}</td>"
                f"<td style='color:{dd_c};font-weight:bold'>${t['max_dd']:,.0f} ({dd_pct:.1f}%)</td>"
                f"<td>${t['min_balance']:,.0f}</td>"
                f"</tr>"
            )
        dd_detail_html += f"""
<details style="margin:12px 0;">
<summary style="cursor:pointer;color:#c084fc;font-size:16px;font-weight:bold">
  ${target} 利確 — DD上位15テーブル (破綻{r['bankrupt']} / 通算${r['total_pnl']:+,.0f})
</summary>
<table>
<thead><tr>
  <th>テーブル</th><th>テレコ/全</th><th>完走</th><th>BET数</th><th>勝率</th>
  <th>PNL</th><th>MaxDD</th><th>最低残高</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</details>"""

    # 資金効率チャート (テキストバー)
    eff_chart = ""
    max_eff = max(r['efficiency'] for r in results.values())
    for target in TARGETS:
        r = results[target]
        bar_w = r['efficiency'] / max_eff * 300 if max_eff > 0 else 0
        c = '#4ade80' if r['efficiency'] > 0 else '#f87171'
        is_best = r['efficiency'] == best_efficiency
        label_style = "font-weight:bold;color:#ffd700;" if is_best else ""
        eff_chart += f"""
<div style="display:flex;align-items:center;gap:12px;margin:6px 0;font-size:14px">
  <div style="min-width:60px;{label_style}">${target}</div>
  <div style="flex:1;height:24px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:80px;text-align:right;color:{c};font-weight:bold">{r['efficiency']:.2f}</div>
</div>"""

    # DD チャート
    dd_chart = ""
    max_dd_all = max(r['max_dd_worst'] for r in results.values())
    for target in TARGETS:
        r = results[target]
        bar_w = r['max_dd_worst'] / max_dd_all * 300 if max_dd_all > 0 else 0
        dd_pct = r['max_dd_worst'] / START_CAPITAL * 100
        c = '#4ade80' if dd_pct < 10 else ('#fbbf24' if dd_pct < 30 else '#f87171')
        dd_chart += f"""
<div style="display:flex;align-items:center;gap:12px;margin:6px 0;font-size:14px">
  <div style="min-width:60px">${target}</div>
  <div style="flex:1;height:24px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:{c}">${r['max_dd_worst']:,.0f} ({dd_pct:.0f}%)</div>
</div>"""

    # PNL チャート
    pnl_chart = ""
    max_pnl_abs = max(abs(r['total_pnl']) for r in results.values())
    for target in TARGETS:
        r = results[target]
        bar_w = abs(r['total_pnl']) / max_pnl_abs * 300 if max_pnl_abs > 0 else 0
        c = '#4ade80' if r['total_pnl'] >= 0 else '#f87171'
        pnl_chart += f"""
<div style="display:flex;align-items:center;gap:12px;margin:6px 0;font-size:14px">
  <div style="min-width:60px">${target}</div>
  <div style="flex:1;height:24px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:{c}">${r['total_pnl']:+,.0f}</div>
</div>"""

    best = max(results.values(), key=lambda x: x['efficiency'])

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>R. 利確ターゲット最適化</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1500px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 22px; }}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block; margin-right: 12px; padding: 8px 16px;
  background: #1a2332; color: #6dd5ed; text-decoration: none;
  border-radius: 4px; border: 1px solid #2a3441; font-size: 13px;
}}
.nav a:hover {{ border-color: #c084fc; }}
.banner {{
  background: #1a2a1a; border-left: 5px solid #4ade80;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: #4ade80; }}
.summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px; margin: 20px 0;
}}
.card {{
  background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.green .value {{ color: #4ade80; }}

table {{
  width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0;
}}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
td.tname {{ font-weight: bold; color: #ffd700; font-size: 12px; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.best {{
  background: #1a3a1a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 20px; margin: 20px 0;
}}
.best h3 {{ color: #4ade80; margin: 0 0 12px 0; }}
.best .param {{ font-size: 18px; color: #ffd700; margin: 4px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>R. 利確ターゲット最適化</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="counter_tereko_backtest.html">N. テレコ逆張り</a>
<a href="equity_per_table_counter.html">Q. 破綻テスト</a>
</div>

<div class="banner">
<strong>📊 利確額を$20〜$100で変えた場合の DD・利益・資金効率を比較。</strong><br>
テレコ混合 × 逆張り × 〇✖ MaruBatsu / 62テーブル独立 / ${START_CAPITAL:,}元本 / 損切なし。<br>
<strong>資金効率 = 通算利益 ÷ 最大DD</strong> (高いほど少ないリスクで大きな利益)。<br>
データ: {DATE_FROM}〜本日 / {total_shoes:,}シュー
</div>

<div class="best">
<h3>🏆 最適解 (資金効率 最大)</h3>
<div class="param">利確ターゲット: <strong>${best['target']}</strong></div>
<div class="param">資金効率: <strong>{best['efficiency']:.2f}</strong> (利益÷DD)</div>
<div class="param">通算PNL: <strong style="color:#4ade80">${best['total_pnl']:+,.0f}</strong> / 最大DD: <strong style="color:#fbbf24">${best['max_dd_worst']:,.0f}</strong></div>
<div class="param">完走: {best['total_sessions']:,}セッション / 破綻: {best['bankrupt']}テーブル / ROI: {best['roi']:+.1f}%</div>
</div>

<h2>1. 全利確額 比較表</h2>
<p class="note">資金効率 (利益÷DD) が高い行が「最もリスクに対して効率的に利益を出す」設定。</p>
<table>
<thead><tr>
  <th>利確額</th><th>破綻</th><th>黒字</th><th>完走</th><th>BET数</th><th>勝率</th>
  <th>通算PNL</th><th>ROI</th>
  <th>最大DD</th><th>平均DD</th><th>中央DD</th>
  <th>資金効率</th><th>$/BET</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

<h2>2. 資金効率 (利益÷DD)</h2>
<p class="note">バーが長いほど「少ないリスクで大きな利益」。</p>
{eff_chart}

<h2>3. 最大DD (ワーストケース)</h2>
<p class="note">全テーブル中の最も深いドローダウン。短いほど安全。</p>
{dd_chart}

<h2>4. 通算PNL</h2>
{pnl_chart}

<h2>5. テーブル別 DD 詳細</h2>
<p class="note">各利確額のDD上位15テーブル。クリックで展開。</p>
{dd_detail_html}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_profit_target_optimization.py</code> /
  テレコ混合 × 逆張り × 〇✖ / ${START_CAPITAL:,}元本 / 損切なし
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "profit_target_optimization.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")
    results = run_backtest(shoes)

    print(f"\n{'='*100}")
    print(f"{'Target':>8} {'破綻':>4} {'黒字':>4} {'完走':>6} {'BET':>10} {'勝率':>7} {'通算PNL':>12} {'MaxDD':>10} {'AvgDD':>10} {'効率':>8} {'ROI':>7}")
    print(f"{'='*100}")
    for target in TARGETS:
        r = results[target]
        print(f"${target:>6} {r['bankrupt']:>4} {r['profit']:>4} {r['total_sessions']:>6,} {r['total_bets']:>10,} {r['avg_hr']:>6.2f}% ${r['total_pnl']:>+10,.0f} ${r['max_dd_worst']:>9,.0f} ${r['avg_dd']:>9,.0f} {r['efficiency']:>7.2f} {r['roi']:>+6.1f}%")

    best = max(results.values(), key=lambda x: x['efficiency'])
    print(f"\n🏆 最適: ${best['target']}利確 (効率={best['efficiency']:.2f}, PNL=${best['total_pnl']:+,.0f}, DD=${best['max_dd_worst']:,.0f})")

    render_html(results, len(shoes))


if __name__ == "__main__":
    main()
