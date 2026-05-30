#!/usr/bin/env python3
"""VPS dual_line_pragmatic_bot.py に v4(10パターン) shadow 並走を追加するパッチ。
6パターン経路には一切触れず、独立した _v4_track/_v4_state を足すだけ。冪等。
Telegram は V2 と同形式（signal/解決の両方に累積 W/L/T(勝率)）。
"""
import sys

F = "/opt/laplace2/dual_line_pragmatic_bot.py"
s = open(F, encoding="utf-8").read()

if "V4_PATTERNS" in s:
    print("ALREADY_PATCHED")
    sys.exit(0)

edits = []

# 1) V4 定数
A1 = "V2_PATTERNS = LIVE_SIGNAL_PATTERNS\n"
I1 = A1 + '''
V4_PATTERNS = frozenset({
    "sansan|telecho|P", "niconico|niconico|B", "telecho|niconico|P",
    "niconico|nikoichi|B", "telecho|niconico|B", "niconico|dragon|B",
    "niconico|nikoichi|P", "telecho|nikoichi|B", "telecho|telecho|B",
    "telecho|nikoichi|P",
})
V4_STATE_PATH = _PERSISTENT_DIR / "dual_line_v4_state.json"
'''
edits.append((A1, I1))

# 2) __init__ に v4 state
A2 = "        self.virtual_pnl = 0.0\n"
I2 = A2 + '''        self._v4_pending: dict = {}
        self._v4 = {"signals": 0, "resolved": 0, "wins": 0, "losses": 0, "ties": 0, "pnl": 0.0}
        self._v4_per_pattern: dict = {}
        self._load_v4_state()
'''
edits.append((A2, I2))

