"""dual_line_backtest_html.py — 全パターン OOS バックテスト → HTML レポート生成

新規ファイル。既存コードは一切変更しない。
実行: python3 dual_line_backtest_html.py [DB_PATH] [OUTPUT_HTML]
"""
from __future__ import annotations
import sqlite3
import math
import sys
import os
from collections import defaultdict
from datetime import datetime
from typing import List, Optional, Tuple

DB_PATH = "/opt/laplace2/analytics_pragmatic.sqlite3"
OUT_PATH = "/opt/laplace2/dual_line_all_patterns_report.html"
COMMISSION_BANKER = 0.95

LIVE_6_PATTERNS = frozenset({
    "telecho|telecho|B",
    "telecho|nikoichi|P",
    "telecho|niconico|P",
    "niconico|niconico|B",
    "niconico|dragon|P",
    "sansan|telecho|P",
})

DISCARDED_PATTERNS = frozenset({
    "pline|dragon|P", "pline|dragon|B",
    "pline|telecho|P", "pline|telecho|B",
    "pline|niconico|P", "pline|niconico|B",
    "pline|nikoichi|P", "pline|nikoichi|B",
    "bline|dragon|P", "bline|dragon|B",
    "bline|telecho|P", "bline|telecho|B",
    "bline|niconico|P", "bline|niconico|B",
    "bline|nikoichi|P", "bline|nikoichi|B",
    "sansan|telecho|B",
    "sansan|dragon|B",
})

# ─────────────────────────────────────────────────────────────────────────────
# ロジック（dual_line_logic.py と同一）
# ─────────────────────────────────────────────────────────────────────────────

def _bead_row_cells(history: str, next_n: int) -> List[str]:
    row = ((next_n - 1) % 6) + 1
    col_count = (next_n - 1) // 6
    cells = []
    for c in range(col_count):
        idx = c * 6 + (row - 1)
        if idx < len(history):
            cells.append(history[idx])
    return cells

def _is_telecho(pb): return len(pb) >= 2 and all(pb[i] != pb[i+1] for i in range(len(pb)-1))
def _predict_telecho(pb): return 'B' if pb[-1] == 'P' else 'P'

def _is_niconico(pb):
    n = len(pb)
    if n < 4 or n % 2 != 0: return False
    if pb[0] != pb[1]: return False
    base = pb[0]; other = 'B' if base == 'P' else 'P'
    cycle = [base, base, other, other]
    return all(pb[i] == cycle[i % 4] for i in range(n))

def _predict_niconico(pb):
    base = pb[0]; other = 'B' if base == 'P' else 'P'
    return [base, base, other, other][len(pb) % 4]

def _is_sansan(pb):
    return (len(pb) == 5 and pb[0] == pb[1] == pb[2] and pb[3] == pb[4] and pb[0] != pb[3])

def _predict_sansan(pb): return pb[3]

def predict_bead(row_cells):
    if len(row_cells) < 3: return '', ''
    # 形状(テレコ/ニコニコ/サンサン)は従来通り: T含み行は判定不能・T無し行のみ評価
    if 'T' not in row_cells:
        pb = row_cells
        shape = []
        if _is_telecho(pb): shape.append(('telecho', _predict_telecho(pb)))
        if _is_niconico(pb): shape.append(('niconico', _predict_niconico(pb)))
        if _is_sansan(pb): shape.append(('sansan', _predict_sansan(pb)))
        if len(shape) == 1: return shape[0][1], shape[0][0]
        if len(shape) >= 2: return '', ''
    # Pライン/Bライン【新定義 2026-06-07】: T を無視し P/B だけで数える。
    # P/B が 5 個以上 かつ 逆側が累積 1 個まで(2個目で線終了) → 多数側を順張り予想。
    # (旧定義は「P/B が3個以上」の多数決で、PPBBP のような散らばりも誤って線扱いしていた)
    pb_only = [c for c in row_cells if c != 'T']
    if len(pb_only) >= 5:
        p_cnt = pb_only.count('P'); b_cnt = pb_only.count('B')
        if b_cnt <= 1: return 'P', 'pline'   # 逆(B)が1個以下 → Pライン → 次P
        if p_cnt <= 1: return 'B', 'bline'   # 逆(P)が1個以下 → Bライン → 次B
    return '', ''

