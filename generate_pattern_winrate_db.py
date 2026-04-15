"""大路パターン × 逆張り勝率 データベース構築

全シューの全ハンドで「直近N列のパターン → 逆張り結果」を記録し、
パターンごとの勝率をランキング化。

出力:
  - report/pattern_winrate_db.html (レポート)
  - pattern_winrate_db.json (データ)

Usage:
  python generate_pattern_winrate_db.py --vps
"""
import sqlite3
import os
import sys
import json
from collections import defaultdict, Counter

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50

# 直近何列でパターンを定義するか (複数試す)
WINDOW_SIZES = [6, 8, 10, 12]
# 最低サンプル数
MIN_SAMPLES = 50


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


def compute_columns_incremental(seq):
    """P/B/T シーケンスから大路列長をインクリメンタルに計算"""
    columns = []
    current_len = 0
    last_side = None
    for ch in seq:
        if ch == 'T':
            continue
        if ch == last_side:
            current_len += 1
        else:
            if last_side is not None:
                columns.append(current_len)
            current_len = 1
            last_side = ch
    return columns


def analyze_all(shoes):
    """全シュー・全ハンドを走査し、パターン別勝率を記録"""

    # pattern_key → {wins, losses}
    results_by_window = {}
    for ws in WINDOW_SIZES:
        results_by_window[ws] = defaultdict(lambda: {'wins': 0, 'losses': 0})

    # 全体統計
    total_hands = 0
    total_wins = 0
    total_losses = 0

    # 列長分布
    col_len_dist = Counter()

    for table_name, seq, started_at in shoes:
        columns = []
        current_len = 0
        last_side = None
        last_non_tie = None

        for ch in seq:
            if ch not in ('P', 'B', 'T'):
                continue
            if ch == 'T':
                continue

            # 大路列長を更新
            if ch == last_side:
                current_len += 1
            else:
                if last_side is not None:
                    columns.append(current_len)
                    col_len_dist[current_len] += 1
                current_len = 1
                last_side = ch

            # 逆張り判定
            if last_non_tie is not None:
                bet_side = 'P' if last_non_tie == 'B' else 'B'
                won = (ch == bet_side)
                total_hands += 1
                if won:
                    total_wins += 1
                else:
                    total_losses += 1

                # 各ウィンドウサイズでパターンを記録
                for ws in WINDOW_SIZES:
                    if len(columns) >= ws:
                        pattern = tuple(columns[-ws:])
                        d = results_by_window[ws][pattern]
                        if won:
                            d['wins'] += 1
                        else:
                            d['losses'] += 1

            last_non_tie = ch

    return results_by_window, total_hands, total_wins, total_losses, col_len_dist


def build_rankings(results_by_window):
    """各ウィンドウサイズで勝率ランキングを構築"""
    rankings = {}
    for ws in WINDOW_SIZES:
        patterns = results_by_window[ws]
        ranked = []
        for pattern, d in patterns.items():
            total = d['wins'] + d['losses']
            if total < MIN_SAMPLES:
                continue
            wr = d['wins'] / total * 100
            ranked.append({
                'pattern': list(pattern),
                'pattern_str': '-'.join(str(x) for x in pattern),
                'wins': d['wins'],
                'losses': d['losses'],
                'total': total,
                'win_rate': wr,
                'short_rate': sum(1 for x in pattern if x <= 2) / len(pattern) * 100,
            })
        ranked.sort(key=lambda x: -x['win_rate'])
        rankings[ws] = ranked
    return rankings


def render_html(rankings, total_hands, total_wins, total_losses, col_len_dist, total_shoes):
    overall_wr = total_wins / total_hands * 100 if total_hands > 0 else 0

    # 列長分布チャート
    col_html = ""
    total_cols = sum(col_len_dist.values())
    for length in sorted(col_len_dist.keys()):
        cnt = col_len_dist[length]
        pct = cnt / total_cols * 100
        bar_w = pct * 4
        col_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:3px 0;font-size:13px">
  <div style="min-width:50px">{length}落ち</div>
  <div style="flex:1;height:18px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:#6dd5ed;border-radius:4px"></div>
  </div>
  <div style="min-width:120px;text-align:right;color:#8a96a8">{cnt:,} ({pct:.1f}%)</div>
