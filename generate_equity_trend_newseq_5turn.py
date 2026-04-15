"""順張り (Trend) バックテスト — 縦流れ環境を狙い撃ち

設計:
  - 入場: 縦流れ判定 (直近15列中の長列比率 >= 閾値)
  - BET: 直前の非Tieと同じ側 (Follow the flow)
  - 退出: テレコ条件復活 (短列多発) で切る
  - SEQ: 逆張りと同じ新SEQ×5ターン制を流用 (検証の公平性)
  - シュー条件: 固定 "テレコ+ニコ混合" フィルタは外す (縦流れは別パターン)

目的:
  縦流れ環境だけを狙って順張りしたときの PNL / 破綻率 / DD を 23K で検証。
  逆張り単独と比較し、ハイブリッド戦略の価値を判断。

Usage:
  python generate_equity_trend_newseq_5turn.py --vps
"""
import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH = "analytics_vps.sqlite3" if "--vps" in sys.argv else "analytics.sqlite3"
START_CAPITAL = 10000
PROFIT_PER_WIN = 30
BANKER_COMMISSION = 0.05
MIN_HANDS_PER_SHOE = 50
MIN_SHOES_FOR_PER_TABLE = 30 if "--vps" in sys.argv else 5
SET_SIZE = 5

# --- 縦流れ判定パラメータ (要検証範囲) ---
ENTRY_WINDOW = 15           # 直近何列で判定
LONG_COL_MIN = 3            # 「長列」の定義 (3以上)
TREND_ENTRY_THRESHOLDS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]  # grid search

# --- 退出条件 (テレコ復活検知) ---
# A: 短列 (<=2) が連続 N 回
# B: 直近 K 列の短列比率が X 以上
EXIT_SHORT_CONSEC = 3       # 短列が連続3回で退出 (tereko復活シグナル)

# --- 新SEQ (逆張りと同じ) ---
SEQ_NEW = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
           60, 70, 80, 90, 100, 110, 120, 130,
           145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
           300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]


def compute_columns(seq):
    """P/B シーケンスから列長リストを返す。Tは無視。"""
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


def long_col_ratio(cols, window=ENTRY_WINDOW, min_len=LONG_COL_MIN):
    """直近window列中、長列(>=min_len)の比率を返す。"""
    if len(cols) < window:
        return 0.0
    recent = cols[-window:]
    return sum(1 for c in recent if c >= min_len) / len(recent)


def is_trend_state(cols, threshold, window=ENTRY_WINDOW, min_len=LONG_COL_MIN):
    """縦流れ環境判定。"""
    return long_col_ratio(cols, window, min_len) >= threshold


def decide_trend_bet(last_non_tie):
    """順張り: 直前と同じ側に BET。"""
    if last_non_tie == 'P':
        return 'player'
    if last_non_tie == 'B':
        return 'banker'
    return None


def should_exit_trend(cols_since_entry, current_col_len):
    """テレコ復活で退出: 短列が連続 EXIT_SHORT_CONSEC 回発生したら終了。"""
    check = list(cols_since_entry)
    if current_col_len >= 1:
        check.append(current_col_len)
    # 末尾から短列(<=2) が何連続か
    consec = 0
    for c in reversed(check):
        if c <= 2:
            consec += 1
            if consec >= EXIT_SHORT_CONSEC:
                return f"tereko-back-{consec}"
        else:
            break
    return None


