"""テレコ系 × 逆張り バックテスト

戦略:
  - シュー序盤でパターン判定
  - テレコ+ニコ混合 (純粋テレコ含む) のみ参加
  - 逆張り: 前手Pならバンカー BET、前手BならプレイヤーBET
  - 資金管理: 〇✖ MaruBatsu ($50利確)
  - Banker BET時: unit × 1.0526 (手数料5%相殺)

比較対象:
  1. 現行 Strategy A (BB後にP狙い) on テレコ混合
  2. 逆張り on テレコ混合
  3. 逆張り on テレコ混合 (Banker増額あり)
  4. 逆張り on 純粋テレコのみ (1段列90%以上)

Usage:
  python generate_counter_tereko_backtest.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter
from pattern_classifier import classify_pattern, compute_big_road_columns

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
STATIC_WARMUP = 30
PROFIT_TARGET = 50
BANKER_COMMISSION = 0.05
BANKER_UNIT_MULT = 1.0 / (1.0 - BANKER_COMMISSION)  # ≈ 1.0526

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


def classify_tereko_detail(seq: str, min_cols=5) -> str:
    """テレコを細分化して分類
    Returns: '純粋テレコ' / 'テレコ+ニコ混合' / (その他のpattern名)
    """
    base = classify_pattern(seq, min_cols=min_cols)
    if base != "テレコ+ニコ混合":
        return base
    cols = compute_big_road_columns(seq)
    col_lens = [len(c) for c in cols]
    if not col_lens:
        return base
    pct1 = sum(1 for L in col_lens if L == 1) / len(col_lens)
    if pct1 >= 0.90:
        return "純粋テレコ"
    return "テレコ+ニコ混合"


# ─── MaruBatsu Sim (Banker増額対応) ───
class MaruBatsuSim:
    def __init__(self, compensate_banker=False):
        self.compensate_banker = compensate_banker
        self.reset_all()

    def reset_all(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0
        self.max_dd = 0
        self.sessions_won = 0
        self.total_profit = 0.0
        self.hands_bet = 0
        self.total_wagered = 0.0

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
            if bet_side == 'B' and self.compensate_banker:
                actual = base_unit * BANKER_UNIT_MULT
            else:
                actual = base_unit
            self.total_wagered += actual
            if outcome == 'O':
                if bet_side == 'B':
                    money += actual * (1.0 - BANKER_COMMISSION)
                else:
                    money += actual
            else:
                money -= actual

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

    def add_bet(self, won: bool, bet_side: str):
        self.hands_bet += 1
        self.turns.append(('O' if won else 'X', bet_side))
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= PROFIT_TARGET:
            self.total_profit += self.cumulative
            self.sessions_won += 1
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
            self.peak = 0


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


def run_backtest():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")

    strategies = {
        'strategy_a':     {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                           'shoes_active': 0, 'desc': '現行 Strategy A (BB→P)'},
        'counter':        {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                           'shoes_active': 0, 'desc': '逆張り (テレコ混合)'},
        'counter_comp':   {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                           'shoes_active': 0, 'desc': '逆張り + Banker増額'},
        'counter_pure':   {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                           'shoes_active': 0, 'desc': '逆張り (純粋テレコのみ)'},
    }

    sim_a = MaruBatsuSim(compensate_banker=False)
    sim_counter = MaruBatsuSim(compensate_banker=False)
    sim_counter_comp = MaruBatsuSim(compensate_banker=True)
    sim_counter_pure = MaruBatsuSim(compensate_banker=True)

    # テーブル別追跡
    table_pnl = defaultdict(lambda: {k: 0 for k in strategies})
    table_shoes = defaultdict(int)

    # パターン分布
    pattern_dist = Counter()
    tereko_detail_dist = Counter()

    shoe_results = []

    for si, (table_name, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si}/{len(shoes)}...")
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue

        warmup = clean[:STATIC_WARMUP]
        base_pattern = classify_pattern(warmup, min_cols=3)
        detail_pattern = classify_tereko_detail(warmup, min_cols=3)

        pattern_dist[base_pattern] += 1
        tereko_detail_dist[detail_pattern] += 1
        table_shoes[table_name] += 1

        is_tereko_mix = (base_pattern == "テレコ+ニコ混合")
        is_pure_tereko = (detail_pattern == "純粋テレコ")

        # Strategy A state
        sa_last_nt = None
        sa_consec_b = 0
        sa_observing = False

        last_nt = None  # 逆張り用

        shoe_flat = {k: 0 for k in strategies}

        for i, ch in enumerate(clean):
            result = ch

            # === Strategy A (テレコ混合のみ) ===
            if is_tereko_mix:
                should_bet_a = (not sa_observing and sa_last_nt == 'B' and sa_consec_b >= 2)
                if should_bet_a:
                    won = (result == 'P')
                    strategies['strategy_a']['bets'] += 1
                    if won:
                        strategies['strategy_a']['wins'] += 1
                        shoe_flat['strategy_a'] += 1
                    else:
                        strategies['strategy_a']['losses'] += 1
                        shoe_flat['strategy_a'] -= 1
                    sim_a.add_bet(won, 'P')
                # update state
                if ch == 'B':
                    sa_consec_b += 1
                    sa_last_nt = 'B'
                    if sa_consec_b >= 3:
                        sa_observing = True
                elif ch == 'P':
                    sa_consec_b = 0
                    sa_last_nt = 'P'
                    if sa_observing:
                        sa_observing = False

            # === 逆張り (テレコ混合) ===
            if is_tereko_mix and last_nt is not None:
                bet_side = 'P' if last_nt == 'B' else 'B'
                won = (result == bet_side)

                # 通常版
                strategies['counter']['bets'] += 1
                if won:
                    strategies['counter']['wins'] += 1
                    shoe_flat['counter'] += 1
                else:
                    strategies['counter']['losses'] += 1
                    shoe_flat['counter'] -= 1
                sim_counter.add_bet(won, bet_side)

                # Banker増額版
                strategies['counter_comp']['bets'] += 1
                if won:
                    strategies['counter_comp']['wins'] += 1
                    shoe_flat['counter_comp'] += 1
                else:
                    strategies['counter_comp']['losses'] += 1
                    shoe_flat['counter_comp'] -= 1
                sim_counter_comp.add_bet(won, bet_side)

            # === 逆張り (純粋テレコのみ) ===
            if is_pure_tereko and last_nt is not None:
                bet_side = 'P' if last_nt == 'B' else 'B'
                won = (result == bet_side)
                strategies['counter_pure']['bets'] += 1
                if won:
                    strategies['counter_pure']['wins'] += 1
                    shoe_flat['counter_pure'] += 1
                else:
                    strategies['counter_pure']['losses'] += 1
                    shoe_flat['counter_pure'] -= 1
                sim_counter_pure.add_bet(won, bet_side)

            # 逆張り用: last_nt 更新
            if ch in ('P', 'B'):
                last_nt = ch

        # active shoes count
        if is_tereko_mix:
            strategies['strategy_a']['shoes_active'] += 1
            strategies['counter']['shoes_active'] += 1
            strategies['counter_comp']['shoes_active'] += 1
        if is_pure_tereko:
            strategies['counter_pure']['shoes_active'] += 1

        for k in strategies:
            table_pnl[table_name][k] += shoe_flat[k]

        shoe_results.append({
            'table': table_name, 'started_at': started_at,
            'pattern': detail_pattern, 'hands': len(clean),
            **{f'{k}_pnl': shoe_flat[k] for k in strategies},
        })

    sims = {
        'strategy_a': sim_a, 'counter': sim_counter,
        'counter_comp': sim_counter_comp, 'counter_pure': sim_counter_pure,
    }
    return strategies, sims, shoe_results, table_pnl, table_shoes, pattern_dist, tereko_detail_dist


def render_html(strategies, sims, shoe_results, table_pnl, table_shoes,
                pattern_dist, tereko_detail_dist):
    total_shoes = len(shoe_results)

    # ─── パターン分布 ───
    pat_html = ""
    for p, c in tereko_detail_dist.most_common():
        pct = c / total_shoes * 100
        bar_w = pct * 4
        color = '#4ade80' if 'テレコ' in p else '#8a96a8'
        pat_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:180px">{p}</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{color};border-radius:4px"></div>
  </div>
  <div style="min-width:100px;text-align:right;color:#8a96a8">{c:,} ({pct:.1f}%)</div>
</div>"""

    # ─── メイン比較表 ───
    comp_rows = ""
    order = ['strategy_a', 'counter', 'counter_comp', 'counter_pure']
    colors = {'strategy_a': '#fbbf24', 'counter': '#6dd5ed',
              'counter_comp': '#4ade80', 'counter_pure': '#c084fc'}
    for key in order:
        s = strategies[key]
        sim = sims[key]
        hr = s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0
        fpnl = s['wins'] - s['losses']
        fpnl_color = '#4ade80' if fpnl >= 0 else '#f87171'
        mb_pnl = sim.total_profit + sim.cumulative
        mb_color = '#4ade80' if mb_pnl >= 0 else '#f87171'
        comp_rows += (
            f"<tr>"
            f"<td style='color:{colors[key]};font-weight:bold'>{s['desc']}</td>"
            f"<td>{s['shoes_active']:,}</td>"
            f"<td>{s['bets']:,}</td>"
            f"<td>{s['wins']:,}</td>"
            f"<td>{s['losses']:,}</td>"
            f"<td style='font-weight:bold'>{hr:.2f}%</td>"
            f"<td style='color:{fpnl_color};font-weight:bold'>{fpnl:+,}</td>"
            f"<td>{sim.sessions_won}</td>"
            f"<td style='color:{mb_color};font-weight:bold'>${mb_pnl:+,.0f}</td>"
            f"<td>${sim.max_dd:,.0f}</td>"
            f"</tr>"
        )

    # ─── テーブル別 ───
    table_list = sorted(table_pnl.items(), key=lambda x: -x[1]['counter_comp'])
    table_html = ""
    for tn, pnls in table_list:
        cells = ""
        for k in order:
            v = pnls[k]
            c = '#4ade80' if v > 0 else ('#f87171' if v < 0 else '#555')
            cells += f"<td style='color:{c}'>{v:+d}</td>"
        table_html += f"<tr><td class='tname'>{tn}</td><td>{table_shoes[tn]}</td>{cells}</tr>"

    # ─── ベスト/ワーストシュー ───
    sorted_shoes = sorted(shoe_results, key=lambda x: -x['counter_comp_pnl'])
    def shoe_rows(items):
        html = ""
        for r in items:
            ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
            cells = ""
            for k in order:
                v = r[f'{k}_pnl']
                c = '#4ade80' if v > 0 else ('#f87171' if v < 0 else '#555')
                cells += f"<td style='color:{c}'>{v:+d}</td>"
            html += f"<tr><td class='ts'>{ts}</td><td class='tname'>{r['table']}</td><td>{r['pattern']}</td>{cells}<td>{r['hands']}h</td></tr>"
        return html

    top_shoes = shoe_rows(sorted_shoes[:20])
    worst_shoes = shoe_rows(sorted_shoes[-20:])

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>N. テレコ × 逆張り バックテスト</title>
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
.card.red .value {{ color: #f87171; }}

table {{
  width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0;
}}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
td.tname {{ font-weight: bold; color: #ffd700; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.highlight {{
  background: #11192a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 16px; margin: 16px 0;
}}
.highlight h3 {{ margin-top: 0; color: #4ade80; }}
</style>
</head>
<body>
<div class="container">
<h1>N. テレコ × 逆張り バックテスト</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="dynamic_backtest.html">M. 動的再評価</a>
</div>

<div class="banner">
<strong>📊 テレコ系パターン × 逆張り戦略のバックテスト。</strong><br>
<strong>コンセプト:</strong> テレコ = 交互に出やすい → 前手の逆にBET (逆張り)。<br>
<strong>資金管理:</strong> 〇✖ MaruBatsu ($50利確) / Banker BET時は unit×1.0526 で手数料相殺。<br>
<strong>比較:</strong> 現行 Strategy A vs 逆張り vs 逆張り+Banker増額 vs 純粋テレコのみ<br>
データ: {DATE_FROM}〜本日 / {total_shoes:,}シュー
</div>

<h2>1. パターン分布 (序盤{STATIC_WARMUP}ハンド判定)</h2>
{pat_html}

<h2>2. 4戦略 比較サマリー</h2>
<table>
<thead><tr>
  <th>戦略</th><th>参加シュー</th><th>BET数</th><th>勝ち</th><th>負け</th>
  <th>勝率</th><th>Flat PNL</th><th>MB完走</th><th>MB累計PNL</th><th>MB MaxDD</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

<div class="highlight">
<h3>📌 戦略の違い</h3>
<p class="note">
<strong style="color:#fbbf24">Strategy A</strong>: テレコ混合シューで BB→P 狙い (現行)。BETは全てP。選択的にBET。<br>
<strong style="color:#6dd5ed">逆張り</strong>: テレコ混合シューで毎ハンド逆張り。前手Pなら→B BET、前手Bなら→P BET。<br>
<strong style="color:#4ade80">逆張り+増額</strong>: 逆張り + Banker BET時にunit×1.0526 で手数料5%を打ち消す。<br>
<strong style="color:#c084fc">純粋テレコのみ</strong>: 1段列90%以上の「ガチテレコ」シューだけに参加する逆張り+増額。
</p>
</div>

<h2>3. テーブル別 Flat PNL</h2>
<p class="note">逆張り+増額PNL の降順。</p>
<table>
<thead><tr>
  <th>テーブル</th><th>シュー</th>
  <th style="color:#fbbf24">Strategy A</th>
  <th style="color:#6dd5ed">逆張り</th>
  <th style="color:#4ade80">逆張り+増額</th>
  <th style="color:#c084fc">純粋テレコ</th>
</tr></thead>
<tbody>{table_html}</tbody>
</table>

<h2>4. ベストシュー Top 20 (逆張り+増額)</h2>
<table>
<thead><tr>
  <th>日時</th><th>テーブル</th><th>パターン</th>
  <th style="color:#fbbf24">A</th><th style="color:#6dd5ed">逆張り</th>
  <th style="color:#4ade80">逆+増</th><th style="color:#c084fc">純テレコ</th>
  <th>ハンド</th>
</tr></thead>
<tbody>{top_shoes}</tbody>
</table>

<h2>5. ワーストシュー Bottom 20 (逆張り+増額)</h2>
<table>
<thead><tr>
  <th>日時</th><th>テーブル</th><th>パターン</th>
  <th style="color:#fbbf24">A</th><th style="color:#6dd5ed">逆張り</th>
  <th style="color:#4ade80">逆+増</th><th style="color:#c084fc">純テレコ</th>
  <th>ハンド</th>
</tr></thead>
<tbody>{worst_shoes}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_counter_tereko_backtest.py</code> / 逆張り + 〇✖ MaruBatsu / Banker unit×1.0526
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "counter_tereko_backtest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


def main():
    strategies, sims, shoe_results, table_pnl, table_shoes, pat_dist, tereko_dist = run_backtest()

    print(f"\n{'='*80}")
    print(f"{'戦略':<30} {'BET':>8} {'WIN':>8} {'LOSS':>8} {'勝率':>8} {'FlatPNL':>8} {'MB完走':>6} {'MB_PNL':>12} {'MaxDD':>10}")
    print(f"{'='*80}")
    order = ['strategy_a', 'counter', 'counter_comp', 'counter_pure']
    for k in order:
        s = strategies[k]
        sim = sims[k]
        hr = s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0
        fpnl = s['wins'] - s['losses']
        mb_pnl = sim.total_profit + sim.cumulative
        print(f"{s['desc']:<30} {s['bets']:>8,} {s['wins']:>8,} {s['losses']:>8,} {hr:>7.2f}% {fpnl:>+8,} {sim.sessions_won:>6} ${mb_pnl:>+11,.0f} ${sim.max_dd:>9,.0f}")

    print(f"\nパターン分布:")
    for p, c in tereko_dist.most_common():
        print(f"  {p}: {c:,} ({c/len(shoe_results)*100:.1f}%)")

    render_html(strategies, sims, shoe_results, table_pnl, table_shoes, pat_dist, tereko_dist)


if __name__ == "__main__":
    main()
