from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class FriendAction:
    action: str  # "LOOK" | "BET"
    side: str = ""  # "PLAYER" | "BANKER" | "TIE" | ""
    amount: float = 0.0
    note: str = ""


@dataclass
class DecisionEvent:
    """Append-only decision log record (for audit + ML training).

    IMPORTANT:
      - snapshot must be captured at decision time (no look-ahead).
      - result must be filled only after the round resolves.
    """

    schema_version: int = 1
    decision_id: str = ""  # UUID recommended by caller
    captured_at: str = field(default_factory=utc_now_iso)
    provider: str = ""  # "evolution" | "pragmatic"
    table_id: str = ""
    table_name: str = ""
    game_id: str = ""
    phase: str = ""  # optional: "BET_OPEN" etc.
    snapshot: dict[str, Any] = field(default_factory=dict)
    friend_action: FriendAction = field(default_factory=FriendAction)
    ack: dict[str, Any] = field(default_factory=dict)
    result: str = ""  # "player" | "banker" | "tie"
    execution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # friend_action is already dict via asdict
        return d


def _append_line(path: str, obj: dict[str, Any]) -> Optional[str]:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return None
    except Exception as e:
        return str(e)


def append_decision_event(event: DecisionEvent | dict[str, Any], *, path: str = "data/decisions.jsonl") -> Optional[str]:
    """Append a DECISION event (friend pressed a button) to JSONL.

    The JSONL is event-sourced: decision / ack / result 各 event を同一 file に append.
    全 event に `type` フィールドを付けて読み手が弁別できるようにする。
    """
    payload = event.to_dict() if isinstance(event, DecisionEvent) else dict(event)
    payload["type"] = "decision"
    payload.setdefault("schema_version", 1)
    payload.setdefault("captured_at", utc_now_iso())
    return _append_line(path, payload)


def append_ack_event(decision_id: str, ack: dict[str, Any], status: str = "processing",
                     *, path: str = "data/decisions.jsonl") -> Optional[str]:
    """Append an ACK event (executor received & started processing) to JSONL."""
    payload: dict[str, Any] = {
        "schema_version": 1,
        "type": "ack",
        "decision_id": str(decision_id or ""),
        "captured_at": utc_now_iso(),
        "status": str(status or "processing"),
        "ack": ack if isinstance(ack, dict) else {},
    }
    return _append_line(path, payload)


def append_result_event(decision_id: str, result: dict[str, Any], status: str = "done",
                        *, path: str = "data/decisions.jsonl") -> Optional[str]:
    """Append a RESULT event (round outcome + execution details) to JSONL.

    ML 契約:
      result.outcome = "player" | "banker" | "tie"
      result.stake_delta = 損益 (USD)
      result.bet_confirm = Stake からの BET 確定メッセージ
      result.error (エラー時のみ)
    """
    payload: dict[str, Any] = {
        "schema_version": 1,
        "type": "result",
        "decision_id": str(decision_id or ""),
        "captured_at": utc_now_iso(),
        "status": str(status or "done"),
        "result": result if isinstance(result, dict) else {},
    }
    return _append_line(path, payload)


def reconstruct_decisions(path: str = "data/decisions.jsonl") -> list[dict[str, Any]]:
    """JSONL の event-sourced log を decision_id 毎にまとめた完全な decision 記録に畳み込む.

    ML 学習用に読み手が 1 行 = 1 decision で扱えるようにする。
    Returns list of dicts, each containing:
      decision_id, captured_at, provider, table_id, table_name, game_id, phase,
      snapshot, friend_action, ack, result (outcome string), execution (full result dict),
      acked_at, resolved_at, status
    """
    if not os.path.exists(path):
        return []
    by_id: dict[str, dict[str, Any]] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
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
                t = str(ev.get("type") or "decision")
                rec = by_id.setdefault(did, {
                    "decision_id": did,
                    "snapshot": {},
                    "friend_action": {},
                    "ack": {},
                    "result": "",
                    "execution": {},
                    "status": "",
                    "captured_at": "",
                    "acked_at": "",
                    "resolved_at": "",
                })
                if t == "decision":
                    for k in ("captured_at", "provider", "table_id", "table_name", "game_id",
                              "phase", "snapshot", "friend_action", "schema_version",
                              "target_executor_id"):
                        if k in ev:
                            rec[k] = ev[k]
                elif t == "ack":
                    ack = ev.get("ack") or {}
                    if isinstance(ack, dict):
                        rec["ack"] = ack
                    rec["acked_at"] = ev.get("captured_at") or rec["acked_at"]
                    if ev.get("status"):
                        rec["status"] = str(ev["status"])
                elif t == "result":
                    r = ev.get("result") or {}
                    if isinstance(r, dict):
                        # "outcome" が ML contract の result 文字列.
                        outcome = r.get("outcome") or r.get("result") or ""
                        rec["result"] = str(outcome).lower()
                        rec["execution"] = r
                    rec["resolved_at"] = ev.get("captured_at") or rec["resolved_at"]
                    if ev.get("status"):
                        rec["status"] = str(ev["status"])
    except Exception:
        pass
    # 時系列ソート
    records = list(by_id.values())
    records.sort(key=lambda r: str(r.get("captured_at") or ""))
    return records
