"""Generate equity-curve / session ledger HTML reports.

3バージョン生成:
  - report/equity_ledger_all.html  : 全テーブルでランダム運用した場合
  - report/equity_ledger_top.html  : 推奨Top4-5テーブルだけで運用した場合
  - report/equity_per_table.html   : 各テーブル独立シミュレーション
  - report/equity_ledger.html      : 旧（互換用に top と同じ）

Usage:
  python generate_equity_report.py            # ローカル DB (5時間ぶん)
  python generate_equity_report.py --vps      # VPS DB (5日間ぶん)
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
START_CAPITAL = 10000
PROFIT_PER_WIN = 50
LOSS_PER_LOSS = 3000

MIN_HANDS_PER_SHOE = 50
# VPS DB は十分なシュー数があるので auto_update_tables.py と同じ 30 を使う
# ローカル DB は 5時間ぶんしか無いので 5 に緩和
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5

# 推奨Top（実運用Sync mode の SYNC_RECOMMENDED_TABLES と同じ並び）
TOP_RECOMMENDED = [
    "Japanese Speed Baccarat A",
    "Korean Speed Baccarat H",
    "Korean Speed Baccarat B",
    "Korean Speed Baccarat A",
    "Korean Speed Baccarat E",
]

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class MaruBatsuSim:
    def __init__(self, target=PROFIT_PER_WIN, lc=LOSS_PER_LOSS):
        self.target = target
        self.lc = lc
        self.reset()

    def reset(self):
        self.cumulative = 0
        self.unit_idx = 0
        self.prev_os = 0
        self.sets = 0
        self.hands = 0
        self.turns = []
        self.max_dd = 0
        self.peak = 0
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
        if self.cumulative > self.peak:
            self.peak = self.cumulative
        self.max_dd = max(self.max_dd, self.peak - self.cumulative)

    def add(self, r):
        if r == 'T':
            return None
        self.hands += 1
        self.turns.append('O' if r == 'P' else 'X')
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        if self.cumulative <= -self.lc:
            return 'loss'
        return None


def simulate_table_sessions(table_name, shoes):
    """Run sim and return list of sessions with timestamps."""
    sessions = []
    sim = MaruBatsuSim()
    for seq, started_at in shoes:
        for r in seq:
            if r not in ('P', 'B', 'T'):
                continue
            o = sim.add(r)
            if o:
                sessions.append({
                    'table': table_name,
                    'started_at': started_at,
                    'outcome': o,
                    'profit': sim.cumulative,
                    'hands': sim.hands,
                    'max_dd': sim.max_dd,
                })
                sim.reset()
    return sessions


def build_table_stats(shoes_by_table):
    """Simulate every table and return per-table statistics."""
    stats = {}
    for tn, shoes in shoes_by_table.items():
        sessions = simulate_table_sessions(tn, shoes)
        if not sessions:
            continue
        wins = sum(1 for s in sessions if s['outcome'] == 'profit')
        losses = sum(1 for s in sessions if s['outcome'] == 'loss')
        total = wins + losses
        if total == 0:
            continue
        stats[tn] = {
            'sessions': sessions,
            '_shoes': shoes,  # ロスカットなしシミュ用に元データを保持
            'shoes': len(shoes),
            'wins': wins,
            'losses': losses,
            'win_rate': wins / total * 100,
            'max_dd': max(s['max_dd'] for s in sessions),
            'total_profit': sum(s['profit'] for s in sessions),
        }
    return stats


def build_ledger(table_stats, table_filter=None):
    """Collect chronological session list for given tables."""
    all_sessions = []
    selected_stats = {}
    for tn, st in table_stats.items():
        if table_filter is not None and tn not in table_filter:
            continue
        all_sessions.extend(st['sessions'])
        selected_stats[tn] = st
    all_sessions.sort(key=lambda s: s['started_at'])

    balance = START_CAPITAL
    rows = []
    peak = balance
    max_dd_dollars = 0
    wins_running = 0
    losses_running = 0
    for i, s in enumerate(all_sessions, 1):
        if s['outcome'] == 'profit':
            delta = PROFIT_PER_WIN
            wins_running += 1
        else:
            delta = -LOSS_PER_LOSS
            losses_running += 1
        balance += delta
        if balance > peak:
            peak = balance
        dd = peak - balance
        if dd > max_dd_dollars:
            max_dd_dollars = dd
        rows.append({
            'turn': i,
            'started_at': s['started_at'],
            'table': s['table'],
            'outcome': s['outcome'],
            'delta': delta,
            'balance': balance,
            'hands': s['hands'],
        })
    return {
        'rows': rows,
        'final_balance': balance,
        'max_dd_dollars': max_dd_dollars,
        'wins': wins_running,
        'losses': losses_running,
        'selected_stats': selected_stats,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>{title}</title>
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
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #6dd5ed; margin-top: 32px; }}
.banner {{
  background: {banner_bg};
  border-left: 5px solid {banner_border};
  padding: 14px 18px;
  margin: 16px 0;
  font-size: 14px;
  border-radius: 4px;
}}
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
.card .label {{ font-size: 12px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 24px; font-weight: bold; color: #ffd700; }}
.card.win {{ border-left-color: #4ade80; }}
.card.win .value {{ color: #4ade80; }}
.card.loss {{ border-left-color: #f87171; }}
.card.loss .value {{ color: #f87171; }}
table {{
  width: 100%;
  border-collapse: collapse;
  background: #1a2332;
  margin: 16px 0;
  font-size: 13px;
}}
th {{
  background: #0f1419;
  color: #ffd700;
  padding: 10px 8px;
  text-align: left;
  border-bottom: 2px solid #ffd700;
  position: sticky;
  top: 0;
}}
td {{ padding: 6px 8px; border-bottom: 1px solid #2a3441; }}
tr.profit td.oc {{ color: #4ade80; font-weight: bold; }}
tr.loss td.oc {{ color: #f87171; font-weight: bold; }}
tr.loss td.dl {{ color: #f87171; }}
tr.profit td.dl {{ color: #4ade80; }}
td.bl {{ font-weight: bold; color: #ffd700; }}
td.turn {{ text-align: right; color: #8a96a8; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
td.hd {{ text-align: right; color: #8a96a8; }}
.ledger-wrapper {{ max-height: 70vh; overflow-y: auto; border: 1px solid #2a3441; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block;
  margin-right: 12px;
  padding: 8px 16px;
  background: #1a2332;
  color: #6dd5ed;
  text-decoration: none;
  border-radius: 4px;
  border: 1px solid #2a3441;
  font-size: 13px;
}}
.nav a.current {{ background: #6dd5ed; color: #0f1419; font-weight: bold; }}
.nav a:hover {{ border-color: #6dd5ed; }}
</style>
</head>
<body>
<div class="container">
<h1>{title}</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="equity_ledger_all.html"{nav_all_class}>A. 全テーブル運用</a>
<a href="equity_ledger_top.html"{nav_top_class}>B. 推奨Top運用</a>
</div>

<div class="banner">{banner_text}</div>

<div class="summary">
  <div class="card">
    <div class="label">スタート資金</div>
    <div class="value">${start:,}</div>
  </div>
  <div class="card {final_class}">
    <div class="label">最終資金</div>
    <div class="value">${final:,}</div>
  </div>
  <div class="card {profit_class}">
    <div class="label">通算損益</div>
    <div class="value">{profit_sign}${total_profit:,}</div>
  </div>
  <div class="card {profit_class}">
    <div class="label">ROI</div>
    <div class="value">{profit_sign}{roi:.1f}%</div>
  </div>
  <div class="card">
    <div class="label">セッション数</div>
    <div class="value">{n_sessions}</div>
  </div>
  <div class="card win">
    <div class="label">勝ちセッション</div>
    <div class="value">{wins}</div>
  </div>
  <div class="card loss">
    <div class="label">負けセッション</div>
    <div class="value">{losses}</div>
  </div>
  <div class="card">
    <div class="label">勝率</div>
    <div class="value">{win_rate:.1f}%</div>
  </div>
  <div class="card loss">
    <div class="label">最大DD ($)</div>
    <div class="value">${max_dd:,}</div>
  </div>
</div>

<h2>対象テーブル一覧 ({n_tables}テーブル)</h2>
<table>
<thead><tr><th>テーブル名</th><th>シュー</th><th>勝ち</th><th>負け</th><th>勝率</th><th>最大DD</th></tr></thead>
<tbody>{table_summary_html}</tbody>
</table>

<h2>セッション台帳（時系列順）</h2>
<p class="note">時系列順にソート。負けセッション（赤）の後に資金がどれだけ落ち込み、その後どう戻すかを確認できます。</p>
<div class="ledger-wrapper">
<table>
<thead>
<tr>
  <th>#</th><th>日時</th><th>テーブル</th><th>結果</th><th>増減</th><th>残高</th><th>消費ハンド</th>
</tr>
</thead>
<tbody>{rows_html}</tbody>
</table>
</div>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_equity_report.py</code> / 利確${profit_per_win}・損切り$-{loss_per_loss}・MaruBatsuロジック適用
</p>

</div>
</body>
</html>
"""


