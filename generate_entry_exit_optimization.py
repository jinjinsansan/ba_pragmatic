"""入室条件・退室条件の最適化 バックテスト

全パラメータ組み合わせを総当たりで検証し、最適な入退室条件を発見する。

入室条件パラメータ:
  - window: 直近何列で判定 (6/8/10/12/15)
  - threshold: 1落ち+2落ちの割合 (70%/75%/80%/85%/90%)

退室条件パラメータ:
  - drop3_limit: 3落ち以上がN回で退室 (1/2/3/4/999=無効)
  - drop4_exit: 4落ち即退室するか (True/False)
  - drop5_exit: 5落ち即退室するか (True/False)
  - max_consec_loss: N連敗で退室 (3/5/7/999=無効)

さらに:
  - テレコ崩壊の前兆分析
  - テレコ持続時間の分布

Usage:
  python generate_entry_exit_optimization.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter
from itertools import product

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
PROFIT_TARGET = 50
BANKER_COMMISSION = 0.05
BANKER_UNIT_MULT = 1.0 / (1.0 - BANKER_COMMISSION)

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class MaruBatsuSim:
    def __init__(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0.0
        self.max_dd = 0.0
        self.sessions_won = 0
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
        base_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        money = 0.0
        for outcome, bet_side in self.turns:
            actual = base_unit * BANKER_UNIT_MULT if bet_side == 'B' else base_unit
            if outcome == 'O':
                money += actual * (1.0 - BANKER_COMMISSION) if bet_side == 'B' else actual
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

    def add_bet(self, won, bet_side):
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
            self.peak = 0.0


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


def run_single(shoes, entry_window, entry_threshold, drop3_limit, drop4_exit, drop5_exit):
    """1パラメータ組み合わせのバックテスト"""
    sim = MaruBatsuSim()
    stats = {'bets': 0, 'wins': 0, 'losses': 0, 'entries': 0, 'exits': 0}

    # テーブル状態
    table_cols = defaultdict(list)      # 列長リスト
    table_cur_col = defaultdict(int)    # 現在の列長
    table_last_side = {}                # 現在の列の側
    table_last_nt = {}                  # 直前の非タイ

    active_table = None
    entry_col_idx = 0

    for table_name, seq, started_at in shoes:
        for ch in seq:
            if ch == 'T':
                continue

            cols = table_cols[table_name]
            cur_col = table_cur_col[table_name]
            last_side = table_last_side.get(table_name)

            # 大路更新
            if ch == last_side:
                table_cur_col[table_name] += 1
            else:
                if last_side is not None:
                    cols.append(table_cur_col[table_name])
                table_cur_col[table_name] = 1
                table_last_side[table_name] = ch

            # 退室チェック
            if active_table == table_name:
                cols_since = cols[entry_col_idx:]
                cur_c = table_cur_col[table_name]
                check = list(cols_since)
                if cur_c >= 3:
                    check.append(cur_c)

                should_exit = False
                if drop4_exit and any(L >= 4 for L in check):
                    should_exit = True
                if drop5_exit and not drop4_exit and any(L >= 5 for L in check):
                    should_exit = True
                if cur_c >= 4 and drop4_exit:
                    should_exit = True
                if cur_c >= 5 and drop5_exit and not drop4_exit:
                    should_exit = True
                d3 = sum(1 for L in check if L >= 3)
                if d3 >= drop3_limit:
                    should_exit = True

                if should_exit:
                    active_table = None
                    stats['exits'] += 1

            # 入室チェック
            if active_table is None:
                if len(cols) >= entry_window:
                    recent = cols[-entry_window:]
                    short = sum(1 for L in recent if L <= 2)
                    if (short / len(recent)) >= entry_threshold:
                        active_table = table_name
                        entry_col_idx = len(cols)
                        stats['entries'] += 1

            # BET
            last_nt = table_last_nt.get(table_name)
            if active_table == table_name and last_nt is not None:
                bet_side = 'P' if last_nt == 'B' else 'B'
                won = (ch == bet_side)
                stats['bets'] += 1
                if won:
                    stats['wins'] += 1
                else:
                    stats['losses'] += 1
                sim.add_bet(won, bet_side)

            table_last_nt[table_name] = ch

        # シュー終了
        if table_cur_col[table_name] > 0 and table_last_side.get(table_name):
            table_cols[table_name].append(table_cur_col[table_name])
        table_cur_col[table_name] = 0
        table_last_side[table_name] = None

    mb_pnl = sim.total_profit + sim.cumulative
    flat_pnl = stats['wins'] - stats['losses']
    hr = stats['wins'] / stats['bets'] * 100 if stats['bets'] > 0 else 0
    return {
        'bets': stats['bets'], 'wins': stats['wins'], 'losses': stats['losses'],
        'entries': stats['entries'], 'exits': stats['exits'],
        'hr': hr, 'flat_pnl': flat_pnl,
        'mb_pnl': mb_pnl, 'mb_sessions': sim.sessions_won, 'mb_maxdd': sim.max_dd,
    }


def analyze_tereko_duration(shoes):
    """テレコ区間の持続時間 (何ハンド続くか) を分析"""
    durations = []
    breakdown_signals = Counter()

    table_cols = defaultdict(list)
    table_cur_col = defaultdict(int)
    table_last_side = {}

    WINDOW = 10
    THRESH = 0.80

    for table_name, seq, started_at in shoes:
        in_tereko = False
        tereko_start_hand = 0
        hand_count = 0

        for ch in seq:
            if ch == 'T':
                continue
            hand_count += 1
            cols = table_cols[table_name]
            if ch == table_last_side.get(table_name):
                table_cur_col[table_name] += 1
            else:
                if table_last_side.get(table_name) is not None:
                    cols.append(table_cur_col[table_name])
                table_cur_col[table_name] = 1
                table_last_side[table_name] = ch

            # テレコ判定
            if len(cols) >= WINDOW:
                recent = cols[-WINDOW:]
                short = sum(1 for L in recent if L <= 2)
                is_tereko = (short / len(recent)) >= THRESH

                if is_tereko and not in_tereko:
                    in_tereko = True
                    tereko_start_hand = hand_count
                elif not is_tereko and in_tereko:
                    duration = hand_count - tereko_start_hand
                    durations.append(duration)
                    # 崩壊直前の列長を記録
                    if cols:
                        breakdown_signals[cols[-1]] += 1
                    in_tereko = False

        if in_tereko:
            duration = hand_count - tereko_start_hand
            durations.append(duration)

        # シュー終了
        if table_cur_col[table_name] > 0:
            table_cols[table_name].append(table_cur_col[table_name])
        table_cur_col[table_name] = 0
        table_last_side[table_name] = None

    return durations, breakdown_signals


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")

    # === 1. テレコ持続時間分析 ===
    print("\n=== テレコ持続時間分析 ===")
    durations, breakdown_signals = analyze_tereko_duration(shoes)
    if durations:
        durations.sort()
        avg_d = sum(durations) / len(durations)
        med_d = durations[len(durations) // 2]
        print(f"  テレコ区間数: {len(durations)}")
        print(f"  平均持続: {avg_d:.1f} ハンド")
        print(f"  中央値:   {med_d} ハンド")
        print(f"  最短: {min(durations)}  最長: {max(durations)}")
        # 分布
        buckets = Counter()
        for d in durations:
            if d <= 5:
                buckets['1-5h'] += 1
            elif d <= 10:
                buckets['6-10h'] += 1
            elif d <= 20:
                buckets['11-20h'] += 1
            elif d <= 30:
                buckets['21-30h'] += 1
            elif d <= 50:
                buckets['31-50h'] += 1
            else:
                buckets['51h+'] += 1
        print(f"  分布: {dict(buckets)}")
        print(f"  崩壊時の列長: {dict(breakdown_signals.most_common(10))}")

    # === 2. パラメータ総当たり ===
    print("\n=== パラメータ総当たり ===")
    entry_windows = [6, 8, 10, 12, 15]
    entry_thresholds = [0.70, 0.75, 0.80, 0.85, 0.90]
    exit_params = [
        # (drop3_limit, drop4_exit, drop5_exit, label)
        (999, False, False, "退室なし"),
        (1,   True,  False, "3落ち×1 or 4落ち"),
        (2,   True,  False, "3落ち×2 or 4落ち"),
        (3,   True,  False, "3落ち×3 or 4落ち"),
        (2,   False, True,  "3落ち×2 or 5落ち"),
        (3,   False, True,  "3落ち×3 or 5落ち"),
        (999, True,  False, "4落ちのみ"),
        (999, False, True,  "5落ちのみ"),
    ]

    results = []
    total = len(entry_windows) * len(entry_thresholds) * len(exit_params)
    count = 0

    for ew in entry_windows:
        for et in entry_thresholds:
            for d3, d4, d5, elabel in exit_params:
                count += 1
                if count % 10 == 0:
                    print(f"  {count}/{total}...")
                r = run_single(shoes, ew, et, d3, d4, d5)
                r['entry_window'] = ew
                r['entry_threshold'] = et
                r['exit_label'] = elabel
                r['d3'] = d3
                r['d4'] = d4
                r['d5'] = d5
                results.append(r)

    # MB PNLでソート
    results.sort(key=lambda x: -x['mb_pnl'])

    # === 3. HTML生成 ===
    print("\n=== HTML生成 ===")

    # 持続時間チャート
    dur_html = ""
    if durations:
        dur_buckets = [
            ('1-5h', sum(1 for d in durations if d <= 5)),
            ('6-10h', sum(1 for d in durations if 6 <= d <= 10)),
            ('11-20h', sum(1 for d in durations if 11 <= d <= 20)),
            ('21-30h', sum(1 for d in durations if 21 <= d <= 30)),
            ('31-50h', sum(1 for d in durations if 31 <= d <= 50)),
            ('51-100h', sum(1 for d in durations if 51 <= d <= 100)),
            ('100h+', sum(1 for d in durations if d > 100)),
        ]
        for label, cnt in dur_buckets:
            pct = cnt / len(durations) * 100
            bar_w = pct * 4
            dur_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:80px">{label}</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:#6dd5ed;border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:#8a96a8">{cnt:,} ({pct:.1f}%)</div>
</div>"""

    # 崩壊信号
    breakdown_html = ""
    for col_len, cnt in breakdown_signals.most_common(10):
        pct = cnt / len(durations) * 100
        bar_w = pct * 4
        breakdown_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:80px">{col_len}落ち</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:#f87171;border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:#8a96a8">{cnt:,} ({pct:.1f}%)</div>
