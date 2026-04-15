"""順張り/逆張り 対応の equity report 生成スクリプト (v2)

既存の generate_equity_report.py は触らず、独立して動く新規スクリプト。
〇✖ ロジックの資金管理は同じだが、BET する側を以下で切替える:

  - 順張り (trend): 直前の結果と同じ側に BET
      P が出たら次は P, B が出たら次は B (chase the streak)
  - 逆張り (counter): 直前の結果の反対側に BET
      P が出たら次は B, B が出たら次は P

両モードとも Banker 勝利時は 5% 手数料を控除して実マネー計算。

データソース: analytics_vps.sqlite3 (--vps オプション)
出力:
  - report/equity_per_table_trend.html   (順張り)
  - report/equity_per_table_counter.html (逆張り)

Usage:
  python generate_equity_report_v2.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
START_CAPITAL = 10000
PROFIT_PER_WIN = 50         # 1 セット利確目標 ($)
LOSS_PER_LOSS = 3000        # 損切り基準 ($)
BANKER_COMMISSION = 0.05    # Banker 勝利時の手数料 5%

MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5

# 〇✖ ロジックの unit 進行 (既存と同じ)
SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


# ─────────────────────────────────────────────
# シミュレータ (順張り/逆張り対応 + Banker 手数料)
# ─────────────────────────────────────────────
class MaruBatsuSim2:
    """順張り/逆張り 対応の MaruBatsu シミュレータ。

    bet_strategy: 'trend' (順張り) or 'counter' (逆張り)
    skip_streak_threshold: N (>=2) 連続が出たら BET 停止 (LOOK)、ストリーク解除で再開
                            0 = スキップなし (常時 BET)
    Banker 勝利時は 5% 手数料を引いた額で cumulative を更新。
    """

    def __init__(self, bet_strategy='trend', skip_streak_threshold=0,
                 target=PROFIT_PER_WIN, lc=LOSS_PER_LOSS):
        assert bet_strategy in ('trend', 'counter')
        self.bet_strategy = bet_strategy
        self.skip_streak_threshold = skip_streak_threshold
        self.target = target
        self.lc = lc
        self.reset()

    def reset(self):
        self.cumulative = 0.0   # 実マネー (Banker 5% 引き込み)
        self.unit_idx = 0
        self.prev_os = 0
        self.sets = 0
        self.hands = 0
        self.skipped = 0        # LOOK で SKIP したハンド数
        self.turns = []         # 各要素は (outcome, side) — outcome='O'/'X', side='P'/'B'
        self.max_dd = 0
        self.peak = 0
        self.history = []
        self.last_non_tie = None
        self.streak_len = 0     # 直近の同一側連続数

    def _decide_bet_side(self):
        """次の手の BET 側を決定 (前の手の結果に基づく)"""
        if self.last_non_tie is None:
            return 'P'  # 最初は Player
        if self.bet_strategy == 'trend':
            return self.last_non_tie  # 順張り: 同じ側
        else:  # counter
            return 'B' if self.last_non_tie == 'P' else 'P'  # 逆張り

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
        """7 ターン埋まったらセット精算"""
        wins = sum(1 for t in self.turns if t[0] == 'O')
        losses = 7 - wins
        diff = wins - losses
        unit = SEQ[self.unit_idx] if self.unit_idx < len(SEQ) else SEQ[-1]

        # 実マネー計算 (Banker 勝ちは 5% 手数料)
        money = 0.0
        for outcome, side in self.turns:
            if outcome == 'O':
                if side == 'B':
                    money += unit * (1.0 - BANKER_COMMISSION)
                else:
                    money += unit
            else:  # 'X'
                money -= unit

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
        """1 ハンドの結果を投入 ('P'/'B'/'T')

        skip_streak_threshold が 0 以外なら、直近の連続が threshold 以上の場合
        この手は LOOK (BET なし) してスキップする。streak が破られた次のハンドから再開。
        """
        if r == 'T':
            return None  # Tie は無視

        # === 1. BET 判定 (前回までの状態を元にする) ===
        # streak_len が threshold 以上 → LOOK モード (この手はスキップ)
        in_look = (self.skip_streak_threshold > 0
                   and self.streak_len >= self.skip_streak_threshold)
        bet_side = None if in_look else self._decide_bet_side()

        # === 2. streak 更新 (この手の結果を反映) ===
        if r == self.last_non_tie:
            self.streak_len += 1
        else:
            self.streak_len = 1
        self.last_non_tie = r
        self.hands += 1

        # === 3. BET 結果記録 (LOOK でなければ) ===
        if bet_side is not None:
            if r == bet_side:
                self.turns.append(('O', bet_side))
            else:
                self.turns.append(('X', bet_side))
            if len(self.turns) == 7:
                self._complete()
        else:
            self.skipped += 1

        if self.cumulative >= self.target:
            return 'profit'
        if self.cumulative <= -self.lc:
            return 'loss'
        return None


# ─────────────────────────────────────────────
# シミュレーション (ロスカットなし、破綻まで継続)
# ─────────────────────────────────────────────
def simulate_no_losscut(shoes, start_capital, bet_strategy, skip_streak=0, target=PROFIT_PER_WIN):
    """ロスカットなしシミュレーション。

    各セッションは $50 利確で終了。負けても止めず、破綻するまで継続。
    skip_streak: N (>=2) で N連続以上の時 LOOK。0 でスキップなし。
    Returns: { turns: [...], final_balance, bankrupt_at }
    """
    sim = MaruBatsuSim2(bet_strategy=bet_strategy, skip_streak_threshold=skip_streak,
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

            # 破綻判定: セッション内損失が残高を超えた
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

            # 利確判定
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
    """analytics_vps.sqlite3 から各テーブルのシューを読む"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, result_sequence, started_at
        FROM shoes_analytics
        WHERE result_sequence IS NOT NULL
          AND length(result_sequence) >= ?
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
# HTML レンダリング
# ─────────────────────────────────────────────
def render_html(out_path, *, title_label, strategy_label, strategy_desc,
                accent_color, table_ledgers, capital_comparison, total_tables,
                bankrupt_count, loss_count, profit_count, total_hands, total_shoes):
    """1 つの戦略 (順張り or 逆張り) の HTML を生成"""

    # 元本別生存比較表
    capital_table_html = ""
    for c in capital_comparison:
        bankrupt_pct = (c['bankrupt'] / (c['bankrupt'] + c['survived']) * 100
                        if (c['bankrupt'] + c['survived']) > 0 else 0)
        b_color = '#f87171' if bankrupt_pct >= 50 else ('#fbbf24' if bankrupt_pct >= 20 else '#4ade80')
        capital_table_html += (
            f"<tr>"
            f"<td><strong>${c['capital']:,}</strong></td>"
            f"<td style='color:#f87171;font-weight:bold'>{c['bankrupt']}</td>"
            f"<td style='color:#4ade80;font-weight:bold'>{c['survived']}</td>"
            f"<td style='color:{b_color}'>{bankrupt_pct:.0f}%</td>"
            f"<td>${c['total_balance']:,.0f}</td>"
            f"<td style='color:{'#4ade80' if c['total_profit']>=0 else '#f87171'}'>"
            f"{'+' if c['total_profit']>=0 else ''}${c['total_profit']:,.0f}</td>"
            f"<td style='color:{'#4ade80' if c['roi']>=0 else '#f87171'}'>"
            f"{'+' if c['roi']>=0 else ''}{c['roi']:.0f}%</td>"
            f"</tr>"
        )

    # テーブル別セクション
    sections_html = ""
    for t in table_ledgers:
        if t['bankrupt_at']:
            status = f"💀 {t['bankrupt_at']}ターン目で破綻"
            status_color = "#7c2d2d"
            status_class = "bankrupt"
        elif t['pnl'] < 0:
            status = f"⚠️ {t['pnl']:+,.0f}$ 損失"
            status_color = "#7a4a1c"
            status_class = "loss"
        elif t['pnl'] > 0:
            status = f"✅ {t['pnl']:+,.0f}$ 利益"
            status_color = "#1a4a2a"
            status_class = "profit"
        else:
            status = "→ ±0"
            status_color = "#2a3441"
            status_class = "neutral"

        rows_html = ""
        for r in t['rows']:
            ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
            if r['outcome'] == 'profit':
                outcome_class = 'profit'
                outcome_label = f"WIN +${r['session_pnl']:,.0f}"
            else:
                outcome_class = 'loss'
                outcome_label = f"💀 BANKRUPT (session loss ${r['session_pnl']:,.0f})"
            bal_color = ('#4ade80' if r['balance'] >= START_CAPITAL
                         else ('#fbbf24' if r['balance'] >= START_CAPITAL * 0.5 else '#f87171'))
            rows_html += (
                f"<tr class='{outcome_class}'>"
                f"<td class='turn'>{r['turn']}</td>"
                f"<td class='ts'>{ts}</td>"
                f"<td class='oc'>{outcome_label}</td>"
                f"<td class='hd'>{r['hands']}h</td>"
                f"<td class='bl' style='color:{bal_color}'>${r['balance']:,.0f}</td>"
                f"</tr>"
            )

        sections_html += f"""
<div class="table-section {status_class}" style="border-left-color:{status_color};">
  <div class="table-header">
    <div>
      <span class="tname">{t['name']}</span>
      <span class="tmeta">{t['shoes']}シュー / {t['wins']}W / {t['losses']}L / {len(t['rows'])}ターン</span>
    </div>
    <div class="status">{status}</div>
    <div class="balance">最終: <strong>${t['final_balance']:,.0f}</strong> / 最低: ${t['min_balance']:,.0f}</div>
  </div>
  <table class="ledger">
    <thead><tr><th>ターン</th><th>日時</th><th>結果</th><th>消費ハンド</th><th>残高</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>{title_label} — $10,000スタート</title>
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
.container {{ max-width: 1200px; margin: 0 auto; }}
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
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
.capital-table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
.capital-table th, .capital-table td {{
  padding: 8px 12px;
  border: 1px solid #2a3441;
  text-align: center;
  font-size: 13px;
}}
.capital-table th {{ background: #1a2332; color: #6dd5ed; }}
.table-section {{
  background: #1a2332;
  border-left: 5px solid #6dd5ed;
  margin: 20px 0;
  border-radius: 4px;
  overflow: hidden;
}}
.table-section.profit {{ border-left-color: #4ade80; }}
.table-section.loss {{ border-left-color: #fbbf24; }}
.table-section.bankrupt {{ border-left-color: #f87171; }}
.table-header {{
  background: #11192a;
  padding: 12px 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}}
.table-header .tname {{ font-size: 16px; font-weight: bold; color: #fff; }}
.table-header .tmeta {{ font-size: 12px; color: #9ca3af; margin-left: 12px; }}
.table-header .status {{ font-size: 13px; color: #fbbf24; }}
.table-header .balance {{ font-size: 13px; color: #6dd5ed; }}
.ledger {{ width: 100%; border-collapse: collapse; }}
.ledger th, .ledger td {{
  padding: 6px 10px;
  border-bottom: 1px solid #2a3441;
  text-align: left;
  font-size: 12px;
}}
.ledger th {{ background: #11192a; color: #9ca3af; font-weight: normal; text-transform: uppercase; font-size: 10px; letter-spacing: 0.5px; }}
.ledger tr.profit td {{ color: #4ade80; }}
.ledger tr.loss td {{ color: #f87171; }}
.ledger .turn {{ width: 50px; }}
.ledger .ts {{ width: 130px; color: #9ca3af; font-family: monospace; }}
.ledger .hd {{ width: 60px; text-align: right; color: #9ca3af; }}
.ledger .bl {{ width: 100px; text-align: right; font-weight: bold; }}
.nav {{ margin-bottom: 24px; }}
.nav a {{
  display: inline-block;
  padding: 8px 16px;
  margin-right: 8px;
  background: #1a2332;
  color: #6dd5ed;
  text-decoration: none;
  border-radius: 4px;
  font-size: 13px;
}}
.nav a:hover {{ background: #2a3441; }}
.nav a.current {{ background: {accent_color}; color: #0f1419; font-weight: bold; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
<a href="index.html">← トップに戻る</a>
<a href="equity_ledger_all.html">A. 全テーブル</a>
<a href="equity_ledger_top.html">B. 推奨Top</a>
<a href="equity_per_table.html">C. 〇✖プレイヤー</a>
<a href="equity_per_table_trend.html">D. 順張り</a>
<a href="equity_per_table_counter.html">E. 逆張り</a>
<a href="equity_per_table_trend_skip3.html">F. 順張り+3連スキップ</a>
<a href="equity_per_table_counter_skip3.html">G. 逆張り+3連スキップ</a>
</div>

<h1>{title_label}</h1>

<div class="banner">
<strong>戦略:</strong> {strategy_desc}<br>
<strong>資金管理:</strong> 〇✖ ロジック (1セット = 7ターン、SEQ で unit 進行)<br>
<strong>Banker 手数料:</strong> 5% 控除込みの実マネー計算<br>
<strong>初期資金:</strong> $10,000 / セッション利確: $50 / ロスカットなし (破綻まで継続)
</div>

<div class="summary">
  <div class="card"><div class="label">対象テーブル</div><div class="value">{total_tables}</div></div>
  <div class="card profit"><div class="label">利益で終了</div><div class="value">{profit_count}</div></div>
  <div class="card warn"><div class="label">損失だが生存</div><div class="value">{loss_count}</div></div>
  <div class="card danger"><div class="label">破綻</div><div class="value">{bankrupt_count}</div></div>
</div>

<h2>元本別 生存・破綻 比較 ({strategy_label})</h2>

<table class="capital-table">
<thead>
<tr><th>初期資金</th><th>破綻</th><th>生存</th><th>破綻率</th><th>合計残高</th><th>合計損益</th><th>ROI</th></tr>
</thead>
<tbody>
{capital_table_html}
</tbody>
</table>

<h2>テーブル別ターン台帳 ({total_tables} テーブル)</h2>

<p style="color: #9ca3af; font-size: 13px;">最終残高が低い順 (破綻リスク高い順) に表示。</p>

{sections_html}

<div style="margin-top: 40px; padding: 16px; background: #1a2332; border-radius: 4px; font-size: 12px; color: #9ca3af;">
<strong>データソース:</strong> analytics_vps.sqlite3 ({total_shoes:,}シュー / 約{total_hands:,}ハンド)<br>
<strong>シミュレーション:</strong> {strategy_label} + 〇✖ resource management + Banker 5% 手数料控除<br>
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
def build_for_strategy(shoes_by_table, bet_strategy, skip_streak=0):
    """指定戦略でテーブル別 ledger を構築"""
    eligible = [(tn, shoes) for tn, shoes in shoes_by_table.items()
                if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]

    table_ledgers = []
    for tn, shoes in eligible:
        ledger = simulate_no_losscut(shoes, START_CAPITAL, bet_strategy, skip_streak=skip_streak)
        rows = ledger['turns']
        if not rows:
            continue
        final_balance = ledger['final_balance']
        min_balance = min((r['balance'] for r in rows), default=START_CAPITAL)
        wins = sum(1 for r in rows if r['outcome'] == 'profit')
        losses = sum(1 for r in rows if r['outcome'] == 'bankrupt')
        table_ledgers.append({
            'name': tn,
            'rows': rows,
            'final_balance': final_balance,
            'min_balance': min_balance,
            'wins': wins,
            'losses': losses,
            'shoes': len(shoes),
            'bankrupt_at': ledger['bankrupt_at'],
            'pnl': final_balance - START_CAPITAL,
        })

    table_ledgers.sort(key=lambda x: x['final_balance'])

    # 元本別比較
    capital_levels = [10000, 20000, 30000, 50000, 100000, 300000]
    capital_comparison = []
    for cap in capital_levels:
        bankrupt_n = 0
        profit_n = 0
        total_bal = 0
        for tn, shoes in eligible:
            r = simulate_no_losscut(shoes, cap, bet_strategy, skip_streak=skip_streak)
            if r['bankrupt_at']:
                bankrupt_n += 1
            else:
                profit_n += 1
            total_bal += r['final_balance']
        n_tables = bankrupt_n + profit_n if (bankrupt_n + profit_n) > 0 else 1
        capital_comparison.append({
            'capital': cap,
            'bankrupt': bankrupt_n,
            'survived': profit_n,
            'total_balance': total_bal,
            'total_profit': total_bal - cap * n_tables,
            'roi': ((total_bal - cap * n_tables) / (cap * n_tables) * 100) if n_tables > 0 else 0,
        })

    return table_ledgers, capital_comparison


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

    # === 順張り (trend) ===
    print()
    print("=== 順張り (trend) シミュレーション ===")
    trend_ledgers, trend_capital = build_for_strategy(shoes_by_table, 'trend')
    trend_total = len(trend_ledgers)
    trend_bankrupt = sum(1 for t in trend_ledgers if t['bankrupt_at'])
    trend_loss = sum(1 for t in trend_ledgers if t['pnl'] < 0 and not t['bankrupt_at'])
    trend_profit = sum(1 for t in trend_ledgers if t['pnl'] > 0)
    print(f"  対象: {trend_total}テーブル / 利益: {trend_profit} / 損失生存: {trend_loss} / 破綻: {trend_bankrupt}")

    render_html(
        os.path.join(out_dir, "equity_per_table_trend.html"),
        title_label="D. 順張り (Trend Following) ターン台帳",
        strategy_label="順張り",
        strategy_desc="直前の結果と<strong>同じ側</strong>に BET (Player→Player, Banker→Banker)。連勝の流れを取る。",
        accent_color="#4ade80",
        table_ledgers=trend_ledgers,
        capital_comparison=trend_capital,
        total_tables=trend_total,
        bankrupt_count=trend_bankrupt,
        loss_count=trend_loss,
        profit_count=trend_profit,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    # === 逆張り (counter) ===
    print()
    print("=== 逆張り (counter) シミュレーション ===")
    counter_ledgers, counter_capital = build_for_strategy(shoes_by_table, 'counter')
    counter_total = len(counter_ledgers)
    counter_bankrupt = sum(1 for t in counter_ledgers if t['bankrupt_at'])
    counter_loss = sum(1 for t in counter_ledgers if t['pnl'] < 0 and not t['bankrupt_at'])
    counter_profit = sum(1 for t in counter_ledgers if t['pnl'] > 0)
    print(f"  対象: {counter_total}テーブル / 利益: {counter_profit} / 損失生存: {counter_loss} / 破綻: {counter_bankrupt}")

    render_html(
        os.path.join(out_dir, "equity_per_table_counter.html"),
        title_label="E. 逆張り (Counter-trend) ターン台帳",
        strategy_label="逆張り",
        strategy_desc="直前の結果の<strong>反対側</strong>に BET (Player→Banker, Banker→Player)。テレコ狙い。",
        accent_color="#c084fc",
        table_ledgers=counter_ledgers,
        capital_comparison=counter_capital,
        total_tables=counter_total,
        bankrupt_count=counter_bankrupt,
        loss_count=counter_loss,
        profit_count=counter_profit,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    # === 順張り + 3連スキップ (trend_skip3) ===
    print()
    print("=== 順張り + 3連スキップ シミュレーション ===")
    trend3_ledgers, trend3_capital = build_for_strategy(shoes_by_table, 'trend', skip_streak=3)
    trend3_total = len(trend3_ledgers)
    trend3_bankrupt = sum(1 for t in trend3_ledgers if t['bankrupt_at'])
    trend3_loss = sum(1 for t in trend3_ledgers if t['pnl'] < 0 and not t['bankrupt_at'])
    trend3_profit = sum(1 for t in trend3_ledgers if t['pnl'] > 0)
    print(f"  対象: {trend3_total}テーブル / 利益: {trend3_profit} / 損失生存: {trend3_loss} / 破綻: {trend3_bankrupt}")

    render_html(
        os.path.join(out_dir, "equity_per_table_trend_skip3.html"),
        title_label="F. 順張り + 3連スキップ ターン台帳",
        strategy_label="順張り + 3連スキップ",
        strategy_desc="順張り (前手と同じ側) で BET。ただし<strong>3連続以上</strong>の同じ側が出たら、ストリークが切れるまで <strong>LOOK (BET なし)</strong>。長いドラゴンの破綻回避狙い。",
        accent_color="#22d3ee",
        table_ledgers=trend3_ledgers,
        capital_comparison=trend3_capital,
        total_tables=trend3_total,
        bankrupt_count=trend3_bankrupt,
        loss_count=trend3_loss,
        profit_count=trend3_profit,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    # === 逆張り + 3連スキップ (counter_skip3) ===
    print()
    print("=== 逆張り + 3連スキップ シミュレーション ===")
    counter3_ledgers, counter3_capital = build_for_strategy(shoes_by_table, 'counter', skip_streak=3)
    counter3_total = len(counter3_ledgers)
    counter3_bankrupt = sum(1 for t in counter3_ledgers if t['bankrupt_at'])
    counter3_loss = sum(1 for t in counter3_ledgers if t['pnl'] < 0 and not t['bankrupt_at'])
    counter3_profit = sum(1 for t in counter3_ledgers if t['pnl'] > 0)
    print(f"  対象: {counter3_total}テーブル / 利益: {counter3_profit} / 損失生存: {counter3_loss} / 破綻: {counter3_bankrupt}")

    render_html(
        os.path.join(out_dir, "equity_per_table_counter_skip3.html"),
        title_label="G. 逆張り + 3連スキップ ターン台帳",
        strategy_label="逆張り + 3連スキップ",
        strategy_desc="逆張り (前手の反対側) で BET。ただし<strong>3連続以上</strong>の同じ側が出たら、ストリークが切れるまで <strong>LOOK (BET なし)</strong>。テレコ崩壊期間の損失回避狙い。",
        accent_color="#f472b6",
        table_ledgers=counter3_ledgers,
        capital_comparison=counter3_capital,
        total_tables=counter3_total,
        bankrupt_count=counter3_bankrupt,
        loss_count=counter3_loss,
        profit_count=counter3_profit,
        total_hands=total_hands,
        total_shoes=total_shoes,
    )

    print()
    print("=== 完了 ===")
    print(f"  D. 順張り:               {trend_profit}/{trend_total} 利益 / {trend_bankrupt} 破綻")
    print(f"  E. 逆張り:               {counter_profit}/{counter_total} 利益 / {counter_bankrupt} 破綻")
    print(f"  F. 順張り+3連スキップ:   {trend3_profit}/{trend3_total} 利益 / {trend3_bankrupt} 破綻")
    print(f"  G. 逆張り+3連スキップ:   {counter3_profit}/{counter3_total} 利益 / {counter3_bankrupt} 破綻")


if __name__ == "__main__":
    main()
