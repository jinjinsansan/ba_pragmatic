"""シュー内パターン変化分析 — 1分割/2分割/4分割

各シューを 1/2/4 区間に分割し、区間ごとにパターン分類。
隣接区間でパターンが変わったかを判定し、変化パターンを集計。

分類:
  - 1分割型: 全体を通して同じパターン (変化0回)
  - 2分割型: 前半/後半で1回パターンが変わる (変化1回)
  - 4分割型: 4区間中2-3回パターンが変わる
  - 混合型:  区間ごとに異なるパターンが入り混じる

出力:
  - report/pattern_shift.html

Usage:
  python generate_pattern_shift.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
MIN_HANDS_PER_SHOE = 50
DATE_FROM = "2026-04-06"


def load_shoes():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS_PER_SHOE, DATE_FROM)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def split_seq(seq, n_parts):
    """seq を n_parts 等分して各区間のパターンを返す"""
    clean = strip_ties(seq)
    if len(clean) < n_parts * 5:  # 各区間最低5ハンド
        return None
    part_len = len(clean) // n_parts
    parts = []
    for i in range(n_parts):
        start = i * part_len
        end = start + part_len if i < n_parts - 1 else len(clean)
        part_seq = clean[start:end]
        pattern = classify_pattern(part_seq, min_cols=3)
        parts.append(pattern)
    return parts


def count_changes(parts):
    """隣接区間間の変化回数"""
    changes = 0
    for i in range(1, len(parts)):
        if parts[i] != parts[i - 1]:
            changes += 1
    return changes


def classify_shift_type(parts_2, parts_4):
    """シューの変化タイプを判定"""
    if parts_2 is None or parts_4 is None:
        return "判定不能"

    changes_2 = count_changes(parts_2)
    changes_4 = count_changes(parts_4)

    if changes_4 == 0:
        return "1分割型 (変化なし)"
    elif changes_4 == 1 and changes_2 == 1:
        return "2分割型 (1回変化)"
    elif changes_4 == 1 and changes_2 == 0:
        return "4分割型 (微細変化)"
    elif changes_4 >= 2:
        return f"多変化型 ({changes_4}回変化)"
    else:
        return "2分割型 (1回変化)"


def transition_label(parts):
    """遷移ラベルを生成 (例: 縦流れ→テレコ+ニコ混合)"""
    return " → ".join(parts)


def short_pattern(p):
    """パターン名を短縮"""
    mapping = {
        "テレコ+ニコ混合": "テレコ混合",
        "テレコ崩れ": "テレコ崩れ",
        "縦流れ": "縦流れ",
        "ブリッジ": "ブリッジ",
        "不規則": "不規則",
        "偏在": "偏在",
        "不明": "不明",
        "ニコニコ・ニコイチ": "ニコニコ",
    }
    return mapping.get(p, p)


def main():
    print(f"Loading {DB_PATH} (from {DATE_FROM})...")
    shoes = load_shoes()
    print(f"  {len(shoes)} shoes loaded")

    # 分析
    results = []
    shift_type_counter = Counter()
    transition_2_counter = Counter()
    transition_4_counter = Counter()
    table_shift_types = defaultdict(lambda: Counter())

    for table_name, seq, started_at in shoes:
        parts_1 = split_seq(seq, 1)
        parts_2 = split_seq(seq, 2)
        parts_4 = split_seq(seq, 4)

        if parts_2 is None or parts_4 is None:
            continue

        whole_pattern = parts_1[0] if parts_1 else classify_pattern(seq)
        shift_type = classify_shift_type(parts_2, parts_4)
        changes_2 = count_changes(parts_2)
        changes_4 = count_changes(parts_4)

        results.append({
            'table': table_name,
            'started_at': started_at,
            'whole': whole_pattern,
            'parts_2': parts_2,
            'parts_4': parts_4,
            'shift_type': shift_type,
            'changes_2': changes_2,
            'changes_4': changes_4,
            'hands': sum(1 for ch in seq if ch in ('P', 'B', 'T')),
        })

        shift_type_counter[shift_type] += 1
        table_shift_types[table_name][shift_type] += 1

        if changes_2 > 0:
            label = f"{short_pattern(parts_2[0])} → {short_pattern(parts_2[1])}"
            transition_2_counter[label] += 1

        for i in range(1, len(parts_4)):
            if parts_4[i] != parts_4[i - 1]:
                label = f"{short_pattern(parts_4[i-1])} → {short_pattern(parts_4[i])}"
                transition_4_counter[label] += 1

    total = len(results)
    print(f"  {total} shoes analyzed")

    # ──────────────────────────────────────────
    # HTML
    # ──────────────────────────────────────────

    # 1. 分割型の集計
    shift_type_html = ""
    for st, cnt in shift_type_counter.most_common():
        pct = cnt / total * 100
        bar_w = pct * 3
        color = "#4ade80" if "変化なし" in st else ("#fbbf24" if "1回" in st else "#f87171")
        shift_type_html += f"""
