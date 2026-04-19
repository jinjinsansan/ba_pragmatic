"""data/decisions.jsonl の ack/result event を SQLite DB から backfill.

既存の decision エントリが ack/result event を欠いているケースを検知し、
SQLite (`bacopy.sqlite3` の `decisions` table) から ack_json/result_json を取り出して
正しい event 形式で JSONL に追記する。

Usage (VPS 上で):
  BACOPY_DB_PATH=/opt/bacopy/data/bacopy.sqlite3 \
  BACOPY_DECISIONS_JSONL=/opt/bacopy/data/decisions.jsonl \
  python3 scripts/backfill_decision_jsonl.py

結果:
  - 欠けていた ack event を追記
  - 欠けていた result event を追記
  - 重複は出さない (decision_id + type で既存チェック)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# project root
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from decision_logger import append_ack_event, append_result_event  # noqa: E402


def main() -> int:
    db_path = os.environ.get("BACOPY_DB_PATH", "data/bacopy.sqlite3")
    jsonl_path = os.environ.get("BACOPY_DECISIONS_JSONL", "data/decisions.jsonl")
    if not os.path.exists(db_path):
        print(f"[backfill] DB not found: {db_path}", file=sys.stderr)
        return 2
    if not os.path.exists(jsonl_path):
        print(f"[backfill] JSONL not found: {jsonl_path} — creating empty.", file=sys.stderr)

    # 1) 既存 JSONL を読み、どの decision_id に ack/result event が有るか調査.
    has_ack: set[str] = set()
    has_res: set[str] = set()
    known_did: set[str] = set()
    if os.path.exists(jsonl_path):
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                did = str(ev.get("decision_id") or "")
                if not did:
                    continue
                known_did.add(did)
                t = str(ev.get("type") or "decision")
                if t == "ack":
                    has_ack.add(did)
                elif t == "result":
                    has_res.add(did)

    print(f"[backfill] JSONL decisions seen: {len(known_did)}, has_ack: {len(has_ack)}, has_result: {len(has_res)}")

    # 2) SQLite から全 decision と ack_json/result_json を取得.
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT decision_id, status, ack_json, result_json FROM decisions")
        rows = cur.fetchall()
    finally:
        conn.close()

    print(f"[backfill] DB rows: {len(rows)}")

    # 3) 欠けている ack/result を append.
    appended_ack = 0
    appended_res = 0
    for did, status, ack_json, result_json in rows:
        did = str(did or "")
        if not did:
            continue
        if ack_json and did not in has_ack:
            try:
                ack = json.loads(ack_json)
            except Exception:
                ack = {}
            if isinstance(ack, dict) and ack:
                err = append_ack_event(did, ack, status="processing" if status in ("processing","pending") else status or "processing", path=jsonl_path)
                if err is None:
                    appended_ack += 1
        if result_json and did not in has_res:
            try:
                res = json.loads(result_json)
            except Exception:
                res = {}
            if isinstance(res, dict) and res:
                err = append_result_event(did, res, status=status or "done", path=jsonl_path)
                if err is None:
                    appended_res += 1

    print(f"[backfill] appended ack events: {appended_ack}")
    print(f"[backfill] appended result events: {appended_res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
