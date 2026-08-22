"""Japanese Speed Baccarat A 専用 ドローダウン分析

損切りなしで 〇✖ ロジック (Player BET 固定) を全シュー連続適用し、
最大ドローダウン・最低残高・回復パターンを可視化する。

データソース: analytics_vps.sqlite3
出力: report/japanese_a_dd.html
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3"
TABLE_NAME = "Japanese Speed Baccarat A"
START_CAPITAL = 10_000      # 実運用相当 ($10,000)
PROFIT_PER_WIN = 50         # セット利確 ($50)
DATE_FROM = "2026-04-06"    # 集計開始日
EXTERNAL_DATA = "japanese_a_4_6_to_today.txt"  # VPS から取得した最新データ

# 〇✖ ロジックの unit 進行
SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class MaruBatsuSim:
    """損切りなしの 〇✖ シミュレータ (Player BET 固定)"""
    def __init__(self, target=PROFIT_PER_WIN):
        self.target = target
        self.reset()

    def reset(self):
        self.cumulative = 0    # チップ単位
        self.unit_idx = 0
        self.prev_os = 0
        self.sets = 0
        self.hands = 0
        self.turns = []
        self.history = []

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
        self.sets += 1
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

    def add(self, r):
        if r == 'T':
            return None
        self.hands += 1
        self.turns.append('O' if r == 'P' else 'X')
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        return None  # 損切りなし


def main():
    # データロード — 外部ファイル (VPS 最新) を優先、なければローカル DB
    shoes = []
    if os.path.exists(EXTERNAL_DATA):
        print(f"Loading from {EXTERNAL_DATA} (VPS最新データ)")
        with open(EXTERNAL_DATA, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if '|' in line:
                    seq, ts = line.rsplit('|', 1)
                    if seq and ts:
                        shoes.append((seq, ts, len(seq), 0, 0, 0))
    else:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT result_sequence, started_at, hand_count, player_count, banker_count, tie_count
            FROM shoes_analytics
            WHERE table_name = ? AND result_sequence IS NOT NULL AND started_at >= ?
            ORDER BY started_at
        """, (TABLE_NAME, DATE_FROM))
        shoes = cur.fetchall()
        conn.close()

    print(f"Loaded {len(shoes)} shoes for {TABLE_NAME} (from {DATE_FROM})")

    # シミュレーション (損切りなし、$50 利確で 1 セッション完了、次セッション継続)
    sim = MaruBatsuSim()
    balance = START_CAPITAL  # チップ単位 (1 chip = $1)
    sessions = []  # 各セッション (ターン) の記録
    equity_curve = []  # 残高推移 (1ハンドごと)
    session_start_ts = None
    session_start_hand = 0
    session_start_balance = balance
    sub_min_balance = balance  # セッション内の最低残高 (ボトム)
    overall_min_balance = balance  # 全体の最低残高
    overall_max_dd = 0  # 全体の最大ドローダウン
    peak_balance = balance

    for shoe_idx, (seq, started_at, hand_count, p, b, t) in enumerate(shoes):
        for ch in seq:
            if ch not in ('P', 'B', 'T'):
                continue
            if session_start_ts is None:
                session_start_ts = started_at
                sub_min_balance = balance + sim.cumulative

            sim.add(ch)

            # 現在の残高 = 元本 + sim.cumulative (進行中のセッション内)
            current_balance = balance + sim.cumulative
            equity_curve.append({
                'shoe_idx': shoe_idx,
                'started_at': started_at,
                'balance': current_balance,
                'session_pnl': sim.cumulative,
            })

            # セッション内最低
            if current_balance < sub_min_balance:
                sub_min_balance = current_balance

            # 全体最低・ドローダウン
            if current_balance < overall_min_balance:
                overall_min_balance = current_balance
            if current_balance > peak_balance:
                peak_balance = current_balance
            dd = peak_balance - current_balance
            if dd > overall_max_dd:
                overall_max_dd = dd

            # 利確
            if sim.cumulative >= PROFIT_PER_WIN:
                profit = sim.cumulative
                balance += profit
                if balance > peak_balance:
                    peak_balance = balance
                sessions.append({
                    'session_idx': len(sessions) + 1,
                    'started_at': session_start_ts,
                    'shoe_idx': shoe_idx,
                    'profit': profit,
                    'balance_after': balance,
                    'min_balance': sub_min_balance,
                    'sets': sim.sets,
                    'hands': sim.hands,
                    'max_dd_in_session': session_start_balance - sub_min_balance if sub_min_balance < session_start_balance else 0,
                })
                session_start_ts = None
                sim.reset()
                session_start_hand = 0
                session_start_balance = balance

    final_balance = balance + sim.cumulative
    print(f"\n=== Results ===")
    print(f"  Total shoes: {len(shoes)}")
    print(f"  Total hands: {sum(1 for s in shoes for ch in s[0] if ch in ('P','B','T'))}")
    print(f"  Sessions completed: {len(sessions)}")
    print(f"  Final balance: ${final_balance:,}")
    print(f"  Peak balance: ${peak_balance:,}")
    print(f"  Min balance: ${overall_min_balance:,}")
    print(f"  Max drawdown: ${overall_max_dd:,}")
    print(f"  Total profit: ${final_balance - START_CAPITAL:,}")

    # 最深ドローダウン時の残高 (元本に対して何 % 減ったか)
    dd_pct = overall_max_dd / START_CAPITAL * 100
    min_pct = (overall_min_balance - START_CAPITAL) / START_CAPITAL * 100

    # HTML 生成
    out_path = "report/japanese_a_dd.html"

    # equity curve の thinning (描画用、最大 500 点)
    if len(equity_curve) > 500:
        step = len(equity_curve) // 500
        thin_curve = equity_curve[::step]
    else:
        thin_curve = equity_curve

    # ドローダウン線
    peak = START_CAPITAL
    dd_curve = []
    for pt in thin_curve:
        if pt['balance'] > peak:
            peak = pt['balance']
        dd_curve.append({
            'idx': len(dd_curve),
            'balance': pt['balance'],
            'dd': peak - pt['balance'],
            'shoe_idx': pt['shoe_idx'],
        })

    # SVG 用座標生成
    width = 1200
    height = 400
    margin = 40
    inner_w = width - 2 * margin
    inner_h = height - 2 * margin

    if not dd_curve:
        return

    min_b = min(pt['balance'] for pt in dd_curve)
    max_b = max(pt['balance'] for pt in dd_curve)
    range_b = max(max_b - min_b, 1)

    points = []
    for i, pt in enumerate(dd_curve):
        x = margin + (i / max(len(dd_curve) - 1, 1)) * inner_w
        y = margin + (1 - (pt['balance'] - min_b) / range_b) * inner_h
        points.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(points)

    # ベースライン (元本)
    base_y = margin + (1 - (START_CAPITAL - min_b) / range_b) * inner_h

    # セッション一覧 HTML
    sessions_html = ""
    for s in sessions:
        ts = s['started_at'][:16].replace('T', ' ') if s['started_at'] else '-'
        dd_color = '#f87171' if s['max_dd_in_session'] >= 5000 else ('#fbbf24' if s['max_dd_in_session'] >= 1000 else '#9ca3af')
        sessions_html += (
            f"<tr>"
            f"<td class='idx'>{s['session_idx']}</td>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='shoe'>#{s['shoe_idx']+1}</td>"
            f"<td class='sets'>{s['sets']}set/{s['hands']}h</td>"
            f"<td class='profit'>+${s['profit']:,}</td>"
            f"<td class='dd' style='color:{dd_color}'>−${s['max_dd_in_session']:,}</td>"
            f"<td class='bal'>${s['balance_after']:,}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Japanese Speed Baccarat A — 損切りなし ドローダウン分析</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419;
  color: #e0e6ed;
  margin: 0;
  padding: 24px;
  line-height: 1.6;
}}
.container {{ max-width: 1300px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #ff6b6b; margin-top: 32px; }}
.banner {{
  background: #2a1a3a;
  border-left: 5px solid #ff6b6b;
  padding: 14px 18px;
  margin: 16px 0;
  font-size: 14px;
  border-radius: 4px;
  line-height: 1.7;
}}
.banner strong {{ color: #ff6b6b; }}
.summary {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin: 24px 0;
}}
.card {{
  background: #1a2332;
  border-left: 4px solid #6dd5ed;
  padding: 16px;
  border-radius: 4px;
}}
.card .label {{ font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; }}
.card .value {{ font-size: 24px; font-weight: bold; margin-top: 4px; color: #fff; }}
.card.profit .value {{ color: #4ade80; }}
.card.danger .value {{ color: #f87171; }}
.card.warn .value {{ color: #fbbf24; }}
.chart {{
  background: #1a2332;
  border-radius: 8px;
  padding: 16px;
  margin: 24px 0;
  overflow-x: auto;
}}
table.sessions {{
  width: 100%;
  border-collapse: collapse;
  background: #1a2332;
  border-radius: 8px;
  overflow: hidden;
  margin: 16px 0;
}}
table.sessions th, table.sessions td {{
  padding: 8px 12px;
  border-bottom: 1px solid #2a3441;
  font-size: 12px;
  text-align: left;
}}
table.sessions th {{
  background: #11192a;
  color: #6dd5ed;
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 0.5px;
}}
table.sessions td.idx {{ width: 50px; color: #9ca3af; }}
table.sessions td.ts {{ width: 130px; font-family: monospace; color: #9ca3af; font-size: 11px; }}
table.sessions td.shoe {{ width: 60px; color: #9ca3af; }}
table.sessions td.sets {{ width: 80px; color: #9ca3af; }}
table.sessions td.profit {{ width: 90px; color: #4ade80; font-weight: bold; text-align: right; }}
table.sessions td.dd {{ width: 100px; font-weight: bold; text-align: right; }}
table.sessions td.bal {{ width: 110px; color: #fff; text-align: right; font-weight: bold; }}
.nav {{ margin-bottom: 24px; }}
.nav a {{
  display: inline-block;
  padding: 8px 14px;
  margin-right: 6px;
  background: #1a2332;
  color: #6dd5ed;
  text-decoration: none;
  border-radius: 4px;
  font-size: 12px;
}}
.nav a:hover {{ background: #2a3441; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
<a href="index.html">← トップに戻る</a>
</div>

<h1>🎯 Japanese Speed Baccarat A — 損切りなしドローダウン分析</h1>

<div class="banner">
<strong>テーブル:</strong> Japanese Speed Baccarat A (Top4 認定の最強テーブル)<br>
<strong>戦略:</strong> 〇✖ ロジック (Player BET 固定、SEQ unit 進行)<br>
<strong>損切り:</strong> <strong>無し</strong> (破綻まで強制継続) — どこまで沈むかを観察<br>
<strong>初期資金:</strong> ${START_CAPITAL:,} (チップ単位)<br>
<strong>セッション利確:</strong> +${PROFIT_PER_WIN} で次セット<br>
<strong>データ:</strong> {len(shoes)} シュー / 全 {sum(1 for s in shoes for ch in s[0] if ch in ('P','B','T')):,} ハンド
</div>

<h2>📊 サマリー</h2>

<div class="summary">
  <div class="card"><div class="label">対象シュー</div><div class="value">{len(shoes)}</div></div>
  <div class="card"><div class="label">セッション完了</div><div class="value">{len(sessions)}</div></div>
  <div class="card profit"><div class="label">最終残高</div><div class="value">${final_balance:,}</div></div>
  <div class="card profit"><div class="label">合計損益</div><div class="value">{'+' if final_balance >= START_CAPITAL else ''}${final_balance - START_CAPITAL:,}</div></div>
</div>

<div class="summary">
  <div class="card profit"><div class="label">ピーク残高</div><div class="value">${peak_balance:,}</div></div>
  <div class="card danger"><div class="label">最低残高 (★ボトム)</div><div class="value">${overall_min_balance:,}</div></div>
  <div class="card danger"><div class="label">最大ドローダウン</div><div class="value">−${overall_max_dd:,}</div></div>
  <div class="card warn"><div class="label">DD/元本比</div><div class="value">{dd_pct:.1f}%</div></div>
</div>

<h2>📈 残高推移グラフ (損切りなし)</h2>

<div class="chart">
  <svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" style="background:#0f1419;border-radius:4px">
    <!-- ベースライン (元本) -->
    <line x1="{margin}" y1="{base_y:.1f}" x2="{width-margin}" y2="{base_y:.1f}" stroke="#fbbf24" stroke-width="1" stroke-dasharray="4,4" />
    <text x="{width-margin-5}" y="{base_y-5:.1f}" fill="#fbbf24" font-size="10" text-anchor="end">元本 ${START_CAPITAL:,}</text>

    <!-- equity curve -->
    <polyline points="{polyline}" fill="none" stroke="#4ade80" stroke-width="2" />

    <!-- Y軸ラベル -->
    <text x="5" y="{margin+10}" fill="#9ca3af" font-size="10">${max_b:,}</text>
    <text x="5" y="{height-margin}" fill="#9ca3af" font-size="10">${min_b:,}</text>

    <!-- X軸ラベル -->
    <text x="{margin}" y="{height-5}" fill="#9ca3af" font-size="10">シュー 1</text>
    <text x="{width-margin}" y="{height-5}" fill="#9ca3af" font-size="10" text-anchor="end">シュー {len(shoes)}</text>
  </svg>
  <p style="font-size:12px;color:#9ca3af;margin-top:8px;text-align:center">
    緑線 = 残高推移 / 黄破線 = 元本 ${START_CAPITAL:,} / 全 {len(equity_curve):,} ハンドのうち {len(thin_curve)} 点をプロット
  </p>
</div>

<h2>📋 セッション一覧 (利確で 1 セッション完了)</h2>

<table class="sessions">
  <thead>
    <tr>
      <th>#</th>
      <th>日時</th>
      <th>シュー</th>
      <th>セット/ハンド</th>
      <th>利益</th>
      <th>セッション内最大DD</th>
      <th>累計残高</th>
    </tr>
  </thead>
  <tbody>
    {sessions_html}
  </tbody>
</table>

<h2>💡 解釈</h2>

<div class="banner">
<strong>★ 最大ドローダウン:</strong> ${overall_max_dd:,} chip ({dd_pct:.1f}% of capital)<br>
これが意味するのは: 損切り無しで運用した場合、運転資金の {dd_pct:.1f}% を一時的に失う可能性がある。<br>
<br>
<strong>★ 最低残高:</strong> ${overall_min_balance:,} (元本比 {min_pct:+.1f}%)<br>
ボトムを確認 → 元本に対してどれだけ余裕が必要かの根拠。<br>
<br>
<strong>★ 必要な運転資金:</strong> 最低でも <strong>${overall_max_dd + START_CAPITAL // 10:,}</strong> ({dd_pct + 10:.0f}% buffer)<br>
最大DDを耐えながら、$50 利確が回るためには、ピークから最大DDを引いた値を超える必要があります。<br>
<br>
<strong>結論:</strong> Japanese Speed Baccarat A は{final_balance >= START_CAPITAL and '勝ちテーブル' or '長期で見ると損失'}だが、
最大DD ${overall_max_dd:,} を耐える資金と精神力が必要。
</div>

<div style="margin-top: 40px; padding: 16px; background: #1a2332; border-radius: 4px; font-size: 12px; color: #9ca3af;">
<strong>データソース:</strong> analytics_vps.sqlite3 / table_name = "{TABLE_NAME}"<br>
<strong>シミュレーション:</strong> MaruBatsu (Player BET 固定) + 損切りなし<br>
<strong>注意:</strong> 過去データの再現結果。将来の保証はない。
</div>

</div>
</body>
</html>
"""

    os.makedirs("report", exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✓ {out_path}")


if __name__ == "__main__":
    main()