# 3) メソッド群を _on_new_hand の前に挿入
A3 = "    def _on_new_hand(self, table_id: str, buf, new_hand: dict, history_hands: list[dict] | None = None):\n"
I3 = '''    def _v4_track(self, table_id, buf, new_hand, history_hands=None):
        """v4(10パターン) shadow 集計。6パターン経路から完全独立。BETしない。"""
        outcome = _winner_to_char(new_hand.get("winner"))
        if not outcome:
            return
        tg_on = os.getenv("BACOPY_V4_TELEGRAM", "1") != "0"
        pend = self._v4_pending.pop(table_id, None)
        if pend:
            side = str(pend.get("side") or "")
            pk = str(pend.get("pattern_key") or "")
            if outcome == "T":
                self._v4["ties"] += 1; res = "TIE"; icon = "\U0001F535"; pnl = 0.0
            elif outcome == side:
                self._v4["wins"] += 1; res = "WIN"; icon = "✅"
                pnl = COMMISSION_BANKER if side == "B" else 1.0
            else:
                self._v4["losses"] += 1; res = "LOSE"; icon = "❌"; pnl = -1.0
            self._v4["pnl"] += pnl
            self._v4["resolved"] += 1
            pp = self._v4_per_pattern.setdefault(pk, {"bets": 0, "wins": 0, "losses": 0, "ties": 0})
            pp["bets"] += 1
            pp["wins" if res == "WIN" else ("losses" if res == "LOSE" else "ties")] += 1
            self._save_v4_state()
            n = self._v4["wins"] + self._v4["losses"]
            wr = (self._v4["wins"] / n * 100) if n else 0.0
            if self._v4["resolved"] % 25 == 0:
                logger.info("[v4 STATUS] signals=%d resolved=%d W/L/T=%d/%d/%d wr=%.2f%% pnl=%.1f" % (
                    self._v4["signals"], self._v4["resolved"], self._v4["wins"],
                    self._v4["losses"], self._v4["ties"], wr, self._v4["pnl"]))
            if tg_on:
                _send_telegram(
                    "%s %s %s [DRY-v4]\\nPattern: %s\\nPred: %s → Got: %s\\n"
                    "W/L/T: %d/%d/%d (%.1f%%)\\nsignals: %d resolved: %d" % (
                        icon, res, buf.table_name or table_id, pk, side, outcome,
                        self._v4["wins"], self._v4["losses"], self._v4["ties"], wr,
                        self._v4["signals"], self._v4["resolved"]))
        if _is_unsupported_table_name(buf.table_name or table_id):
            return
        seq_chars = []
        for h in (history_hands if history_hands is not None else (buf.hands or [])):
            c = _winner_to_char(h.get("winner"))
            if c and c != "T":
                seq_chars.append(c)
        seq = "".join(seq_chars)
        d = decide(seq, next_n=len(seq) + 1)
        if d.action == "LOOK":
            return
        side = "P" if d.action == "BET_P" else "B"
        pk = f"{d.china_pattern}|{d.big_pattern}|{side}"
        if pk not in V4_PATTERNS:
            return
        self._v4["signals"] += 1
        self._v4_pending[table_id] = {"side": side, "pattern_key": pk}
        self._save_v4_state()
        if tg_on:
            n = self._v4["wins"] + self._v4["losses"]
            wr = (self._v4["wins"] / n * 100) if n else 0.0
            side_name = "BANKER" if side == "B" else "PLAYER"
            _send_telegram(
                "\U0001F3AF v4 SIGNAL #%d [DRY]\\nTable: %s\\nPattern: %s\\n"
                "Predict: %s (%s)\\nW/L/T: %d/%d/%d (%.1f%%) | 解決: %d/%d" % (
                    self._v4["signals"], buf.table_name or table_id, pk, side, side_name,
                    self._v4["wins"], self._v4["losses"], self._v4["ties"], wr,
                    self._v4["resolved"], self._v4["signals"]))

    def _save_v4_state(self):
        try:
            import json as _json
            V4_STATE_PATH.write_text(_json.dumps({
                "updated_at": _utc_now_iso(),
                "patterns": sorted(V4_PATTERNS),
                "signals": self._v4["signals"], "resolved": self._v4["resolved"],
                "wins": self._v4["wins"], "losses": self._v4["losses"],
                "ties": self._v4["ties"], "pnl": self._v4["pnl"],
                "per_pattern": self._v4_per_pattern,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[v4] save failed: {e}")

    def _load_v4_state(self):
        try:
            if not V4_STATE_PATH.exists():
                return
            import json as _json
            s = _json.loads(V4_STATE_PATH.read_text(encoding="utf-8-sig"))
            if sorted(s.get("patterns") or []) != sorted(V4_PATTERNS):
                logger.info("[v4] pattern set changed -> reset v4 state")
                return
            self._v4["signals"] = s.get("signals", 0)
            self._v4["resolved"] = s.get("resolved", 0)
            self._v4["wins"] = s.get("wins", 0)
            self._v4["losses"] = s.get("losses", 0)
            self._v4["ties"] = s.get("ties", 0)
            self._v4["pnl"] = float(s.get("pnl", 0.0))
            self._v4_per_pattern = s.get("per_pattern", {}) or {}
            logger.info("[v4] state restored: signals=%d W/L/T=%d/%d/%d" % (
                self._v4["signals"], self._v4["wins"], self._v4["losses"], self._v4["ties"]))
        except Exception as e:
            logger.debug(f"[v4] load failed: {e}")

''' + A3
edits.append((A3, I3))

# 4) _on_new_hand 内で v4_track 呼び出し
A4 = "        # 1) 前回の予想を resolve\n"
I4 = '''        try:
            self._v4_track(table_id, buf, new_hand, history_hands)
        except Exception as _v4e:
            logger.debug(f"[v4] track error: {_v4e}")

''' + A4
edits.append((A4, I4))

for anchor, ins in edits:
    n = s.count(anchor)
    if n != 1:
        print("ANCHOR_FAIL count=%d for: %r" % (n, anchor[:50]))
        sys.exit(1)
    s = s.replace(anchor, ins)

open(F, "w", encoding="utf-8").write(s)
print("PATCHED_OK")
