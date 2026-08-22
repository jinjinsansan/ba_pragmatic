"""ハイブリッド戦略 (Counter + Trend) バックテスト

設計:
  - 各シューで "今の環境" を判定
      → テレコ (短列率 >= 0.80) → Counter
      → 縦流れ (長列率 >= 0.30)  → Trend
      → どちらでもない → スキップ
  - 同じ SEQ プールを両戦略で共有 (1本のセッションとして累積)
  - 利確 / 退出は戦略毎の条件

目的:
  Counter のみ / Trend のみ / ハイブリッド の 3 パターンを 23K で比較。
  ハイブリッドが両方の良いとこ取りできるかを検証。

Usage:
  python generate_equity_hybrid.py --vps
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

# --- Counter (逆張り) ---
COUNTER_ENTRY_THRESHOLD = 0.85   # 短列率 >= 0.85 でテレコ判定
EXIT_DROP3_LIMIT = 2
EXIT_DROP5_IMMEDIATE = True

# --- Trend (順張り) ---
TREND_ENTRY_THRESHOLD = 0.30     # 長列率 >= 0.30 で縦流れ判定
TREND_EXIT_SHORT_CONSEC = 3

ENTRY_WINDOW = 15
LONG_COL_MIN = 3

SEQ_NEW = [1, 3, 5, 7, 10, 13, 16, 20, 25, 30, 35, 40, 45, 50,
           60, 70, 80, 90, 100, 110, 120, 130,
           145, 160, 175, 190, 205, 220, 235, 250, 265, 280,
           300, 320, 340, 360, 380, 400, 420, 440, 460, 480, 500]


def compute_columns(seq):
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


def strip_ties(seq):
    return ''.join(ch for ch in seq if ch in ('P', 'B'))


def classify_env(cols):
    """環境分類: 'tereko' / 'trend' / 'mixed'"""
    if len(cols) < ENTRY_WINDOW:
        return 'mixed'
    recent = cols[-ENTRY_WINDOW:]
    short_ratio = sum(1 for c in recent if c <= 2) / len(recent)
    long_ratio = sum(1 for c in recent if c >= LONG_COL_MIN) / len(recent)
    if short_ratio >= COUNTER_ENTRY_THRESHOLD:
        return 'tereko'
    if long_ratio >= TREND_ENTRY_THRESHOLD:
        return 'trend'
    return 'mixed'


def should_exit_counter(cols_since_entry, current_col_len):
    check = list(cols_since_entry)
    if current_col_len >= 3:
        check.append(current_col_len)
    if EXIT_DROP5_IMMEDIATE:
        if any(L >= 5 for L in check) or current_col_len >= 5:
            return "streak-5"
    drop3_count = sum(1 for L in check if L >= 3)
    if drop3_count >= EXIT_DROP3_LIMIT:
        return f"streak-3x{drop3_count}"
    return None


def should_exit_trend(cols_since_entry, current_col_len):
    check = list(cols_since_entry)
    if current_col_len >= 1:
        check.append(current_col_len)
    consec = 0
    for c in reversed(check):
        if c <= 2:
            consec += 1
            if consec >= TREND_EXIT_SHORT_CONSEC:
                return f"tereko-back"
        else:
            break
    return None


class HybridSim:
    """SEQ shared between counter/trend strategies within one session."""
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

    def feed(self, r, mode):
        """mode: 'counter' or 'trend'"""
        if r == 'T':
            return None
        if self.last_non_tie is None:
            self.last_non_tie = r
            return None
        if mode == 'trend':
            bet_side = self.last_non_tie
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


def simulate_hybrid(shoes, start_capital):
    """シュー冒頭で環境判定 → counter / trend / skip。"""
    sim = HybridSim(seq=SEQ_NEW, target=PROFIT_PER_WIN, set_size=SET_SIZE)
    balance = start_capital
    turns = []
    bankrupt = False
    session_start_ts = None

    counter_entries = 0
    trend_entries = 0
    skips = 0
    exits_counter = 0
    exits_trend = 0

    peak_equity = start_capital
    true_max_dd = 0.0

    for seq, started_at in shoes:
        if len(strip_ties(seq)) < MIN_HANDS_PER_SHOE:
            continue

        in_position = False
        current_mode = None  # 'counter' or 'trend'
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
                if current_mode == 'counter':
                    reason = should_exit_counter(cse_prev, current_col_len)
                else:
                    reason = should_exit_trend(cse_prev, current_col_len)
                if reason:
                    in_position = False
                    sim.abandon_set()
                    if current_mode == 'counter':
                        exits_counter += 1
                    else:
                        exits_trend += 1
                    current_mode = None

            # --- エントリー判定 ---
            if not in_position:
                env = classify_env(columns)
                if env == 'tereko':
                    in_position = True
                    current_mode = 'counter'
                    columns_at_entry = len(columns)
                    counter_entries += 1
                    if session_start_ts is None:
                        session_start_ts = started_at
                    sim.last_non_tie = pb_buffer[-1] if pb_buffer else None
                elif env == 'trend':
                    in_position = True
                    current_mode = 'trend'
                    columns_at_entry = len(columns)
                    trend_entries += 1
                    if session_start_ts is None:
                        session_start_ts = started_at
                    sim.last_non_tie = pb_buffer[-1] if pb_buffer else None
                else:
                    skips += 1
                continue

            # --- エントリー中: モードに応じて BET ---
            result = sim.feed(r, mode=current_mode)

            equity = balance + sim.cumulative
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > true_max_dd:
                true_max_dd = dd

            if balance + sim.cumulative <= 0:
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'bankrupt', 'session_pnl': sim.cumulative,
                              'balance': 0, 'mode': current_mode})
                balance = 0
                bankrupt = True
                break
            if result == 'profit':
                balance += sim.cumulative
                turns.append({'turn': len(turns)+1, 'started_at': session_start_ts,
                              'outcome': 'profit', 'session_pnl': sim.cumulative,
                              'balance': balance, 'mode': current_mode})
                session_start_ts = None
                sim.reset()
                in_position = False
                current_mode = None

        if bankrupt:
            break

    return {
        'turns': turns,
        'final_balance': balance,
        'bankrupt_at': turns[-1]['turn'] if bankrupt else None,
        'max_dd': true_max_dd,
        'counter_entries': counter_entries,
        'trend_entries': trend_entries,
        'skips': skips,
        'exits_counter': exits_counter,
        'exits_trend': exits_trend,
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
    print(f"Total {th:,} hands / {len(sbt)} tables / {ts} shoes\n")

    print("Running Hybrid (Counter + Trend)...")
    tbl = []
    total_c = total_t = total_s = total_exc = total_ext = 0
    for tn, shoes in eligible:
        r = simulate_hybrid(shoes, START_CAPITAL)
        tbl.append({'name': tn, 'pnl': r['final_balance'] - START_CAPITAL,
                    'bankrupt': r['bankrupt_at'] is not None, 'max_dd': r['max_dd'],
                    'final': r['final_balance'], 'turns': r['turns'], 'shoes': len(shoes),
                    'c_ent': r['counter_entries'], 't_ent': r['trend_entries'],
                    'skips': r['skips'], 'exc': r['exits_counter'], 'ext': r['exits_trend']})
        total_c += r['counter_entries']
        total_t += r['trend_entries']
        total_s += r['skips']
        total_exc += r['exits_counter']
        total_ext += r['exits_trend']
    bankrupt_n = sum(1 for t in tbl if t['bankrupt'])
    profit_n = sum(1 for t in tbl if t['pnl'] > 0)
    total_pnl = sum(t['pnl'] for t in tbl)
    worst_dd = max((t['max_dd'] for t in tbl), default=0)
    avg_dd = sum(t['max_dd'] for t in tbl) / len(tbl) if tbl else 0
    print(f"  破綻{bankrupt_n} / 黒字{profit_n}/{len(tbl)} / PNL ${total_pnl:+,.0f}")
    print(f"  Counter入場: {total_c:,} / Trend入場: {total_t:,} / Skip: {total_s:,}")
    print(f"  Counter退出: {total_exc:,} / Trend退出: {total_ext:,}")

    # --- HTML 生成 ---
    best_tables = sorted(tbl, key=lambda x: x['final'])
    sections = ""
    for t in best_tables:
        if t['bankrupt']:
            st = "💀 破綻"; sc = "#7c2d2d"
        elif t['pnl'] > 0:
            st = f"✅ +${t['pnl']:,.0f}"; sc = "#1a4a2a"
        else:
            st = "±0"; sc = "#2a3441"
        rows = ""
        for r in t['turns'][:60]:
            ts_str = r['started_at'][:16].replace('T', ' ') if r.get('started_at') else '-'
            mode_tag = f"<span style='font-size:10px;color:#8a96a8'>[{r.get('mode','?')}]</span>"
            if r['outcome'] == 'profit':
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#4ade80'>{mode_tag} WIN +${r['session_pnl']:,.0f}</td><td style='color:#4ade80;text-align:right'>${r['balance']:,.0f}</td></tr>"
            else:
                rows += f"<tr><td class='ts'>{ts_str}</td><td style='color:#f87171'>{mode_tag} 💀 (${r['session_pnl']:,.0f})</td><td style='color:#f87171;text-align:right'>$0</td></tr>"
        sections += f"""
