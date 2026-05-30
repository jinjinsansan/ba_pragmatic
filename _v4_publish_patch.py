#!/usr/bin/env python3
"""Phase 2: VPS dual_line_pragmatic_bot.py に v4(10パターン) decision の
master API 配信を追加する冪等パッチ。

設計 (安全第一):
  - v3 (_publish_live_decision / source=dual_line_vps_whitelist6) には一切触れない。
    完全に独立した _publish_v4_decision を _v4_track 内から呼ぶだけ。
  - BACOPY_V4_PUBLISH=1 のときのみ実際に配信する。未設定/!=1 なら return ""
    で即抜け = 適用しても従来とバイト単位で同一の挙動 (暴発しない)。
  - 既存 Phase1 (_v4_patch.py による _v4_track shadow 集計) が適用済みである
    ことが前提。未適用なら PHASE1_NOT_APPLIED で停止 (誤適用防止)。
  - 冪等: _publish_v4_decision が既にあれば ALREADY_PATCHED。

⚠️ デプロイ順序 (PLAN_DUAL_LINE_V4_10PATTERN.md):
    本パッチを「適用」するだけなら env 未設定で無動作のため安全。
    ただし BACOPY_V4_PUBLISH=1 で「有効化」するのは Phase 3a(GUI mode
    フィルタ)を bafather にデプロイした後 (順序②)。mode を無視する旧GUI に
    v4 を流すと v3 と共有のパターンを実BETしてしまう (暴発)。

使い方 (VPS 上):
    python3 _v4_publish_patch.py
ローカル自己検証 (VPSファイル不要):
    apply(s) を任意の文字列に適用してテストできる (本リポジトリの
    コミットでは _v4_track 文字列に対する py_compile 検証済み)。
"""
from __future__ import annotations

F = "/opt/laplace2/dual_line_pragmatic_bot.py"

# ── 注入する v4 配信メソッド (4 space indent = クラスメソッド) ──────────
_PUBLISH_METHOD = '''    def _publish_v4_decision(self, table_id, buf, side, pattern_key):
        """Phase2: v4(10パターン) signal を master API に mode=v4 で配信。
        BACOPY_V4_PUBLISH=1 のときのみ動作 (既定OFF=何もしない)。
        v3(_publish_live_decision)とは完全独立。実際の賭け額/SEQ は受信側
        GUI(Phase4 money/SEQ)が決めるため amount は base unit を nominal 送付。"""
        if os.getenv("BACOPY_V4_PUBLISH", "0").strip() != "1":
            return ""
        if pattern_key not in V4_PATTERNS:
            return ""
        base_url = (
            os.getenv("BACOPY_SIGNAL_API_URL", "").rstrip("/")
            or os.getenv("BACOPY_REMOTE_API_URL", "").rstrip("/")
            or "https://master.bafather.uk"
        )
        api_key = (
            os.getenv("BACOPY_SIGNAL_API_KEY", "").strip()
            or os.getenv("BACOPY_REMOTE_API_KEY", "").strip()
            or os.getenv("BACOPY_API_KEY", "").strip()
            or os.getenv("LAPLACE_API_KEY", "").strip()
        )
        if not api_key:
            logger.error("[v4-publish] skipped: remote API key is not configured")
            return ""
        qpid = str(getattr(buf, "qpid_table_id", "") or table_id).strip()
        did = "dl_v4_" + uuid.uuid4().hex[:16]
        try:
            unit = float(getattr(getattr(self, "money", None), "unit", 0.0) or 0.0)
        except Exception:
            unit = 0.0
        parts = str(pattern_key).split("|")
        china_pattern = parts[0] if len(parts) >= 1 else ""
        big_pattern = parts[1] if len(parts) >= 2 else ""
        payload = {
            "decision_id": did,
            "provider": "pragmatic",
            "table_id": qpid,
            "table_name": str(buf.table_name or table_id),
            "captured_at": _utc_now_iso(),
            "source": "dual_line_vps_v4_10pattern",
            "mode": "v4",
            "logic_version": LOGIC_VERSION,
            "friend_action": {
                "action": "BET",
                "side": side,
                "amount": unit,
                "pattern_key": pattern_key,
                "china_pattern": china_pattern,
                "big_pattern": big_pattern,
                "qpid_table_id": qpid,
                "mode": "v4",
            },
        }
        result = self._api_post("/api/decisions", payload, base_url=base_url, api_key=api_key)
        if result.get("accepted") or result.get("ok"):
            logger.info("[v4-publish] accepted did=%s table=%s pattern=%s" % (did, qpid, pattern_key))
            return did
        logger.error("[v4-publish] failed did=%s base=%s result=%s" % (did, base_url, result))
        return ""

'''

# ── _v4_track 内: signal 確定直後 (signals+=1 / pending / save) の直後に
#    配信呼び出しを挿入。Telegram 通知(if tg_on:)より前。例外は track を
#    壊さないよう debug ログのみ。 ──────────────────────────────────────
_TRACK_ANCHOR = (
    '        self._v4["signals"] += 1\n'
    '        self._v4_pending[table_id] = {"side": side, "pattern_key": pk}\n'
    '        self._save_v4_state()\n'
)
_TRACK_INSERT = _TRACK_ANCHOR + (
    '        try:\n'
    '            self._publish_v4_decision(table_id, buf, side, pk)\n'
    '        except Exception as _v4pe:\n'
    '            logger.debug("[v4-publish] error: %s" % _v4pe)\n'
)

# 配信メソッドは _save_v4_state の直前に置く。
_METHOD_ANCHOR = "    def _save_v4_state(self):\n"


def apply(s: str) -> str:
    """文字列に Phase2 編集を施して返す。冪等・前提チェック付き。"""
    if "_publish_v4_decision" in s:
        return s  # already patched (idempotent)
    if "_v4_track" not in s or "V4_PATTERNS" not in s:
        raise SystemExit("PHASE1_NOT_APPLIED: _v4_track / V4_PATTERNS not found")
    for anchor in (_TRACK_ANCHOR, _METHOD_ANCHOR):
        n = s.count(anchor)
        if n != 1:
            raise SystemExit("ANCHOR_FAIL count=%d for %r" % (n, anchor[:48]))
    s = s.replace(_TRACK_ANCHOR, _TRACK_INSERT)
    s = s.replace(_METHOD_ANCHOR, _PUBLISH_METHOD + _METHOD_ANCHOR)
    return s


def main() -> int:
    s = open(F, encoding="utf-8").read()
    if "_publish_v4_decision" in s:
        print("ALREADY_PATCHED")
        return 0
    out = apply(s)
    open(F, "w", encoding="utf-8").write(out)
    print("PATCHED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
