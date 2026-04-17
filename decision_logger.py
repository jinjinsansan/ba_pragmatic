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


def append_decision_event(event: DecisionEvent | dict[str, Any], *, path: str = "data/decisions.jsonl") -> Optional[str]:
    """Best-effort JSONL append. Returns error string if failed, else None."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = event.to_dict() if isinstance(event, DecisionEvent) else dict(event)
        if "schema_version" not in payload:
            payload["schema_version"] = 1
        if "captured_at" not in payload:
            payload["captured_at"] = utc_now_iso()
        line = json.dumps(payload, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return None
    except Exception as e:
        return str(e)
