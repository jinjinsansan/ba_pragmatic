"""時間帯別 逆張りパフォーマンス分析

全シューを時間帯 (JST) ごとに集計し、
- 逆張り勝率
- OS進行速度 (7ターンあたりの負け越し率)
- テレコ出現率
- 平均テレコ寿命
を時間帯別に可視化。

「何時が勝ちやすいか」「何時にOSが進みやすいか」を特定。

Usage:
  python generate_hourly_analysis.py --vps
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

# JST offset
JST_OFFSET_HOURS = 9


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


def parse_jst_hour(started_at):
    """started_at文字列からJST時間を取得"""
    try:
        # Format: 2026-04-06T14:30:00.000000+09:00 or 2026-04-06 14:30:00
        ts = started_at.replace('T', ' ')
        hour_part = ts.split(' ')[1].split(':')[0]
        utc_hour = int(hour_part)
        if '+09' in started_at or '+9' in started_at:
            return utc_hour  # Already JST
        return (utc_hour + JST_OFFSET_HOURS) % 24
    except Exception:
        return -1


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


def analyze(shoes):
    # 時間帯別データ
    hourly = defaultdict(lambda: {
        'total_hands': 0, 'counter_wins': 0, 'counter_losses': 0,
        'shoes': 0, 'tereko_shoes': 0,
        'sets_played': 0, 'sets_won': 0, 'sets_lost': 0,  # 7ターンセットの勝ち越し/負け越し
        'tereko_durations': [],
    })

    # 日別×時間帯
    daily_hourly = defaultdict(lambda: defaultdict(lambda: {
        'wins': 0, 'losses': 0, 'hands': 0,
    }))

    TEREKO_WINDOW = 10
    TEREKO_THRESH = 0.80

    for table_name, seq, started_at in shoes:
        jst_hour = parse_jst_hour(started_at)
        if jst_hour < 0:
            continue

        date_str = started_at[:10]
        h = hourly[jst_hour]
        h['shoes'] += 1

        clean = ''.join(ch for ch in seq if ch in ('P', 'B'))
        if len(clean) < STATIC_WARMUP:
            continue

        # テレコ判定
        warmup = clean[:STATIC_WARMUP]
        pattern = classify_pattern(warmup, min_cols=3)
        is_tereko = (pattern == "テレコ+ニコ混合")
        if is_tereko:
            h['tereko_shoes'] += 1

        # テレコ寿命計測
        cols = compute_columns(clean)
        in_tereko = False
        tereko_start = 0
        hand_idx = 0
        current_cols = []
        last_side = None
        for i, ch in enumerate(clean):
            hand_idx += 1
            if ch == last_side:
                pass
            else:
                if last_side is not None:
                    current_cols.append(0)  # placeholder
                last_side = ch
            # Simplified: just use pre-computed cols
        # Use column-based tereko duration
        if len(cols) >= TEREKO_WINDOW:
            in_t = False
            t_start = 0
            for ci in range(TEREKO_WINDOW, len(cols)):
                recent = cols[ci-TEREKO_WINDOW:ci]
                short = sum(1 for L in recent if L <= 2)
                is_t = (short / len(recent)) >= TEREKO_THRESH
                if is_t and not in_t:
                    in_t = True
                    t_start = ci
                elif not is_t and in_t:
                    h['tereko_durations'].append(ci - t_start)
                    in_t = False
            if in_t:
                h['tereko_durations'].append(len(cols) - t_start)

        # 逆張り勝率 (テレコシューのみ)
        if is_tereko:
            last_nt = None
            turn_results = []
            for ch in seq:
                if ch not in ('P', 'B', 'T'):
                    continue
                if ch == 'T':
                    continue
                if last_nt is not None:
                    bet_side = 'P' if last_nt == 'B' else 'B'
                    won = (ch == bet_side)
                    h['total_hands'] += 1
                    dh = daily_hourly[date_str][jst_hour]
                    dh['hands'] += 1
                    if won:
                        h['counter_wins'] += 1
                        dh['wins'] += 1
                        turn_results.append('W')
                    else:
                        h['counter_losses'] += 1
                        dh['losses'] += 1
                        turn_results.append('L')

                    # 7ターンセット集計
                    if len(turn_results) == 7:
                        wins_in_set = turn_results.count('W')
                        h['sets_played'] += 1
                        if wins_in_set >= 4:
                            h['sets_won'] += 1
                        else:
                            h['sets_lost'] += 1
                        turn_results = []

                last_nt = ch

    return hourly, daily_hourly


def render_html(hourly, daily_hourly, total_shoes):
    # 時間帯別テーブル
    hours_data = []
    for hour in range(24):
        h = hourly[hour]
        total = h['counter_wins'] + h['counter_losses']
        wr = h['counter_wins'] / total * 100 if total > 0 else 0
        tereko_rate = h['tereko_shoes'] / h['shoes'] * 100 if h['shoes'] > 0 else 0
        avg_dur = sum(h['tereko_durations']) / len(h['tereko_durations']) if h['tereko_durations'] else 0
        set_win_rate = h['sets_won'] / h['sets_played'] * 100 if h['sets_played'] > 0 else 0
        os_tendency = h['sets_lost'] / h['sets_played'] * 100 if h['sets_played'] > 0 else 0

        hours_data.append({
            'hour': hour, 'shoes': h['shoes'], 'tereko_shoes': h['tereko_shoes'],
            'tereko_rate': tereko_rate, 'hands': total, 'wr': wr,
            'avg_duration': avg_dur, 'sets': h['sets_played'],
            'set_wr': set_win_rate, 'os_rate': os_tendency,
        })

    # メインテーブル
    main_rows = ""
    best_hour = max(hours_data, key=lambda x: x['wr'] if x['hands'] > 100 else 0)
    worst_hour = min(hours_data, key=lambda x: x['wr'] if x['hands'] > 100 else 100)

    for d in hours_data:
        wr_c = '#4ade80' if d['wr'] >= 53 else ('#fbbf24' if d['wr'] >= 51 else ('#f87171' if d['wr'] < 50 else '#e0e8f0'))
        os_c = '#4ade80' if d['os_rate'] < 40 else ('#fbbf24' if d['os_rate'] < 50 else '#f87171')
        bg = ""
        if d['hour'] == best_hour['hour'] and d['hands'] > 100:
            bg = "background:#1a3a1a;"
        elif d['hour'] == worst_hour['hour'] and d['hands'] > 100:
            bg = "background:#3a1a1a;"
        main_rows += (
            f"<tr style='{bg}'>"
            f"<td style='font-weight:bold;font-size:15px'>{d['hour']:02d}:00</td>"
            f"<td>{d['shoes']}</td>"
            f"<td>{d['tereko_shoes']} ({d['tereko_rate']:.0f}%)</td>"
            f"<td>{d['hands']:,}</td>"
            f"<td style='color:{wr_c};font-weight:bold;font-size:15px'>{d['wr']:.2f}%</td>"
            f"<td>{d['avg_duration']:.1f}</td>"
            f"<td>{d['sets']}</td>"
            f"<td>{d['set_wr']:.1f}%</td>"
            f"<td style='color:{os_c}'>{d['os_rate']:.1f}%</td>"
            f"</tr>"
        )

    # 勝率チャート (時間帯別バー)
    wr_chart = ""
    for d in hours_data:
        if d['hands'] < 10:
            continue
        bar_w = max(0, (d['wr'] - 45) * 20)
        c = '#4ade80' if d['wr'] >= 53 else ('#fbbf24' if d['wr'] >= 51 else '#f87171')
        wr_chart += f"""