</div>"""

    # 各ウィンドウサイズのランキング
    ws_sections = ""
    for ws in WINDOW_SIZES:
        ranked = rankings[ws]
        total_patterns = len(ranked)
        above_53 = sum(1 for r in ranked if r['win_rate'] >= 53)
        above_55 = sum(1 for r in ranked if r['win_rate'] >= 55)
        below_50 = sum(1 for r in ranked if r['win_rate'] < 50)

        # 勝率分布
        wr_buckets = {
            '55%+': sum(1 for r in ranked if r['win_rate'] >= 55),
            '53-55%': sum(1 for r in ranked if 53 <= r['win_rate'] < 55),
            '51-53%': sum(1 for r in ranked if 51 <= r['win_rate'] < 53),
            '50-51%': sum(1 for r in ranked if 50 <= r['win_rate'] < 51),
            '50%未満': sum(1 for r in ranked if r['win_rate'] < 50),
        }
        wr_dist = ""
        for label, cnt in wr_buckets.items():
            pct = cnt / total_patterns * 100 if total_patterns > 0 else 0
            c = '#4ade80' if '55' in label or '53' in label else ('#fbbf24' if '51' in label else '#f87171')
            wr_dist += f"<span style='color:{c}'>{label}: {cnt} ({pct:.0f}%)</span> &nbsp; "

        # Top 30 / Bottom 10
        top_rows = ""
        for i, r in enumerate(ranked[:30]):
            bg = "background:#1a3a1a;" if r['win_rate'] >= 55 else ("background:#1a2a1a;" if r['win_rate'] >= 53 else "")
            sr_c = '#4ade80' if r['short_rate'] >= 80 else ('#fbbf24' if r['short_rate'] >= 60 else '#8a96a8')
            top_rows += (
                f"<tr style='{bg}'>"
                f"<td>#{i+1}</td>"
                f"<td style='font-family:monospace;font-size:12px'>{r['pattern_str']}</td>"
                f"<td>{r['total']:,}</td>"
                f"<td style='font-weight:bold;color:#4ade80'>{r['win_rate']:.2f}%</td>"
                f"<td style='color:{sr_c}'>{r['short_rate']:.0f}%</td>"
                f"<td>{r['wins']:,}W / {r['losses']:,}L</td>"
                f"</tr>"
            )

        bottom_rows = ""
        for r in ranked[-10:]:
            bottom_rows += (
                f"<tr style='background:#2a1a1a'>"
                f"<td style='font-family:monospace;font-size:12px'>{r['pattern_str']}</td>"
                f"<td>{r['total']:,}</td>"
                f"<td style='font-weight:bold;color:#f87171'>{r['win_rate']:.2f}%</td>"
                f"<td>{r['short_rate']:.0f}%</td>"
                f"</tr>"
            )

        ws_sections += f"""
<h2>直近 {ws} 列パターン</h2>
<div class="summary">
  <div class="card"><div class="label">パターン数</div><div class="value">{total_patterns}</div></div>
  <div class="card profit"><div class="label">勝率53%+</div><div class="value">{above_53}</div></div>
  <div class="card" style="border-left-color:#4ade80"><div class="label">勝率55%+</div><div class="value">{above_55}</div></div>
  <div class="card bankrupt"><div class="label">勝率50%未満</div><div class="value">{below_50}</div></div>
</div>
<p class="note">勝率分布: {wr_dist}</p>

<h3>Top 30 (勝率が高い大路パターン)</h3>
<p class="note">パターン = 直近{ws}列の列長。例: 1-2-1-1-2-1 = テレコ+ニコ混合。短列率 = 1落ち+2落ちの割合。</p>
<table>
<thead><tr><th>#</th><th>パターン (列長)</th><th>サンプル</th><th>逆張り勝率</th><th>短列率</th><th>勝敗</th></tr></thead>
<tbody>{top_rows}</tbody>
</table>

<h3>Bottom 10 (最も勝率が低いパターン)</h3>
<table>
<thead><tr><th>パターン</th><th>サンプル</th><th>勝率</th><th>短列率</th></tr></thead>
<tbody>{bottom_rows}</tbody>
</table>
"""

    # 短列率と勝率の相関分析
    correlation_data = []
    for ws in [10]:  # 代表的なウィンドウサイズ
        for r in rankings[ws]:
            correlation_data.append({
                'short_rate': r['short_rate'],
                'win_rate': r['win_rate'],
                'samples': r['total'],
            })

    # 短列率バケットごとの平均勝率
    sr_buckets = defaultdict(lambda: {'wins': 0, 'losses': 0})
    for d in correlation_data:
        bucket = int(d['short_rate'] // 10) * 10
        sr_buckets[bucket]['wins'] += int(d['win_rate'] * d['samples'] / 100)
        sr_buckets[bucket]['losses'] += d['samples'] - int(d['win_rate'] * d['samples'] / 100)

    corr_html = ""
    for bucket in sorted(sr_buckets.keys()):
        d = sr_buckets[bucket]
        total = d['wins'] + d['losses']
        wr = d['wins'] / total * 100 if total > 0 else 0
        bar_w = (wr - 45) * 20  # 45-55% range → 0-200px
        bar_w = max(0, min(200, bar_w))
        c = '#4ade80' if wr >= 53 else ('#fbbf24' if wr >= 51 else '#f87171')
        corr_html += f"""