<div style="background:#1a2332;border-left:5px solid {sc};margin:14px 0;padding:14px;border-radius:4px;">
  <div style="display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;margin-bottom:8px;align-items:center;">
    <div><span style="font-size:16px;font-weight:bold;color:#ffd700">{t['name']}</span>
         <span style="font-size:11px;color:#8a96a8">{t['shoes']}シュー / C:{t['c_ent']} T:{t['t_ent']} Skip:{t['skips']}</span></div>
    <div style="text-align:center">{st}</div>
    <div style="text-align:right;color:#8a96a8">最終: <strong style="color:{'#f87171' if t['bankrupt'] else '#ffd700'}">${t['final']:,.0f}</strong> / MaxDD: <strong style="color:#fbbf24">${t['max_dd']:,.0f}</strong></div>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="text-align:left;padding:4px 8px;color:#c084fc">日時</th><th style="text-align:left;padding:4px 8px;color:#c084fc">結果</th><th style="text-align:right;padding:4px 8px;color:#c084fc">残高</th></tr></thead><tbody>{rows}</tbody></table>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>AA. ハイブリッド (Counter + Trend) — $10,000スタート</title>
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
<h1>AA. ハイブリッド (Counter + Trend) — $10,000スタート</h1>
<div class="nav"><a href="index.html">← レポートTOP</a><a href="equity_counter_newseq_5turn_realistic_10k.html">Y. Counter</a><a href="equity_trend_newseq_5turn_10k.html">Z. Trend</a></div>

