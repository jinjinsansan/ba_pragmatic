"""環境識別レポート — 各テーブルのテレコ/縦流れ/混在分布

目的:
  23Kシューに対し、各シューを「テレコ / 縦流れ / 混在」に分類し、
  テーブル毎の環境分布を集計。
  ハイブリッド戦略の切替根拠データとして可視化。

Usage:
  python generate_environment_classification.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
MIN_HANDS = 50

ENTRY_WINDOW = 15
LONG_COL_MIN = 3
COUNTER_THRESHOLD = 0.85   # 短列率 >= これでテレコ判定
TREND_THRESHOLD = 0.30     # 長列率 >= これで縦流れ判定


def compute_columns(seq):
    cols, cur, side = [], 0, None
    for ch in seq:
        if ch not in ('P', 'B'):
            continue
        if ch == side:
            cur += 1
        else:
            if side is not None:
                cols.append(cur)
            side = ch
            cur = 1
    if cur > 0:
        cols.append(cur)
    return cols


def classify_shoe(seq):
    """シューをテレコ/縦流れ/混在に分類。冒頭30ハンド以降の直近15列で判断。"""
    cols = compute_columns(seq)
    if len(cols) < ENTRY_WINDOW:
        return 'short'  # 短すぎて判断不能
    recent = cols[-ENTRY_WINDOW:]
    short_ratio = sum(1 for c in recent if c <= 2) / len(recent)
    long_ratio = sum(1 for c in recent if c >= LONG_COL_MIN) / len(recent)
    if short_ratio >= COUNTER_THRESHOLD:
        return 'tereko'
    if long_ratio >= TREND_THRESHOLD:
        return 'trend'
    return 'mixed'


def main():
    print(f"Loading {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT table_name, result_sequence, started_at FROM shoes_analytics "
                "WHERE hand_count >= ? ORDER BY started_at", (MIN_HANDS,))
    rows = cur.fetchall()
    conn.close()
    print(f"Total {len(rows):,} shoes\n")

    # テーブル別カウント
    table_stats = defaultdict(lambda: {'tereko': 0, 'trend': 0, 'mixed': 0, 'short': 0, 'total': 0})
    hourly_stats = defaultdict(lambda: {'tereko': 0, 'trend': 0, 'mixed': 0, 'short': 0, 'total': 0})
    dow_stats = defaultdict(lambda: {'tereko': 0, 'trend': 0, 'mixed': 0, 'short': 0, 'total': 0})

    for table_name, seq, ts in rows:
        env = classify_shoe(seq)
        table_stats[table_name][env] += 1
        table_stats[table_name]['total'] += 1
        try:
            from datetime import datetime, timezone, timedelta
            JST = timezone(timedelta(hours=9))
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
            hour = d.hour
            dow = d.weekday()
            hourly_stats[hour][env] += 1
            hourly_stats[hour]['total'] += 1
            dow_stats[dow][env] += 1
            dow_stats[dow]['total'] += 1
        except Exception:
            pass

    # テーブル別結果を PNL スコア順に
    table_list = []
    for tn, s in table_stats.items():
        if s['total'] < 30:
            continue
        t = s['total']
        table_list.append({
            'name': tn,
            'total': t,
            'tereko': s['tereko'],
            'trend': s['trend'],
            'mixed': s['mixed'],
            'short': s['short'],
            'tereko_pct': s['tereko'] / t * 100,
            'trend_pct': s['trend'] / t * 100,
            'mixed_pct': s['mixed'] / t * 100,
            'score': s['tereko'] * 2 + s['trend'] * 1.5,  # 戦略適用可能度
        })
    table_list.sort(key=lambda x: -x['score'])

    # HTML 生成
    table_rows = ""
    for r in table_list:
        # カラーバー
        bar_width = 180
        t_w = int(r['tereko_pct'] * bar_width / 100)
        r_w = int(r['trend_pct'] * bar_width / 100)
        m_w = int(r['mixed_pct'] * bar_width / 100)
        bar = (
            f'<div style="display:flex;height:12px;border-radius:3px;overflow:hidden;width:{bar_width}px">'
            f'<div style="background:#4aa8ff;width:{t_w}px" title="Tereko {r["tereko_pct"]:.0f}%"></div>'
            f'<div style="background:#ff8080;width:{r_w}px" title="Trend {r["trend_pct"]:.0f}%"></div>'
            f'<div style="background:#888;width:{m_w}px" title="Mixed {r["mixed_pct"]:.0f}%"></div>'
            f'</div>'
        )
        strategy = (
            "<span style='color:#4aa8ff'>Counter優位</span>" if r['tereko_pct'] > r['trend_pct'] * 1.5
            else "<span style='color:#ff8080'>Trend優位</span>" if r['trend_pct'] > r['tereko_pct'] * 1.5
            else "<span style='color:#ffcc00'>ハイブリッド</span>"
        )
        table_rows += (
            f"<tr>"
            f"<td style='font-weight:bold'>{r['name']}</td>"
            f"<td style='color:#8a96a8'>{r['total']:,}</td>"
            f"<td>{bar}</td>"
            f"<td style='color:#4aa8ff;font-weight:bold'>{r['tereko_pct']:.0f}%</td>"
            f"<td style='color:#ff8080;font-weight:bold'>{r['trend_pct']:.0f}%</td>"
            f"<td style='color:#888'>{r['mixed_pct']:.0f}%</td>"
            f"<td>{strategy}</td>"
            f"</tr>"
        )

    # 時間帯別
    hour_rows = ""
    for h in sorted(hourly_stats.keys()):
        s = hourly_stats[h]
        t = s['total']
        if t < 30:
            continue
        t_pct = s['tereko'] / t * 100
        r_pct = s['trend'] / t * 100
        hour_rows += (
            f"<tr><td>{h:02d}:00</td><td>{t:,}</td>"
            f"<td style='color:#4aa8ff'>{t_pct:.0f}%</td>"
            f"<td style='color:#ff8080'>{r_pct:.0f}%</td>"
            f"<td style='color:#888'>{s['mixed']/t*100:.0f}%</td></tr>"
        )

    # 曜日別
    DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    dow_rows = ""
    for dow in sorted(dow_stats.keys()):
        s = dow_stats[dow]
        t = s['total']
        if t < 30:
            continue
        t_pct = s['tereko'] / t * 100
        r_pct = s['trend'] / t * 100
        dow_rows += (
            f"<tr><td>{DOW_NAMES[dow]}</td><td>{t:,}</td>"
            f"<td style='color:#4aa8ff'>{t_pct:.0f}%</td>"
            f"<td style='color:#ff8080'>{r_pct:.0f}%</td>"
            f"<td style='color:#888'>{s['mixed']/t*100:.0f}%</td></tr>"
        )

    # 全体集計
    total = sum(r['total'] for r in table_list)
    total_tereko = sum(r['tereko'] for r in table_list)
    total_trend = sum(r['trend'] for r in table_list)
    total_mixed = sum(r['mixed'] for r in table_list)

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>AB. 環境識別レポート — テーブル毎のテレコ/縦流れ分布</title>
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
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
.legend {{ display: flex; gap: 20px; font-size: 12px; margin: 10px 0; }}
.legend span {{ display: flex; align-items: center; gap: 6px; }}
.legend-sq {{ width: 12px; height: 12px; border-radius: 2px; }}
</style></head><body><div class="container">
<h1>AB. 環境識別レポート</h1>
<div class="nav"><a href="index.html">← レポートTOP</a><a href="strategy_hybrid_10k.html">AA. ハイブリッド</a></div>

<div class="banner">
<strong>📊 各シューを環境分類 → テーブル毎に集計。</strong><br>
判定: 直近{ENTRY_WINDOW}列、短列率 ≥ {COUNTER_THRESHOLD} → Tereko / 長列率 ≥ {TREND_THRESHOLD} → Trend / どちらでもない → Mixed
</div>

<div class="legend">
  <span><div class="legend-sq" style="background:#4aa8ff"></div>Tereko (Counter 向き)</span>
  <span><div class="legend-sq" style="background:#ff8080"></div>Trend (順張り向き)</span>
  <span><div class="legend-sq" style="background:#888"></div>Mixed (Skip)</span>
</div>

<div class="summary">
  <div class="card"><div class="label">総シュー</div><div class="value">{total:,}</div></div>
  <div class="card"><div class="label">Tereko</div><div class="value" style="color:#4aa8ff">{total_tereko/total*100:.1f}%</div></div>
  <div class="card"><div class="label">Trend</div><div class="value" style="color:#ff8080">{total_trend/total*100:.1f}%</div></div>
  <div class="card"><div class="label">Mixed</div><div class="value" style="color:#888">{total_mixed/total*100:.1f}%</div></div>
</div>

<h2>📋 テーブル別環境分布 (戦略適用可能度順)</h2>
<table><thead><tr><th>テーブル</th><th>シュー数</th><th>分布</th><th>Tereko</th><th>Trend</th><th>Mixed</th><th>推奨戦略</th></tr></thead>
<tbody>{table_rows}</tbody></table>

<h2>🕐 時間帯別環境分布 (JST)</h2>
<table><thead><tr><th>時刻</th><th>シュー</th><th>Tereko</th><th>Trend</th><th>Mixed</th></tr></thead>
<tbody>{hour_rows}</tbody></table>

<h2>📅 曜日別環境分布</h2>
<table><thead><tr><th>曜日</th><th>シュー</th><th>Tereko</th><th>Trend</th><th>Mixed</th></tr></thead>
<tbody>{dow_rows}</tbody></table>

</div></body></html>"""

    out_path = os.path.join("report", "environment_classification.html")
    os.makedirs("report", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path}")
    print(f"Totals: Tereko {total_tereko:,} ({total_tereko/total*100:.1f}%) / "
          f"Trend {total_trend:,} ({total_trend/total*100:.1f}%) / "
          f"Mixed {total_mixed:,} ({total_mixed/total*100:.1f}%)")


if __name__ == "__main__":
    main()