class TrendSim:
    """SEQ ×5ターン制のシミュレーション (CounterSim と同構造、順張り用)"""
    def __init__(self, seq, target=PROFIT_PER_WIN, set_size=5):
        self.seq = seq
        self.target = target
        self.set_size = set_size
        self.reset()

    def reset(self):
        self.cumulative = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []
        self.peak = 0.0
        self.max_dd = 0.0
        self.last_non_tie = None

    def abandon_set(self):
        self.turns = []

    def _next_idx(self, used_idx, diff, new_os):
        if diff < 0:
            return min(used_idx + 1, len(self.seq) - 1)
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
                    bad, ba = dd, s['next']
                if dd < 0 and (-dd) < bbd:
                    bbd, bb = -dd, s['next']
        if ba >= 0:
            return ba
        if bb >= 0:
            return min(bb + 1, len(self.seq) - 1)
        return 0

    def _complete(self):
        base_unit = self.seq[self.unit_idx] if self.unit_idx < len(self.seq) else self.seq[-1]
        money = 0.0
        for outcome, bet_side in self.turns:
            if outcome == 'O':
                money += base_unit * (1.0 - BANKER_COMMISSION) if bet_side == 'B' else base_unit
            else:
                money -= base_unit
        wins = sum(1 for t in self.turns if t[0] == 'O')
        losses = self.set_size - wins
        diff = wins - losses
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

    def feed(self, r, bet_mode='trend'):
        if r == 'T':
            return None
        if self.last_non_tie is None:
            self.last_non_tie = r
            return None
        # 順張り: 直前と同じ側、逆張り: 反対
        if bet_mode == 'trend':
            bet_side = 'P' if self.last_non_tie == 'P' else 'B'
        else:
            bet_side = 'P' if self.last_non_tie == 'B' else 'B'
        won = (r == bet_side)
        self.turns.append(('O' if won else 'X', bet_side))
        self.last_non_tie = r
        if len(self.turns) == self.set_size:
            self._complete()
        if self.cumulative >= self.target:
            return 'profit'
        return None


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def simulate_trend(shoes, start_capital, threshold):
    """縦流れ環境だけで順張り、テレコ復活で退出。"""
    sim = TrendSim(seq=SEQ_NEW, target=PROFIT_PER_WIN, set_size=SET_SIZE)
    balance = start_capital
    turns = []
    bankrupt = False
    session_start_ts = None

    total_entries = 0
    total_exits_tereko = 0

    peak_equity = start_capital
    true_max_dd = 0.0

    for seq, started_at in shoes:
        if len(strip_ties(seq)) < MIN_HANDS_PER_SHOE:
            continue

        in_position = False
        columns_at_entry = 0
        pb_buffer = []

        for r in seq:
            if r not in ('P', 'B', 'T'):
                continue
            if r in ('P', 'B'):
                pb_buffer.append(r)

            columns = compute_columns(pb_buffer)
            current_col_len = columns[-1] if columns else 0

            # --- 退出チェック ---
            if in_position and columns:
                columns_since_entry = columns[columns_at_entry:]
                cse_prev = columns_since_entry[:-1] if columns_since_entry else []
                reason = should_exit_trend(cse_prev, current_col_len)
                if reason:
                    in_position = False
                    sim.abandon_set()
                    total_exits_tereko += 1

            # --- エントリー判定 ---
            if not in_position:
                if is_trend_state(columns, threshold):
                    in_position = True
                    columns_at_entry = len(columns)
                    total_entries += 1
                    if session_start_ts is None:
                        session_start_ts = started_at
                    sim.last_non_tie = pb_buffer[-1] if pb_buffer else None
                continue

            # --- エントリー中: 順張り BET ---
            result = sim.feed(r, bet_mode='trend')

            equity = balance + sim.cumulative
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > true_max_dd:
                true_max_dd = dd

            if balance + sim.cumulative <= 0:
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'bankrupt', 'session_pnl': sim.cumulative, 'balance': 0})
                balance = 0
                bankrupt = True
                break
            if result == 'profit':
                balance += sim.cumulative
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'profit', 'session_pnl': sim.cumulative, 'balance': balance})
                session_start_ts = None
                sim.reset()
                in_position = False

        if bankrupt:
            break

    return {
        'turns': turns,
        'final_balance': balance,
        'bankrupt_at': turns[-1]['turn'] if bankrupt else None,
        'max_dd': true_max_dd,
        'entries': total_entries,
        'exits_tereko': total_exits_tereko,
    }


