"""リアルタイム テレコ入退室 × 逆張り バックテスト

運用フロー:
  1. 62テーブルの大路を時系列にリアルタイム監視
  2. 直近N列が 1落ち+2落ちで80%以上 → テレコ状態 → 入室
  3. 逆張りでBET (前手P→B BET, 前手B→P BET)
  4. 退室条件: 3落ち以上が2回発生 or 4落ちが1回発生 → 即退室
  5. ロビーに戻り、別のテレコテーブルを探す
  6. Banker BET時: unit × 1.0526 (手数料相殺)

全テーブルのハンドを started_at でソートし、時系列でシミュレーション。

Usage:
  python generate_realtime_counter_backtest.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict, Counter

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
DATE_FROM = "2026-04-06"
MIN_HANDS = 50
PROFIT_TARGET = 50
BANKER_COMMISSION = 0.05
BANKER_UNIT_MULT = 1.0 / (1.0 - BANKER_COMMISSION)

# テレコ判定パラメータ
TEREKO_WINDOW = 10        # 直近何列で判定
TEREKO_THRESHOLD = 0.80   # 1落ち+2落ちの割合
# 退室条件
EXIT_3DROP_LIMIT = 2      # 3落ち以上がN回で退室
EXIT_4DROP_IMMEDIATE = True  # 4落ち1回で即退室

SEQ = [1, 2, 3, 5, 7, 9, 11, 13, 16, 19, 22, 25, 28, 31, 35, 39, 43, 47, 51, 55,
       60, 65, 70, 75, 80, 85, 90, 95, 100, 106, 112, 118, 124, 130, 136, 142,
       148, 154, 160, 170, 180, 190, 200, 210, 220, 230, 240, 250]


class MaruBatsuSim:
    def __init__(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0.0
        self.max_dd = 0.0
        self.sessions_won = 0
        self.total_profit = 0.0
        self.hands_bet = 0

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
            if bet_side == 'B':
                actual = base_unit * BANKER_UNIT_MULT
            else:
                actual = base_unit
            if outcome == 'O':
                if bet_side == 'B':
                    money += actual * (1.0 - BANKER_COMMISSION)
                else:
                    money += actual
            else:
                money -= actual

        wins = sum(1 for t in self.turns if t[0] == 'O')
        diff = wins - (7 - wins)
        self.cumulative += money
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

    def add_bet(self, won: bool, bet_side: str):
        self.hands_bet += 1
        self.turns.append(('O' if won else 'X', bet_side))
        if len(self.turns) == 7:
            self._complete()
        if self.cumulative >= PROFIT_TARGET:
            self.total_profit += self.cumulative
            self.sessions_won += 1
            self.cumulative = 0.0
            self.unit_idx = 0
            self.prev_os = 0
            self.turns = []
            self.history = []
            self.peak = 0.0


class TableState:
    """テーブルの大路リアルタイム状態"""
    def __init__(self, name):
        self.name = name
        self.columns = []       # 大路列長リスト [1, 2, 1, 3, ...]
        self.current_col = 0    # 現在の列長
        self.last_side = None   # 現在の列の側 (P/B)
        self.last_nt = None     # 直前の非タイ結果
        self.drop3_count = 0    # 入室後の3落ち以上カウント
        self.is_active = False  # 現在BET中か
        self.entry_col_idx = 0  # 入室時のcolumn index
        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.sessions = []      # [(entry_time, exit_time, reason, bets, wins)]

    def feed(self, ch):
        """P/B/T を feed して大路を更新"""
        if ch == 'T':
            return
        if ch == self.last_side:
            self.current_col += 1
        else:
            if self.last_side is not None:
                self.columns.append(self.current_col)
            self.current_col = 1
            self.last_side = ch
        self.last_nt = ch

    def finalize_shoe(self):
        """シュー終了時に現在の列を確定"""
        if self.current_col > 0 and self.last_side is not None:
            self.columns.append(self.current_col)
        self.current_col = 0
        self.last_side = None
        # last_nt は保持 (次のシューでも逆張りの基準に使える)

    def is_tereko(self) -> bool:
        """直近N列がテレコ状態か"""
        if len(self.columns) < TEREKO_WINDOW:
            return False
        recent = self.columns[-TEREKO_WINDOW:]
        short = sum(1 for L in recent if L <= 2)
        return (short / len(recent)) >= TEREKO_THRESHOLD

    def check_exit(self) -> str | None:
        """退室条件チェック。退室理由を返す (None=継続)"""
        if not self.is_active:
            return None
        # 入室後の列のみチェック
        cols_since_entry = self.columns[self.entry_col_idx:]
        # 現在進行中の列も含める
        check_cols = cols_since_entry
        if self.current_col >= 3:
            check_cols = list(cols_since_entry) + [self.current_col]

        drop3_count = sum(1 for L in check_cols if L >= 3)
        if EXIT_4DROP_IMMEDIATE:
            for L in check_cols:
                if L >= 4:
                    return "4落ち発生"
            # 現在進行中の列が4以上
            if self.current_col >= 4:
                return "4落ち発生(進行中)"
        if drop3_count >= EXIT_3DROP_LIMIT:
            return f"3落ち以上×{drop3_count}"
        return None

    def enter(self):
        self.is_active = True
        self.entry_col_idx = len(self.columns)
        self.drop3_count = 0

    def exit(self):
        self.is_active = False


def load_all_hands():
    """全テーブルのハンドをシュー単位で読み込み、時系列ソート"""
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


def run_backtest():
    shoes = load_all_hands()
    print(f"Loaded {len(shoes)} shoes")

    sim = MaruBatsuSim()

    # 比較用: 退室条件なし版
    sim_no_exit = MaruBatsuSim()

    table_states = {}
    stats = {
        'total_entries': 0, 'total_exits': 0,
        'bets': 0, 'wins': 0, 'losses': 0,
        'exit_reasons': Counter(),
    }
    stats_no_exit = {'bets': 0, 'wins': 0, 'losses': 0}

    # テーブル別追跡
    table_stats = defaultdict(lambda: {
        'entries': 0, 'bets': 0, 'wins': 0, 'losses': 0,
        'bets_ne': 0, 'wins_ne': 0, 'losses_ne': 0, 'shoes': 0,
    })

    # 入退室ログ
    entry_exit_log = []
    session_log = []

    # テレコ状態のテーブルが常にどれくらいあるかの時系列
    tereko_available_counts = []

    current_table = None  # 現在 BET しているテーブル
    current_entry_ts = None
    current_session_bets = 0
    current_session_wins = 0

    for si, (table_name, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si}/{len(shoes)}...")

        if table_name not in table_states:
            table_states[table_name] = TableState(table_name)
        ts = table_states[table_name]
        table_stats[table_name]['shoes'] += 1

        clean = ''.join(ch for ch in seq if ch in ('P', 'B', 'T'))

        for ch in clean:
            if ch == 'T':
                ts.feed(ch)
                continue

            # === 退室チェック (BET前に) ===
            if current_table == table_name and ts.is_active:
                exit_reason = ts.check_exit()
                if exit_reason:
                    ts.exit()
                    stats['total_exits'] += 1
                    stats['exit_reasons'][exit_reason] += 1
                    session_log.append({
                        'table': table_name,
                        'entry_ts': current_entry_ts,
                        'exit_ts': started_at,
                        'reason': exit_reason,
                        'bets': current_session_bets,
                        'wins': current_session_wins,
                        'pnl': current_session_wins - (current_session_bets - current_session_wins),
                    })
                    current_table = None
                    current_entry_ts = None

            # === 入室チェック (BET中でなければ) ===
            if current_table is None:
                # 全テーブルからテレコ状態のものを探す
                # (実運用ではロビーで全テーブル監視)
                tereko_tables = [
                    name for name, state in table_states.items()
                    if state.is_tereko() and not state.is_active
                ]
                tereko_available_counts.append(len(tereko_tables))

                if ts.is_tereko():
                    # このテーブルに入室
                    ts.enter()
                    current_table = table_name
                    current_entry_ts = started_at
                    current_session_bets = 0
                    current_session_wins = 0
                    stats['total_entries'] += 1
                    table_stats[table_name]['entries'] += 1

            # === BET (入室中のテーブルのみ) ===
            if current_table == table_name and ts.is_active and ts.last_nt is not None:
                # 逆張り
                bet_side = 'P' if ts.last_nt == 'B' else 'B'
                won = (ch == bet_side)

                # メイン (退室条件あり)
                stats['bets'] += 1
                current_session_bets += 1
                table_stats[table_name]['bets'] += 1
                if won:
                    stats['wins'] += 1
                    current_session_wins += 1
                    table_stats[table_name]['wins'] += 1
                else:
                    stats['losses'] += 1
                    table_stats[table_name]['losses'] += 1
                sim.add_bet(won, bet_side)

            # === 比較用: 退室条件なし (テレコ混合シューなら全ハンド逆張り) ===
            # (テーブルがテレコ状態なら常にBET、退室しない)
            if ts.is_tereko() and ts.last_nt is not None:
                bet_side = 'P' if ts.last_nt == 'B' else 'B'
                won = (ch == bet_side)
                stats_no_exit['bets'] += 1
                table_stats[table_name]['bets_ne'] += 1
                if won:
                    stats_no_exit['wins'] += 1
                    table_stats[table_name]['wins_ne'] += 1
                else:
                    stats_no_exit['losses'] += 1
                    table_stats[table_name]['losses_ne'] += 1
                sim_no_exit.add_bet(won, bet_side)

            # 大路更新
            ts.feed(ch)

        # シュー終了
        ts.finalize_shoe()

    # 最終セッションを記録
    if current_table and current_session_bets > 0:
        session_log.append({
            'table': current_table,
            'entry_ts': current_entry_ts,
            'exit_ts': 'END',
            'reason': 'データ終了',
            'bets': current_session_bets,
            'wins': current_session_wins,
            'pnl': current_session_wins - (current_session_bets - current_session_wins),
        })

    avg_tereko = sum(tereko_available_counts) / len(tereko_available_counts) if tereko_available_counts else 0

    return (stats, stats_no_exit, sim, sim_no_exit, table_stats,
            session_log, avg_tereko, len(shoes))


def render_html(stats, stats_ne, sim, sim_ne, table_stats,
                session_log, avg_tereko, total_shoes):

    def hr(s):
        return s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0

    def fpnl(s):
        return s['wins'] - s['losses']

    mb_pnl = sim.total_profit + sim.cumulative
    mb_pnl_ne = sim_ne.total_profit + sim_ne.cumulative

    # 比較表
    comp_rows = ""
    for label, s, sm, color in [
        ("逆張り + 退室条件あり", stats, sim, "#4ade80"),
        ("逆張り + 退室条件なし", stats_ne, sim_ne, "#fbbf24"),
    ]:
        h = hr(s)
        fp = fpnl(s)
        mp = sm.total_profit + sm.cumulative
        fp_c = '#4ade80' if fp >= 0 else '#f87171'
        mp_c = '#4ade80' if mp >= 0 else '#f87171'
        comp_rows += (
            f"<tr>"
            f"<td style='color:{color};font-weight:bold'>{label}</td>"
            f"<td>{s['bets']:,}</td>"
            f"<td>{s['wins']:,}</td>"
            f"<td>{s['losses']:,}</td>"
            f"<td style='font-weight:bold'>{h:.2f}%</td>"
            f"<td style='color:{fp_c};font-weight:bold'>{fp:+,}</td>"
            f"<td>{sm.sessions_won:,}</td>"
            f"<td style='color:{mp_c};font-weight:bold'>${mp:+,.0f}</td>"
            f"<td>${sm.max_dd:,.0f}</td>"
            f"</tr>"
        )

    # 退室理由
    exit_html = ""
    for reason, cnt in stats['exit_reasons'].most_common():
        exit_html += f"<tr><td>{reason}</td><td>{cnt:,}</td></tr>"

    # テーブル別
    t_list = sorted(table_stats.items(), key=lambda x: -(x[1]['wins'] - x[1]['losses']))
    table_html = ""
    for tn, t in t_list:
        fp = t['wins'] - t['losses']
        fp_ne = t['wins_ne'] - t['losses_ne']
        fp_c = '#4ade80' if fp > 0 else ('#f87171' if fp < 0 else '#555')
        fp_ne_c = '#4ade80' if fp_ne > 0 else ('#f87171' if fp_ne < 0 else '#555')
        diff = fp - fp_ne
        diff_c = '#4ade80' if diff > 0 else ('#f87171' if diff < 0 else '#555')
        h = t['wins'] / t['bets'] * 100 if t['bets'] > 0 else 0
        table_html += (
            f"<tr>"
            f"<td class='tname'>{tn}</td>"
            f"<td>{t['shoes']}</td>"
            f"<td>{t['entries']}</td>"
            f"<td>{t['bets']:,}</td>"
            f"<td>{h:.1f}%</td>"
            f"<td style='color:{fp_c};font-weight:bold'>{fp:+d}</td>"
            f"<td style='color:{fp_ne_c}'>{fp_ne:+d}</td>"
            f"<td style='color:{diff_c}'>{diff:+d}</td>"
            f"</tr>"
        )

    # 入退室ログ (最新50)
    log_html = ""
    for s in session_log[-50:]:
        ts = s['entry_ts'][:16].replace('T', ' ') if s['entry_ts'] else '-'
        pnl_c = '#4ade80' if s['pnl'] > 0 else ('#f87171' if s['pnl'] < 0 else '#555')
        wr = s['wins'] / s['bets'] * 100 if s['bets'] > 0 else 0
        log_html += (
            f"<tr>"
            f"<td class='ts'>{ts}</td>"
            f"<td class='tname'>{s['table']}</td>"
            f"<td>{s['reason']}</td>"
            f"<td>{s['bets']}</td>"
            f"<td>{wr:.0f}%</td>"
            f"<td style='color:{pnl_c}'>{s['pnl']:+d}</td>"
            f"</tr>"
        )

    avoided_loss = fpnl(stats) - fpnl(stats_ne)
    avoided_label = "改善" if avoided_loss > 0 else "悪化"
    avoided_color = "green" if avoided_loss > 0 else "red"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>O. リアルタイム テレコ入退室 バックテスト</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, "Segoe UI", "Hiragino Sans", "Yu Gothic UI", sans-serif;
  background: #0f1419; color: #e0e6ed; margin: 0; padding: 24px; line-height: 1.5;
}}
.container {{ max-width: 1400px; margin: 0 auto; }}
h1 {{ color: #ffd700; font-size: 28px; border-bottom: 2px solid #ffd700; padding-bottom: 8px; }}
h2 {{ color: #c084fc; margin-top: 32px; font-size: 22px; }}
.nav {{ margin: 16px 0 24px 0; }}
.nav a {{
  display: inline-block; margin-right: 12px; padding: 8px 16px;
  background: #1a2332; color: #6dd5ed; text-decoration: none;
  border-radius: 4px; border: 1px solid #2a3441; font-size: 13px;
}}
.nav a:hover {{ border-color: #c084fc; }}
.banner {{
  background: #1a2a1a; border-left: 5px solid #4ade80;
  padding: 14px 18px; margin: 16px 0; font-size: 14px; border-radius: 4px; line-height: 1.8;
}}
.banner strong {{ color: #4ade80; }}
.summary {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px; margin: 20px 0;
}}
.card {{
  background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed;
}}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.green .value {{ color: #4ade80; }}
.card.red .value {{ color: #f87171; }}
.card.yellow .value {{ color: #fbbf24; }}

table {{
  width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0;
}}
table th {{
  background: #0f1419; color: #c084fc; padding: 8px; text-align: left;
  border-bottom: 2px solid #2a3441;
}}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
table tr:hover {{ background: rgba(255,255,255,0.03); }}
td.tname {{ font-weight: bold; color: #ffd700; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
.note {{ color: #8a96a8; font-size: 13px; margin: 8px 0; }}
.highlight {{
  background: #11192a; border: 2px solid #4ade80; border-radius: 8px;
  padding: 16px; margin: 16px 0;
}}
.highlight h3 {{ margin-top: 0; color: #4ade80; }}
</style>
</head>
<body>
<div class="container">
<h1>O. リアルタイム テレコ入退室 × 逆張り バックテスト</h1>

<div class="nav">
<a href="index.html">← レポートTOP</a>
<a href="counter_tereko_backtest.html">N. テレコ逆張り</a>
</div>

<div class="banner">
<strong>📊 テレコ状態のテーブルに動的に入退室する実運用シミュレーション。</strong><br>
<strong>入室:</strong> 直近{TEREKO_WINDOW}列が 1落ち+2落ち {TEREKO_THRESHOLD*100:.0f}%以上 → テレコ状態 → 入室<br>
<strong>BET:</strong> 逆張り (前手P→B BET, 前手B→P BET) + Banker BET時 unit×{BANKER_UNIT_MULT:.4f}<br>
<strong>退室:</strong> 3落ち以上×{EXIT_3DROP_LIMIT}回 or 4落ち×1回 → 即退室 → ロビーで次のテレコテーブルを探す<br>
<strong>資金管理:</strong> 〇✖ MaruBatsu ($50利確)<br>
データ: {DATE_FROM}〜本日 / {total_shoes:,}シュー / 62テーブル同時監視
</div>

<div class="summary">
  <div class="card">
    <div class="label">入室回数</div>
    <div class="value">{stats['total_entries']:,}</div>
  </div>
  <div class="card">
    <div class="label">退室回数</div>
    <div class="value">{stats['total_exits']:,}</div>
  </div>
  <div class="card green">
    <div class="label">勝率</div>
    <div class="value">{hr(stats):.2f}%</div>
  </div>
  <div class="card {'green' if fpnl(stats) >= 0 else 'red'}">
    <div class="label">Flat PNL</div>
    <div class="value">{fpnl(stats):+,}</div>
  </div>
  <div class="card {'green' if mb_pnl >= 0 else 'red'}">
    <div class="label">MB累計PNL</div>
    <div class="value">${mb_pnl:+,.0f}</div>
  </div>
  <div class="card">
    <div class="label">MB完走</div>
    <div class="value">{sim.sessions_won:,}</div>
  </div>
  <div class="card">
    <div class="label">平均テレコテーブル数</div>
    <div class="value">{avg_tereko:.1f}</div>
  </div>
  <div class="card {avoided_color}">
    <div class="label">退室条件の効果 (Flat PNL差)</div>
    <div class="value">{avoided_loss:+,} {avoided_label}</div>
  </div>
</div>

<h2>1. 退室条件あり vs なし 比較</h2>
<table>
<thead><tr>
  <th>戦略</th><th>BET数</th><th>勝ち</th><th>負け</th>
  <th>勝率</th><th>Flat PNL</th><th>MB完走</th><th>MB累計PNL</th><th>MB MaxDD</th>
</tr></thead>
<tbody>{comp_rows}</tbody>
</table>

<div class="highlight">
<h3>📌 運用フローの解説</h3>
<p class="note">
<strong>1テーブルずつ順番に BET</strong> するシミュレーション。<br>
実運用と同じく「1テーブルに入室 → BET → 退室条件発動 → ロビーに戻る → 次のテレコテーブルを探す」。<br>
62テーブル全てのハンドは裏で常に大路を更新し続けている (ロビー監視に相当)。<br>
<strong>退室条件なし版</strong>はテレコ判定されたら退室せずBET し続ける版 (比較用)。
</p>
</div>

<h2>2. 退室理由の内訳</h2>
<table style="max-width:400px">
<thead><tr><th>理由</th><th>回数</th></tr></thead>
<tbody>{exit_html}</tbody>
</table>

<h2>3. テーブル別 パフォーマンス</h2>
<p class="note">Flat PNL (退室あり) の降順。差分 = 退室あり − 退室なし (プラスなら退室条件が損失を回避した)。</p>
<table>
<thead><tr>
  <th>テーブル</th><th>シュー</th><th>入室</th><th>BET数</th><th>勝率</th>
  <th style="color:#4ade80">PNL (退室あり)</th>
  <th style="color:#fbbf24">PNL (退室なし)</th>
  <th>差分</th>
</tr></thead>
<tbody>{table_html}</tbody>
</table>

<h2>4. 入退室ログ (直近50件)</h2>
<table>
<thead><tr>
  <th>入室時刻</th><th>テーブル</th><th>退室理由</th>
  <th>BET数</th><th>勝率</th><th>PNL</th>
</tr></thead>
<tbody>{log_html}</tbody>
</table>

<p class="note" style="margin-top:32px;">
  生成元: <code>generate_realtime_counter_backtest.py</code> /
  テレコ判定: 直近{TEREKO_WINDOW}列・{TEREKO_THRESHOLD*100:.0f}%閾値 /
  退室: 3落ち×{EXIT_3DROP_LIMIT} or 4落ち×1
</p>
</div>
</body>
</html>
"""
    out_path = os.path.join("report", "realtime_counter_backtest.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


def main():
    result = run_backtest()
    stats, stats_ne, sim, sim_ne, table_stats, session_log, avg_tereko, total_shoes = result

    print(f"\n{'='*80}")
    print(f"入室: {stats['total_entries']:,}  退室: {stats['total_exits']:,}")
    print(f"平均テレコテーブル数: {avg_tereko:.1f}")
    print(f"\n退室条件あり:")
    print(f"  BET: {stats['bets']:,}  WIN: {stats['wins']:,}  LOSS: {stats['losses']:,}")
    print(f"  勝率: {stats['wins']/stats['bets']*100:.2f}%  Flat PNL: {stats['wins']-stats['losses']:+,}")
    mb_pnl = sim.total_profit + sim.cumulative
    print(f"  MB完走: {sim.sessions_won}  MB PNL: ${mb_pnl:+,.0f}  MaxDD: ${sim.max_dd:,.0f}")

    print(f"\n退室条件なし:")
    print(f"  BET: {stats_ne['bets']:,}  WIN: {stats_ne['wins']:,}  LOSS: {stats_ne['losses']:,}")
    hr_ne = stats_ne['wins']/stats_ne['bets']*100 if stats_ne['bets'] > 0 else 0
    print(f"  勝率: {hr_ne:.2f}%  Flat PNL: {stats_ne['wins']-stats_ne['losses']:+,}")
    mb_ne = sim_ne.total_profit + sim_ne.cumulative
    print(f"  MB完走: {sim_ne.sessions_won}  MB PNL: ${mb_ne:+,.0f}  MaxDD: ${sim_ne.max_dd:,.0f}")

    print(f"\n退室理由: {dict(stats['exit_reasons'].most_common())}")

    render_html(stats, stats_ne, sim, sim_ne, table_stats,
                session_log, avg_tereko, total_shoes)


if __name__ == "__main__":
    main()
