"""テレコ寿命リスク分析

テレコ区間の持続時間が短くなった場合のシミュレーション:
1. 現在のテレコ寿命分布を日別に可視化
2. テレコ寿命を人工的に短縮した場合のPNL/DDへの影響
3. 「何ハンド以下になったら戦略が破綻するか」の閾値を特定
4. 早期警戒指標 (日次テレコ寿命の推移)

Usage:
  python generate_tereko_lifespan_risk.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
STATIC_WARMUP = 30
BANKER_COMMISSION = 0.05

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]

PROFIT_TARGET = 30  # 最適解


class CounterSim:
    def __init__(self, target=PROFIT_TARGET):
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
        self.hands_bet += 1
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


def compute_columns(seq):
    cols = []
    cur = 0
    last = None
    for ch in seq:
        if ch == 'T':
            continue
        if ch == last:
            cur += 1
        else:
            if last is not None:
                cols.append(cur)
            cur = 1
            last = ch
    if cur > 0:
        cols.append(cur)
    return cols


def analyze_tereko_segments(shoes):
    """全テーブル・全シューからテレコ区間を抽出し、各区間の寿命と収益を記録"""
    WINDOW = 15
    THRESH = 0.85

    segments = []  # [{duration, date, table, hands_bet, wins, losses, pnl_flat}]
    daily_durations = defaultdict(list)

    for table_name, seq, started_at in shoes:
        clean = strip_ties(seq)
        if len(clean) < 20:
            continue
        date = started_at[:10] if started_at else "unknown"

        cols = compute_columns(clean)
        in_tereko = False
        tereko_start = 0
        tereko_hands = 0
        tereko_wins = 0
        tereko_losses = 0
        last_nt = None
        hand_idx = 0

        for i, ch in enumerate(clean):
            hand_idx += 1

            # 列長更新 (簡易: 列確定チェック)
            current_cols = compute_columns(clean[:i+1])

            if len(current_cols) >= WINDOW:
                recent = current_cols[-WINDOW:]
                short = sum(1 for L in recent if L <= 2)
                is_t = (short / len(recent)) >= THRESH

                if is_t and not in_tereko:
                    in_tereko = True
                    tereko_start = hand_idx
                    tereko_hands = 0
                    tereko_wins = 0
                    tereko_losses = 0
                elif not is_t and in_tereko:
                    duration = hand_idx - tereko_start
                    segments.append({
                        'duration': duration,
                        'date': date,
                        'table': table_name,
                        'wins': tereko_wins,
                        'losses': tereko_losses,
                        'pnl_flat': tereko_wins - tereko_losses,
                    })
                    daily_durations[date].append(duration)
                    in_tereko = False

            # 逆張り勝敗
            if in_tereko and last_nt is not None:
                bet_side = 'P' if last_nt == 'B' else 'B'
                if ch == bet_side:
                    tereko_wins += 1
                else:
                    tereko_losses += 1
            last_nt = ch

        if in_tereko:
            duration = hand_idx - tereko_start
            segments.append({
                'duration': duration,
                'date': date,
                'table': table_name,
                'wins': tereko_wins,
                'losses': tereko_losses,
                'pnl_flat': tereko_wins - tereko_losses,
            })
            daily_durations[date].append(duration)

    return segments, daily_durations


def simulate_with_max_lifespan(shoes, max_lifespan):
    """テレコ区間をmax_lifespanハンドで強制打ち切りした場合のPNLをシミュレート"""
    WINDOW = 15
    THRESH = 0.85

    sim = CounterSim(target=PROFIT_TARGET)
    total_bets = 0
    total_wins = 0
    total_sessions = 0
    balance = 10000
    peak_balance = 10000
    max_dd = 0

    for table_name, seq, started_at in shoes:
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue
        pattern = classify_pattern(clean[:STATIC_WARMUP], min_cols=3)
        if pattern != "テレコ+ニコ混合":
            continue

        hands_in_tereko = 0

        for r in seq:
            if r not in ('P', 'B', 'T'):
                continue

            # 強制打ち切り
            if hands_in_tereko >= max_lifespan:
                # テレコ区間終了 → リセットせずスキップ
                continue

            if r != 'T' and sim.last_non_tie is not None:
                total_bets += 1
                bet_side = 'P' if sim.last_non_tie == 'B' else 'B'
                if r == bet_side:
                    total_wins += 1
                hands_in_tereko += 1

            result = sim.add(r)
            current = balance + sim.cumulative
            if current > peak_balance:
                peak_balance = current
            dd = peak_balance - current
            if dd > max_dd:
                max_dd = dd

            if result == 'profit':
                balance += sim.cumulative
                total_sessions += 1
                sim.reset()
                hands_in_tereko = 0  # 利確でリセット

    total_pnl = balance + sim.cumulative - 10000
    hr = total_wins / total_bets * 100 if total_bets > 0 else 0
    return {
        'max_lifespan': max_lifespan,
        'total_bets': total_bets,
        'total_wins': total_wins,
        'hit_rate': hr,
        'total_pnl': total_pnl,
        'sessions': total_sessions,
        'max_dd': max_dd,
        'efficiency': total_pnl / max_dd if max_dd > 0 else float('inf'),
    }


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")

    # 1. テレコ区間分析
    print("\n=== テレコ区間分析 ===")
    segments, daily_durations = analyze_tereko_segments(shoes)
    print(f"  テレコ区間数: {len(segments)}")

    # 2. 寿命別シミュレーション
    print("\n=== 寿命別シミュレーション ===")
    lifespans = [3, 5, 7, 10, 15, 20, 30, 50, 100, 999]
    lifespan_results = []
    for ls in lifespans:
        print(f"  max_lifespan={ls}...")
        r = simulate_with_max_lifespan(shoes, ls)
        lifespan_results.append(r)
        print(f"    PNL=${r['total_pnl']:+,.0f} DD=${r['max_dd']:,.0f} eff={r['efficiency']:.2f}")

    # 3. 日別統計
    dates = sorted(daily_durations.keys())

    # === HTML ===

    # 日別テレコ寿命チャート
    daily_html = ""
    for date in dates:
        durs = daily_durations[date]
        avg = sum(durs) / len(durs) if durs else 0
        med = sorted(durs)[len(durs)//2] if durs else 0
        short_pct = sum(1 for d in durs if d <= 5) / len(durs) * 100 if durs else 0
        long_pct = sum(1 for d in durs if d >= 20) / len(durs) * 100 if durs else 0
        bar_w = avg * 5
        avg_c = '#4ade80' if avg >= 15 else ('#fbbf24' if avg >= 10 else '#f87171')
        daily_html += (
            f"<tr>"
            f"<td>{date}</td>"
            f"<td>{len(durs):,}</td>"
            f"<td style='color:{avg_c};font-weight:bold'>{avg:.1f}h</td>"
            f"<td>{med}h</td>"
            f"<td style='color:{'#f87171' if short_pct > 40 else '#8a96a8'}'>{short_pct:.0f}%</td>"
            f"<td style='color:{'#4ade80' if long_pct > 20 else '#8a96a8'}'>{long_pct:.0f}%</td>"
            f"</tr>"
        )

    # 寿命制限シミュレーション
    lifespan_html = ""
    current_pnl = [r for r in lifespan_results if r['max_lifespan'] == 999][0]['total_pnl']
    for r in lifespan_results:
        pnl_c = '#4ade80' if r['total_pnl'] > 0 else '#f87171'
        pnl_pct = r['total_pnl'] / current_pnl * 100 if current_pnl != 0 else 0
        danger = ""
        if r['total_pnl'] <= 0:
            danger = " 💀 破綻ライン"
        elif r['total_pnl'] < current_pnl * 0.3:
            danger = " ⚠️ 危険域"
        ls_label = f"{r['max_lifespan']}h" if r['max_lifespan'] < 999 else "制限なし"
        lifespan_html += (
            f"<tr>"
            f"<td style='font-weight:bold'>{ls_label}</td>"
            f"<td>{r['total_bets']:,}</td>"
            f"<td>{r['hit_rate']:.2f}%</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${r['total_pnl']:+,.0f}</td>"
            f"<td>{pnl_pct:.0f}%</td>"
            f"<td>{r['sessions']:,}</td>"
            f"<td>${r['max_dd']:,.0f}</td>"
            f"<td>{r['efficiency']:.2f}</td>"
            f"<td>{danger}</td>"
            f"</tr>"
        )

    # 寿命分布
    dur_buckets = [
        ('1-3h', sum(1 for s in segments if s['duration'] <= 3)),
        ('4-5h', sum(1 for s in segments if 4 <= s['duration'] <= 5)),
        ('6-10h', sum(1 for s in segments if 6 <= s['duration'] <= 10)),
        ('11-15h', sum(1 for s in segments if 11 <= s['duration'] <= 15)),
        ('16-20h', sum(1 for s in segments if 16 <= s['duration'] <= 20)),
        ('21-30h', sum(1 for s in segments if 21 <= s['duration'] <= 30)),
        ('31-50h', sum(1 for s in segments if 31 <= s['duration'] <= 50)),
        ('51h+', sum(1 for s in segments if s['duration'] > 50)),
    ]
    dur_html = ""
    for label, cnt in dur_buckets:
        pct = cnt / len(segments) * 100 if segments else 0
        bar_w = pct * 4
        dur_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:14px">
  <div style="min-width:80px">{label}</div>
  <div style="flex:1;height:22px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:#6dd5ed;border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:#8a96a8">{cnt:,} ({pct:.1f}%)</div>
</div>"""

    # 区間別PNL (短い区間 vs 長い区間)
    short_segs = [s for s in segments if s['duration'] <= 5]
    mid_segs = [s for s in segments if 6 <= s['duration'] <= 15]
    long_segs = [s for s in segments if s['duration'] > 15]

    def seg_stats(segs, label):
        if not segs:
            return f"<tr><td>{label}</td><td>0</td><td>-</td><td>-</td><td>-</td></tr>"
        total_w = sum(s['wins'] for s in segs)
        total_l = sum(s['losses'] for s in segs)
        total_pnl = sum(s['pnl_flat'] for s in segs)
        hr = total_w / (total_w + total_l) * 100 if (total_w + total_l) > 0 else 0
        pnl_c = '#4ade80' if total_pnl > 0 else '#f87171'
        avg_pnl = total_pnl / len(segs)
        return (
            f"<tr>"
            f"<td style='font-weight:bold'>{label}</td>"
            f"<td>{len(segs):,}</td>"
            f"<td>{hr:.1f}%</td>"
            f"<td style='color:{pnl_c}'>{total_pnl:+,}</td>"
            f"<td>{avg_pnl:+.1f}</td>"
            f"</tr>"
        )

    seg_stats_html = seg_stats(short_segs, "短命 (1-5h)") + seg_stats(mid_segs, "中間 (6-15h)") + seg_stats(long_segs, "長命 (16h+)")

    # 早期警戒の閾値
    breakeven_lifespan = "不明"
    for r in lifespan_results:
        if r['total_pnl'] <= 0:
            breakeven_lifespan = f"{r['max_lifespan']}ハンド以下"
            break

    avg_dur = sum(s['duration'] for s in segments) / len(segments) if segments else 0

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>S. テレコ寿命リスク分析</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1400px; margin: 0 auto; }}
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
  background: #2a1a1a; border-left: 5px solid #f87171;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: #f87171; }}
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

