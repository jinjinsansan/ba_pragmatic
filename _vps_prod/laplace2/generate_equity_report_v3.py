"""Banker unit 増額版 equity report (v3) — 比較表示

通常版 (Banker 通常 unit) と Banker unit 増額版 (×1/0.95 ≈ 1.0526) を
テーブルごとに横並び比較する HTML を生成。

順張り、逆張り両方を生成。

unit 増額の仕組み:
  - Banker BET 時の unit を base_unit / 0.95 ≈ 1.0526 倍に拡大
  - 勝った時: 1.0526 × 0.95 = base_unit (Player と等価)
  - 負けた時: -1.0526 × base_unit (5.3% 大きい損失)
  - → 勝率 50% なら期待値同じ、それ以外は分散増加

データソース: analytics_vps.sqlite3 (--vps オプション)
出力:
  - report/equity_per_table_trend_comp.html   (順張り 比較)
  - report/equity_per_table_counter_comp.html (逆張り 比較)

Usage:
  python generate_equity_report_v3.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
START_CAPITAL = 10000
PROFIT_PER_WIN = 50
LOSS_PER_LOSS = 3000
BANKER_COMMISSION = 0.05
BANKER_UNIT_MULT = 1.0 / (1.0 - BANKER_COMMISSION)  # ≈ 1.0526

MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


# ─────────────────────────────────────────────
# シミュレータ (Banker unit 増額対応)
# ─────────────────────────────────────────────
class MaruBatsuSim3:
    """順張り/逆張り 対応 + Banker unit 増額オプション。

    bet_strategy: 'trend' (順張り) or 'counter' (逆張り)
    compensate_banker: True なら Banker BET 時に unit を 1/0.95 倍 (≈1.0526)
    """

    def __init__(self, bet_strategy='trend', compensate_banker=False,
                 target=PROFIT_PER_WIN, lc=LOSS_PER_LOSS):
        assert bet_strategy in ('trend', 'counter')
        self.bet_strategy = bet_strategy
        self.compensate_banker = compensate_banker
        self.target = target
        self.lc = lc
        self.reset()

    def reset(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.sets = 0
        self.hands = 0
        self.banker_extra_bet = 0.0  # Banker 増額で余分に置いた累計額
        self.turns = []  # (outcome, side, base_unit)
        self.max_dd = 0
        self.peak = 0
        self.history = []
        self.last_non_tie = None
        self.streak_len = 0

    def _decide_bet_side(self):
        if self.last_non_tie is None:
            return 'P'
        if self.bet_strategy == 'trend':
            return self.last_non_tie
        else:
            return 'B' if self.last_non_tie == 'P' else 'P'

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
        wins = sum(1 for t in self.turns if t[0] == 'O')
        losses = 7 - wins
        diff = wins - losses
        base_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]

        money = 0.0
        for outcome, side, _ in self.turns:
            # Banker 増額版なら unit を 1/0.95 倍
            if side == 'B' and self.compensate_banker:
                actual_unit = base_unit * BANKER_UNIT_MULT
                self.banker_extra_bet += (actual_unit - base_unit)  # 増額部分を記録
            else:
                actual_unit = base_unit

            if outcome == 'O':
                if side == 'B':
                    # Banker 勝利: actual_unit × (1 - 0.05) を獲得
                    money += actual_unit * (1.0 - BANKER_COMMISSION)
                else:
                    money += actual_unit
            else:  # 'X'
                money -= actual_unit

        self.cumulative += money
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
        if self.cumulative > self.peak:
            self.peak = self.cumulative
        self.max_dd = max(self.max_dd, self.peak - self.cumulative)

    def add(self, r):
        if r == 'T':
            return None
        bet_side = self._decide_bet_side()
        # streak 更新
        if r == self.last_non_tie:
            self.streak_len += 1
        else:
            self.streak_len = 1
        self.last_non_tie = r
        self.hands += 1

        base_unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]
        if r == bet_side:
            self.turns.append(('O', bet_side, base_unit))
        else:
            self.turns.append(('X', bet_side, base_unit))
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        if self.cumulative <= -self.lc:
            return 'loss'
        return None


# ─────────────────────────────────────────────
# シミュレーション
# ─────────────────────────────────────────────
def simulate_no_losscut(shoes, start_capital, bet_strategy, compensate_banker=False, target=PROFIT_PER_WIN):
    sim = MaruBatsuSim3(bet_strategy=bet_strategy, compensate_banker=compensate_banker,
                        target=target, lc=10**12)
    balance = start_capital
    turns = []
    bankrupt = False
    session_start_ts = None
    session_start_hand_count = 0

    for seq, started_at in shoes:
        for r in seq:
            if r not in ('P', 'B', 'T'):
                continue
            if session_start_ts is None:
                session_start_ts = started_at

            sim.add(r)

            if balance + sim.cumulative <= 0:
                turns.append({
                    'turn': len(turns) + 1,
                    'started_at': session_start_ts,
                    'outcome': 'bankrupt',
                    'session_pnl': sim.cumulative,
                    'balance': 0,
                    'hands': sim.hands - session_start_hand_count,
                })
                balance = 0
                bankrupt = True
                break

            if sim.cumulative >= target:
                profit = sim.cumulative
                balance += profit
                turns.append({
                    'turn': len(turns) + 1,
                    'started_at': session_start_ts,
                    'outcome': 'profit',
                    'session_pnl': profit,
                    'balance': balance,
                    'hands': sim.hands - session_start_hand_count,
                })
                session_start_hand_count = sim.hands
                session_start_ts = None
                sim.reset()
                session_start_hand_count = 0

        if bankrupt:
            break

    return {
        'turns': turns,
        'final_balance': balance,
        'bankrupt_at': turns[-1]['turn'] if bankrupt else None,
    }


# ─────────────────────────────────────────────
# データ読み込み
# ─────────────────────────────────────────────
def load_shoes_by_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, result_sequence, started_at
        FROM shoes_analytics
        WHERE result_sequence IS NOT NULL AND length(result_sequence) >= ?
        ORDER BY started_at
    """, (MIN_HANDS_PER_SHOE,))
    rows = cur.fetchall()
    conn.close()
    shoes_by_table = defaultdict(list)
    for tn, seq, ts in rows:
        if tn and seq:
            shoes_by_table[tn].append((seq, ts))
    return shoes_by_table


