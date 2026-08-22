"""リアルタイム テレコ入退室 × 逆張り（新SEQ × 5ターン）バックテスト

目的:
  - 実運用の counter モード（agent_api.py の入退室・乗換フロー）に近い前提で、
    新SEQ(SEQ_COUNTER) × 5ターン(set) × $30利確 のリスク/ドローダウンを可視化する。

注意:
  - shoes_analytics は「シュー単位の結果列」しか無いので、テーブル間の完全な同時進行は再現できない。
    ただし「入退室の有無」「SEQ継続」「短期テレコ判定/退室判定」によるリスク差は概ね確認できる。

Usage:
  python generate_realtime_counter_backtest_newseq_5turn.py --vps
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter, defaultdict

from counter_logic import ENTRY_THRESHOLD, ENTRY_WINDOW, EXIT_DROP3_LIMIT, EXIT_DROP5_IMMEDIATE
from marubatsu_strategy import SEQ_COUNTER, SET_SIZE_COUNTER

if "--today" in sys.argv:
    DB_PATH = "analytics_today.sqlite3"
elif "--vps" in sys.argv:
    DB_PATH = "analytics_vps.sqlite3"
else:
    DB_PATH = "analytics.sqlite3"

DATE_FROM = "2026-04-06"
MIN_HANDS = 50
PROFIT_TARGET = 30
START_CAPITAL = 10000
BANKER_COMMISSION = 0.05

# 破綻(残高<=0)時の扱い:
# - "stop": その時点でシミュレーション終了
# - "reset": 破綻1回として記録し、資金をSTART_CAPITALに補充して続行（破綻率を見る）
BANKRUPT_MODE = "stop"


class MaruBatsuSim:
    """実運用の MaruBatsuTracker を簡易再現（SEQ継続/overshoot/斜線）+ banker手数料反映の損益。"""

    def __init__(self, seq: list[int], set_size: int, profit_target: float):
        self.seq = seq
        self.set_size = set_size
        self.profit_target = profit_target

        # セッション内
        self.session_pnl = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns: list[tuple[bool, str]] = []  # [(won, bet_side 'P'|'B')]
        self.history: list[dict] = []

        # 全体
        self.total_profit = 0.0
        self.sessions_won = 0
        self.hands_bet = 0
        self.max_unit = 0

        # 残高ベース（START_CAPITAL を足す）
        self.peak_balance = float(START_CAPITAL)
        self.min_balance = float(START_CAPITAL)
        self.max_dd = 0.0
        self.bankrupt = False
        self.bankrupt_at_hand: int | None = None

    def equity(self) -> float:
        return self.total_profit + self.session_pnl

    def balance(self) -> float:
        return float(START_CAPITAL) + self.equity()

    def _next_idx(self, used_idx: int, diff: int, new_os: int) -> int:
        if diff < 0:
            return min(used_idx + 1, len(self.seq) - 1)
        for fi in range(len(self.history) - 1, -1, -1):
            s = self.history[fi]
            if not s["slashed"] and s["os"] == new_os:
                return s["next"]
        ba, bad = -1, float("inf")
        bb, bbd = -1, float("inf")
        for s in self.history:
            if not s["slashed"]:
                dd = s["os"] - new_os
                if dd > 0 and dd < bad:
                    bad = dd
                    ba = s["next"]
                if dd < 0 and (-dd) < bbd:
                    bbd = -dd
                    bb = s["next"]
        if ba >= 0:
            return ba
        if bb >= 0:
            return min(bb + 1, len(self.seq) - 1)
        return 0

    def _update_dd(self):
        bal = self.balance()
        if bal > self.peak_balance:
            self.peak_balance = bal
        if bal < self.min_balance:
            self.min_balance = bal
        dd = self.peak_balance - bal
        if dd > self.max_dd:
            self.max_dd = dd

    def _complete_set(self):
        base_unit = self.seq[min(self.unit_idx, len(self.seq) - 1)]
        self.max_unit = max(self.max_unit, base_unit)
        wins = sum(1 for won, _ in self.turns if won)
        losses = self.set_size - wins
        diff = wins - losses

        new_os = max(self.prev_os - diff, 0)
        if diff > 0:
            for s in self.history:
                if (not s["slashed"]) and s["os"] > new_os:
                    s["slashed"] = True
        next_idx = self._next_idx(self.unit_idx, diff, new_os)
        self.history.append({"os": new_os, "slashed": False, "next": next_idx})

        self.prev_os = new_os
        self.unit_idx = next_idx
        self.turns = []
        # DD は add_bet で都度更新する

    def _reset_session(self):
        self.total_profit += self.session_pnl
        self.sessions_won += 1

        self.session_pnl = 0.0
        self.unit_idx = 0
        self.prev_os = 0
        self.turns = []
        self.history = []

    def add_bet(self, won: bool, bet_side: str):
        self.hands_bet += 1

        # 1ベットごとの損益（残高/DDの正確性のため即時反映）
        base_unit = self.seq[min(self.unit_idx, len(self.seq) - 1)]
        self.max_unit = max(self.max_unit, base_unit)
        if won:
            self.session_pnl += base_unit * (1.0 - BANKER_COMMISSION) if bet_side == "B" else base_unit
        else:
            self.session_pnl -= base_unit

        # 現実の残高はマイナスに行かないので 0 にクランプ（DD過大化防止）
        if self.balance() < 0:
            self.session_pnl = -float(START_CAPITAL) - self.total_profit

        self._update_dd()

        if (not self.bankrupt) and self.balance() <= 0:
            self.bankrupt = True
            self.bankrupt_at_hand = self.hands_bet
            self.min_balance = 0.0

        self.turns.append((won, bet_side))
        if len(self.turns) == self.set_size:
            self._complete_set()
        if self.session_pnl >= self.profit_target:
            self._reset_session()


class TableState:
    def __init__(self, name: str):
        self.name = name
        self.columns: list[int] = []
        self.current_col = 0
        self.last_side: str | None = None
        self.last_nt: str | None = None
        self.is_active = False
        self.entry_col_idx = 0

    def feed(self, ch: str):
        if ch == "T":
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
        if self.current_col > 0 and self.last_side is not None:
            self.columns.append(self.current_col)
        self.current_col = 0
        self.last_side = None

    def is_tereko(self) -> bool:
        if len(self.columns) < ENTRY_WINDOW:
            return False
        recent = self.columns[-ENTRY_WINDOW:]
        short = sum(1 for L in recent if L <= 2)
        return (short / len(recent)) >= ENTRY_THRESHOLD

    def check_exit(self) -> str | None:
        if not self.is_active:
            return None
        cols_since_entry = self.columns[self.entry_col_idx :]
        check_cols = cols_since_entry
        if self.current_col >= 3:
            check_cols = list(cols_since_entry) + [self.current_col]

        if EXIT_DROP5_IMMEDIATE:
            if any(L >= 5 for L in check_cols) or self.current_col >= 5:
                return "streak-5"

        drop3_count = sum(1 for L in check_cols if L >= 3)
        if drop3_count >= EXIT_DROP3_LIMIT:
            return f"streak-3x{drop3_count}"
        return None

    def enter(self):
        self.is_active = True
        self.entry_col_idx = len(self.columns)

    def exit(self):
        self.is_active = False


def load_all_shoes():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT table_name, result_sequence, started_at FROM shoes_analytics "
        "WHERE hand_count >= ? AND started_at >= ? ORDER BY started_at",
        (MIN_HANDS, DATE_FROM),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def run_backtest():
    shoes = load_all_shoes()
    print(f"Loaded {len(shoes)} shoes from {DB_PATH}")

    sim = MaruBatsuSim(seq=SEQ_COUNTER, set_size=SET_SIZE_COUNTER, profit_target=PROFIT_TARGET)

    table_states: dict[str, TableState] = {}
    stats = {
        "entries": 0,
        "exits": 0,
        "bets": 0,
        "wins": 0,
        "losses": 0,
        "exit_reasons": Counter(),
    }

    table_stats = defaultdict(lambda: {"entries": 0, "bets": 0, "wins": 0, "losses": 0, "shoes": 0})

    current_table = None
    current_entry_ts = None
    current_session_bets = 0
    current_session_wins = 0
    session_log = []

    tereko_available_counts: list[int] = []
    bankruptcies = 0
    episodes: list[dict] = []
    episode_idx = 1
    episode_start_ts = None
    episode_entries = 0
    episode_exits = 0
    episode_bets = 0
    episode_wins = 0
    episode_losses = 0

    stop_all = False

    for si, (table_name, seq, started_at) in enumerate(shoes):
        if si % 2000 == 0:
            print(f"  {si}/{len(shoes)}...")

        if table_name not in table_states:
            table_states[table_name] = TableState(table_name)
        ts = table_states[table_name]
        table_stats[table_name]["shoes"] += 1

        clean = "".join(ch for ch in seq if ch in ("P", "B", "T"))
        for ch in clean:
            if ch == "T":
                ts.feed(ch)
                continue

            # 退室チェック（BET前）
            if current_table == table_name and ts.is_active:
                exit_reason = ts.check_exit()
                if exit_reason:
                    ts.exit()
                    stats["exits"] += 1
                    episode_exits += 1
                    stats["exit_reasons"][exit_reason] += 1
                    session_log.append(
                        {
                            "table": table_name,
                            "entry_ts": current_entry_ts,
                            "exit_ts": started_at,
                            "reason": exit_reason,
                            "bets": current_session_bets,
                            "wins": current_session_wins,
                        }
                    )
                    current_table = None
                    current_entry_ts = None

            # 入室チェック（BET中でなければ）
            if current_table is None:
                tereko_tables = [name for name, state in table_states.items() if state.is_tereko() and not state.is_active]
                tereko_available_counts.append(len(tereko_tables))

                if ts.is_tereko():
                    ts.enter()
                    current_table = table_name
                    current_entry_ts = started_at
                    current_session_bets = 0
                    current_session_wins = 0
                    stats["entries"] += 1
                    episode_entries += 1
                    table_stats[table_name]["entries"] += 1

            # BET（入室中テーブルのみ）
            if current_table == table_name and ts.is_active and ts.last_nt is not None:
                bet_side = "P" if ts.last_nt == "B" else "B"
                won = ch == bet_side

                stats["bets"] += 1
                current_session_bets += 1
                episode_bets += 1
                table_stats[table_name]["bets"] += 1
                if won:
                    stats["wins"] += 1
                    current_session_wins += 1
                    episode_wins += 1
                    table_stats[table_name]["wins"] += 1
                else:
                    stats["losses"] += 1
                    table_stats[table_name]["losses"] += 1
                    episode_losses += 1

                if episode_start_ts is None:
                    episode_start_ts = started_at

                sim.add_bet(won, bet_side)
                if sim.bankrupt:
                    bankruptcies += 1
                    episodes.append(
                        {
                            "episode": episode_idx,
                            "start_ts": episode_start_ts,
                            "end_ts": started_at,
                            "reason": "bankrupt",
                            "table": current_table,
                            "entries": episode_entries,
                            "exits": episode_exits,
                            "bets": episode_bets,
                            "wins": episode_wins,
                            "losses": episode_losses,
                            "pnl": sim.balance() - float(START_CAPITAL),
                            "max_dd": sim.max_dd,
                            "min_balance": sim.min_balance,
                            "max_unit": sim.max_unit,
                        }
                    )

                    if BANKRUPT_MODE == "stop":
                        stop_all = True
                        break

                    # reset して続行（破綻率を見る）
                    episode_idx += 1
                    episode_start_ts = None
                    episode_entries = 0
                    episode_exits = 0
                    episode_bets = 0
                    episode_wins = 0
                    episode_losses = 0
                    sim = MaruBatsuSim(seq=SEQ_COUNTER, set_size=SET_SIZE_COUNTER, profit_target=PROFIT_TARGET)
                    # 破綻時はそのテーブルを離脱扱いにしてリスキャンへ
                    if current_table == table_name and ts.is_active:
                        ts.exit()
                    current_table = None
                    current_entry_ts = None

            ts.feed(ch)

        if stop_all:
            break
        ts.finalize_shoe()

    # 最終エピソード（破綻せずに終了）
    if episode_bets > 0:
        episodes.append(
            {
                "episode": episode_idx,
                "start_ts": episode_start_ts,
                "end_ts": "END",
                "reason": "end-of-data",
                "table": current_table,
                "entries": episode_entries,
                "exits": episode_exits,
                "bets": episode_bets,
                "wins": episode_wins,
                "losses": episode_losses,
                "pnl": sim.balance() - float(START_CAPITAL),
                "max_dd": sim.max_dd,
                "min_balance": sim.min_balance,
                "max_unit": sim.max_unit,
            }
        )

    if current_table and current_session_bets > 0:
        session_log.append(
            {
                "table": current_table,
                "entry_ts": current_entry_ts,
                "exit_ts": "END",
                "reason": "end-of-data",
                "bets": current_session_bets,
                "wins": current_session_wins,
            }
        )

    avg_tereko = sum(tereko_available_counts) / len(tereko_available_counts) if tereko_available_counts else 0
    return stats, sim, table_stats, session_log, avg_tereko, len(shoes), bankruptcies, episodes


def render_html(stats, sim, table_stats, session_log, avg_tereko, total_shoes, bankruptcies: int, episodes: list[dict]):
    def hr():
        return stats["wins"] / stats["bets"] * 100 if stats["bets"] else 0

    equity = sim.equity()
    mp_c = "#4ade80" if equity >= 0 else "#f87171"
    bankrupt_text = "YES" if sim.bankrupt else "NO"
    bankrupt_detail = f" (hand #{sim.bankrupt_at_hand:,})" if sim.bankrupt_at_hand else ""

    # エピソード表
    ep_rows = ""
    for e in episodes[:200]:
        pnl_c = "#4ade80" if (e.get("pnl", 0) or 0) >= 0 else "#f87171"
        ep_rows += (
            "<tr>"
            f"<td>#{e.get('episode')}</td>"
            f"<td>{e.get('start_ts') or ''}</td>"
            f"<td>{e.get('end_ts') or ''}</td>"
            f"<td>{e.get('reason') or ''}</td>"
            f"<td>{e.get('table') or ''}</td>"
            f"<td>{e.get('bets') or 0}</td>"
            f"<td>{e.get('wins') or 0}</td>"
            f"<td>{e.get('losses') or 0}</td>"
            f"<td style='color:{pnl_c};font-weight:bold'>${(e.get('pnl') or 0):+,.0f}</td>"
            f"<td>${(e.get('max_dd') or 0):,.0f}</td>"
            f"<td>${(e.get('min_balance') or 0):,.0f}</td>"
            f"<td>${(e.get('max_unit') or 0):,.0f}</td>"
            "</tr>"
        )

    top_tables = sorted(
        (
            {
                "name": name,
                "entries": st["entries"],
                "bets": st["bets"],
                "w": st["wins"],
                "l": st["losses"],
            }
            for name, st in table_stats.items()
        ),
        key=lambda x: x["bets"],
        reverse=True,
    )[:40]

    table_rows = ""
    for t in top_tables:
        h = (t["w"] / t["bets"] * 100) if t["bets"] else 0
        table_rows += (
            f"<tr><td>{t['name']}</td><td>{t['entries']}</td><td>{t['bets']}</td>"
            f"<td>{t['w']}</td><td>{t['l']}</td><td>{h:.2f}%</td></tr>"
        )

    exit_rows = ""
    for reason, cnt in stats["exit_reasons"].most_common():
        exit_rows += f"<tr><td>{reason}</td><td>{cnt}</td></tr>"

    html = f"""<!doctype html>