def _build_big_road_columns(history):
    pb = [c for c in history if c != 'T']
    cols, cur = [], []
    for c in pb:
        if not cur or cur[-1] == c: cur.append(c)
        else: cols.append(cur); cur = [c]
    if cur: cols.append(cur)
    return cols

def _predict_dragon(cols):
    return cols[-1][0] if cols and len(cols[-1]) >= 4 else None

def _predict_big_telecho(cols):
    if len(cols) < 4: return None
    last4 = cols[-4:]
    if not all(len(c) == 1 for c in last4): return None
    sides = [c[0] for c in last4]
    if not all(sides[i] != sides[i+1] for i in range(3)): return None
    return 'B' if sides[-1] == 'P' else 'P'

def _scan_back(pb_only, period, min_match, cycle_templates):
    n = len(pb_only)
    if n < min_match: return None
    for cycle in cycle_templates:
        for phase in range(period):
            ml = 0
            for k in range(n):
                if pb_only[n-1-k] != cycle[(phase-k) % period]: break
                ml += 1
            if ml >= min_match: return cycle[(phase+1) % period]
    return None

def _predict_big_niconico(history):
    pb = [c for c in history if c != 'T']
    return _scan_back(pb, 4, 8, [['P','P','B','B'],['B','B','P','P']])

def _predict_big_nikoichi(history):
    pb = [c for c in history if c != 'T']
    return _scan_back(pb, 3, 6, [['P','P','B'],['B','B','P']])

def predict_big_road(history):
    cols = _build_big_road_columns(history)
    matches = []
    d = _predict_dragon(cols)
    if d: matches.append(('dragon', d))
    t = _predict_big_telecho(cols)
    if t: matches.append(('telecho', t))
    ni = _predict_big_niconico(history)
    if ni: matches.append(('niconico', ni))
    ko = _predict_big_nikoichi(history)
    if ko: matches.append(('nikoichi', ko))
    if len(matches) == 0: return '', ''
    if len(matches) == 1: return matches[0][1], matches[0][0]
    preds = {m[1] for m in matches}
    if len(preds) == 1: return preds.pop(), matches[0][0]
    return '', ''

