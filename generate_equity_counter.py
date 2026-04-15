"""テレコ逆張り版 equity per-table report

各テーブル独立シミュレーション。テレコ混合シューで逆張り。
Banker BET時は通常unit (増額なし、手数料5%は吸収)。
$10,000 元本スタート、$1 BET スタート、損切なし。

Usage:
  python generate_equity_counter.py --vps
  python generate_equity_counter.py --vps --capital 30000
  python generate_equity_counter.py --vps --capital 50000
"""
import sqlite3
import os
import sys
from collections import defaultdict
from pattern_classifier import classify_pattern

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"

def _parse_capital() -> int:
    for i, a in enumerate(sys.argv):
        if a == "--capital" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
        if a.startswith("--capital="):
            return int(a.split("=", 1)[1])
    return 10000

START_CAPITAL = _parse_capital()
PROFIT_PER_WIN = 50
BANKER_COMMISSION = 0.05
MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5
STATIC_WARMUP = 30

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class CounterMaruBatsuSim:
    """逆張り + 〇✖ MaruBatsu。P/B 両方に BET。"""

    def __init__(self, target=PROFIT_PER_WIN):
        self.target = target
        self.reset()

    def reset(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.sets = 0
        self.hands = 0
        self.turns = []
        self.max_dd = 0
        self.peak = 0
        self.history = []
        self.last_non_tie = None

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
        self.hands += 1
        # 逆張り: 前手の逆にBET
        if self.last_non_tie is None:
            self.last_non_tie = r
            return None  # 初手はBETしない

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


def simulate_no_losscut(shoes, start_capital, target=PROFIT_PER_WIN):
    """テレコ混合シューのみで逆張りシミュレーション"""
    sim = CounterMaruBatsuSim(target=target)
    balance = start_capital
    turns = []
    bankrupt = False
    session_start_ts = None
    session_start_hand_count = 0

    for seq, started_at in shoes:
        # テレコ混合判定 (序盤30ハンド)
        clean = strip_ties(seq)
        if len(clean) < STATIC_WARMUP:
            continue
        warmup = clean[:STATIC_WARMUP]
        pattern = classify_pattern(warmup, min_cols=3)
        if pattern != "テレコ+ニコ混合":
            continue  # テレコ混合以外はSKIP

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


def load_shoes_by_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? ORDER BY started_at",
        (MIN_HANDS_PER_SHOE,)
    )
    shoes_by_table = defaultdict(list)
    total_hands = 0
    for tn, seq, ts in cur.fetchall():
        shoes_by_table[tn].append((seq, ts))
        total_hands += sum(1 for c in seq if c in ('P', 'B', 'T'))
    conn.close()
    return shoes_by_table, total_hands