<html lang='ja'>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width,initial-scale=1'/>
  <title>Realtime Counter Backtest (NewSEQ×5turn)</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; background:#0b1020; color:#e5e7eb; margin:0; padding:24px; }}
    .card {{ background:#111827; border:1px solid #243042; border-radius:12px; padding:16px; margin-bottom:16px; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ border-bottom:1px solid #243042; padding:8px; font-size:13px; text-align:left; }}
    th {{ color:#93c5fd; }}
    code {{ background:#0b1224; padding:2px 6px; border-radius:6px; }}
  </style>
</head>
<body>
  <h1>リアルタイム テレコ入退室 × 逆張り（新SEQ×5ターン）</h1>

  <div class='card'>
    <div><strong>DB:</strong> <code>{DB_PATH}</code> / <strong>期間:</strong> {DATE_FROM}〜 / <strong>シュー数:</strong> {total_shoes:,}</div>
    <div><strong>入室条件:</strong> 直近{ENTRY_WINDOW}列で 1-2落ち率 ≥ {ENTRY_THRESHOLD*100:.0f}%</div>
    <div><strong>退室条件:</strong> {'5落ち即' if EXIT_DROP5_IMMEDIATE else ''} / 3落ち以上×{EXIT_DROP3_LIMIT}</div>
    <div><strong>資金管理:</strong> 新SEQ(SEQ_COUNTER) + {SET_SIZE_COUNTER}ターンset / ${PROFIT_TARGET}利確（損切なし） / 初期資金 ${START_CAPITAL:,}</div>
    <div><strong>破綻時の扱い:</strong> {BANKRUPT_MODE}（resetの場合、破綻回数=破綻率の観測用）</div>
  </div>

  <div class='card'>
    <h2>結果</h2>
    <ul>
      <li>入室: {stats['entries']:,} / 退室: {stats['exits']:,} / 平均テレコ候補数: {avg_tereko:.2f}</li>
      <li>BET: {stats['bets']:,} / 勝: {stats['wins']:,} / 負: {stats['losses']:,} / 勝率: {hr():.2f}%</li>
      <li>累計損益(推定): <span style='color:{mp_c};font-weight:bold'>${equity:+,.0f}</span></li>
      <li>最大DD(推定): <span style='color:#fbbf24;font-weight:bold'>${sim.max_dd:,.0f}</span></li>
      <li>最低残高(推定): <span style='color:#fbbf24;font-weight:bold'>${sim.min_balance:,.0f}</span></li>
      <li>最大ユニット: <span style='color:#fbbf24;font-weight:bold'>${sim.max_unit:,.0f}</span></li>
      <li>利確回数: {sim.sessions_won:,}（${PROFIT_TARGET}到達でセッションリセット）</li>
      <li>破綻: <strong>{bankrupt_text}</strong>{bankrupt_detail}</li>
      <li>破綻回数（resetモード時）: <strong>{bankruptcies:,}</strong></li>
    </ul>
  </div>

  <div class='card'>
    <h2>退室理由</h2>
    <table>
      <thead><tr><th>reason</th><th>count</th></tr></thead>
      <tbody>{exit_rows}</tbody>
    </table>
  </div>

  <div class='card'>
    <h2>テーブル別（BET数上位40）</h2>
    <table>
      <thead><tr><th>table</th><th>entries</th><th>bets</th><th>w</th><th>l</th><th>winrate</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>

  <div class='card'>
    <h2>エピソード（破綻ごとに資金リセット / 先頭200件）</h2>
    <table>
      <thead>
        <tr>
          <th>#</th><th>start</th><th>end</th><th>reason</th><th>table</th>
          <th>bets</th><th>w</th><th>l</th><th>pnl</th><th>maxDD</th><th>minBal</th><th>maxUnit</th>
        </tr>
      </thead>
      <tbody>{ep_rows}</tbody>
    </table>
  </div>

  <div class='card'>
    <p>生成元: <code>generate_realtime_counter_backtest_newseq_5turn.py</code></p>
  </div>
</body>
</html>
"""
    return html


def main():
    stats, sim, table_stats, session_log, avg_tereko, total_shoes, bankruptcies, episodes = run_backtest()
    html = render_html(stats, sim, table_stats, session_log, avg_tereko, total_shoes, bankruptcies, episodes)

    out = "report/realtime_counter_newseq_5turn.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote: {out}")
    print(
        f"Equity ${sim.equity():+.0f} / Balance ${sim.balance():,.0f} / MaxDD ${sim.max_dd:,.0f} / "
        f"MinBal ${sim.min_balance:,.0f} / MaxUnit ${sim.max_unit:,.0f} / Bets {stats['bets']:,} / "
        f"Bankrupt={'YES' if sim.bankrupt else 'NO'}"
    )


if __name__ == "__main__":
    main()