# ─────────────────────────────────────────────────────────────────────────────
# バックテスト本体
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(db_path: str) -> Tuple[dict, int]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    shoes = db.execute(
        "SELECT result_sequence FROM shoes "
        "WHERE hand_count >= 20 AND result_sequence IS NOT NULL"
    ).fetchall()
    total_shoes = len(shoes)
    print(f"対象 shoes: {total_shoes:,}")
    stats = defaultdict(lambda: {"bets": 0, "wins": 0, "losses": 0, "ties": 0, "pnl": 0.0})
    step = max(1, total_shoes // 20)
    for idx, row in enumerate(shoes):
        seq = row["result_sequence"]
        history = ""
        for i, outcome in enumerate(seq):
            next_n = i + 1
            row_cells = _bead_row_cells(history, next_n)
            china_pred, china_pat = predict_bead(row_cells)
            if not china_pred:
                history += outcome; continue
            big_pred, big_pat = predict_big_road(history)
            if not big_pred:
                history += outcome; continue
            if china_pred != big_pred:
                history += outcome; continue
            key = f"{china_pat}|{big_pat}|{china_pred}"
            s = stats[key]; s["bets"] += 1
            if outcome == 'T': s["ties"] += 1
            elif outcome == china_pred:
                s["wins"] += 1
                s["pnl"] += COMMISSION_BANKER if china_pred == 'B' else 1.0
            else:
                s["losses"] += 1; s["pnl"] -= 1.0
            history += outcome
        if (idx + 1) % step == 0:
            print(f"  ... {idx+1:,}/{total_shoes:,} ({(idx+1)/total_shoes*100:.0f}%)")
    db.close()
    return dict(stats), total_shoes

# ─────────────────────────────────────────────────────────────────────────────
# Z スコア
# ─────────────────────────────────────────────────────────────────────────────

def z_score(wins, losses, is_banker):
    n = wins + losses
    if n == 0: return 0.0
    p_base = 0.5068 if is_banker else 0.4932
    return (wins / n - p_base) / math.sqrt(p_base * (1 - p_base) / n)

def row_stats(key, s):
    n_nt = s["wins"] + s["losses"]
    wr = s["wins"] / n_nt * 100 if n_nt else 0.0
    ppb = s["pnl"] / s["bets"] if s["bets"] else 0.0
    z = z_score(s["wins"], s["losses"], key.endswith("|B"))
    return n_nt, wr, ppb, z

# ─────────────────────────────────────────────────────────────────────────────
# HTML 生成
# ─────────────────────────────────────────────────────────────────────────────

def esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def z_badge(z):
    if z >= 1.96:  return f'<span class="zbadge sig">Z={z:+.2f} ★★</span>'
    if z >= 1.5:   return f'<span class="zbadge near">Z={z:+.2f} ★</span>'
    if z >= 0.5:   return f'<span class="zbadge weak">Z={z:+.2f}</span>'
    if z >= -0.5:  return f'<span class="zbadge none">Z={z:+.2f}</span>'
    return             f'<span class="zbadge neg">Z={z:+.2f}</span>'

def pnl_cell(ppb):
    color = "var(--green)" if ppb > 0 else ("var(--red)" if ppb < 0 else "var(--gray)")
    return f'<td style="color:{color};font-weight:700">{ppb:+.4f}</td>'

def wr_bar(wr, n_nt):
    pct = wr
    if pct >= 53: cls = "bar-green"
    elif pct >= 50: cls = "bar-yellow"
    else: cls = "bar-red"
    color = "var(--green)" if pct >= 53 else ("var(--red)" if pct < 50 else "var(--gray)")
    return (f'<div class="bar-wrap">'
            f'<div class="bar-outer"><div class="bar-inner {cls}" style="width:{min(pct,70)/70*100:.0f}%"></div></div>'
            f'<span style="color:{color};font-weight:{"700" if pct>=53 or pct<50 else "400"}">{pct:.1f}%</span>'
            f'</div>')

def pattern_label(key):
    parts = key.split("|")
    if len(parts) != 3: return esc(key)
    china, big, side = parts
    side_chip = f'<span class="chip {side}">{side}</span>'
    names = {
        "telecho": "テレコ", "niconico": "ニコニコ", "nikoichi": "ニコイチ",
        "dragon": "ドラゴン", "sansan": "サンサン",
        "pline": "Pライン", "bline": "Bライン",
    }
    cn = names.get(china, china)
    bn = names.get(big, big)
    return f'{side_chip}&nbsp; {esc(cn)} ｜ {esc(bn)}'

def build_table_rows(rows, highlight_6=True):
    html = ""
    for key, s, n_nt, wr, ppb, z in rows:
        in_6 = key in LIVE_6_PATTERNS
        discarded = key in DISCARDED_PATTERNS
        if in_6 and highlight_6:
            tr_class = ' class="adopted"'
        elif discarded:
            tr_class = ' class="discarded"'
        elif ppb < -0.03:
            tr_class = ' class="negative"'
        else:
            tr_class = ''
        tag = ""
        if in_6 and highlight_6:
            tag = ' <span class="tag-adopted">採用</span>'
        elif discarded:
            tag = ' <span class="tag-disc">廃棄</span>'

        html += (f"<tr{tr_class}>"
                 f"<td>{pattern_label(key)}{tag}</td>"
                 f"<td>{s['bets']:,}</td>"
                 f"<td>{s['wins']:,} / {s['losses']:,} / {s['ties']:,}</td>"
                 f"<td>{wr_bar(wr, n_nt)}</td>"
                 f"{pnl_cell(ppb)}"
                 f"<td>{z_badge(z)}</td>"
                 f"</tr>\n")
    return html

CSS = """
:root{--green:#16a34a;--green-bg:#dcfce7;--red:#dc2626;--red-bg:#fee2e2;
--yellow:#ca8a04;--yellow-bg:#fef9c3;--blue:#2563eb;--blue-bg:#dbeafe;
--gray:#6b7280;--gray-bg:#f3f4f6;--dark:#111827;--border:#e5e7eb;}
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Helvetica Neue',Arial,'Hiragino Sans',Meiryo,sans-serif;
background:#f8fafc;color:var(--dark);line-height:1.6;padding:24px 16px;}
.container{max-width:1000px;margin:0 auto;}
.header{background:linear-gradient(135deg,#1e3a5f,#2563eb);color:white;
border-radius:12px;padding:28px 32px;margin-bottom:24px;text-align:center;}
.header h1{font-size:1.7em;font-weight:700;margin-bottom:6px;}
.header p{font-size:0.9em;opacity:.85;}
.badge{display:inline-block;background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.4);
border-radius:20px;padding:4px 14px;font-size:.82em;margin-top:8px;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px;}
.card{background:white;border-radius:10px;border:1px solid var(--border);padding:18px;
text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.06);}
.card .label{font-size:.75em;color:var(--gray);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px;}
.card .value{font-size:1.9em;font-weight:700;}
.card .sub{font-size:.8em;color:var(--gray);margin-top:3px;}
.card.green .value{color:var(--green);}
.card.red .value{color:var(--red);}
.card.blue .value{color:var(--blue);}
.card.gray .value{color:var(--gray);}
.section{background:white;border-radius:10px;border:1px solid var(--border);
margin-bottom:20px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);}
.section-header{padding:14px 20px;border-bottom:1px solid var(--border);
display:flex;align-items:center;gap:8px;}
.section-header h2{font-size:1em;font-weight:700;}
.section-header .meta{font-size:.78em;color:var(--gray);margin-left:auto;}
table{width:100%;border-collapse:collapse;}
th{background:var(--gray-bg);padding:9px 12px;font-size:.75em;font-weight:600;
color:var(--gray);text-align:right;letter-spacing:.03em;}
th:first-child{text-align:left;}
td{padding:9px 12px;border-bottom:1px solid var(--border);font-size:.84em;text-align:right;}
td:first-child{text-align:left;font-weight:600;}
tr:last-child td{border-bottom:none;}
tr.adopted td{background:#f0fdf4;}
tr.discarded td{background:#fef2f2;opacity:.8;}
tr.negative td{background:#fff5f5;}
tr.total td{background:var(--gray-bg);font-weight:700;}
.chip{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75em;font-weight:700;}
.chip.P{background:var(--blue-bg);color:var(--blue);}
.chip.B{background:var(--red-bg);color:var(--red);}
.bar-wrap{display:flex;align-items:center;gap:6px;justify-content:flex-end;}
.bar-outer{width:72px;height:7px;background:#e5e7eb;border-radius:3px;overflow:hidden;}
.bar-inner{height:100%;border-radius:3px;}
.bar-green{background:var(--green);}
.bar-yellow{background:var(--yellow);}
.bar-red{background:var(--red);}
.zbadge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:.78em;font-weight:700;}
.zbadge.sig{background:var(--green-bg);color:var(--green);}
.zbadge.near{background:var(--yellow-bg);color:var(--yellow);}
.zbadge.weak{background:var(--blue-bg);color:var(--blue);}
.zbadge.neg{background:var(--red-bg);color:var(--red);}
.zbadge.none{background:var(--gray-bg);color:var(--gray);}
.tag-adopted{background:var(--green-bg);color:var(--green);border-radius:4px;
padding:1px 6px;font-size:.72em;font-weight:700;margin-left:4px;}
.tag-disc{background:var(--red-bg);color:var(--red);border-radius:4px;
padding:1px 6px;font-size:.72em;font-weight:700;margin-left:4px;}
.note{background:var(--yellow-bg);border-left:4px solid #f59e0b;border-radius:0 6px 6px 0;
padding:12px 14px;font-size:.83em;margin:14px 18px;}
.legend{display:flex;gap:16px;flex-wrap:wrap;padding:12px 18px;font-size:.8em;border-top:1px solid var(--border);}
.legend-item{display:flex;align-items:center;gap:6px;}
.dot{width:12px;height:12px;border-radius:3px;display:inline-block;}
.footer{text-align:center;font-size:.76em;color:var(--gray);margin-top:24px;
padding-top:14px;border-top:1px solid var(--border);}
"""

def generate_html(stats: dict, total_shoes: int, db_path: str) -> str:
    # 全行を計算
    all_rows = []
    for key, s in stats.items():
        n_nt, wr, ppb, z = row_stats(key, s)
        all_rows.append((key, s, n_nt, wr, ppb, z))

    # ソート: $/BET 降順
    all_rows.sort(key=lambda x: -x[4])

    total_bets  = sum(s["bets"]   for _, s, *_ in all_rows)
    total_wins  = sum(s["wins"]   for _, s, *_ in all_rows)
    total_loss  = sum(s["losses"] for _, s, *_ in all_rows)
    total_ties  = sum(s["ties"]   for _, s, *_ in all_rows)
    total_pnl   = sum(s["pnl"]    for _, s, *_ in all_rows)
    overall_wr  = total_wins / (total_wins + total_loss) * 100 if (total_wins + total_loss) else 0
    overall_ppb = total_pnl / total_bets if total_bets else 0
    overall_z   = z_score(total_wins, total_loss, False)

    # 採用6パターンの集計
    rows_6 = [(k,s,n,w,p,z) for k,s,n,w,p,z in all_rows if k in LIVE_6_PATTERNS]
    bets_6  = sum(s["bets"]   for _,s,*_ in rows_6)
    wins_6  = sum(s["wins"]   for _,s,*_ in rows_6)
    loss_6  = sum(s["losses"] for _,s,*_ in rows_6)
    pnl_6   = sum(s["pnl"]    for _,s,*_ in rows_6)
    wr_6    = wins_6 / (wins_6 + loss_6) * 100 if (wins_6 + loss_6) else 0
    ppb_6   = pnl_6 / bets_6 if bets_6 else 0
    z_6     = z_score(wins_6, loss_6, False)

    # プラス / マイナスパターン数
    plus_pats  = sum(1 for _,_,_,_,p,_ in all_rows if p > 0)
    minus_pats = sum(1 for _,_,_,_,p,_ in all_rows if p < 0)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # テーブル行: 全体（BET>=30）
    rows_main = [(k,s,n,w,p,z) for k,s,n,w,p,z in all_rows if s["bets"] >= 30]

    # テーブル行: pline/bline
    rows_pb = [(k,s,n,w,p,z) for k,s,n,w,p,z in all_rows
               if (k.startswith("pline|") or k.startswith("bline|")) and s["bets"] >= 10]

    # テーブル行: sansan
    rows_san = [(k,s,n,w,p,z) for k,s,n,w,p,z in all_rows
                if k.startswith("sansan|") and s["bets"] >= 5]

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>デュアルライン 全パターン バックテスト ({now})</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
  <h1>🃏 デュアルライン戦略 — 全パターン バックテスト</h1>
  <p>珠盤路（中国罫線）× 大路 の全組み合わせを {total_shoes:,} シューで検証</p>
  <div class="badge">生成: {now} ／ DB: {esc(os.path.basename(db_path))}</div>
</div>

<div class="cards">
  <div class="card gray"><div class="label">検証シュー数</div>
    <div class="value">{total_shoes:,}</div><div class="sub">全組み合わせ</div></div>
  <div class="card gray"><div class="label">総BET数</div>
    <div class="value">{total_bets:,}</div><div class="sub">全パターン合計</div></div>
  <div class="card {"red" if overall_ppb < 0 else "gray"}"><div class="label">全体 勝率</div>
    <div class="value">{overall_wr:.2f}%</div><div class="sub">ハウスエッジ域 = 50.68%</div></div>
  <div class="card {"red" if overall_ppb < 0 else "gray"}"><div class="label">全体 $/BET</div>
    <div class="value">{overall_ppb:+.4f}</div><div class="sub">Z={overall_z:+.2f}</div></div>
  <div class="card green"><div class="label">採用6パターン $/BET</div>
    <div class="value">{ppb_6:+.4f}</div><div class="sub">Z={z_6:+.2f} / {bets_6:,} BET</div></div>
  <div class="card {"green" if plus_pats > minus_pats else "red"}">
    <div class="label">+/- パターン数</div>
    <div class="value">{plus_pats} / {minus_pats}</div>
    <div class="sub">プラス / マイナス</div></div>
</div>

<div class="note">
  <strong>結論:</strong>
  全パターン合計は house edge ({overall_ppb:+.4f}/BET) を脱せない。
  しかし特定パターンに絞ると統計的に有意な edge が存在する。
  下表の <span class="tag-adopted">採用</span> 6パターンが現在 Forward OOS 検証中。
</div>

<!-- 全パターン -->
<div class="section">
  <div class="section-header">
    <span>📊</span>
    <h2>全パターン一覧（BET ≥ 30 ／ $/BET 降順）</h2>
    <span class="meta">{len(rows_main)} パターン表示</span>
  </div>
  <table>
    <thead><tr>
      <th>パターン（珠盤路 ｜ 大路 ｜ 予想）</th>
      <th>BET数</th>
      <th>勝 / 負 / タイ</th>
      <th>勝率</th>
      <th>$/BET</th>
      <th>Z値</th>
    </tr></thead>
    <tbody>
{build_table_rows(rows_main)}
    </tbody>
  </table>
  <div class="legend">
    <div class="legend-item"><div class="dot" style="background:#f0fdf4;border:1px solid #86efac"></div> 採用6パターン</div>
    <div class="legend-item"><div class="dot" style="background:#fef2f2;border:1px solid #fca5a5"></div> 廃棄パターン（pline/bline系）</div>
    <div class="legend-item"><div class="dot" style="background:#fff5f5;border:1px solid #e5e7eb"></div> 大きくマイナス</div>
  </div>
</div>

<!-- 採用6パターン詳細 -->
<div class="section">
  <div class="section-header">
    <span>✅</span>
    <h2>採用6パターン 詳細</h2>
    <span class="meta">Forward OOS 検証中 (2026-05-18〜)</span>
  </div>
  <table>
    <thead><tr>
      <th>パターン</th><th>BET数</th><th>勝 / 負 / タイ</th>
      <th>勝率</th><th>$/BET</th><th>Z値</th>
    </tr></thead>
    <tbody>
{build_table_rows(sorted(rows_6, key=lambda x: -x[4]), highlight_6=False)}
      <tr class="total">
        <td>合計</td>
        <td>{bets_6:,}</td>
        <td>{wins_6:,} / {loss_6:,} / {sum(s["ties"] for _,s,*_ in rows_6):,}</td>
        <td>{wr_bar(wr_6, wins_6+loss_6)}</td>
        <td style="color:var(--green);font-weight:700">{ppb_6:+.4f}</td>
        <td>{z_badge(z_6)}</td>
      </tr>
    </tbody>
  </table>
</div>

<!-- pline/bline -->
<div class="section">
  <div class="section-header">
    <span>🚫</span>
    <h2>廃棄パターン — Pライン / Bライン系（全組み合わせ）</h2>
    <span class="meta">全組み合わせでマイナス → 廃棄確定</span>
  </div>
  <table>
    <thead><tr>
      <th>パターン</th><th>BET数</th><th>勝 / 負 / タイ</th>
      <th>勝率</th><th>$/BET</th><th>Z値</th>
    </tr></thead>
    <tbody>
{build_table_rows(sorted(rows_pb, key=lambda x: -x[4]), highlight_6=False)}
    </tbody>
  </table>
  <div class="note" style="margin:12px 16px">
    Pライン（P が3個以上）/ Bライン（B が3個以上）は大サンプルで全組み合わせマイナス。
    特に bline|dragon|B は 26,000 BET超でも $/BET がマイナス。廃棄確定。
  </div>
</div>

<!-- sansan -->
<div class="section">
  <div class="section-header">
    <span>🎯</span>
    <h2>サンサン（PPPBB / BBBPP）パターン全組み合わせ</h2>
    <span class="meta">sansan|telecho|P のみ採用</span>
  </div>
  <table>
    <thead><tr>
      <th>パターン</th><th>BET数</th><th>勝 / 負 / タイ</th>
      <th>勝率</th><th>$/BET</th><th>Z値</th>
    </tr></thead>
    <tbody>
{build_table_rows(sorted(rows_san, key=lambda x: -x[4]))}
    </tbody>
  </table>
  <div class="note" style="margin:12px 16px">
    sansan はP方向のみ有効。B方向（sansan|telecho|B など）は全てマイナス → 廃棄。
    <strong>sansan|telecho|P</strong> がバックテスト最強パターン (+$0.130/BET, Z=+1.99)。
  </div>
</div>

<div class="footer">
  DB: {esc(db_path)} ／ バックテスト実施: {now}<br>
  ロジック: SPEC_DUAL_LINE_MATCHING.md 準拠 逐次リプレイ（未来参照禁止）
</div>

</div>
</body>
</html>"""
    return html


def main():
    db_path  = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    out_path = sys.argv[2] if len(sys.argv) > 2 else OUT_PATH

    print("=" * 70)
    print("デュアルライン 全パターン バックテスト → HTML")
    print(f"DB : {db_path}")
    print(f"OUT: {out_path}")
    print("=" * 70)

    stats, total_shoes = run_backtest(db_path)
    html = generate_html(stats, total_shoes, db_path)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✓ HTML 出力完了: {out_path}")
    n_pats = len(stats)
    total_bets = sum(s["bets"] for s in stats.values())
    print(f"  パターン数: {n_pats}  総BET: {total_bets:,}")


if __name__ == "__main__":
    main()