<div class="bar-row">
  <div class="bar-label">{st}</div>
  <div class="bar-wrap"><div class="bar" style="width:{bar_w}px;background:{color}"></div></div>
  <div class="bar-count">{cnt} ({pct:.1f}%)</div>
</div>"""

    # 2. 2分割遷移マトリクス
    all_patterns = sorted(set(
        short_pattern(p)
        for r in results
        for p in r['parts_2']
    ))

    matrix_2_data = defaultdict(lambda: defaultdict(int))
    for r in results:
        if r['changes_2'] > 0:
            f = short_pattern(r['parts_2'][0])
            t = short_pattern(r['parts_2'][1])
            matrix_2_data[f][t] += 1

    matrix_2_header = "<th>前半 ＼ 後半</th>" + "".join(f"<th>{p}</th>" for p in all_patterns)
    matrix_2_rows = ""
    for pf in all_patterns:
        cells = ""
        for pt in all_patterns:
            v = matrix_2_data[pf][pt]
            if v > 0:
                bg = "rgba(74,222,128,0.15)" if v >= 10 else ("rgba(251,191,36,0.15)" if v >= 5 else "")
                cells += f"<td style='background:{bg};font-weight:bold'>{v}</td>"
            else:
                cells += "<td style='color:#555'>-</td>"
        matrix_2_rows += f"<tr><th>{pf}</th>{cells}</tr>"

    # 3. 4分割遷移ランキング (上位30)
    trans_4_html = ""
    for label, cnt in transition_4_counter.most_common(30):
        pct = cnt / total * 100
        bar_w = pct * 5
        trans_4_html += f"""
<div class="bar-row">
  <div class="bar-label" style="min-width:280px">{label}</div>
  <div class="bar-wrap"><div class="bar" style="width:{bar_w}px;background:#6dd5ed"></div></div>
  <div class="bar-count">{cnt} ({pct:.1f}%)</div>
