"""動的パターン再評価 vs 固定判定 バックテスト

3つの戦略を比較:
  1. Baseline: 常に Player BET (無戦略)
  2. Static:   シュー序盤にパターン判定、テレコ混合なら Strategy A 固定
  3. Dynamic:  直近40ハンドを20ハンドごとに再判定、テレコ混合区間のみ Strategy A

全17,240シューで flat $1 bet のヒット率 + MaruBatsu session PNL を両方計算。

Usage:
  python generate_dynamic_backtest.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter
from pattern_classifier import classify_pattern
# strategy_router の decide_bet_strategy_a は毎回 O(n) でseqを走査するため重い
# StrategyAState クラスで O(1) インクリメンタル追跡に置き換え

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50

# 動的再評価パラメータ
WINDOW_SIZE = 40       # 直近何ハンドでパターン判定
REEVAL_INTERVAL = 20   # 何ハンドごとに再評価
STATIC_WARMUP = 30     # 固定判定: 最初の何ハンドで判定


SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]

PROFIT_TARGET = 50


class MaruBatsuSim:
    def __init__(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0
        self.max_dd = 0
        self.sessions_won = 0
        self.sessions_lost = 0
        self.total_profit = 0.0
        self.hands_bet = 0

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
        wins = self.turns.count('O')
        diff = wins - (7 - wins)
        unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        self.cumulative += unit * diff
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

    def add_bet(self, won: bool):
        self.hands_bet += 1
        self.turns.append('O' if won else 'X')
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= PROFIT_TARGET:
            self.total_profit += self.cumulative
            self.sessions_won += 1
            # reset session
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
            self.peak = 0


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


class StrategyAState:
    """Strategy A (A_b2_obs3) のインクリメンタル状態追跡"""
    def __init__(self):
        self.last_nt = None
        self.consec_b = 0
        self.observing = False

    def update(self, ch):
        """P/B を feed して状態更新"""
        if ch == 'B':
            self.consec_b += 1
            self.last_nt = 'B'
            if self.consec_b >= 3:
                self.observing = True
        elif ch == 'P':
            self.consec_b = 0
            self.last_nt = 'P'
            if self.observing:
                self.observing = False

    def should_bet(self) -> bool:
        """次の手で BET P すべきか (A_b2_obs3)"""
        if self.observing:
            return False
        if self.last_nt == 'B' and self.consec_b >= 2:
            return True
        return False


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
    print(f"Loaded {len(shoes)} shoes from {DATE_FROM}")

    # ─── 3戦略それぞれの統計 ───
    stats = {
        'baseline': {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0},
        'static':   {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                     'shoes_active': 0, 'shoes_skip': 0},
        'dynamic':  {'bets': 0, 'wins': 0, 'losses': 0, 'skips': 0,
                     'active_segments': 0, 'skip_segments': 0},
    }

    sim_baseline = MaruBatsuSim()
    sim_static = MaruBatsuSim()
    sim_dynamic = MaruBatsuSim()

    # 区間別ヒット率追跡 (dynamic)
    dynamic_pattern_at_bet = Counter()

    # シュー別PNL追跡
    shoe_results = []

    for si, (table_name, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  Processing shoe {si}/{len(shoes)}...")
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue

        # ─── Static: 序盤30ハンドで判定 ───
        warmup_seq = clean[:STATIC_WARMUP]
        static_pattern = classify_pattern(warmup_seq, min_cols=3)
        static_active = (static_pattern == "テレコ+ニコ混合")
        if static_active:
            stats['static']['shoes_active'] += 1
        else:
            stats['static']['shoes_skip'] += 1

        # ─── Strategy A 状態 (インクリメンタル) ───
        sa_static = StrategyAState()
        sa_dynamic = StrategyAState()

        # ─── Dynamic: 状態 ───
        dynamic_active = False
        last_eval_pos = 0
        current_dynamic_pattern = "不明"

        shoe_flat = {'baseline': 0, 'static': 0, 'dynamic': 0}

        for i, ch in enumerate(clean):
            result = ch  # P or B

            # === Dynamic 再評価 ===
            if i >= WINDOW_SIZE and (i - last_eval_pos) >= REEVAL_INTERVAL:
                window = clean[i - WINDOW_SIZE:i]
                current_dynamic_pattern = classify_pattern(window, min_cols=3)
                dynamic_active = (current_dynamic_pattern == "テレコ+ニコ混合")
                last_eval_pos = i
            elif i == WINDOW_SIZE:
                window = clean[:WINDOW_SIZE]
                current_dynamic_pattern = classify_pattern(window, min_cols=3)
                dynamic_active = (current_dynamic_pattern == "テレコ+ニコ混合")
                last_eval_pos = i

            # === Baseline: 常に P BET ===
            won = (result == 'P')
            stats['baseline']['bets'] += 1
            if won:
                stats['baseline']['wins'] += 1
                shoe_flat['baseline'] += 1
            else:
                stats['baseline']['losses'] += 1
                shoe_flat['baseline'] -= 1
            sim_baseline.add_bet(won)

            # === Static: BET判定してから状態更新 ===
            if static_active and sa_static.should_bet():
                won_p = (result == 'P')
                stats['static']['bets'] += 1
                if won_p:
                    stats['static']['wins'] += 1
                    shoe_flat['static'] += 1
                else:
                    stats['static']['losses'] += 1
                    shoe_flat['static'] -= 1
                sim_static.add_bet(won_p)
            else:
                stats['static']['skips'] += 1
            sa_static.update(ch)

            # === Dynamic: BET判定してから状態更新 ===
            if dynamic_active and sa_dynamic.should_bet():
                won_p = (result == 'P')
                stats['dynamic']['bets'] += 1
                if won_p:
                    stats['dynamic']['wins'] += 1
                    shoe_flat['dynamic'] += 1
                else:
                    stats['dynamic']['losses'] += 1
                    shoe_flat['dynamic'] -= 1
                sim_dynamic.add_bet(won_p)
                dynamic_pattern_at_bet[current_dynamic_pattern] += 1
            else:
                stats['dynamic']['skips'] += 1
            sa_dynamic.update(ch)

        shoe_results.append({
            'table': table_name,
            'started_at': started_at,
            'hands': len(clean),
            'static_pattern': static_pattern,
            'baseline_pnl': shoe_flat['baseline'],
            'static_pnl': shoe_flat['static'],
            'dynamic_pnl': shoe_flat['dynamic'],
        })

    return stats, sim_baseline, sim_static, sim_dynamic, shoe_results, dynamic_pattern_at_bet


def render_html(stats, sim_b, sim_s, sim_d, shoe_results, dyn_pat):
    total_shoes = len(shoe_results)

    def hit_rate(s):
        total = s['bets']
        return s['wins'] / total * 100 if total > 0 else 0

    def flat_pnl(s):
        return s['wins'] - s['losses']

    # ─── 比較表 ───
    comparison_rows = ""
    for label, s, sim, color in [
        ("Baseline (常にP)", stats['baseline'], sim_b, "#8a96a8"),
        ("Static (固定判定)", stats['static'], sim_s, "#fbbf24"),
        ("Dynamic (動的再評価)", stats['dynamic'], sim_d, "#4ade80"),
    ]:
        hr = hit_rate(s)
        fpnl = flat_pnl(s)
        fpnl_color = '#4ade80' if fpnl >= 0 else '#f87171'
        mb_pnl = sim.total_profit + sim.cumulative
        mb_color = '#4ade80' if mb_pnl >= 0 else '#f87171'
        comparison_rows += (
            f"<tr>"
            f"<td style='color:{color};font-weight:bold'>{label}</td>"
            f"<td>{s['bets']:,}</td>"
            f"<td>{s['wins']:,}</td>"
            f"<td>{s['losses']:,}</td>"
            f"<td>{s['skips']:,}</td>"
            f"<td style='font-weight:bold'>{hr:.2f}%</td>"
            f"<td style='color:{fpnl_color};font-weight:bold'>{fpnl:+,}</td>"
            f"<td>{sim.sessions_won}</td>"
            f"<td style='color:{mb_color};font-weight:bold'>${mb_pnl:+,.0f}</td>"
            f"<td>${sim.max_dd:,.0f}</td>"
            f"</tr>"
        )

    # ─── Dynamic の区間パターン分布 ───
    dyn_pat_html = ""
    for p, c in dyn_pat.most_common():
        dyn_pat_html += f"<tr><td>{p}</td><td>{c:,}</td></tr>"

    # ─── シュー別 PNL 分布 (Top/Bottom) ───
    shoe_results_sorted = sorted(shoe_results, key=lambda x: -x['dynamic_pnl'])
    top_shoes_html = ""
    for r in shoe_results_sorted[:30]:
        ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
        d_color = '#4ade80' if r['dynamic_pnl'] >= 0 else '#f87171'
        s_color = '#4ade80' if r['static_pnl'] >= 0 else '#f87171'
        b_color = '#4ade80' if r['baseline_pnl'] >= 0 else '#f87171'
        top_shoes_html += (
            f"<tr>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tname'>{r['table']}</td>"
            f"<td>{r['static_pattern']}</td>"
            f"<td style='color:{b_color}'>{r['baseline_pnl']:+d}</td>"
            f"<td style='color:{s_color}'>{r['static_pnl']:+d}</td>"
            f"<td style='color:{d_color};font-weight:bold'>{r['dynamic_pnl']:+d}</td>"
            f"<td>{r['hands']}h</td>"
            f"</tr>"
        )

    worst_shoes_html = ""
    for r in shoe_results_sorted[-30:]:
        ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
        d_color = '#4ade80' if r['dynamic_pnl'] >= 0 else '#f87171'
        s_color = '#4ade80' if r['static_pnl'] >= 0 else '#f87171'
        b_color = '#4ade80' if r['baseline_pnl'] >= 0 else '#f87171'
        worst_shoes_html += (
            f"<tr>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tname'>{r['table']}</td>"
            f"<td>{r['static_pattern']}</td>"
            f"<td style='color:{b_color}'>{r['baseline_pnl']:+d}</td>"
            f"<td style='color:{s_color}'>{r['static_pnl']:+d}</td>"
            f"<td style='color:{d_color};font-weight:bold'>{r['dynamic_pnl']:+d}</td>"
            f"<td>{r['hands']}h</td>"
            f"</tr>"
        )

    # ─── テーブル別 累計PNL ───
    table_pnl = defaultdict(lambda: {'baseline': 0, 'static': 0, 'dynamic': 0, 'shoes': 0})
    for r in shoe_results:
        t = table_pnl[r['table']]
        t['baseline'] += r['baseline_pnl']
        t['static'] += r['static_pnl']
        t['dynamic'] += r['dynamic_pnl']
        t['shoes'] += 1

    table_pnl_sorted = sorted(table_pnl.items(), key=lambda x: -x[1]['dynamic'])
    table_pnl_html = ""
    for tn, t in table_pnl_sorted:
        d_color = '#4ade80' if t['dynamic'] >= 0 else '#f87171'
        s_color = '#4ade80' if t['static'] >= 0 else '#f87171'
        b_color = '#4ade80' if t['baseline'] >= 0 else '#f87171'
        diff = t['dynamic'] - t['static']
        diff_color = '#4ade80' if diff > 0 else ('#f87171' if diff < 0 else '#8a96a8')
        table_pnl_html += (
            f"<tr>"
            f"<td class='tname'>{tn}</td>"
            f"<td>{t['shoes']}</td>"
            f"<td style='color:{b_color}'>{t['baseline']:+d}</td>"
            f"<td style='color:{s_color}'>{t['static']:+d}</td>"
            f"<td style='color:{d_color};font-weight:bold'>{t['dynamic']:+d}</td>"
            f"<td style='color:{diff_color}'>{diff:+d}</td>"
            f"</tr>"
        )

    # ─── 勝率改善幅 ───
    hr_baseline = hit_rate(stats['baseline'])
    hr_static = hit_rate(stats['static'])
    hr_dynamic = hit_rate(stats['dynamic'])
    improvement = hr_dynamic - hr_static

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>M. 動的パターン再評価 バックテスト</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 22px; }}
h3 {{ color: #6dd5ed; margin-top: 24px; }}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block; margin-right: 12px; padding: 8px 16px;
  background: #1a2332; color: #6dd5ed; text-decoration: none;
  border-radius: 4px; border: 1px solid #2a3441; font-size: 13px;
}}
.nav a:hover {{ border-color: #c084fc; }}
.banner {{
  background: #2a1a3a; border-left: 5px solid #c084fc;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: #c084fc; }}
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
.card.yellow .value {{ color: #fbbf24; }}

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
.highlight {{ background: #11192a; border: 2px solid #4ade80; border-radius: 8px; padding: 16px; margin: 16px 0; }}
.highlight h3 {{ margin-top: 0; }}
</style>
</head>
<body>
<div class="container">
<h1>M. 動的パターン再評価 バックテスト</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="pattern_shift.html">L. パターン変化分析</a>
</div>

<div class="banner">
<strong>📊 3戦略を同一データで比較バックテスト。</strong><br>
<strong>1. Baseline</strong>: 全ハンド常に Player BET (無戦略、ヒット率の理論下限)。<br>
<strong>2. Static (現行)</strong>: シュー序盤{STATIC_WARMUP}ハンドでパターン判定 → テレコ+ニコ混合なら Strategy A 固定、それ以外は全SKIP。<br>
<strong>3. Dynamic (提案)</strong>: 直近{WINDOW_SIZE}ハンドを{REEVAL_INTERVAL}ハンドごとに再評価 → テレコ+ニコ混合区間のみ Strategy A。<br>
データ: {DATE_FROM}〜本日 / {total_shoes:,}シュー / flat $1 BET + MaruBatsu $50利確
</div>

<div class="summary">
  <div class="card">
    <div class="label">分析シュー数</div>
    <div class="value">{total_shoes:,}</div>
  </div>
  <div class="card {'green' if improvement > 0 else 'red'}">
    <div class="label">Dynamic 勝率改善</div>
    <div class="value">{improvement:+.2f}%</div>
  </div>
  <div class="card {'green' if flat_pnl(stats['dynamic']) > flat_pnl(stats['static']) else 'red'}">
    <div class="label">Dynamic flat PNL</div>
    <div class="value">{flat_pnl(stats['dynamic']):+,}</div>
  </div>
  <div class="card">
    <div class="label">Dynamic BET数</div>
    <div class="value">{stats['dynamic']['bets']:,}</div>
  </div>
</div>

<h2>1. 3戦略 比較サマリー</h2>
<table>
<thead><tr>
  <th>戦略</th>
  <th>BET数</th>
  <th>勝ち</th>
  <th>負け</th>
  <th>SKIP</th>
  <th>勝率</th>
  <th>Flat PNL ($1)</th>
  <th>MB完走</th>
  <th>MB累計PNL</th>
  <th>MB MaxDD</th>
</tr></thead>
<tbody>{comparison_rows}</tbody>
</table>

<div class="highlight">
<h3>📌 読み方ガイド</h3>
<p class="note">
<strong>勝率</strong>: BETしたハンドの的中率。Baseline(常にP BET) の勝率がバカラの理論値に近い。<br>
<strong>Flat PNL</strong>: $1均一BETの場合の損益。ヒット率の純粋な差が見える。<br>
<strong>MB完走</strong>: MaruBatsu セッションが $50 利確で完了した回数。多い = 安定的にセッションが回っている。<br>
<strong>MB累計PNL</strong>: MaruBatsu (SEQ progression + $50利確) の累計損益。実運用の収益イメージ。<br>
<strong>MB MaxDD</strong>: MaruBatsu の最大セッション内ドローダウン。大きい = 破綻リスクが高い。
</p>
</div>

<h2>2. Dynamic BET時のパターン分布</h2>
<p class="note">Dynamic がBETした時点で、直近{WINDOW_SIZE}ハンドがどのパターンだったか。テレコ+ニコ混合 = 100% のはず。</p>
<table style="max-width:400px">
<thead><tr><th>パターン</th><th>BET数</th></tr></thead>
<tbody>{dyn_pat_html}</tbody>
</table>

<h2>3. テーブル別 累計 Flat PNL</h2>
<p class="note">Dynamic PNLの降順。Static との差分 (Dynamic - Static) がプラスなら動的再評価が優位。</p>
<table>
<thead><tr>
  <th>テーブル</th>
  <th>シュー数</th>
  <th>Baseline</th>
  <th>Static</th>
  <th>Dynamic</th>
  <th>差分 (D-S)</th>
</tr></thead>
<tbody>{table_pnl_html}</tbody>
</table>

<h2>4. ベストシュー Top 30 (Dynamic PNL)</h2>
<table>
<thead><tr>
  <th>日時</th><th>テーブル</th><th>Static判定</th>
  <th>Baseline</th><th>Static</th><th>Dynamic</th><th>ハンド</th>
</tr></thead>
<tbody>{top_shoes_html}</tbody>
</table>

<h2>5. ワーストシュー Bottom 30 (Dynamic PNL)</h2>
<table>
<thead><tr>
  <th>日時</th><th>テーブル</th><th>Static判定</th>
  <th>Baseline</th><th>Static</th><th>Dynamic</th><th>ハンド</th>
</tr></thead>
<tbody>{worst_shoes_html}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_dynamic_backtest.py</code> /
  Window={WINDOW_SIZE}h, Reeval={REEVAL_INTERVAL}h, Warmup={STATIC_WARMUP}h /
  Strategy A (A_b2_obs3)
</p>

</div>
</body>
</html>
"""
    out_path = os.path.join("report", "dynamic_backtest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


def main():
    stats, sim_b, sim_s, sim_d, shoe_results, dyn_pat = run_backtest()

    # コンソールサマリー
    print(f"\n{'='*60}")
    print(f"{'戦略':<25} {'BET':>8} {'WIN':>8} {'LOSS':>8} {'勝率':>8} {'FlatPNL':>10} {'MB_PNL':>10}")
    print(f"{'='*60}")
    for label, s, sim in [
        ("Baseline (常にP)", stats['baseline'], sim_b),
        ("Static (固定判定)", stats['static'], sim_s),
        ("Dynamic (動的再評価)", stats['dynamic'], sim_d),
    ]:
        hr = s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0
        fpnl = s['wins'] - s['losses']
        mb_pnl = sim.total_profit + sim.cumulative
        print(f"{label:<25} {s['bets']:>8,} {s['wins']:>8,} {s['losses']:>8,} {hr:>7.2f}% {fpnl:>+10,} ${mb_pnl:>+10,.0f}")

    print(f"\nStatic active shoes: {stats['static']['shoes_active']} / skip: {stats['static']['shoes_skip']}")
    print(f"Dynamic BET pattern: {dict(dyn_pat.most_common(5))}")

    render_html(stats, sim_b, sim_s, sim_d, shoe_results, dyn_pat)


if __name__ == "__main__":
    main()