<div class="banner">
<strong>📊 環境によって自動切替する併用戦略。</strong><br>
シュー毎に直近{ENTRY_WINDOW}列を分析 → <br>
&nbsp;&nbsp;短列率 ≥ {COUNTER_ENTRY_THRESHOLD} → <span style="color:#4aa8ff">Counter (逆張り)</span><br>
&nbsp;&nbsp;長列率 ≥ {TREND_ENTRY_THRESHOLD} → <span style="color:#ff8080">Trend (順張り)</span><br>
&nbsp;&nbsp;どちらでもない → Skip<br>
新SEQ × 5ターン制 / $30利確 / 損切なし
</div>

<div class="summary">
  <div class="card"><div class="label">対象テーブル</div><div class="value">{len(tbl)}</div></div>
  <div class="card g"><div class="label">黒字</div><div class="value">{profit_n}/{len(tbl)}</div></div>
  <div class="card r"><div class="label">破綻</div><div class="value">{bankrupt_n}</div></div>
  <div class="card g"><div class="label">通算PNL</div><div class="value">${total_pnl:+,.0f}</div></div>
  <div class="card"><div class="label">Counter 入場</div><div class="value">{total_c:,}</div></div>
  <div class="card"><div class="label">Trend 入場</div><div class="value">{total_t:,}</div></div>
  <div class="card"><div class="label">Skip</div><div class="value">{total_s:,}</div></div>
  <div class="card"><div class="label">Max DD</div><div class="value">${worst_dd:,.0f}</div></div>
</div>

<h2>📋 テーブル別 (最終残高降順)</h2>
{sections}

</div></body></html>"""

    out_path = os.path.join("report", "strategy_hybrid_10k.html")
    os.makedirs("report", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