def render_html(out_path, *, title, banner_text, banner_bg, banner_border,
                ledger, total_hands_dataset, total_shoes_dataset,
                is_all_view):
    rows = ledger['rows']
    final_balance = ledger['final_balance']
    total_profit = final_balance - START_CAPITAL
    roi = total_profit / START_CAPITAL * 100
    wins = ledger['wins']
    losses = ledger['losses']
    n = wins + losses
    win_rate = (wins / n * 100) if n else 0
    max_dd = ledger['max_dd_dollars']
    selected_stats = ledger['selected_stats']

    final_class = 'win' if total_profit >= 0 else 'loss'
    profit_class = final_class
    profit_sign = '+' if total_profit >= 0 else ''

    table_summary_html = ""
    for tn, st in sorted(selected_stats.items(), key=lambda x: -x[1]['wins']):
        wr_color = '#4ade80' if st['win_rate'] >= 95 else ('#fbbf24' if st['win_rate'] >= 80 else '#f87171')
        table_summary_html += (
            f"<tr><td>{tn}</td>"
            f"<td>{st['shoes']}</td>"
            f"<td style='color:#4ade80'>{st['wins']}</td>"
            f"<td style='color:#f87171'>{st['losses']}</td>"
            f"<td style='color:{wr_color};font-weight:bold'>{st['win_rate']:.1f}%</td>"
            f"<td>${st['max_dd']}</td></tr>"
        )

    rows_html = ""
    for r in rows:
        ts = r['started_at'][:16].replace('T', ' ') if r['started_at'] else '-'
        outcome_class = 'profit' if r['outcome'] == 'profit' else 'loss'
        outcome_label = 'WIN' if r['outcome'] == 'profit' else 'LOSS'
        delta_sign = '+' if r['delta'] >= 0 else ''
        rows_html += (
            f"<tr class='{outcome_class}'>"
            f"<td class='turn'>{r['turn']}</td>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tn'>{r['table']}</td>"
            f"<td class='oc'>{outcome_label}</td>"
            f"<td class='dl'>{delta_sign}${r['delta']:,}</td>"
            f"<td class='bl'>${r['balance']:,}</td>"
            f"<td class='hd'>{r['hands']}</td>"
            f"</tr>"
        )

    nav_all_class = " class='current'" if is_all_view else ""
    nav_top_class = "" if is_all_view else " class='current'"

    html = HTML_TEMPLATE.format(
        title=title,
        banner_text=banner_text,
        banner_bg=banner_bg,
        banner_border=banner_border,
        nav_all_class=nav_all_class,
        nav_top_class=nav_top_class,
        start=START_CAPITAL,
        final=final_balance,
        final_class=final_class,
        profit_class=profit_class,
        profit_sign=profit_sign,
        total_profit=abs(total_profit),
        roi=abs(roi) if total_profit >= 0 else -abs(roi),
        n_sessions=n,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        max_dd=max_dd,
        n_tables=len(selected_stats),
        table_summary_html=table_summary_html,
        rows_html=rows_html,
        profit_per_win=PROFIT_PER_WIN,
        loss_per_loss=LOSS_PER_LOSS,
    )
    os.makedirs("report", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {out_path}")
    print(f"  Final ${final_balance:,} (P&L {profit_sign}${total_profit:+,}, {wins}W/{losses}L, MaxDD ${max_dd:,})")


def main():
    print(f"Loading {DB_PATH}...")
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

    total_shoes = sum(len(v) for v in shoes_by_table.values())
    print(f"Total {total_hands:,} hands across {len(shoes_by_table)} tables ({total_shoes} shoes)\n")

    # 全テーブルの統計を構築
    table_stats = build_table_stats(shoes_by_table)

    # === A: 全テーブル ledger ===
    ledger_all = build_ledger(table_stats, table_filter=None)

    # === B: Top推奨だけ ledger ===
    available_top = [t for t in TOP_RECOMMENDED if t in table_stats]
    print(f"Top推奨で利用可能: {available_top}\n")
    ledger_top = build_ledger(table_stats, table_filter=set(available_top))

    # 共通のフッタ情報
    dataset_note = (
        f"データソース: analytics.sqlite3 全{total_hands:,}ハンド・{total_shoes}シュー "
        f"（最低{MIN_HANDS_PER_SHOE}ハンド/シュー）"
    )

    # === ファイルA: 全テーブル ===
    render_html(
        os.path.join("report", "equity_ledger_all.html"),
        title="A. 全テーブル運用 — $10,000スタート",
        banner_text=(
            "<strong>⚠️ 推奨フィルタなし。</strong>"
            f"全{len(table_stats)}テーブルをランダムに運用した場合のシミュレーション。"
            "勝てないテーブルや危険なテーブルも含むため、損失セッションが頻発します。"
            "<br>これが何の選別もせず BET した時の本当の姿です。<br>"
            f"{dataset_note}"
        ),
        banner_bg="#3a1a1a",
        banner_border="#f87171",
        ledger=ledger_all,
        total_hands_dataset=total_hands,
        total_shoes_dataset=total_shoes,
        is_all_view=True,
    )

    # === ファイルB: 推奨Topのみ ===
    render_html(
        os.path.join("report", "equity_ledger_top.html"),
        title="B. 推奨Top運用 — $10,000スタート",
        banner_text=(
            "<strong>✅ 推奨Top実運用シナリオ。</strong>"
            f"Sync mode が実際にプレイする推奨テーブル（{', '.join(available_top)}）"
            f"のみで運用した場合のシミュレーション。"
            "<br>テーブルを選別することで損失リスクが大幅に下がる様子を確認できます。<br>"
            f"{dataset_note}"
        ),
        banner_bg="#1a3a1a",
        banner_border="#4ade80",
        ledger=ledger_top,
        total_hands_dataset=total_hands,
        total_shoes_dataset=total_shoes,
        is_all_view=False,
    )

    # === 互換用: 旧 equity_ledger.html を Top に向けたシンボリックHTML ===
    redirect_html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="0; url=equity_ledger_top.html">
</head><body>Redirecting to <a href="equity_ledger_top.html">Top運用版</a>...</body></html>
"""
    with open(os.path.join("report", "equity_ledger.html"), "w", encoding="utf-8") as f:
        f.write(redirect_html)
    print("\nWrote report/equity_ledger.html (redirect → top)")

    # === C: テーブル別 個別 ledger ===
    render_per_table_html(table_stats, total_hands, total_shoes)


def simulate_no_losscut(shoes, start_capital, target=PROFIT_PER_WIN):
    """ロスカットなしシミュレーション。

    各セッションは $50 利確で終了。負ければセッション内損失が増え続ける。
    残高 + セッション内累計 が 0 以下になった瞬間に破綻。

    Returns: {
        'turns': [{turn, started_at, outcome ('profit'/'bankrupt'), balance, session_pnl, hands}],
        'final_balance': float,
        'bankrupt_at': turn_number or None,
    }
    """
    sim = MaruBatsuSim(target=target, lc=10**12)  # 損切りなし（事実上）
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

            # 破綻判定: セッション内損失が残高を上回った
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
                # reset 後は hands もリセットされるので session_start_hand_count もリセット
                session_start_hand_count = 0

        if bankrupt:
            break

    return {
        'turns': turns,
        'final_balance': balance,
        'bankrupt_at': turns[-1]['turn'] if bankrupt else None,
    }


def render_per_table_html(table_stats, total_hands, total_shoes):
    """テーブルごとに独立した equity ledger を1ページにまとめる。

    【ロスカットなしモデル】
    $10,000 元本スタート → 各セッションは $50 利確で終了、負けても止めない。
    セッション内の損失が残高を超えた瞬間に破綻して $0。
    最終資金が低い順（破綻リスク高い順）にソート。
    """
    table_ledgers = []
    eligible_tables = [(tn, st) for tn, st in table_stats.items()
                       if st['shoes'] >= MIN_SHOES_FOR_PER_TABLE]

    for tn, st in eligible_tables:
        ledger = simulate_no_losscut(st['_shoes'], START_CAPITAL)
        rows = ledger['turns']

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
            'shoes': st['shoes'],
            'bankrupt_at': ledger['bankrupt_at'],
            'pnl': final_balance - START_CAPITAL,
        })

    # === 元本別生存テーブル数の比較 ===
    capital_levels = [10000, 20000, 30000, 50000, 100000, 300000]
    capital_comparison = []
    for cap in capital_levels:
        bankrupt_n = 0
        profit_n = 0
        total_bal = 0
        for tn, st in eligible_tables:
            r = simulate_no_losscut(st['_shoes'], cap)
            if r['bankrupt_at']:
                bankrupt_n += 1
            else:
                profit_n += 1
            total_bal += r['final_balance']
        capital_comparison.append({
            'capital': cap,
            'bankrupt': bankrupt_n,
            'survived': profit_n,
            'total_balance': total_bal,
            'total_profit': total_bal - cap * len(eligible_tables),
            'roi': (total_bal - cap * len(eligible_tables)) / (cap * len(eligible_tables)) * 100,
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
            f"<td>${c['total_balance']:,}</td>"
            f"<td style='color:{'#4ade80' if c['total_profit']>=0 else '#f87171'}'>"
            f"{'+' if c['total_profit']>=0 else ''}${c['total_profit']:,}</td>"
            f"<td style='color:{'#4ade80' if c['roi']>=0 else '#f87171'}'>"
            f"{'+' if c['roi']>=0 else ''}{c['roi']:.0f}%</td>"
            f"</tr>"
        )

    # ソート: 最終残高が低い順（危ない順）
    table_ledgers.sort(key=lambda x: x['final_balance'])

    # サマリー集計
    total_tables = len(table_ledgers)
    bankrupt_count = sum(1 for t in table_ledgers if t['bankrupt_at'])
    loss_count = sum(1 for t in table_ledgers if t['pnl'] < 0)
    profit_count = sum(1 for t in table_ledgers if t['pnl'] > 0)

    # テーブル別セクションを生成
    sections_html = ""
    for t in table_ledgers:
        if t['bankrupt_at']:
            status = f"💀 {t['bankrupt_at']}ターン目で破綻"
            status_color = "#7c2d2d"
            status_class = "bankrupt"
        elif t['pnl'] < 0:
            status = f"⚠️ {t['pnl']:+,}$ 損失"
            status_color = "#7a4a1c"
            status_class = "loss"
        elif t['pnl'] > 0:
            status = f"✅ {t['pnl']:+,}$ 利益"
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
            bal_color = '#4ade80' if r['balance'] >= START_CAPITAL else ('#fbbf24' if r['balance'] >= START_CAPITAL * 0.5 else '#f87171')
            rows_html += (
                f"<tr class='{outcome_class}'>"
                f"<td class='turn'>{r['turn']}</td>"
                f"<td class='ts'>{ts}</td>"
                f"<td class='oc'>{outcome_label}</td>"
                f"<td class='hd'>{r['hands']}h</td>"
                f"<td class='bl' style='color:{bal_color}'>${r['balance']:,}</td>"
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
    <div class="balance">最終: <strong>${t['final_balance']:,}</strong> / 最低: ${t['min_balance']:,}</div>
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
<title>C. テーブル別 ターン台帳 — $10,000スタート</title>
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
.banner {{
  background: #2a1a3a;
  border-left: 5px solid #c084fc;
  padding: 14px 18px;
  margin: 16px 0;
  font-size: 14px;
  border-radius: 4px;
}}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block;
  margin-right: 12px;
  padding: 8px 16px;
  background: #1a2332;
  color: #6dd5ed;
  text-decoration: none;
  border-radius: 4px;
  border: 1px solid #2a3441;
  font-size: 13px;
}}
.nav a.current {{ background: #c084fc; color: #0f1419; font-weight: bold; }}
.nav a:hover {{ border-color: #c084fc; }}
.summary {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin: 20px 0;
}}
.card {{
  background: #1a2332;
  padding: 14px;
  border-radius: 4px;
  border-left: 4px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.profit {{ border-left-color: #4ade80; }}
.card.profit .value {{ color: #4ade80; }}
.card.loss {{ border-left-color: #fbbf24; }}
.card.loss .value {{ color: #fbbf24; }}
.card.bankrupt {{ border-left-color: #f87171; }}
.card.bankrupt .value {{ color: #f87171; }}

.table-section {{
  background: #1a2332;
  border-left: 5px solid #6dd5ed;
  margin: 20px 0;
  padding: 16px;
  border-radius: 4px;
}}
.table-section.bankrupt {{ background: #2a1a1a; }}
.table-section.loss {{ background: #2a221a; }}
.table-header {{
  display: grid;
  grid-template-columns: 2fr 1fr 1fr;
  gap: 10px;
  margin-bottom: 12px;
  align-items: center;
}}
.table-header .tname {{ font-size: 18px; font-weight: bold; color: #ffd700; }}
.table-header .tmeta {{ font-size: 12px; color: #8a96a8; margin-left: 12px; }}
.table-header .status {{ font-size: 14px; text-align: center; }}
.table-header .balance {{ font-size: 13px; color: #8a96a8; text-align: right; }}
.table-header .balance strong {{ color: #ffd700; font-size: 16px; }}
.bankrupt .table-header .balance strong {{ color: #f87171; }}
.loss .table-header .balance strong {{ color: #fbbf24; }}

.ledger {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}}
.ledger th {{
  background: #0f1419;
  color: #ffd700;
  padding: 6px 8px;
  text-align: left;
  border-bottom: 1px solid #2a3441;
}}
.ledger td {{ padding: 4px 8px; border-bottom: 1px solid #2a3441; }}
.ledger tr.profit td.oc {{ color: #4ade80; }}
.ledger tr.loss td.oc {{ color: #f87171; font-weight: bold; }}
.ledger td.turn {{ text-align: right; color: #8a96a8; }}
.ledger td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.ledger td.bl {{ font-weight: bold; text-align: right; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
</style>
</head>
<body>
<div class="container">
<h1>C. テーブル別 ターン台帳</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="equity_ledger_all.html">A. 全テーブル運用</a>
<a href="equity_ledger_top.html">B. 推奨Top運用</a>
<a href="equity_per_table.html" class="current">C. テーブル別</a>
</div>

<div class="banner">
<strong>📊 ロスカットなしモデル — 各テーブルを独立にシミュレーション。</strong><br>
$10,000元本スタート → セッションは <strong>$50 利確で終了</strong>、ロスカットは設けません。<br>
1ターン = 1セッション完了。$50稼ぐごとに残高は $10,050 → $10,100 → $10,150... と増えていきます。<br>
ただし負けが続くとセッション内損失が膨らみ、<strong>残高 + 累計損失 が 0以下になった瞬間に破綻 (BANKRUPT)</strong>。<br>
〇✖ロジックの本質: 「コツコツ$50を積んで、ある日連敗で全額消える」その瞬間がいつ来るか。<br>
データソース: 全{total_hands:,}ハンド・{total_shoes}シュー / 各テーブル独立シミュレーション。<br>
<strong>並び順: 最終資金の少ない順（破綻したテーブルが上）</strong>
</div>

<div class="summary">
  <div class="card">
    <div class="label">対象テーブル数</div>
    <div class="value">{total_tables}</div>
  </div>
  <div class="card profit">
    <div class="label">プラス終了 ($10k)</div>
    <div class="value">{profit_count}</div>
  </div>
  <div class="card bankrupt">
    <div class="label">破綻 ($10k)</div>
    <div class="value">{bankrupt_count}</div>
  </div>
</div>

<h2 style="color:#c084fc;margin-top:32px;">💰 元本別 生存テーブル数の比較</h2>
<p class="note">
  元本を増やせばセッション内ドローダウンを吸収できる範囲が広がり、破綻するテーブルが減ります。<br>
  ただし「絶対に死ぬテーブル」は元本いくらでも沈むので注意。
</p>
<table style="margin-bottom:32px;font-size:14px;">
<thead>
<tr>
  <th>元本</th>
  <th>破綻</th>
  <th>生存</th>
  <th>破綻率</th>
  <th>通算残高</th>
  <th>通算損益</th>
  <th>ROI</th>
</tr>
</thead>
<tbody>{capital_table_html}</tbody>
</table>

<h2 style="color:#c084fc;">📋 各テーブルのターン台帳 (元本$10,000)</h2>

{sections_html}

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_equity_report.py</code> / 各テーブル独立シミュレーション / 利確$50・損切り$-3,000・MaruBatsuロジック
</p>

</div>
</body>
</html>
"""
    out_path = os.path.join("report", "equity_per_table.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")
    print(f"  {total_tables} tables: {profit_count} profit, {loss_count} loss, {bankrupt_count} bankrupt")


if __name__ == "__main__":
    main()