# ─────────────────────────────────────────────
# build pairs (original vs compensated)
# ─────────────────────────────────────────────
def build_comparison(shoes_by_table, bet_strategy):
    """各テーブルで通常版と Banker 増額版を両方シミュレートし、比較データを返す"""
    eligible = [(tn, shoes) for tn, shoes in shoes_by_table.items()
                if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]

    pairs = []
    for tn, shoes in eligible:
        orig = simulate_no_losscut(shoes, START_CAPITAL, bet_strategy, compensate_banker=False)
        comp = simulate_no_losscut(shoes, START_CAPITAL, bet_strategy, compensate_banker=True)
        if not orig['turns'] and not comp['turns']:
            continue
        pairs.append({
            'name': tn,
            'shoes': len(shoes),
            'orig': orig,
            'comp': comp,
            'orig_pnl': orig['final_balance'] - START_CAPITAL,
            'comp_pnl': comp['final_balance'] - START_CAPITAL,
            'diff': comp['final_balance'] - orig['final_balance'],
        })

    # 改善幅 (差分) の大きい順 (= 増額が効いた順)
    pairs.sort(key=lambda x: -x['diff'])

    return pairs


# ─────────────────────────────────────────────
# HTML レンダリング
# ─────────────────────────────────────────────
def render_compare_html(out_path, *, title_label, strategy_label, strategy_desc,
                        accent_color, pairs, total_hands, total_shoes):
    """通常版 vs Banker 増額版の比較 HTML を生成"""

    # 全体サマリー
    total = len(pairs)
    orig_profit = sum(1 for p in pairs if p['orig_pnl'] > 0 and not p['orig']['bankrupt_at'])
    orig_bankrupt = sum(1 for p in pairs if p['orig']['bankrupt_at'])
    comp_profit = sum(1 for p in pairs if p['comp_pnl'] > 0 and not p['comp']['bankrupt_at'])
    comp_bankrupt = sum(1 for p in pairs if p['comp']['bankrupt_at'])
    improved = sum(1 for p in pairs if p['diff'] > 0)
    worsened = sum(1 for p in pairs if p['diff'] < 0)
    neutral = sum(1 for p in pairs if p['diff'] == 0)

    total_orig = sum(p['orig']['final_balance'] for p in pairs)
    total_comp = sum(p['comp']['final_balance'] for p in pairs)
    total_diff = total_comp - total_orig

    def fmt_status(p, key):
        ledger = p[key]
        pnl = p[f'{key}_pnl']
        if ledger['bankrupt_at']:
            return f"💀 {ledger['bankrupt_at']}T 破綻", "#7c2d2d", "bankrupt"
        elif pnl < 0:
            return f"⚠️ {pnl:+,.0f}$", "#7a4a1c", "loss"
        elif pnl > 0:
            return f"✅ {pnl:+,.0f}$", "#1a4a2a", "profit"
        else:
            return "→ ±0", "#2a3441", "neutral"

    sections_html = ""
    for p in pairs:
        orig_status, orig_color, orig_class = fmt_status(p, 'orig')
        comp_status, comp_color, comp_class = fmt_status(p, 'comp')
        diff = p['diff']
        if diff > 0:
            diff_label = f"<span style='color:#4ade80;font-size:18px;font-weight:bold'>+${diff:,.0f} ✅ 改善</span>"
        elif diff < 0:
            diff_label = f"<span style='color:#f87171;font-size:18px;font-weight:bold'>-${abs(diff):,.0f} ❌ 悪化</span>"
        else:
            diff_label = "<span style='color:#9ca3af;font-size:18px'>±0</span>"

        # ターン数とWin/Loss
        orig_turns = p['orig']['turns']
        comp_turns = p['comp']['turns']
        orig_w = sum(1 for r in orig_turns if r['outcome'] == 'profit')
        orig_l = sum(1 for r in orig_turns if r['outcome'] == 'bankrupt')
        comp_w = sum(1 for r in comp_turns if r['outcome'] == 'profit')
        comp_l = sum(1 for r in comp_turns if r['outcome'] == 'bankrupt')

        # mini ledger 行 (最後の 8 ターンのみ表示してコンパクトに)
        def mini_rows(rows, key='balance'):
            html = ""
            for r in rows[-8:]:  # 最後の 8 ターン
                if r['outcome'] == 'profit':
                    label = f"WIN +${r['session_pnl']:,.0f}"
                    cls = 'profit'
                else:
                    label = f"💀 BANKRUPT"
                    cls = 'loss'
                bal_color = ('#4ade80' if r['balance'] >= START_CAPITAL
                             else ('#fbbf24' if r['balance'] >= START_CAPITAL * 0.5 else '#f87171'))
                html += (f"<tr class='{cls}'>"
                         f"<td style='width:30px'>{r['turn']}</td>"
                         f"<td>{label}</td>"
                         f"<td style='text-align:right;color:{bal_color}'>${r['balance']:,.0f}</td>"
                         f"</tr>")
            return html

        sections_html += f"""
<div class="pair-section">
  <div class="pair-header">
    <div class="tname">{p['name']}</div>
    <div class="tmeta">{p['shoes']}シュー</div>
    <div class="diff-display">{diff_label}</div>
  </div>
  <div class="pair-body">
    <div class="pair-col original" style="border-left-color:{orig_color}">
      <div class="col-header">
        <span class="col-title">📋 通常版 (Banker 通常 unit)</span>
        <span class="col-status">{orig_status}</span>
      </div>
      <div class="col-summary">
        最終: <strong>${p['orig']['final_balance']:,.0f}</strong> · {orig_w}W / {orig_l}L / {len(orig_turns)}ターン
      </div>
      <table class="mini-ledger">
        <thead><tr><th>T</th><th>結果</th><th style="text-align:right">残高</th></tr></thead>
        <tbody>{mini_rows(orig_turns)}</tbody>
      </table>
    </div>
    <div class="pair-col compensated" style="border-left-color:{comp_color}">
      <div class="col-header">
        <span class="col-title">⚖️ Banker 増額版 (×{BANKER_UNIT_MULT:.4f})</span>
        <span class="col-status">{comp_status}</span>
      </div>
      <div class="col-summary">
        最終: <strong>${p['comp']['final_balance']:,.0f}</strong> · {comp_w}W / {comp_l}L / {len(comp_turns)}ターン
      </div>
      <table class="mini-ledger">
        <thead><tr><th>T</th><th>結果</th><th style="text-align:right">残高</th></tr></thead>
        <tbody>{mini_rows(comp_turns)}</tbody>
      </table>
    </div>
  </div>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>{title_label} — Banker 増額 比較</title>
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
h2 {{ color: {accent_color}; margin-top: 32px; }}
.banner {{
  background: #2a1a3a;
  border-left: 5px solid {accent_color};
  padding: 14px 18px;
  margin: 16px 0;
  font-size: 14px;
  border-radius: 4px;
  line-height: 1.7;
}}
.banner strong {{ color: {accent_color}; }}
.summary {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin: 20px 0;
}}
.card {{
  background: #1a2332;
  border-left: 4px solid {accent_color};
  padding: 14px;
  border-radius: 4px;
}}
.card .label {{ font-size: 11px; color: #9ca3af; text-transform: uppercase; letter-spacing: 1px; }}
.card .value {{ font-size: 22px; font-weight: bold; margin-top: 4px; color: #fff; }}
.card.profit .value {{ color: #4ade80; }}
.card.danger .value {{ color: #f87171; }}
.card.warn .value {{ color: #fbbf24; }}
.compare-summary {{
  background: #11192a;
  border-radius: 8px;
  padding: 20px;
  margin: 20px 0;
  border: 2px solid {accent_color};
}}
.compare-summary h3 {{ margin: 0 0 12px 0; color: {accent_color}; }}
.compare-summary .row {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 16px;
  margin: 8px 0;
  font-size: 14px;
}}
.compare-summary .col-h {{ color: #9ca3af; font-size: 11px; text-transform: uppercase; }}
.compare-summary .col-v {{ font-size: 18px; font-weight: bold; color: #fff; }}

.pair-section {{
  background: #1a2332;
  border-radius: 8px;
  margin: 16px 0;
  overflow: hidden;
  box-shadow: 0 2px 6px rgba(0,0,0,0.4);
}}
.pair-header {{
  background: #11192a;
  padding: 14px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  border-bottom: 1px solid #2a3441;
}}
.pair-header .tname {{ font-size: 17px; font-weight: bold; color: #fff; }}
.pair-header .tmeta {{ font-size: 12px; color: #9ca3af; }}
.pair-header .diff-display {{ margin-left: auto; }}
.pair-body {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: #2a3441;
}}
.pair-col {{
  background: #1a2332;
  padding: 12px 16px;
  border-left: 4px solid #6dd5ed;
}}
.pair-col.compensated {{ background: #1f1a2a; }}
.col-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid #2a3441;
}}
.col-title {{ font-size: 13px; color: #6dd5ed; font-weight: bold; }}
.col-status {{ font-size: 12px; }}
.col-summary {{ font-size: 12px; color: #9ca3af; margin-bottom: 8px; }}
.col-summary strong {{ color: #fff; font-size: 14px; }}
.mini-ledger {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
.mini-ledger th, .mini-ledger td {{
  padding: 4px 8px;
  border-bottom: 1px solid #2a3441;
  font-size: 11px;
}}
.mini-ledger th {{
  background: #11192a;
  color: #9ca3af;
  font-weight: normal;
  text-transform: uppercase;
  font-size: 9px;
  letter-spacing: 0.5px;
}}
.mini-ledger tr.profit td {{ color: #4ade80; }}
.mini-ledger tr.loss td {{ color: #f87171; }}
.nav {{ margin-bottom: 24px; }}
.nav a {{
  display: inline-block;
  padding: 8px 14px;
  margin-right: 6px;
  margin-bottom: 6px;
  background: #1a2332;
  color: #6dd5ed;
  text-decoration: none;
  border-radius: 4px;
  font-size: 12px;
}}
.nav a:hover {{ background: #2a3441; }}

@media (max-width: 900px) {{
  .pair-body {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="container">

<div class="nav">
<a href="index.html">← トップに戻る</a>
<a href="equity_per_table.html">C. 〇✖プレイヤー</a>
<a href="equity_per_table_trend.html">D. 順張り</a>
<a href="equity_per_table_counter.html">E. 逆張り</a>
<a href="equity_per_table_trend_skip3.html">F. 順張り+3連</a>
<a href="equity_per_table_counter_skip3.html">G. 逆張り+3連</a>
<a href="equity_per_table_trend_comp.html">H. 順張り Banker増額</a>
<a href="equity_per_table_counter_comp.html">I. 逆張り Banker増額</a>
</div>

<h1>{title_label}</h1>

<div class="banner">
<strong>戦略:</strong> {strategy_desc}<br>
<strong>Banker unit 増額:</strong> Banker BET 時の unit を base × {BANKER_UNIT_MULT:.4f} (1/0.95) に拡大。<br>
<strong>仕組み:</strong> 勝った時 → Player と等価の利益 (commission を打ち消す)。負けた時 → 5.3% 大きい損失。<br>
<strong>狙い:</strong> 〇✖ ロジックの「diff = wins - losses」前提を Banker BET 時にも維持する。<br>
<strong>初期資金:</strong> $10,000 / セッション利確: $50 / ロスカットなし
</div>

<div class="compare-summary">
<h3>📊 全体比較サマリー ({total} テーブル)</h3>
<div class="row">
  <div><div class="col-h">通常版 利益 / 破綻</div><div class="col-v">{orig_profit} / {orig_bankrupt}</div></div>
  <div><div class="col-h">増額版 利益 / 破綻</div><div class="col-v">{comp_profit} / {comp_bankrupt}</div></div>
  <div><div class="col-h">改善 / 悪化 / 同じ</div><div class="col-v">{improved} / {worsened} / {neutral}</div></div>
</div>
<div class="row">
  <div><div class="col-h">通常版 合計残高</div><div class="col-v">${total_orig:,.0f}</div></div>
  <div><div class="col-h">増額版 合計残高</div><div class="col-v">${total_comp:,.0f}</div></div>
  <div><div class="col-h">差分</div><div class="col-v" style="color:{'#4ade80' if total_diff>=0 else '#f87171'}">{'+' if total_diff>=0 else ''}${total_diff:,.0f}</div></div>
</div>
</div>

<h2>テーブル別 比較 (差分の大きい順)</h2>

<p style="color: #9ca3af; font-size: 13px;">
左が通常版 (Banker 通常 unit) / 右が増額版 (Banker × {BANKER_UNIT_MULT:.4f})。<br>
台帳は最後の 8 ターンを表示。差分は最終残高の差。
</p>

{sections_html}

<div style="margin-top: 40px; padding: 16px; background: #1a2332; border-radius: 4px; font-size: 12px; color: #9ca3af;">
<strong>データソース:</strong> analytics_vps.sqlite3 ({total_shoes:,}シュー / 約{total_hands:,}ハンド)<br>
<strong>シミュレーション:</strong> {strategy_label} + 〇✖ + Banker 増額検証<br>
<strong>生成日:</strong> {os.popen('date /t' if os.name == 'nt' else 'date').read().strip()}
</div>

</div>
</body>
</html>
"""
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✓ {out_path}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ DB not found: {DB_PATH}")
        sys.exit(1)

    print(f"Loading shoes from {DB_PATH}...")
    shoes_by_table = load_shoes_by_table()
    total_shoes = sum(len(v) for v in shoes_by_table.values())
    total_hands = sum(len(seq) for shoes in shoes_by_table.values() for seq, _ in shoes)
    print(f"  → {len(shoes_by_table)} tables / {total_shoes} shoes / ~{total_hands} hands")

    out_dir = "report"
    os.makedirs(out_dir, exist_ok=True)

    # === 順張り 比較 ===
    print()
    print("=== 順張り 通常 vs Banker 増額 ===")
    trend_pairs = build_comparison(shoes_by_table, 'trend')
    print(f"  対象: {len(trend_pairs)} テーブル")

    render_compare_html(
        os.path.join(out_dir, "equity_per_table_trend_comp.html"),
        title_label="H. 順張り — Banker 増額 比較",
        strategy_label="順張り",
        strategy_desc="直前の結果と<strong>同じ側</strong>に BET (連勝の流れに乗る)。",
        accent_color="#22d3ee",
        pairs=trend_pairs,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    # === 逆張り 比較 ===
    print()
    print("=== 逆張り 通常 vs Banker 増額 ===")
    counter_pairs = build_comparison(shoes_by_table, 'counter')
    print(f"  対象: {len(counter_pairs)} テーブル")

    render_compare_html(
        os.path.join(out_dir, "equity_per_table_counter_comp.html"),
        title_label="I. 逆張り — Banker 増額 比較",
        strategy_label="逆張り",
        strategy_desc="直前の結果の<strong>反対側</strong>に BET (テレコ狙い)。",
        accent_color="#f472b6",
        pairs=counter_pairs,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    # === 集計 ===
    print()
    print("=== 完了 ===")
    for label, pairs in [("順張り", trend_pairs), ("逆張り", counter_pairs)]:
        improved = sum(1 for p in pairs if p['diff'] > 0)
        worsened = sum(1 for p in pairs if p['diff'] < 0)
        total_orig = sum(p['orig']['final_balance'] for p in pairs)
        total_comp = sum(p['comp']['final_balance'] for p in pairs)
        diff = total_comp - total_orig
        print(f"  {label}: 改善 {improved} / 悪化 {worsened} / 合計差分 {'+' if diff>=0 else ''}${diff:,.0f}")


if __name__ == "__main__":
    main()