def render_html(shoes_by_table, total_hands):
    eligible = [(tn, shoes) for tn, shoes in shoes_by_table.items()
                if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]
    total_shoes = sum(len(shoes) for _, shoes in eligible)

    table_ledgers = []
    for tn, shoes in eligible:
        ledger = simulate_no_losscut(shoes, START_CAPITAL)
        rows = ledger['turns']
        if not rows:
            # テレコ混合シューが0 → PNL=0
            table_ledgers.append({
                'name': tn, 'rows': [], 'final_balance': START_CAPITAL,
                'min_balance': START_CAPITAL, 'wins': 0, 'losses': 0,
                'shoes': len(shoes), 'bankrupt_at': None, 'pnl': 0,
                'tereko_shoes': 0,
            })
            continue
        final_balance = ledger['final_balance']
        min_balance = min((r['balance'] for r in rows), default=START_CAPITAL)
        wins = sum(1 for r in rows if r['outcome'] == 'profit')
        losses = sum(1 for r in rows if r['outcome'] == 'bankrupt')

        # テレコ混合シュー数
        tereko_count = 0
        for seq, _ in shoes:
            clean = strip_ties(seq)
            if len(clean) >= STATIC_WARMUP:
                if classify_pattern(clean[:STATIC_WARMUP], min_cols=3) == "テレコ+ニコ混合":
                    tereko_count += 1

        table_ledgers.append({
            'name': tn, 'rows': rows, 'final_balance': final_balance,
            'min_balance': min_balance, 'wins': wins, 'losses': losses,
            'shoes': len(shoes), 'bankrupt_at': ledger['bankrupt_at'],
            'pnl': final_balance - START_CAPITAL, 'tereko_shoes': tereko_count,
        })

    # 元本別比較
    capital_levels = [10000, 20000, 30000, 50000, 100000, 300000]
    capital_comparison = []
    for cap in capital_levels:
        bankrupt_n = 0
        profit_n = 0
        total_bal = 0
        for tn, shoes in eligible:
            r = simulate_no_losscut(shoes, cap)
            if r['bankrupt_at']:
                bankrupt_n += 1
            else:
                profit_n += 1
            total_bal += r['final_balance']
        capital_comparison.append({
            'capital': cap, 'bankrupt': bankrupt_n, 'survived': profit_n,
            'total_balance': total_bal,
            'total_profit': total_bal - cap * len(eligible),
            'roi': (total_bal - cap * len(eligible)) / (cap * len(eligible)) * 100 if len(eligible) > 0 else 0,
        })

    capital_table_html = ""
    for c in capital_comparison:
        bankrupt_pct = c['bankrupt'] / (c['bankrupt'] + c['survived']) * 100 if (c['bankrupt'] + c['survived']) > 0 else 0
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

    table_ledgers.sort(key=lambda x: x['final_balance'])

    total_tables = len(table_ledgers)
    bankrupt_count = sum(1 for t in table_ledgers if t['bankrupt_at'])
    profit_count = sum(1 for t in table_ledgers if t['pnl'] > 0)
    neutral_count = total_tables - bankrupt_count - profit_count

    sections_html = ""
    for t in table_ledgers:
        if t['bankrupt_at']:
            status = f"💀 {t['bankrupt_at']}ターン目で破綻"
            status_color = "#7c2d2d"
            status_class = "bankrupt"
        elif t['pnl'] > 0:
            status = f"✅ {t['pnl']:+,.0f}$ 利益"
            status_color = "#1a4a2a"
            status_class = "profit"
        else:
            status = f"±0 / 未参加"
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
            bal_color = '#4ade80' if r['balance'] >= START_CAPITAL else ('#fbbf24' if r['balance'] >= START_CAPITAL * 0.5 else '#f87171')
            rows_html += (
                f"<tr class='{outcome_class}'>"
                f"<td class='turn'>{r['turn']}</td>"
                f"<td class='ts'>{ts}</td>"
                f"<td class='oc'>{outcome_label}</td>"
                f"<td class='hd'>{r['hands']}h</td>"
                f"<td class='bl' style='color:{bal_color}'>${r['balance']:,.0f}</td>"
                f"</tr>"
            )

        tereko_label = f" / テレコ混合 {t['tereko_shoes']}シュー" if t['tereko_shoes'] > 0 else " / テレコ混合 0 (全SKIP)"
        sections_html += f"""
<div class="table-section {status_class}" style="border-left-color:{status_color};">
  <div class="table-header">
    <div>
      <span class="tname">{t['name']}</span>
      <span class="tmeta">{t['shoes']}シュー{tereko_label} / {t['wins']}W / {t['losses']}L / {len(t['rows'])}ターン</span>
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
<title>Q. テレコ逆張り版 ターン台帳 — ${START_CAPITAL:,}スタート</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
.banner {{
  background: #1a2a1a; border-left: 5px solid #4ade80;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.7;
}}
.banner strong {{ color: #4ade80; }}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block; margin-right: 12px; padding: 8px 16px;
  background: #1a2332; color: #6dd5ed; text-decoration: none;
  border-radius: 4px; border: 1px solid #2a3441; font-size: 13px;
}}
.nav a.current {{ background: #4ade80; color: #0f1419; font-weight: bold; }}
.nav a:hover {{ border-color: #4ade80; }}
.summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 20px 0;
}}
.card {{
  background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.profit {{ border-left-color: #4ade80; }}
.card.profit .value {{ color: #4ade80; }}
.card.bankrupt {{ border-left-color: #f87171; }}
.card.bankrupt .value {{ color: #f87171; }}

h2 {{ color: #c084fc; margin-top: 32px; }}
.table-section {{
  background: #1a2332; border-left: 5px solid #6dd5ed; margin: 20px 0; padding: 16px; border-radius: 4px;
}}
.table-section.bankrupt {{ background: #2a1a1a; }}
.table-section.neutral {{ background: #1a1a2a; }}
.table-header {{
  display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 10px; margin-bottom: 12px; align-items: center;
}}
.table-header .tname {{ font-size: 18px; font-weight: bold; color: #ffd700; }}
.table-header .tmeta {{ font-size: 12px; color: #8a96a8; margin-left: 12px; }}
.table-header .status {{ font-size: 14px; text-align: center; }}
.table-header .balance {{ font-size: 13px; color: #8a96a8; text-align: right; }}
.table-header .balance strong {{ color: #ffd700; font-size: 16px; }}
.bankrupt .table-header .balance strong {{ color: #f87171; }}

.ledger {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.ledger th {{ background: #0f1419; color: #ffd700; padding: 6px 8px; text-align: left; border-bottom: 1px solid #2a3441; }}
.ledger td {{ padding: 4px 8px; border-bottom: 1px solid #2a3441; }}
.ledger tr.profit td.oc {{ color: #4ade80; }}
.ledger tr.loss td.oc {{ color: #f87171; font-weight: bold; }}
.ledger td.turn {{ text-align: right; color: #8a96a8; }}
.ledger td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.ledger td.bl {{ font-weight: bold; text-align: right; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}

table {{ width: 100%; border-collapse: collapse; }}
table th, table td {{ padding: 8px 12px; border-bottom: 1px solid #2a3441; text-align: left; }}
table th {{ background: #0f1419; color: #c084fc; }}
</style>
</head>
<body>
<div class="container">
<h1>Q. テレコ逆張り版 ターン台帳 (元本 ${START_CAPITAL:,})</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="equity_per_table.html">C. Player専用</a>
<a href="equity_per_table_banker.html">K. Banker専用</a>
<a href="equity_per_table_counter.html"{'class="current"' if START_CAPITAL == 10000 else ''}>Q. テレコ逆張り $10k</a>
<a href="equity_per_table_counter_30k.html"{' class="current"' if START_CAPITAL == 30000 else ''}>Q. 逆張り $30k</a>
<a href="equity_per_table_counter_50k.html"{' class="current"' if START_CAPITAL == 50000 else ''}>Q. 逆張り $50k</a>
</div>

<div class="banner">
<strong>📊 テレコ逆張り — 各テーブルを独立にシミュレーション。</strong><br>
${START_CAPITAL:,}元本スタート → セッションは <strong>$50 利確で終了</strong>、ロスカットは設けません。<br>
テレコ+ニコ混合と判定されたシューのみ参加。<strong>逆張り (前手の逆にBET)</strong>。<br>
Player / Banker 両方に BET。Banker勝利時は 5%手数料が引かれる (増額なし)。<br>
$1 BETスタート (SEQ[0]=1)。残高が 0以下になった瞬間に<strong>破綻 (BANKRUPT)</strong>。<br>
データソース: 全{total_hands:,}ハンド・{total_shoes}シュー / 各テーブル独立。<br>
<strong>並び順: 最終資金の少ない順（破綻したテーブルが上）</strong>
</div>

<div class="summary">
  <div class="card">
    <div class="label">対象テーブル数</div>
    <div class="value">{total_tables}</div>
  </div>
  <div class="card profit">
    <div class="label">プラス終了</div>
    <div class="value">{profit_count}</div>
  </div>
  <div class="card">
    <div class="label">±0 / 未参加</div>
    <div class="value">{neutral_count}</div>
  </div>
  <div class="card bankrupt">
    <div class="label">破綻</div>
    <div class="value">{bankrupt_count}</div>
  </div>
</div>

<h2 style="color:#c084fc;margin-top:32px;">💰 元本別 生存テーブル数の比較</h2>
<p class="note">
  元本を増やせばセッション内ドローダウンを吸収できる範囲が広がり、破綻するテーブルが減ります。
</p>
<table style="margin-bottom:32px;font-size:14px;">
<thead>
<tr><th>元本</th><th>破綻</th><th>生存</th><th>破綻率</th><th>通算残高</th><th>通算損益</th><th>ROI</th></tr>
</thead>
<tbody>{capital_table_html}</tbody>
</table>

<h2 style="color:#c084fc;">📋 各テーブルのターン台帳 (元本${START_CAPITAL:,})</h2>

{sections_html}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_equity_counter.py</code> / テレコ混合シュー × 逆張り / 利確$50 / 損切なし / MaruBatsuロジック
</p>
</div>
</body>
</html>
"""
    suffix = "" if START_CAPITAL == 10000 else f"_{START_CAPITAL // 1000}k"
    out_path = os.path.join("report", f"equity_per_table_counter{suffix}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")
    print(f"  {total_tables} tables: {profit_count} profit, {neutral_count} neutral, {bankrupt_count} bankrupt")


def main():
    print(f"Loading {DB_PATH} (capital=${START_CAPITAL:,})...")
    shoes_by_table, total_hands = load_shoes_by_table()
    total_shoes = sum(len(v) for v in shoes_by_table.values())
    print(f"Total {total_hands:,} hands across {len(shoes_by_table)} tables ({total_shoes} shoes)\n")
    render_html(shoes_by_table, total_hands)


if __name__ == "__main__":
    main()