table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.danger-box {{
  background: #2a1a1a; border: 2px solid #f87171; border-radius: 8px;
  padding: 20px; margin: 20px 0;
}}
.danger-box h3 {{ color: #f87171; margin: 0 0 12px 0; }}
.safe-box {{
  background: #1a2a1a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 20px; margin: 20px 0;
}}
.safe-box h3 {{ color: #4ade80; margin: 0 0 12px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>S. テレコ寿命リスク分析</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="entry_exit_optimization.html">P. 入退室最適化</a>
<a href="profit_target_optimization.html">R. 利確最適化</a>
</div>

<div class="banner">
<strong>⚠️ テレコ逆張り戦略の最大の弱点: テレコの寿命。</strong><br>
テレコ区間が短くなると、入室→即崩壊→損失 のループに陥る。<br>
Evolution がパターンを変更してテレコの命が短くなった場合の影響をシミュレーション。<br>
<strong>早期警戒指標: 日別テレコ平均寿命が低下したら危険信号。</strong>
</div>

<div class="summary">
  <div class="card">
    <div class="label">テレコ区間数</div>
    <div class="value">{len(segments):,}</div>
  </div>
  <div class="card green">
    <div class="label">平均寿命</div>
    <div class="value">{avg_dur:.1f}h</div>
  </div>
  <div class="card yellow">
    <div class="label">短命区間 (≤5h)</div>
    <div class="value">{sum(1 for s in segments if s['duration']<=5)/len(segments)*100:.0f}%</div>
  </div>
  <div class="card red">
    <div class="label">破綻ライン</div>
    <div class="value">{breakeven_lifespan}</div>
  </div>
</div>

<h2>1. テレコ寿命の分布</h2>
<p class="note">テレコ区間が何ハンド続くかの分布。短命が多いほどリスクが高い。</p>
{dur_html}

<h2>2. 寿命別の逆張り収益 (flat $1)</h2>
<p class="note">短い区間 vs 長い区間で逆張りの勝率・収益は異なるか。</p>
<table>
<thead><tr><th>区間種別</th><th>区間数</th><th>逆張り勝率</th><th>Flat PNL</th><th>平均PNL/区間</th></tr></thead>
<tbody>{seg_stats_html}</tbody>
</table>

<h2>3. テレコ寿命制限シミュレーション</h2>
<p class="note">
テレコ区間を「最大Nハンドで強制終了」した場合の〇✖ PNL。<br>
Evolution がテレコの命を短くした場合のワーストケース分析。<br>
<strong>「制限なし」が現在の結果。制限が厳しいほど寿命が短い世界を模擬。</strong>
</p>

<table>
<thead><tr>
  <th>最大寿命</th><th>BET数</th><th>勝率</th><th>PNL ($30利確)</th><th>現在比</th>
  <th>完走</th><th>MaxDD</th><th>資金効率</th><th>判定</th>
</tr></thead>
<tbody>{lifespan_html}</tbody>
</table>

<div class="danger-box">
<h3>💀 破綻ライン: テレコ寿命が {breakeven_lifespan} になると利益が消える</h3>
<p class="note">
テレコ区間が極端に短くなると、〇✖ progression が利確に到達する前にテレコが崩壊する。<br>
崩壊後の損失が積み重なり、利益を食い尽くす。<br>
<strong>現在の平均寿命 {avg_dur:.1f}h が この閾値より十分に上にある限り安全。</strong>
</p>
</div>

<div class="safe-box">
<h3>✅ 早期警戒の基準値</h3>
<p class="note">
以下を日次で監視し、3日連続で閾値を下回ったら「環境変化」と判断して戦略を再評価:
</p>
<ul style="color:#e0e6ed;font-size:14px;margin:8px 0;">
<li><strong>平均テレコ寿命</strong>: 現在 {avg_dur:.1f}h → <strong style="color:#fbbf24">10h 以下は警戒</strong> → <strong style="color:#f87171">7h 以下は危険</strong></li>
<li><strong>短命区間率 (≤5h)</strong>: 現在 {sum(1 for s in segments if s['duration']<=5)/len(segments)*100:.0f}% → <strong style="color:#fbbf24">50%以上は警戒</strong> → <strong style="color:#f87171">60%以上は危険</strong></li>
<li><strong>逆張り勝率</strong>: 現在 53.5% → <strong style="color:#fbbf24">51%以下は警戒</strong> → <strong style="color:#f87171">50%以下は危険</strong></li>
</ul>
</div>

<h2>4. 日別テレコ寿命推移</h2>
<p class="note">日ごとのテレコ平均寿命。トレンドが下がっていたら Evolution が対策している可能性。</p>
<table>
<thead><tr>
  <th>日付</th><th>テレコ区間数</th><th>平均寿命</th><th>中央値</th>
  <th>短命率 (≤5h)</th><th>長命率 (≥20h)</th>
</tr></thead>
<tbody>{daily_html}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_tereko_lifespan_risk.py</code> /
  テレコ逆張り × $30利確 × 〇✖ MaruBatsu
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "tereko_lifespan_risk.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
