#!/usr/bin/env python3
"""VPS本番 dual_line_pragmatic_bot.py のテレグラム決済メッセージに
「直近200勝率 + 直近20手の絵文字ストリーム」を追加する冪等パッチ。

対象: /opt/laplace2/dual_line_pragmatic_bot.py (v4パッチ済のVPS本番ファイル。
      repo直下とはv3決済メッセージの文面が違うため、repoのscp上書きではなく
      本スクリプトのアンカーパッチで入れる)

設計:
  - 累計W/L/Tは分母が大きすぎて動かない(1勝で0.005pt)ため、体感の温度計として
    直近200手(TIE除外・系統別ファイル永続)を各決済メッセージに1行+絵文字20手を追加。
  - v3(_resolve_prediction)とv4(_v4_track)の両方。系統別に別ファイル=合算禁止
    (同ハンド二重配信が偽の波を作る既知の罠)。
  - 表示専用: 挿入コードは全てtry/exceptで包み、決済処理を絶対に壊さない。
  - 冪等: _recent_wl_add_and_lines が既にあれば ALREADY_PATCHED。

使い方 (VPS上): python3 _patch_recent200_telegram.py
"""
from __future__ import annotations

import py_compile
import shutil
import time

F = "/opt/laplace2/dual_line_pragmatic_bot.py"

# ── 1) ヘルパー関数: _is_fast_table_name の直前に挿入 ──────────────────
HELPER_ANCHOR = "def _is_fast_table_name(name: str) -> bool:\n"
HELPER = '''_RECENT_WL_V3_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dual_line_recent200_v3.json")
_RECENT_WL_V4_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dual_line_recent200_v4.json")


def _recent_wl_add_and_lines(store_path, result, cap=200):
    """直近cap手のW/L窓(TIEは窓に入れない)を更新し、テレグラム表示行を返す。
    返り値: "直近200: 52.5% (105W95L)\\n✅❌…"(ストリームは直近20手) / データ無しは ""。
    表示専用: 呼び出し側で必ずtryで包む(決済処理を壊さない)。系統(v3/v4)別ファイルで
    永続化=再起動でも窓が消えない。合算禁止(同ハンド二重配信が偽の波を作るため)。"""
    import json as _json
    items = []
    try:
        with open(store_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, list):
            items = [c for c in data if c in ("W", "L")]
    except Exception:
        pass
    mark = "W" if result == "WIN" else ("L" if result == "LOSE" else "")
    if mark:
        items.append(mark)
    items = items[-int(cap):]
    if mark:
        try:
            with open(store_path, "w", encoding="utf-8") as f:
                _json.dump(items, f)
        except Exception:
            pass
    n = len(items)
    if not n:
        return ""
    w = items.count("W")
    stream = "".join("✅" if x == "W" else "❌" for x in items[-20:])
    return "直近%d: %.1f%% (%dW%dL)\\n%s" % (int(cap), w * 100.0 / n, w, n - w, stream)


''' + HELPER_ANCHOR

# ── 2) v3: _v3_res_text 構築直後(notify判定の前)に窓更新+行追記 ────────
V3_ANCHOR = (
    '            f"signals: {self.total_signals} resolved: {self.total_resolved}"\n'
    "        )\n"
    '        if self.notify_resolution and (result != "TIE" or self.notify_tie):\n'
)
V3_INSERT = (
    '            f"signals: {self.total_signals} resolved: {self.total_resolved}"\n'
    "        )\n"
    "        try:\n"
    "            _v3_recent = _recent_wl_add_and_lines(_RECENT_WL_V3_PATH, result)\n"
    "            if _v3_recent:\n"
    '                _v3_res_text += "\\n" + _v3_recent\n'
    "        except Exception:\n"
    "            pass\n"
    '        if self.notify_resolution and (result != "TIE" or self.notify_tie):\n'
)

# ── 3) v4: _v4_res_text 構築直後(if tg_on: の前)に窓更新+行追記 ─────────
V4_ANCHOR = (
    '                    self._v4["signals"], self._v4["resolved"]))\n'
    "            if tg_on:\n"
)
V4_INSERT = (
    '                    self._v4["signals"], self._v4["resolved"]))\n'
    "            try:\n"
    "                _v4_recent = _recent_wl_add_and_lines(_RECENT_WL_V4_PATH, res)\n"
    "                if _v4_recent:\n"
    '                    _v4_res_text += "\\n" + _v4_recent\n'
    "            except Exception:\n"
    "                pass\n"
    "            if tg_on:\n"
)


def apply(s: str) -> str:
    if "_recent_wl_add_and_lines" in s:
        return s  # already patched (idempotent)
    for anchor in (HELPER_ANCHOR, V3_ANCHOR, V4_ANCHOR):
        n = s.count(anchor)
        if n != 1:
            raise SystemExit("ANCHOR_FAIL count=%d for %r" % (n, anchor[:60]))
    s = s.replace(HELPER_ANCHOR, HELPER)
    s = s.replace(V3_ANCHOR, V3_INSERT)
    s = s.replace(V4_ANCHOR, V4_INSERT)
    return s


def main() -> int:
    s = open(F, encoding="utf-8").read()
    if "_recent_wl_add_and_lines" in s:
        print("ALREADY_PATCHED")
        return 0
    out = apply(s)
    bak = F + ".bak_recent200_" + time.strftime("%Y%m%d_%H%M%S")
    shutil.copy2(F, bak)
    open(F, "w", encoding="utf-8").write(out)
    py_compile.compile(F, doraise=True)
    print("PATCHED_OK backup=" + bak)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