</div>"""

    # 4. テーブル別傾向 (変化なし率でソート)
    table_summary_html = ""
    table_list = []
    for tn, type_counts in table_shift_types.items():
        total_shoes = sum(type_counts.values())
        no_change = sum(v for k, v in type_counts.items() if "変化なし" in k)
        change_1 = sum(v for k, v in type_counts.items() if "1回変化" in k)
        change_multi = sum(v for k, v in type_counts.items() if "変化" in k and "なし" not in k and "1回" not in k)
        table_list.append({
            'name': tn,
            'total': total_shoes,
            'no_change': no_change,
            'change_1': change_1,
            'change_multi': change_multi,
            'no_change_pct': no_change / total_shoes * 100 if total_shoes > 0 else 0,
        })

    table_list.sort(key=lambda x: -x['no_change_pct'])
    for t in table_list:
        nc_color = '#4ade80' if t['no_change_pct'] >= 50 else ('#fbbf24' if t['no_change_pct'] >= 30 else '#f87171')
        table_summary_html += (
            f"<tr>"
            f"<td class='tname'>{t['name']}</td>"
            f"<td>{t['total']}</td>"
            f"<td style='color:#4ade80;font-weight:bold'>{t['no_change']} ({t['no_change_pct']:.0f}%)</td>"
            f"<td style='color:#fbbf24'>{t['change_1']}</td>"
            f"<td style='color:#f87171'>{t['change_multi']}</td>"
            f"</tr>"
        )

    # 5. 全シュー一覧 (4分割パターン遷移表示)
    shoe_list_html = ""
    for r in results:
        p4 = " → ".join(short_pattern(p) for p in r['parts_4'])
        changes = r['changes_4']
        if changes == 0:
            cls = "stable"
            badge = "✅ 安定"
        elif changes == 1:
            cls = "shift1"
            badge = "⚡ 1変化"
        else:
            cls = "shift_multi"
            badge = f"🔄 {changes}変化"

        ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
        shoe_list_html += (
            f"<tr class='{cls}'>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tname'>{r['table']}</td>"
            f"<td>{short_pattern(r['whole'])}</td>"
            f"<td class='transition'>{p4}</td>"
            f"<td>{badge}</td>"
            f"<td>{r['hands']}h</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>L. シュー内パターン変化分析</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419;
  color: #e0e6ed;
  margin: 0;
  padding: 24px;
  line-height: 1.5;
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
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.7;
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
.card.yellow .value {{ color: #fbbf24; }}
.card.red .value {{ color: #f87171; }}

.bar-row {{
  display: flex; align-items: center; gap: 12px; margin: 6px 0; font-size: 14px;
}}
.bar-label {{ min-width: 200px; color: #e0e6ed; }}
.bar-wrap {{ flex: 1; height: 22px; background: #1a2332; border-radius: 4px; overflow: hidden; }}
.bar {{ height: 100%; border-radius: 4px; min-width: 2px; }}
.bar-count {{ min-width: 100px; text-align: right; color: #8a96a8; font-size: 13px; }}

table {{
  width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0;
}}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441; position: sticky; top: 0;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
td.tname {{ font-weight: bold; color: #ffd700; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
td.transition {{ font-family: monospace; font-size: 12px; }}
tr.stable td.transition {{ color: #4ade80; }}
tr.shift1 td.transition {{ color: #fbbf24; }}
tr.shift_multi td.transition {{ color: #f87171; }}

.matrix {{ font-size: 12px; }}
.matrix th {{ background: #11192a; padding: 6px 8px; text-align: center; font-size: 11px; }}
.matrix td {{ text-align: center; padding: 6px 8px; }}

.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>L. シュー内パターン変化分析</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
</div>

<div class="banner">
<strong>📊 各シューを1/2/4区間に分割し、区間ごとにパターンを判定。</strong><br>
シュー内でパターンが変化する頻度と方向を分析。<br>
「前半 縦流れ → 後半 テレコ」のような遷移を全テーブル・全シューで集計。<br>
データ: {DATE_FROM} 〜 本日 / {total:,} シュー / 全62テーブル
</div>

<div class="summary">
  <div class="card">
    <div class="label">分析シュー数</div>
    <div class="value">{total:,}</div>
  </div>
  <div class="card green">
    <div class="label">変化なし (1分割型)</div>
    <div class="value">{shift_type_counter.get("1分割型 (変化なし)", 0):,}</div>
  </div>
  <div class="card yellow">
    <div class="label">1回変化 (2分割型)</div>
    <div class="value">{shift_type_counter.get("2分割型 (1回変化)", 0):,}</div>
  </div>
  <div class="card red">
    <div class="label">多変化 (2回+)</div>
    <div class="value">{sum(v for k,v in shift_type_counter.items() if "多変化" in k or "微細" in k):,}</div>
  </div>
</div>

<h2>1. 分割型の分布</h2>
<p class="note">4分割したときの変化回数で分類。「変化なし」はシュー全体が同じパターン。</p>
{shift_type_html}

<h2>2. 2分割 遷移マトリクス (前半 → 後半)</h2>
<p class="note">前半と後半でパターンが変わったシューのみ集計。行 = 前半、列 = 後半。</p>
<table class="matrix">
<thead><tr>{matrix_2_header}</tr></thead>
<tbody>{matrix_2_rows}</tbody>
</table>

<h2>3. 4分割 遷移ランキング (隣接区間間)</h2>
<p class="note">4区間の隣接ペアでパターンが変わった場合の遷移パターン。出現数上位30。</p>
{trans_4_html}

<h2>4. テーブル別 変化傾向</h2>
<p class="note">テーブルごとの「変化なし率」でソート。変化なし率が高いテーブル = パターンが安定。</p>
<table>
<thead><tr>
  <th>テーブル名</th>
  <th>シュー数</th>
  <th>変化なし</th>
  <th>1回変化</th>
  <th>多変化</th>
</tr></thead>
<tbody>{table_summary_html}</tbody>
</table>

<h2>5. 全シュー 4分割パターン遷移一覧</h2>
<p class="note">各シューの4区間パターン遷移を表示。✅=安定 ⚡=1回変化 🔄=多変化</p>
<table>
<thead><tr>
  <th>日時</th>
  <th>テーブル</th>
  <th>全体</th>
  <th>4分割遷移</th>
  <th>判定</th>
  <th>ハンド</th>
</tr></thead>
<tbody>{shoe_list_html}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_pattern_shift.py</code> / pattern_classifier.classify_pattern() 使用
</p>

</div>
</body>
</html>
"""
    out_path = os.path.join("report", "pattern_shift.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")

    # サマリー表示
    print(f"\n=== 分割型の分布 ===")
    for st, cnt in shift_type_counter.most_common():
        print(f"  {st}: {cnt} ({cnt/total*100:.1f}%)")

    print(f"\n=== 2分割 遷移 Top 10 ===")
    for label, cnt in transition_2_counter.most_common(10):
        print(f"  {label}: {cnt}")

    print(f"\n=== 4分割 遷移 Top 10 ===")
    for label, cnt in transition_4_counter.most_common(10):
        print(f"  {label}: {cnt}")


if __name__ == "__main__":
    main()