def load_shoes_by_table():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT table_name, result_sequence, started_at FROM shoes_analytics "
                "WHERE hand_count >= ? ORDER BY started_at",
                (MIN_HANDS_PER_SHOE,))
    sbt = defaultdict(list)
    th = 0
    for tn, seq, ts in cur.fetchall():
        sbt[tn].append((seq, ts))
        th += sum(1 for c in seq if c in ('P', 'B', 'T'))
    conn.close()
    return sbt, th


def main():
    print(f"Loading {DB_PATH}...")
    sbt, th = load_shoes_by_table()
    ts = sum(len(v) for v in sbt.values())
    eligible = [(tn, shoes) for tn, shoes in sbt.items() if len(shoes) >= MIN_SHOES_FOR_PER_TABLE]
    print(f"Total {th:,} hands / {len(sbt)} tables / {ts} shoes")
    print(f"Trend entry: long({LONG_COL_MIN}+) ratio >= threshold over {ENTRY_WINDOW} cols")
    print(f"Exit: {EXIT_SHORT_CONSEC} consecutive short cols\n")

    all_results = {}
    for threshold in TREND_ENTRY_THRESHOLDS:
        label = f"Trend T={threshold}"
        print(f"  {label}...")
        tbl = []
        total_entries = 0
        total_exits = 0
        for tn, shoes in eligible:
            r = simulate_trend(shoes, START_CAPITAL, threshold)
            tbl.append({'name': tn, 'pnl': r['final_balance'] - START_CAPITAL,
                        'bankrupt': r['bankrupt_at'] is not None, 'max_dd': r['max_dd'],
                        'final': r['final_balance'], 'turns': r['turns'], 'shoes': len(shoes),
                        'entries': r['entries'], 'ex_tereko': r['exits_tereko']})
            total_entries += r['entries']
            total_exits += r['exits_tereko']
        bankrupt_n = sum(1 for t in tbl if t['bankrupt'])
        profit_n = sum(1 for t in tbl if t['pnl'] > 0)
        total_pnl = sum(t['pnl'] for t in tbl)
        worst_dd = max((t['max_dd'] for t in tbl), default=0)
        avg_dd = sum(t['max_dd'] for t in tbl) / len(tbl) if tbl else 0
        all_results[label] = {'tables': tbl, 'bankrupt': bankrupt_n, 'profit': profit_n,
                              'total_pnl': total_pnl, 'worst_dd': worst_dd, 'avg_dd': avg_dd,
                              'entries': total_entries, 'exits': total_exits,
                              'threshold': threshold}
        print(f"    破綻{bankrupt_n} / 黒字{profit_n}/{len(tbl)} / PNL ${total_pnl:+,.0f} "
              f"/ エントリー{total_entries} / 退出{total_exits}")

    # HTML 出力
    comp_rows = ""
    for label in all_results:
        r = all_results[label]
        pnl_c = '#4ade80' if r['total_pnl'] >= 0 else '#f87171'
        comp_rows += (f"<tr><td style='font-weight:bold'>{label}</td>"
                      f"<td>{r['bankrupt']}</td><td>{r['profit']}/{len(r['tables'])}</td>"
                      f"<td style='color:{pnl_c};font-weight:bold'>${r['total_pnl']:+,.0f}</td>"
                      f"<td style='color:#fbbf24'>${r['worst_dd']:,.0f}</td>"
                      f"<td>${r['avg_dd']:,.0f}</td>"
                      f"<td style='color:#8a96a8'>{r['entries']:,}</td>"
                      f"<td style='color:#8a96a8'>{r['exits']:,}</td></tr>")

    # 最良 threshold のテーブル別結果を表示
    best_label = max(all_results, key=lambda k: all_results[k]['total_pnl'])
    best = all_results[best_label]
    best_tables = sorted(best['tables'], key=lambda x: x['final'])
    sections = ""
    for t in best_tables:
        if t['bankrupt']:
            st = "💀 破綻"; sc = "#7c2d2d"
        elif t['pnl'] > 0:
            st = f"✅ +${t['pnl']:,.0f}"; sc = "#1a4a2a"
        else:
            st = "±0"; sc = "#2a3441"
        rows = ""
        for r in t['turns'][:50]:  # 最初50件のみ (長いので)
            ts_str = r['started_at'][:16].replace('T', ' ') if r.get('started_at') else '-'
            if r['outcome'] == 'profit':
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#4ade80'>WIN +${r['session_pnl']:,.0f}</td><td style='color:#4ade80;text-align:right'>${r['balance']:,.0f}</td></tr>"
            else:
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#f87171'>💀 破綻 (${r['session_pnl']:,.0f})</td><td style='color:#f87171;text-align:right'>$0</td></tr>"
        sections += f"""
<div style="background:#1a2332;border-left:5px solid {sc};margin:14px 0;padding:14px;border-radius:4px;">
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;margin-bottom:8px;align-items:center;">
    <div><span style="font-size:16px;font-weight:bold;color:#ffd700">{t['name']}</span> <span style="font-size:11px;color:#8a96a8">{t['shoes']}シュー / IN {t['entries']}</span></div>
    <div style="text-align:center">{st}</div>
    <div style="text-align:right;color:#8a96a8">最終: <strong style="color:{'#f87171' if t['bankrupt'] else '#ffd700'}">${t['final']:,.0f}</strong> / MaxDD: <strong style="color:#fbbf24">${t['max_dd']:,.0f}</strong></div>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left;padding:4px 8px;color:#c084fc">日時</th><th style="text-align:left;padding:4px 8px;color:#c084fc">結果</th><th style="text-align:right;padding:4px 8px;color:#c084fc">残高</th></tr></thead><tbody>{rows}</tbody></table>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>Z. 順張り (Trend) × 新SEQ × 5ターン — $10,000スタート</title>
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
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #1a2332; padding: 14px; border-radius: 4px; border-left: 4px solid #6dd5ed; }}
.card .label {{ font-size: 11px; color: #8a96a8; text-transform: uppercase; }}
.card .value {{ font-size: 22px; font-weight: bold; color: #ffd700; }}
.card.g .value {{ color: #4ade80; }}
.card.r .value {{ color: #f87171; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin: 16px 0; }}
table th {{ background: #0f1419; color: #c084fc; padding: 8px; text-align: left; border-bottom: 2px solid #2a3441; }}
table td {{ padding: 6px 8px; border-bottom: 1px solid #1a2332; }}
td.ts {{ font-family: monospace; color: #8a96a8; font-size: 11px; }}
</style></head><body><div class="container">
<h1>Z. 順張り (Trend) × 新SEQ × 5ターン ($10,000スタート)</h1>
<div class="nav"><a href="index.html">← レポートTOP</a><a href="equity_counter_newseq_5turn_realistic_10k.html">Y. 逆張り (Counter)</a></div>

<div class="banner">
<strong>📊 縦流れ環境のみを狙った順張り戦略。</strong><br>
Entry: 直近{ENTRY_WINDOW}列中、長列({LONG_COL_MIN}連以上)の比率が閾値以上<br>
BET: 直前の非Tieと同じ側 (Follow the flow)<br>
Exit: 短列({EXIT_SHORT_CONSEC}連続)発生 = テレコ復活シグナル<br>
SEQ: 新SEQ×5ターン制 (逆張りと同条件で比較)
</div>

<h2>💰 閾値別の結果 (grid search)</h2>
<table><thead><tr><th>構成</th><th>破綻</th><th>黒字/全</th><th>通算損益</th><th>最大DD</th><th>平均DD</th><th>総入場</th><th>退出</th></tr></thead><tbody>{comp_rows}</tbody></table>

<h2>📋 最良構成 ({best_label}) テーブル別</h2>
{sections}

</div></body></html>"""

    out_path = os.path.join("report", "equity_trend_newseq_5turn_10k.html")
    os.makedirs("report", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")
    print(f"Best: {best_label} → PNL ${best['total_pnl']:+,.0f}")


if __name__ == "__main__":
    main()