</div>"""

    # Top 30 結果テーブル
    top_results_html = ""
    for i, r in enumerate(results[:40]):
        mb_c = '#4ade80' if r['mb_pnl'] >= 0 else '#f87171'
        fp_c = '#4ade80' if r['flat_pnl'] >= 0 else '#f87171'
        rank_style = "background:#1a3a1a;" if i < 3 else ""
        top_results_html += (
            f"<tr style='{rank_style}'>"
            f"<td style='font-weight:bold'>#{i+1}</td>"
            f"<td>{r['entry_window']}</td>"
            f"<td>{r['entry_threshold']*100:.0f}%</td>"
            f"<td>{r['exit_label']}</td>"
            f"<td>{r['entries']:,}</td>"
            f"<td>{r['bets']:,}</td>"
            f"<td style='font-weight:bold'>{r['hr']:.2f}%</td>"
            f"<td style='color:{fp_c}'>{r['flat_pnl']:+,}</td>"
            f"<td>{r['mb_sessions']:,}</td>"
            f"<td style='color:{mb_c};font-weight:bold'>${r['mb_pnl']:+,.0f}</td>"
            f"<td>${r['mb_maxdd']:,.0f}</td>"
            f"</tr>"
        )

    # 最適パラメータ
    best = results[0]

    # 入室条件別の集計 (退室条件固定)
    entry_summary = defaultdict(list)
    for r in results:
        key = (r['entry_window'], r['entry_threshold'])
        entry_summary[key].append(r['mb_pnl'])
    entry_avg = {k: sum(v)/len(v) for k, v in entry_summary.items()}
    entry_sorted = sorted(entry_avg.items(), key=lambda x: -x[1])

    entry_heat_html = ""
    for (ew, et), avg_pnl in entry_sorted[:15]:
        c = '#4ade80' if avg_pnl >= 0 else '#f87171'
        bar_w = max(1, abs(avg_pnl) / 100)
        entry_heat_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:150px">Window={ew}, Thresh={et*100:.0f}%</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:{c}">${avg_pnl:+,.0f}</div>
</div>"""

    # 退室条件別の集計 (入室条件固定)
    exit_summary = defaultdict(list)
    for r in results:
        exit_summary[r['exit_label']].append(r['mb_pnl'])
    exit_avg = {k: sum(v)/len(v) for k, v in exit_summary.items()}
    exit_sorted = sorted(exit_avg.items(), key=lambda x: -x[1])

    exit_heat_html = ""
    for elabel, avg_pnl in exit_sorted:
        c = '#4ade80' if avg_pnl >= 0 else '#f87171'
        bar_w = max(1, abs(avg_pnl) / 100)
        exit_heat_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:200px">{elabel}</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:{c}">${avg_pnl:+,.0f}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>P. 入退室条件の最適化</title>
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
  border-bottom: 2px solid #2a3441; position: sticky; top: 0;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.highlight {{
  background: #11192a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 16px; margin: 16px 0;
}}
.highlight h3 {{ margin-top: 0; color: #4ade80; }}
.best {{ background: #1a3a1a; border: 2px solid #4ade80; border-radius: 8px; padding: 20px; margin: 20px 0; }}
.best h3 {{ color: #4ade80; margin: 0 0 12px 0; }}
.best .param {{ font-size: 18px; color: #ffd700; margin: 4px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>P. 入退室条件の最適化</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="realtime_counter_backtest.html">O. リアルタイム入退室</a>
</div>

<div class="banner">
<strong>📊 入室条件 {len(entry_windows)*len(entry_thresholds)}通り × 退室条件 {len(exit_params)}通り = {total}パターンを総当たり検証。</strong><br>
全パラメータ組み合わせで最適な入退室条件をデータから導出。<br>
データ: {DATE_FROM}〜本日 / {len(shoes):,}シュー / 62テーブル
</div>

<div class="best">
<h3>🏆 最適パラメータ (MB PNL 最大)</h3>
<div class="param">入室: 直近 <strong>{best['entry_window']}列</strong>で 1落ち+2落ち <strong>{best['entry_threshold']*100:.0f}%</strong>以上</div>
<div class="param">退室: <strong>{best['exit_label']}</strong></div>
<div class="param">結果: 勝率 <strong>{best['hr']:.2f}%</strong> / BET <strong>{best['bets']:,}</strong>回 / 入室 <strong>{best['entries']:,}</strong>回</div>
<div class="param">Flat PNL: <strong style="color:{'#4ade80' if best['flat_pnl']>=0 else '#f87171'}">{best['flat_pnl']:+,}</strong></div>
<div class="param">MB PNL: <strong style="color:{'#4ade80' if best['mb_pnl']>=0 else '#f87171'}">${best['mb_pnl']:+,.0f}</strong> / {best['mb_sessions']}セッション完走 / MaxDD ${best['mb_maxdd']:,.0f}</div>
</div>

<h2>1. テレコ区間の持続時間</h2>
<p class="note">テレコ状態が何ハンド続くか。短いほど頻繁に出入りが必要。</p>
<div class="summary">
  <div class="card">
    <div class="label">テレコ区間数</div>
    <div class="value">{len(durations):,}</div>
  </div>
  <div class="card green">
    <div class="label">平均持続</div>
    <div class="value">{sum(durations)/len(durations) if durations else 0:.1f}h</div>
  </div>
  <div class="card yellow">
    <div class="label">中央値</div>
    <div class="value">{durations[len(durations)//2] if durations else 0}h</div>
  </div>
</div>
{dur_html}

<h2>2. テレコ崩壊時の列長 (何落ちで崩れるか)</h2>
<p class="note">テレコ区間が終わる直前の列長。「何落ちが出たらテレコが崩壊したか」。</p>
{breakdown_html}

<h2>3. 入室条件の効果 (退室条件の全平均)</h2>
<p class="note">各入室条件パラメータの平均MB PNL。どのWindow/Thresholdが最も利益を生むか。</p>
{entry_heat_html}

<h2>4. 退室条件の効果 (入室条件の全平均)</h2>
<p class="note">各退室条件の平均MB PNL。退室なし vs 各退室条件の比較。</p>
{exit_heat_html}

<h2>5. 全パラメータ組み合わせ Top 40</h2>
<p class="note">MB PNL の降順。</p>
<table>
<thead><tr>
  <th>#</th><th>Window</th><th>Threshold</th><th>退室条件</th>
  <th>入室</th><th>BET数</th><th>勝率</th><th>Flat PNL</th>
  <th>MB完走</th><th>MB PNL</th><th>MB MaxDD</th>
</tr></thead>
<tbody>{top_results_html}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_entry_exit_optimization.py</code> /
  逆張り + Banker増額 (×{BANKER_UNIT_MULT:.4f}) + 〇✖ MaruBatsu ($50利確)
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "entry_exit_optimization.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")
    print(f"\n🏆 最適: Window={best['entry_window']}, Thresh={best['entry_threshold']*100:.0f}%, 退室={best['exit_label']}")
    print(f"   MB PNL: ${best['mb_pnl']:+,.0f} / 勝率: {best['hr']:.2f}% / BET: {best['bets']:,}")


if __name__ == "__main__":
    main()