<div style="display:flex;align-items:center;gap:12px;margin:4px 0;font-size:13px">
  <div style="min-width:80px">短列{bucket}-{bucket+10}%</div>
  <div style="flex:1;height:20px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:100px;text-align:right;color:{c};font-weight:bold">{wr:.2f}%</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>V. 大路パターン × 逆張り勝率 データベース</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, "Segoe UI", sans-serif; background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 22px; }}
h3 {{ color: #6dd5ed; margin-top: 20px; }}
.nav {{ margin: 16px 0 24px; }}
.nav a {{ display: inline-block; margin-right: 12px; padding: 8px 16px; background: #1a2332; color: #6dd5ed; text-decoration: none; border-radius: 4px; border: 1px solid #2a3441; font-size: 13px; }}
.banner {{ background: #1a2a1a; border-left: 5px solid #4ade80; padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8; }}
.banner strong {{ color: #4ade80; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.profit .value {{ color: #4ade80; }}
.card.bankrupt .value {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.highlight {{ background: #11192a; border: 2px solid #4ade80; border-radius: 8px; padding: 16px; margin: 16px 0; }}
.highlight h3 {{ margin-top: 0; color: #4ade80; }}
</style>
</head>
<body>
<div class="container">
<h1>V. 大路パターン × 逆張り勝率 データベース</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="entry_exit_optimization.html">P. 入退室最適化</a>
<a href="tereko_lifespan_risk.html">S. テレコ寿命リスク</a>
</div>

<div class="banner">
<strong>📊 大路罫線のパターンと逆張り勝率の関係をデータベース化。</strong><br>
全{total_shoes:,}シュー・{total_hands:,}ハンドで、直近N列の大路パターン別に逆張り勝率を計算。<br>
「どのような大路の形の時に逆張りが当たりやすいか」を可視化。<br>
<strong>Evolution が大路を変えてきても、このデータベースを更新すれば最新の勝てるパターンがわかる。</strong>
</div>

<div class="summary">
  <div class="card"><div class="label">総ハンド数</div><div class="value">{total_hands:,}</div></div>
  <div class="card profit"><div class="label">逆張り勝率</div><div class="value">{overall_wr:.2f}%</div></div>
  <div class="card"><div class="label">勝ち</div><div class="value">{total_wins:,}</div></div>
  <div class="card bankrupt"><div class="label">負け</div><div class="value">{total_losses:,}</div></div>
</div>

<h2>大路の列長分布</h2>
<p class="note">全テーブルで出現した列長 (何落ちか) の分布。1落ち (テレコ) が圧倒的に多い。</p>
{col_html}

<h2>短列率 × 逆張り勝率の相関 (10列窓)</h2>
<p class="note">
短列率 (1落ち+2落ちの割合) が高いほど逆張り勝率が高い傾向。<br>
<strong>現在の入室条件 (短列率85%以上) の根拠がここにある。</strong>
</p>
{corr_html}

<div class="highlight">
<h3>📌 読み方ガイド</h3>
<p class="note">
<strong>パターン</strong>: 直近N列の列長を並べたもの。例: 1-2-1-1-2-1 = 1落ち2落ちの繰り返し (テレコ混合)。<br>
<strong>短列率</strong>: パターン内の1落ち+2落ちの割合。高いほどテレコ。<br>
<strong>逆張り勝率</strong>: そのパターンの時に逆張り (前手の逆) でBETした勝率。53%+ が有効。<br>
<strong>サンプル</strong>: 最低{MIN_SAMPLES}以上のパターンのみ表示。
</p>
</div>

{ws_sections}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_pattern_winrate_db.py</code> /
  ウィンドウサイズ: {', '.join(str(w) for w in WINDOW_SIZES)} / 最低サンプル: {MIN_SAMPLES}
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "pattern_winrate_db.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")

    # JSONデータも保存
    json_data = {}
    for ws in WINDOW_SIZES:
        json_data[f"window_{ws}"] = rankings[ws][:100]  # Top 100
    json_path = os.path.join("report", "pattern_winrate_db.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    print(f"Wrote {json_path}")


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")
    results_by_window, total_hands, total_wins, total_losses, col_len_dist = analyze_all(shoes)
    print(f"Total {total_hands:,} counter-bet opportunities")
    print(f"Overall win rate: {total_wins/total_hands*100:.2f}%")
    rankings = build_rankings(results_by_window)
    for ws in WINDOW_SIZES:
        print(f"\nWindow {ws}: {len(rankings[ws])} patterns (>={MIN_SAMPLES} samples)")
        if rankings[ws]:
            top = rankings[ws][0]
            print(f"  Best: {top['pattern_str']} → {top['win_rate']:.2f}% ({top['total']} samples)")
    render_html(rankings, total_hands, total_wins, total_losses, col_len_dist, len(shoes))


if __name__ == "__main__":
    main()