<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:13px">
  <div style="min-width:50px;font-weight:bold">{d['hour']:02d}:00</div>
  <div style="flex:1;height:18px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:70px;text-align:right;color:{c};font-weight:bold">{d['wr']:.1f}%</div>
</div>"""

    # OS進行チャート
    os_chart = ""
    for d in hours_data:
        if d['sets'] < 5:
            continue
        bar_w = d['os_rate'] * 3
        c = '#4ade80' if d['os_rate'] < 40 else ('#fbbf24' if d['os_rate'] < 50 else '#f87171')
        os_chart += f"""
<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:13px">
  <div style="min-width:50px;font-weight:bold">{d['hour']:02d}:00</div>
  <div style="flex:1;height:18px;background:#1a2332;border-radius:4px;overflow:hidden">
    <div style="width:{bar_w}px;height:100%;background:{c};border-radius:4px"></div>
  </div>
  <div style="min-width:70px;text-align:right;color:{c}">{d['os_rate']:.1f}%</div>
</div>"""

    # 日別×時間帯ヒートマップデータ
    dates = sorted(daily_hourly.keys())
    heat_rows = ""
    for date in dates:
        cells = ""
        for hour in range(24):
            dh = daily_hourly[date][hour]
            total = dh['wins'] + dh['losses']
            if total < 3:
                cells += "<td style='background:#0f1419;color:#2a3441;font-size:10px'>-</td>"
            else:
                wr = dh['wins'] / total * 100
                if wr >= 55:
                    bg = "rgba(0,255,136,0.3)"
                elif wr >= 52:
                    bg = "rgba(0,255,136,0.15)"
                elif wr >= 50:
                    bg = "rgba(255,204,0,0.15)"
                else:
                    bg = "rgba(255,51,102,0.2)"
                cells += f"<td style='background:{bg};font-size:10px;text-align:center'>{wr:.0f}</td>"
        heat_rows += f"<tr><td style='font-size:11px;color:#8a96a8;white-space:nowrap'>{date}</td>{cells}</tr>"

    heat_header = "<th>日付</th>" + "".join(f"<th style='font-size:10px'>{h:02d}</th>" for h in range(24))

    # ゾーン分析
    zone_data = {
        'アジア昼 (09-15 JST)': [h for h in hours_data if 9 <= h['hour'] <= 14],
        'アジア夜 (18-24 JST)': [h for h in hours_data if 18 <= h['hour'] <= 23],
        'ヨーロッパ (01-08 JST)': [h for h in hours_data if 1 <= h['hour'] <= 8],
        '深夜 (00-01, 15-17 JST)': [h for h in hours_data if h['hour'] in (0, 15, 16, 17)],
    }
    zone_html = ""
    for zone_name, zone_hours in zone_data.items():
        total_w = sum(hourly[h['hour']]['counter_wins'] for h in zone_hours)
        total_l = sum(hourly[h['hour']]['counter_losses'] for h in zone_hours)
        total = total_w + total_l
        wr = total_w / total * 100 if total > 0 else 0
        total_sets = sum(h['sets'] for h in zone_hours)
        avg_os = sum(h['os_rate'] * h['sets'] for h in zone_hours) / total_sets if total_sets > 0 else 0
        wr_c = '#4ade80' if wr >= 53 else ('#fbbf24' if wr >= 51 else '#f87171')
        zone_html += (
            f"<tr>"
            f"<td style='font-weight:bold'>{zone_name}</td>"
            f"<td>{total:,}</td>"
            f"<td style='color:{wr_c};font-weight:bold'>{wr:.2f}%</td>"
            f"<td>{total_sets}</td>"
            f"<td>{avg_os:.1f}%</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>W. 時間帯別パフォーマンス分析</title>
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
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.green .value {{ color: #4ade80; }}
.card.red .value {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>W. 時間帯別パフォーマンス分析</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="pattern_winrate_db.html">V. パターンDB</a>
</div>

<div class="banner">
<strong>📊 JST時間帯ごとの逆張り勝率とOS進行速度を分析。</strong><br>
Evolutionの組み罫線は時間帯によって微妙に変化する。<br>
「何時が攻め時で、何時が守り時か」をデータで特定。<br>
<strong>OS進行率 = 7ターンセットで負け越した割合 (低いほど安全)。</strong>
</div>

<div class="summary">
  <div class="card green"><div class="label">最高勝率時間帯</div><div class="value">{best_hour['hour']:02d}:00 ({best_hour['wr']:.1f}%)</div></div>
  <div class="card red"><div class="label">最低勝率時間帯</div><div class="value">{worst_hour['hour']:02d}:00 ({worst_hour['wr']:.1f}%)</div></div>
</div>

<h2>1. 時間帯別 逆張り勝率</h2>
<p class="note">緑 = 53%+、黄 = 51-53%、赤 = 50%未満。バーが長いほど勝率が高い。</p>
{wr_chart}

<h2>2. 時間帯別 OS進行率</h2>
<p class="note">7ターンセットで負け越した割合。低いほどOSが進みにくい (安全)。</p>
{os_chart}

<h2>3. ゾーン別サマリー</h2>
<table>
<thead><tr><th>ゾーン</th><th>ハンド数</th><th>逆張り勝率</th><th>セット数</th><th>OS進行率</th></tr></thead>
<tbody>{zone_html}</tbody>
</table>

<h2>4. 全時間帯 詳細テーブル</h2>
<table>
<thead><tr>
  <th>時間 (JST)</th><th>シュー数</th><th>テレコ</th><th>BET数</th>
  <th>逆張り勝率</th><th>平均寿命</th>
  <th>セット数</th><th>セット勝率</th><th>OS進行率</th>
</tr></thead>
<tbody>{main_rows}</tbody>
</table>

<h2>5. 日別×時間帯 ヒートマップ</h2>
<p class="note">セル内の数字 = その日・その時間帯の逆張り勝率 (%)。緑 = 高勝率、赤 = 低勝率。</p>
<div style="overflow-x:auto;">
<table style="font-size:11px;">
<thead><tr>{heat_header}</tr></thead>
<tbody>{heat_rows}</tbody>
</table>
</div>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_hourly_analysis.py</code> / テレコ混合シュー × 逆張り / JST基準
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "hourly_analysis.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


def main():
    shoes = load_shoes()
    print(f"Loaded {len(shoes)} shoes")
    hourly, daily_hourly = analyze(shoes)
    print("\nHourly win rates (JST):")
    for hour in range(24):
        h = hourly[hour]
        total = h['counter_wins'] + h['counter_losses']
        if total > 0:
            wr = h['counter_wins'] / total * 100
            print(f"  {hour:02d}:00  {wr:.2f}%  ({total:,} hands, {h['tereko_shoes']}/{h['shoes']} tereko)")
    render_html(hourly, daily_hourly, len(shoes))


if __name__ == "__main__":
    main()
