"""LAPLACE Logic API (FastAPI)

VPS 上で稼働する LAPLACE ロジックエンジン API。
GUI (ローカル PC) からの BET 判断依頼/結果報告を受け付け、
MaruBatsuTracker のロジックと状態管理を一元化する。

エンドポイント:
  POST   /api/sessions               - セッション作成 (user_id 指定)
  GET    /api/sessions/{user_id}     - 現在の状態取得
  POST   /api/sessions/{user_id}/decide       - 次 BET 情報を取得
  POST   /api/sessions/{user_id}/result       - ハンド結果を報告 → 次アクション返却
  POST   /api/sessions/{user_id}/reset        - セッションリセット (利確/損切り)
  POST   /api/sessions/{user_id}/shoe-change  - シュー交換処理
  DELETE /api/sessions/{user_id}     - セッション削除
  GET    /api/health                 - ヘルスチェック

認証: Bearer トークン (LAPLACE_API_KEY 環境変数)
状態永続化: /opt/laplace/api_state/{user_id}.json
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure we can import marubatsu_strategy from the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from marubatsu_strategy import MaruBatsuTracker, SetData, SEQ, SEQ_COUNTER, SET_SIZE_COUNTER
from bot_manager import get_bot_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("laplace.api")

# --- Configuration ---
API_KEY = os.getenv("LAPLACE_API_KEY", "").strip()  # Legacy master key (fallback)
ADMIN_KEY = os.getenv("LAPLACE_ADMIN_KEY", "").strip()  # Admin endpoint auth
STATE_DIR = Path(os.getenv("LAPLACE_STATE_DIR", "/opt/laplace/api_state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
KEYS_FILE = Path(os.getenv("LAPLACE_KEYS_FILE", str(STATE_DIR.parent / "api_keys.json")))

DEFAULT_PROFIT_STOP = 50
DEFAULT_LOSS_CUT = 200
DEFAULT_CHIP_BASE = 1.0

# --- Thread-safe session store ---
_sessions_lock = threading.RLock()

# Per-user table selector wait state (for primary-threshold wait-then-relax logic)
_selector_wait_state: dict[str, float] = {}
_selector_wait_lock = threading.RLock()


# ======== Models ========

class CreateSessionRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    chip_base: float = Field(DEFAULT_CHIP_BASE, gt=0)
    profit_stop: int = Field(DEFAULT_PROFIT_STOP, gt=0)
    loss_cut: int = Field(DEFAULT_LOSS_CUT, gt=0)
    resume: bool = True
    counter_mode: bool = False
    counter_set_size: int | None = None


class UpdateConfigRequest(BaseModel):
    chip_base: Optional[float] = None
    profit_stop: Optional[int] = None
    loss_cut: Optional[int] = None


class RestoreSessionRequest(BaseModel):
    state: dict


class ResultRequest(BaseModel):
    result: str = Field(..., description="player | banker | tie")
    side: str = Field("player", description="player | banker (bet side)")


class SessionState(BaseModel):
    user_id: str
    chip_base: float
    profit_stop: int
    loss_cut: int
    counter_mode: bool
    set_size: int
    session_count: int
    total_bets: int
    total_wins: int
    total_losses: int
    total_ties: int
    set_count: int
    current_turn: int
    current_unit_idx: int
    current_unit: int
    cumulative_profit: int
    cumulative_money: float
    effective_profit: int
    overshoot: int
    total_o: int
    total_x: int
    turns_display: str
    sets: list[dict]
    should_reset: bool
    reset_reason: Optional[str]
    created_at: str
    updated_at: str


class DecideResponse(BaseModel):
    action: str  # "bet" | "reset"
    side: str    # "player" (常に)
    unit_idx: int
    unit_chips: int
    bet_amount: float  # chip_base * unit
    turn_number: int
    set_index: int
    state: SessionState


class ResultResponse(BaseModel):
    accepted: bool
    result: str
    won: Optional[bool]
    completed_set: Optional[dict]
    should_reset: bool
    reset_reason: Optional[str]
    state: SessionState


class SelectTableRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    configs: dict = Field(..., description="tid -> {title, frontendApp, bl, gt, published}")
    players: dict = Field(..., description="tid -> int")
    histories: dict = Field(..., description="tid -> list of {c, ties, ...}")
    excluded_ids: list[str] = Field(default_factory=list)
    fixed_name: Optional[str] = None
    selector_config: Optional[dict] = Field(default=None, description="GUI-configured thresholds")


class SelectTableResponse(BaseModel):
    found: bool
    table_id: Optional[str] = None
    title: Optional[str] = None
    players: Optional[int] = None
    hands: Optional[int] = None
    p_count: Optional[int] = None
    b_count: Optional[int] = None
    tie_count: Optional[int] = None
    last_5: list[str] = Field(default_factory=list)
    score: Optional[float] = None
    wait_status: Optional[str] = None  # "waiting_primary" | "still_waiting" | "no_candidates"
    debug: Optional[dict] = None


class ExitCheckRequest(BaseModel):
    table_id: str
    players: int
    history: list = Field(default_factory=list)


class ExitCheckResponse(BaseModel):
    exit_reason: Optional[str] = None


class BotStartRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    target_table_name: str = Field(
        "Japanese Speed Baccarat A",
        description="Fixed target table name for verification mode",
    )
    dry_run: bool = True
    chip_base: float = Field(DEFAULT_CHIP_BASE, gt=0)
    profit_stop: int = Field(DEFAULT_PROFIT_STOP, gt=0)
    loss_cut: int = Field(DEFAULT_LOSS_CUT, gt=0)
    resume_session: bool = True


class BotStartResponse(BaseModel):
    started: bool
    run_id: str
    pid: int
    log_path: str
    config: dict


class BotStopResponse(BaseModel):
    was_running: bool
    run_id: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    stopped_at: Optional[float] = None


class BotStatusResponse(BaseModel):
    running: bool
    run_id: Optional[str]
    pid: Optional[int]
    started_at: Optional[float]
    uptime_seconds: Optional[float]
    log_path: Optional[str]
    config: Optional[dict]
    last_exit: Optional[dict]
    session_state: Optional[SessionState] = None


# ======== Auth & rate limiting ========

KEY_PREFIX = "lpk_live_"  # laplace key, live environment


@dataclass
class ApiKeyRecord:
    key: str                      # full secret (lpk_live_...)
    user_id: str
    name: str
    created_at: str
    rate_limit_per_hour: int = 3600
    ip_allowlist: list[str] = field(default_factory=list)  # CIDR or plain IP; empty = any
    enabled: bool = True

    def to_public_dict(self) -> dict:
        return {
            "prefix": self.key[: len(KEY_PREFIX) + 8],  # lpk_live_XXXXXXXX (masked tail)
            "user_id": self.user_id,
            "name": self.name,
            "created_at": self.created_at,
            "rate_limit_per_hour": self.rate_limit_per_hour,
            "ip_allowlist": list(self.ip_allowlist),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ApiKeyRecord":
        return cls(
            key=d["key"],
            user_id=d["user_id"],
            name=d.get("name", ""),
            created_at=d.get("created_at", datetime.utcnow().isoformat() + "Z"),
            rate_limit_per_hour=int(d.get("rate_limit_per_hour", 3600)),
            ip_allowlist=list(d.get("ip_allowlist", [])),
            enabled=bool(d.get("enabled", True)),
        )


class ApiKeyRegistry:
    """Thread-safe file-backed registry of per-user API keys.

    Layout: {"keys": {"<full_key>": {...record fields...}}}
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._keys: dict[str, ApiKeyRecord] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._keys.clear()
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Failed to parse {self.path}: {e}")
                return
            for key, rec_dict in data.get("keys", {}).items():
                try:
                    rec_dict = {**rec_dict, "key": key}
                    self._keys[key] = ApiKeyRecord.from_dict(rec_dict)
                except Exception as e:
                    logger.error(f"Skipping malformed key entry: {e}")

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "keys": {
                    k: {
                        "user_id": r.user_id,
                        "name": r.name,
                        "created_at": r.created_at,
                        "rate_limit_per_hour": r.rate_limit_per_hour,
                        "ip_allowlist": r.ip_allowlist,
                        "enabled": r.enabled,
                    }
                    for k, r in self._keys.items()
                }
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)

    def issue(
        self,
        user_id: str,
        name: str,
        rate_limit_per_hour: int = 3600,
        ip_allowlist: Optional[list[str]] = None,
    ) -> ApiKeyRecord:
        key = KEY_PREFIX + secrets.token_hex(16)
        rec = ApiKeyRecord(
            key=key,
            user_id=user_id,
            name=name,
            created_at=datetime.utcnow().isoformat() + "Z",
            rate_limit_per_hour=rate_limit_per_hour,
            ip_allowlist=list(ip_allowlist or []),
            enabled=True,
        )
        with self._lock:
            self._keys[key] = rec
        self.save()
        return rec

    def revoke(self, prefix: str) -> bool:
        """Revoke by prefix (full key or first N chars). Returns True if removed."""
        with self._lock:
            match = next(
                (k for k in self._keys if k == prefix or k.startswith(prefix)),
                None,
            )
            if not match:
                return False
            del self._keys[match]
        self.save()
        return True

    def set_enabled(self, prefix: str, enabled: bool) -> bool:
        with self._lock:
            match = next(
                (k for k in self._keys if k == prefix or k.startswith(prefix)),
                None,
            )
            if not match:
                return False
            self._keys[match].enabled = enabled
        self.save()
        return True

    def get(self, key: str) -> Optional[ApiKeyRecord]:
        with self._lock:
            return self._keys.get(key)

    def list(self) -> list[ApiKeyRecord]:
        with self._lock:
            return list(self._keys.values())

    @property
    def empty(self) -> bool:
        with self._lock:
            return not self._keys


class RateLimiter:
    """Sliding-window in-memory rate limiter, keyed by API key."""

    def __init__(self, window_seconds: int = 3600):
        self.window = window_seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str, limit_per_hour: int) -> tuple[bool, int]:
        """Return (allowed, remaining). Counts this call if allowed."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit_per_hour:
                return False, 0
            dq.append(now)
            return True, max(0, limit_per_hour - len(dq))

    def usage(self, key: str) -> int:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            dq = self._hits.get(key)
            if not dq:
                return 0
            while dq and dq[0] < cutoff:
                dq.popleft()
            return len(dq)


def _ip_matches_allowlist(client_ip: str, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            else:
                if ip == ipaddress.ip_address(entry):
                    return True
        except ValueError:
            continue
    return False


@dataclass
class UserContext:
    user_id: str
    key_prefix: str
    is_admin: bool = False
    rate_remaining: int = 0


# Global instances
_api_keys = ApiKeyRegistry(KEYS_FILE)
_rate_limiter = RateLimiter()


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer ") :].strip() or None


async def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> UserContext:
    """Per-user key auth with rate limit + IP allowlist.

    Resolution order:
      1. If registry has keys, look up the Bearer token there.
      2. Else fall back to legacy single-key mode (LAPLACE_API_KEY env).
      3. Else open access (dev mode, no auth).

    Admin keys (ADMIN_KEY env) bypass user_id scoping.
    """
    token = _extract_bearer(authorization)
    client_ip = request.client.host if request.client else "0.0.0.0"

    # --- Admin master key (for admin endpoints and cross-user operations) ---
    if ADMIN_KEY and token == ADMIN_KEY:
        return UserContext(user_id="*", key_prefix="admin", is_admin=True)

    # --- Legacy single key: always accepted as admin for backward compat.
    # Even after per-user keys are populated, the legacy key continues to
    # work so existing deployments don't break mid-migration. ---
    if API_KEY and token == API_KEY:
        return UserContext(user_id="*", key_prefix="legacy", is_admin=True)

    # --- Per-user registry mode ---
    if not _api_keys.empty:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
            )
        rec = _api_keys.get(token)
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
        if not rec.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key disabled",
            )
        if not _ip_matches_allowlist(client_ip, rec.ip_allowlist):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"IP {client_ip} not in allowlist",
            )
        allowed, remaining = _rate_limiter.check_and_record(
            token, rec.rate_limit_per_hour
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded ({rec.rate_limit_per_hour}/hour)",
            )
        return UserContext(
            user_id=rec.user_id,
            key_prefix=rec.key[: len(KEY_PREFIX) + 8],
            is_admin=False,
            rate_remaining=remaining,
        )

    # --- Legacy-only mode: key set but no registry ---
    if API_KEY:
        # Already handled above; reaching here means the token was wrong.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # --- Dev mode: no auth configured ---
    return UserContext(user_id="*", key_prefix="dev", is_admin=True)


def require_user_scope(ctx: UserContext, path_user_id: str) -> None:
    """Ensure the request's user_id matches the key owner (unless admin)."""
    if ctx.is_admin or ctx.user_id == "*":
        return
    if ctx.user_id != path_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Key is scoped to user '{ctx.user_id}', not '{path_user_id}'",
        )


def require_admin(ctx: UserContext) -> None:
    if not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin key required",
        )


# ======== Session Wrapper ========

class LaplaceSession:
    """Pure-logic wrapper around MaruBatsuTracker with persistence."""

    def __init__(
        self,
        user_id: str,
        chip_base: float = DEFAULT_CHIP_BASE,
        profit_stop: int = DEFAULT_PROFIT_STOP,
        loss_cut: int = DEFAULT_LOSS_CUT,
        counter_mode: bool = False,
        counter_set_size: int | None = None,
    ):
        self.user_id = user_id
        self.chip_base = chip_base
        self.profit_stop = profit_stop
        self.loss_cut = loss_cut
        self.counter_mode = bool(counter_mode)
        self.set_size = counter_set_size or (SET_SIZE_COUNTER if self.counter_mode else 7)
        self.seq = SEQ_COUNTER if self.counter_mode else SEQ
        self.tracker = MaruBatsuTracker(chip_base=chip_base, seq=self.seq, set_size=self.set_size)
        self.session_count = 0
        self.total_bets = 0
        self.total_wins = 0
        self.total_losses = 0
        self.total_ties = 0
        self.created_at = datetime.utcnow().isoformat() + "Z"
        self.updated_at = self.created_at

    # --- Persistence ---

    @property
    def state_path(self) -> Path:
        # Sanitize user_id for filesystem safety
        safe = "".join(c for c in self.user_id if c.isalnum() or c in ("-", "_"))
        return STATE_DIR / f"{safe or 'default'}.json"

    def save(self) -> None:
        state = {
            "user_id": self.user_id,
            "chip_base": self.chip_base,
            "profit_stop": self.profit_stop,
            "loss_cut": self.loss_cut,
            "counter_mode": self.counter_mode,
            "set_size": self.set_size,
            "sets": [
                {
                    "set_index": s.set_index,
                    "results": s.results,
                    "wins": s.wins,
                    "losses": s.losses,
                    "overshoot": s.overshoot,
                    "slashed": s.slashed,
                    "used_unit_idx": s.used_unit_idx,
                    "next_unit_idx": s.next_unit_idx,
                    "set_profit": s.set_profit,
                    "cumulative_profit": s.cumulative_profit,
                }
                for s in self.tracker.sets
            ],
            "current_turns": self.tracker.current_turns,
            "total_o": self.tracker.total_o,
            "total_x": self.tracker.total_x,
            "session_count": self.session_count,
            "total_bets": self.total_bets,
            "total_wins": self.total_wins,
            "total_losses": self.total_losses,
            "total_ties": self.total_ties,
            "created_at": self.created_at,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        self.updated_at = state["updated_at"]
        self.state_path.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def apply_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        counter_mode = state.get("counter_mode", self.counter_mode)
        set_size = state.get("set_size", self.set_size)
        if counter_mode != self.counter_mode or set_size != self.set_size:
            self.counter_mode = bool(counter_mode)
            self.set_size = int(set_size or (SET_SIZE_COUNTER if self.counter_mode else 7))
            self.seq = SEQ_COUNTER if self.counter_mode else SEQ
            self.tracker = MaruBatsuTracker(chip_base=self.chip_base, seq=self.seq, set_size=self.set_size)
        chip_base = state.get("chip_base")
        if isinstance(chip_base, (int, float)) and chip_base > 0:
            self.chip_base = float(chip_base)
            self.tracker.chip_base = float(chip_base)
        profit_stop = state.get("profit_stop")
        if isinstance(profit_stop, (int, float)) and profit_stop > 0:
            self.profit_stop = int(profit_stop)
        loss_cut = state.get("loss_cut")
        if isinstance(loss_cut, (int, float)) and loss_cut > 0:
            self.loss_cut = int(loss_cut)
        self.tracker.sets.clear()
        for sd in state.get("sets", []):
            try:
                self.tracker.sets.append(SetData(**sd))
            except Exception:
                pass
        turns = state.get("current_turns")
        if turns is None:
            turns_display = state.get("turns_display", "")
            if isinstance(turns_display, str):
                turns = list(turns_display)
        self.tracker.current_turns = list(turns) if isinstance(turns, list) else []
        self.tracker.total_o = state.get("total_o", 0) or 0
        self.tracker.total_x = state.get("total_x", 0) or 0
        self.session_count = state.get("session_count", 0) or 0
        self.total_bets = state.get("total_bets", 0) or 0
        self.total_wins = state.get("total_wins", 0) or 0
        self.total_losses = state.get("total_losses", 0) or 0
        self.total_ties = state.get("total_ties", 0) or 0
        self.created_at = state.get("created_at", self.created_at)
        self.updated_at = state.get("updated_at", self.updated_at)

    @classmethod
    def load(cls, user_id: str) -> Optional["LaplaceSession"]:
        safe = "".join(c for c in user_id if c.isalnum() or c in ("-", "_")) or "default"
        path = STATE_DIR / f"{safe}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"load {user_id}: {e}")
            return None

        obj = cls(
            user_id=user_id,
            chip_base=data.get("chip_base", DEFAULT_CHIP_BASE),
            profit_stop=data.get("profit_stop", DEFAULT_PROFIT_STOP),
            loss_cut=data.get("loss_cut", DEFAULT_LOSS_CUT),
            counter_mode=data.get("counter_mode", False),
            counter_set_size=data.get("set_size"),
        )
        for sd in data.get("sets", []):
            obj.tracker.sets.append(SetData(**sd))
        obj.tracker.current_turns = data.get("current_turns", [])
        obj.tracker.total_o = data.get("total_o", 0)
        obj.tracker.total_x = data.get("total_x", 0)
        obj.session_count = data.get("session_count", 0)
        obj.total_bets = data.get("total_bets", 0)
        obj.total_wins = data.get("total_wins", 0)
        obj.total_losses = data.get("total_losses", 0)
        obj.total_ties = data.get("total_ties", 0)
        obj.created_at = data.get("created_at", datetime.utcnow().isoformat() + "Z")
        obj.updated_at = data.get("updated_at", obj.created_at)
        return obj

    def delete_state(self) -> None:
        try:
            if self.state_path.exists():
                self.state_path.unlink()
        except Exception as e:
            logger.warning(f"delete_state {self.user_id}: {e}")

    # --- Logic ---

    def effective_profit(self) -> int:
        cp = self.tracker.cumulative_profit
        turns = self.tracker.current_turns
        if turns:
            wins = turns.count("O")
            losses = turns.count("X")
            unit = self.seq[self.tracker.current_unit_idx]
            cp += (wins - losses) * unit
        return cp

    def should_reset(self) -> tuple[bool, Optional[str]]:
        cp = self.effective_profit()
        if cp >= self.profit_stop:
            return True, "利確"
        if cp <= -self.loss_cut:
            return True, "損切り"
        return False, None

    def add_result(self, result: str, side: str = "player") -> tuple[Optional[SetData], Optional[bool]]:
        """Register a hand result. Returns (completed_set | None, won | None)."""
        if result not in ("player", "banker", "tie"):
            raise ValueError(f"invalid result: {result}")
        if side not in ("player", "banker"):
            raise ValueError(f"invalid side: {side}")

        self.total_bets += 1
        if result == "tie":
            self.total_ties += 1
            return None, None

        won = result == side
        if won:
            self.total_wins += 1
        else:
            self.total_losses += 1

        completed = self.tracker.add_result("player" if won else "banker")
        return completed, won

    def reset_session(self, reason: str) -> None:
        self.session_count += 1
        self.tracker.sets.clear()
        self.tracker.current_turns.clear()
        # Note: total_o/total_x/total_bets/wins/losses are not reset (cumulative stats)

    def handle_shoe_change(self) -> list[str]:
        discarded = list(self.tracker.current_turns)
        self.tracker.current_turns.clear()
        return discarded

    # --- Serialization ---

    def to_state(self) -> SessionState:
        turns = self.tracker.current_turns
        turns_display = "".join("O" if t == "O" else "X" for t in turns)
        cp = self.tracker.cumulative_profit
        ep = self.effective_profit()
        should, reason = self.should_reset()
        return SessionState(
            user_id=self.user_id,
            chip_base=self.chip_base,
            profit_stop=self.profit_stop,
            loss_cut=self.loss_cut,
            counter_mode=self.counter_mode,
            set_size=self.set_size,
            session_count=self.session_count,
            total_bets=self.total_bets,
            total_wins=self.total_wins,
            total_losses=self.total_losses,
            total_ties=self.total_ties,
            set_count=len(self.tracker.sets),
            current_turn=len(turns),
            current_unit_idx=self.tracker.current_unit_idx,
            current_unit=self.seq[self.tracker.current_unit_idx],
            cumulative_profit=cp,
            cumulative_money=cp * self.chip_base,
            effective_profit=ep,
            overshoot=self.tracker.prev_overshoot,
            total_o=self.tracker.total_o,
            total_x=self.tracker.total_x,
            turns_display=turns_display,
            sets=[
                {
                    "set_index": s.set_index,
                    "results": s.results,
                    "wins": s.wins,
                    "losses": s.losses,
                    "overshoot": s.overshoot,
                    "slashed": s.slashed,
                    "used_unit_idx": s.used_unit_idx,
                    "next_unit_idx": s.next_unit_idx,
                    "used_unit_chips": self.seq[s.used_unit_idx],
                    "next_unit_chips": self.seq[s.next_unit_idx],
                    "set_profit": s.set_profit,
                    "cumulative_profit": s.cumulative_profit,
                }
                for s in self.tracker.sets
            ],
            should_reset=should,
            reset_reason=reason,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


# ======== In-memory session registry ========

_SESSIONS: dict[str, LaplaceSession] = {}


def get_or_load(user_id: str) -> Optional[LaplaceSession]:
    with _sessions_lock:
        if user_id in _SESSIONS:
            return _SESSIONS[user_id]
        loaded = LaplaceSession.load(user_id)
        if loaded:
            _SESSIONS[user_id] = loaded
        return loaded


def get_required(user_id: str) -> LaplaceSession:
    sess = get_or_load(user_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"session '{user_id}' not found")
    return sess


# ======== FastAPI app ========

app = FastAPI(
    title="LAPLACE Logic API",
    version="1.0.0",
    description="VPS-hosted MaruBatsu logic engine for LAPLACE baccarat bot",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Fingerprint logging middleware ---
# Every shipped client sends X-Client-Build-Id / X-Client-User headers
# that are embedded per-user at build time. Logging them here lets us
# correlate leaked binaries with usage patterns (L.7).

_fp_logger = logging.getLogger("laplace.fingerprint")


@app.middleware("http")
async def fingerprint_middleware(request: Request, call_next):
    build_id = request.headers.get("x-client-build-id", "-")
    client_user = request.headers.get("x-client-user", "-")
    channel = request.headers.get("x-client-channel", "-")
    client_ip = request.client.host if request.client else "-"
    response = await call_next(request)
    if build_id != "-" or client_user != "-":
        _fp_logger.info(
            "fp=%s user=%s channel=%s ip=%s path=%s status=%d",
            build_id,
            client_user,
            channel,
            client_ip,
            request.url.path,
            response.status_code,
        )
    return response


@app.get("/api/health")
async def health():
    """Public unauthenticated health probe."""
    with _sessions_lock:
        in_memory = len(_SESSIONS)
    disk_count = len(list(STATE_DIR.glob("*.json")))
    if not _api_keys.empty:
        auth_mode = "per_user_keys"
    elif API_KEY:
        auth_mode = "legacy_single_key"
    else:
        auth_mode = "open"
    return {
        "status": "ok",
        "in_memory_sessions": in_memory,
        "persisted_sessions": disk_count,
        "auth_mode": auth_mode,
        "registered_keys": len(_api_keys.list()),
        "admin_configured": bool(ADMIN_KEY),
        "state_dir": str(STATE_DIR),
        "time": datetime.utcnow().isoformat() + "Z",
    }


# ======== Admin: API key management ========


class IssueKeyRequest(BaseModel):
    user_id: str
    name: str = ""
    rate_limit_per_hour: int = 3600
    ip_allowlist: list[str] = []


class IssueKeyResponse(BaseModel):
    key: str  # full secret — returned ONCE
    user_id: str
    name: str
    created_at: str
    rate_limit_per_hour: int
    ip_allowlist: list[str]


class KeyListResponse(BaseModel):
    keys: list[dict]


@app.post("/api/admin/keys", response_model=IssueKeyResponse)
async def admin_issue_key(
    req: IssueKeyRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    """Issue a new per-user API key. Returns the full secret ONCE."""
    require_admin(ctx)
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id required")
    if req.rate_limit_per_hour < 1 or req.rate_limit_per_hour > 100_000:
        raise HTTPException(status_code=400, detail="rate_limit_per_hour out of range")
    rec = _api_keys.issue(
        user_id=req.user_id.strip(),
        name=req.name.strip(),
        rate_limit_per_hour=req.rate_limit_per_hour,
        ip_allowlist=req.ip_allowlist,
    )
    logger.info(f"admin_issue_key: user={rec.user_id} prefix={rec.key[:16]}...")
    return IssueKeyResponse(
        key=rec.key,
        user_id=rec.user_id,
        name=rec.name,
        created_at=rec.created_at,
        rate_limit_per_hour=rec.rate_limit_per_hour,
        ip_allowlist=rec.ip_allowlist,
    )


@app.get("/api/admin/keys", response_model=KeyListResponse)
async def admin_list_keys(ctx: UserContext = Depends(verify_api_key)):
    """List all issued keys (masked — full secret never returned again)."""
    require_admin(ctx)
    return KeyListResponse(
        keys=[
            {
                **rec.to_public_dict(),
                "rate_usage_last_hour": _rate_limiter.usage(rec.key),
            }
            for rec in _api_keys.list()
        ]
    )


@app.delete("/api/admin/keys/{prefix}")
async def admin_revoke_key(
    prefix: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    if not _api_keys.revoke(prefix):
        raise HTTPException(status_code=404, detail=f"No key matching '{prefix}'")
    logger.info(f"admin_revoke_key: prefix={prefix}")
    return {"revoked": True, "prefix": prefix}


@app.patch("/api/admin/keys/{prefix}")
async def admin_toggle_key(
    prefix: str,
    enabled: bool,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    if not _api_keys.set_enabled(prefix, enabled):
        raise HTTPException(status_code=404, detail=f"No key matching '{prefix}'")
    logger.info(f"admin_toggle_key: prefix={prefix} enabled={enabled}")
    return {"updated": True, "prefix": prefix, "enabled": enabled}


@app.post("/api/admin/keys/reload")
async def admin_reload_keys(ctx: UserContext = Depends(verify_api_key)):
    """Reload the key registry from disk (useful after manual JSON edits)."""
    require_admin(ctx)
    _api_keys.load()
    return {"reloaded": True, "count": len(_api_keys.list())}


@app.post("/api/sessions")
async def create_session(
    req: CreateSessionRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, req.user_id)
    with _sessions_lock:
        existing = get_or_load(req.user_id)
        if existing and req.resume:
            desired_set_size = req.counter_set_size or (SET_SIZE_COUNTER if req.counter_mode else 7)
            mode_changed = existing.counter_mode != req.counter_mode
            size_changed = existing.set_size != desired_set_size
            if mode_changed or size_changed:
                existing.delete_state()
                _SESSIONS.pop(req.user_id, None)
                existing = None
            else:
                # Update config but keep state
                existing.chip_base = req.chip_base
                existing.profit_stop = req.profit_stop
                existing.loss_cut = req.loss_cut
                existing.tracker.chip_base = req.chip_base
                existing.save()
                logger.info(f"session resumed: {req.user_id}")
                return {"created": False, "resumed": True, "state": existing.to_state()}

        # Create fresh (or overwrite)
        if existing and not req.resume:
            existing.delete_state()
            _SESSIONS.pop(req.user_id, None)

        sess = LaplaceSession(
            user_id=req.user_id,
            chip_base=req.chip_base,
            profit_stop=req.profit_stop,
            loss_cut=req.loss_cut,
            counter_mode=req.counter_mode,
            counter_set_size=req.counter_set_size,
        )
        sess.save()
        _SESSIONS[req.user_id] = sess
        logger.info(f"session created: {req.user_id}")
        return {"created": True, "resumed": False, "state": sess.to_state()}


@app.get("/api/sessions/{user_id}")
async def get_session(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_required(user_id)
        return {"state": sess.to_state()}


@app.patch("/api/sessions/{user_id}")
async def update_session(
    user_id: str,
    req: UpdateConfigRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_required(user_id)
        if req.chip_base is not None and req.chip_base > 0:
            sess.chip_base = req.chip_base
            sess.tracker.chip_base = req.chip_base
        if req.profit_stop is not None and req.profit_stop > 0:
            sess.profit_stop = req.profit_stop
        if req.loss_cut is not None and req.loss_cut > 0:
            sess.loss_cut = req.loss_cut
        sess.save()
        return {"updated": True, "state": sess.to_state()}


@app.post("/api/sessions/{user_id}/restore")
async def restore_session(
    user_id: str,
    req: RestoreSessionRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_or_load(user_id)
        if sess is None:
            sess = LaplaceSession(
                user_id=user_id,
                chip_base=req.state.get("chip_base", DEFAULT_CHIP_BASE),
                profit_stop=req.state.get("profit_stop", DEFAULT_PROFIT_STOP),
                loss_cut=req.state.get("loss_cut", DEFAULT_LOSS_CUT),
                counter_mode=req.state.get("counter_mode", False),
                counter_set_size=req.state.get("set_size"),
            )
        sess.apply_state(req.state)
        sess.save()
        _SESSIONS[user_id] = sess
        return {"restored": True, "state": sess.to_state()}


@app.post("/api/sessions/{user_id}/decide")
async def decide_bet(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    """Return next BET parameters (always Player side for maru-batsu)."""
    require_user_scope(ctx, user_id)
    # Billing enforcement: suspended users cannot place bets
    if not _billing.check_grace(user_id):
        raise HTTPException(
            status_code=403,
            detail="Account suspended: insufficient balance. Please top up your charge.",
        )
    with _sessions_lock:
        sess = get_required(user_id)
        should, reason = sess.should_reset()
        if should:
            return DecideResponse(
                action="reset",
                side="player",
                unit_idx=sess.tracker.current_unit_idx,
                unit_chips=sess.seq[sess.tracker.current_unit_idx],
                bet_amount=sess.seq[sess.tracker.current_unit_idx] * sess.chip_base,
                turn_number=sess.tracker.current_turn_number,
                set_index=sess.tracker.current_set_index,
                state=sess.to_state(),
            )
        unit_idx = sess.tracker.current_unit_idx
        unit = sess.seq[unit_idx]
        return DecideResponse(
            action="bet",
            side="player",
            unit_idx=unit_idx,
            unit_chips=unit,
            bet_amount=unit * sess.chip_base,
            turn_number=sess.tracker.current_turn_number,
            set_index=sess.tracker.current_set_index,
            state=sess.to_state(),
        )


@app.post("/api/sessions/{user_id}/result")
async def submit_result(
    user_id: str,
    req: ResultRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_required(user_id)
        try:
            completed, won = sess.add_result(req.result, req.side)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        completed_dict = None
        if completed:
            # Include resolved chip counts so clients never need the SEQ table.
            completed_dict = {
                "set_index": completed.set_index,
                "results": completed.results,
                "wins": completed.wins,
                "losses": completed.losses,
                "overshoot": completed.overshoot,
                "used_unit_idx": completed.used_unit_idx,
                "next_unit_idx": completed.next_unit_idx,
                "used_unit_chips": sess.seq[completed.used_unit_idx],
                "next_unit_chips": sess.seq[completed.next_unit_idx],
                "set_profit": completed.set_profit,
                "cumulative_profit": completed.cumulative_profit,
            }

        should, reason = sess.should_reset()
        sess.save()
        return ResultResponse(
            accepted=True,
            result=req.result,
            won=won,
            completed_set=completed_dict,
            should_reset=should,
            reset_reason=reason,
            state=sess.to_state(),
        )


@app.post("/api/sessions/{user_id}/reset")
async def reset_session_endpoint(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_required(user_id)
        should, reason = sess.should_reset()
        effective = sess.effective_profit()
        sess.reset_session(reason or "manual")
        sess.save()
        return {
            "reset": True,
            "reason": reason or "manual",
            "locked_profit": effective,
            "locked_money": effective * sess.chip_base,
            "state": sess.to_state(),
        }


@app.post("/api/sessions/{user_id}/shoe-change")
async def shoe_change(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_required(user_id)
        discarded = sess.handle_shoe_change()
        sess.save()
        return {
            "discarded_turns": discarded,
            "discarded_count": len(discarded),
            "state": sess.to_state(),
        }


@app.delete("/api/sessions/{user_id}")
async def delete_session(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, user_id)
    with _sessions_lock:
        sess = get_or_load(user_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"session '{user_id}' not found")
        sess.delete_state()
        _SESSIONS.pop(user_id, None)
        return {"deleted": True, "user_id": user_id}


# ======== Table Selector endpoints ========
#
# These endpoints move the sensitive table selection / scoring logic
# off the client entirely. The client only sends raw observations (table
# configs, player counts, histories) and receives a verdict.
# This way the scoring formula, thresholds and exclusion rules never
# appear in any shipped client binary.


@app.post(
    "/api/select-table",
    response_model=SelectTableResponse,
)
async def select_table_endpoint(
    req: SelectTableRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, req.user_id)
    import time as _time
    from table_selector import (
        is_excluded,
        has_banker_dragon,
        analyze_history,
        compute_score,
        TableCandidate,
        PLAYERS_PRIMARY,
        PLAYERS_RELAXED,
        RELAX_WAIT_SECONDS,
        MIN_HANDS,
        MAX_HANDS,
        DRAGON_LIMIT,
    )

    sc = req.selector_config or {}
    _players_primary = sc.get("players_primary", PLAYERS_PRIMARY)
    _players_relaxed = sc.get("players_relaxed", PLAYERS_RELAXED)
    _relax_wait = sc.get("relax_wait_sec", RELAX_WAIT_SECONDS)
    _min_hands = sc.get("min_hands", MIN_HANDS)
    _max_hands = sc.get("max_hands", MAX_HANDS)
    _dragon_limit = sc.get("dragon_limit", DRAGON_LIMIT)
    _require_pb = sc.get("require_pb", True)

    candidates: list = []
    debug_stats = {
        "excluded": 0,
        "no_players_data": 0,
        "low_players": 0,
        "dragon": 0,
        "bad_hands": 0,
        "bad_pb_ratio": 0,
    }

    excluded = set(req.excluded_ids or [])

    for tid, cfg in req.configs.items():
        if tid in excluded:
            continue
        reason = is_excluded(cfg)
        if reason:
            debug_stats["excluded"] += 1
            continue
        title = cfg.get("title", tid)
        if req.fixed_name and req.fixed_name.lower() not in title.lower():
            continue
        p_count = req.players.get(tid)
        if p_count is None:
            debug_stats["no_players_data"] += 1
            continue
        raw = req.histories.get(tid, []) or []
        hands, p, b, tie, last5 = analyze_history(raw)
        if not req.fixed_name:
            if has_banker_dragon(raw, limit=_dragon_limit):
                debug_stats["dragon"] += 1
                continue
            if hands < _min_hands or hands > _max_hands:
                debug_stats["bad_hands"] += 1
                continue
            if _require_pb and p <= b:
                debug_stats["bad_pb_ratio"] += 1
                continue
        candidates.append(
            TableCandidate(
                table_id=tid,
                title=title,
                players=p_count,
                hands=hands,
                p_count=p,
                b_count=b,
                tie_count=tie,
                last_5=last5,
            )
        )

    now = _time.time()
    primary_cands = [c for c in candidates if c.players >= _players_primary]
    relaxed_cands = [c for c in candidates if c.players >= _players_relaxed]

    logger.info(
        f"[select-table] user={req.user_id} configs={len(req.configs)} "
        f"candidates={len(candidates)} primary={len(primary_cands)} "
        f"relaxed={len(relaxed_cands)} debug={debug_stats}"
    )

    chosen_list: list = []
    wait_status: Optional[str] = None

    if primary_cands:
        chosen_list = primary_cands
        with _selector_wait_lock:
            _selector_wait_state.pop(req.user_id, None)
    else:
        with _selector_wait_lock:
            wait_start = _selector_wait_state.get(req.user_id)
            if wait_start is None:
                _selector_wait_state[req.user_id] = now
                return SelectTableResponse(
                    found=False, wait_status="waiting_primary", debug=debug_stats
                )
            if now - wait_start < _relax_wait:
                return SelectTableResponse(
                    found=False, wait_status="still_waiting", debug=debug_stats
                )
        if relaxed_cands:
            chosen_list = relaxed_cands
        else:
            return SelectTableResponse(
                found=False, wait_status="no_candidates", debug=debug_stats
            )

    for c in chosen_list:
        c.score = compute_score(c)
    chosen_list.sort(key=lambda x: -x.score)

    best = chosen_list[0]
    return SelectTableResponse(
        found=True,
        table_id=best.table_id,
        title=best.title,
        players=best.players,
        hands=best.hands,
        p_count=best.p_count,
        b_count=best.b_count,
        tie_count=best.tie_count,
        last_5=best.last_5,
        score=best.score,
        debug=debug_stats,
    )


@app.post(
    "/api/exit-check",
    response_model=ExitCheckResponse,
)
async def exit_check_endpoint(
    req: ExitCheckRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    from table_selector import has_banker_dragon, PLAYERS_RELAXED

    if req.players < PLAYERS_RELAXED:
        return ExitCheckResponse(exit_reason=f"players dropped to {req.players}")
    if has_banker_dragon(req.history):
        return ExitCheckResponse(exit_reason="banker dragon detected")
    return ExitCheckResponse(exit_reason=None)


# ======== Bot control endpoints ========


@app.post(
    "/api/bot/start",
    response_model=BotStartResponse,
)
async def bot_start(
    req: BotStartRequest,
    ctx: UserContext = Depends(verify_api_key),
):
    require_user_scope(ctx, req.user_id)
    """Spawn the bet runner subprocess with the given config.

    The runner will read config from LAPLACE_BOT_CONFIG env var (pointing
    to a JSON file written by the bot manager).
    """
    # Ensure a session exists for the user (create if missing)
    with _sessions_lock:
        sess = get_or_load(req.user_id)
        if sess is None:
            sess = LaplaceSession(
                user_id=req.user_id,
                chip_base=req.chip_base,
                profit_stop=req.profit_stop,
                loss_cut=req.loss_cut,
            )
            sess.save()
            _SESSIONS[req.user_id] = sess
            logger.info(f"bot_start: auto-created session {req.user_id}")
        else:
            # Sync live config
            sess.chip_base = req.chip_base
            sess.profit_stop = req.profit_stop
            sess.loss_cut = req.loss_cut
            sess.tracker.chip_base = req.chip_base
            if not req.resume_session:
                sess.delete_state()
                _SESSIONS.pop(req.user_id, None)
                sess = LaplaceSession(
                    user_id=req.user_id,
                    chip_base=req.chip_base,
                    profit_stop=req.profit_stop,
                    loss_cut=req.loss_cut,
                )
                _SESSIONS[req.user_id] = sess
            sess.save()

    mgr = get_bot_manager()
    try:
        info = mgr.start(req.dict())
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    logger.info(f"bot_start: run_id={info['run_id']} pid={info['pid']}")
    return BotStartResponse(started=True, **info)


@app.post(
    "/api/bot/stop",
    response_model=BotStopResponse,
)
async def bot_stop(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    mgr = get_bot_manager()
    result = mgr.stop()
    logger.info(f"bot_stop: {result}")
    return BotStopResponse(**result)


@app.get(
    "/api/bot/status",
    response_model=BotStatusResponse,
)
async def bot_status(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    mgr = get_bot_manager()
    st = mgr.status()

    session_state = None
    cfg = st.get("config") or {}
    user_id = cfg.get("user_id")
    if user_id:
        with _sessions_lock:
            sess = get_or_load(user_id)
            if sess:
                session_state = sess.to_state()

    return BotStatusResponse(
        running=st["running"],
        run_id=st["run_id"],
        pid=st["pid"],
        started_at=st["started_at"],
        uptime_seconds=st["uptime_seconds"],
        log_path=st["log_path"],
        config=st["config"],
        last_exit=st["last_exit"],
        session_state=session_state,
    )


@app.get("/api/bot/log")
async def bot_log(
    lines: int = 100,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    if lines < 1 or lines > 1000:
        raise HTTPException(status_code=400, detail="lines must be 1..1000")
    mgr = get_bot_manager()
    return {"lines": mgr.log_tail(lines)}


# ======== Sessions listing ========


@app.get("/api/sessions")
async def list_sessions(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    with _sessions_lock:
        # Load any persisted sessions not yet in memory
        for f in STATE_DIR.glob("*.json"):
            uid = f.stem
            if uid not in _SESSIONS:
                loaded = LaplaceSession.load(uid)
                if loaded:
                    _SESSIONS[uid] = loaded
        return {
            "sessions": [
                {
                    "user_id": sid,
                    "cumulative_profit": s.tracker.cumulative_profit,
                    "cumulative_money": s.tracker.cumulative_profit * s.chip_base,
                    "sets": len(s.tracker.sets),
                    "current_turn": len(s.tracker.current_turns),
                    "updated_at": s.updated_at,
                }
                for sid, s in _SESSIONS.items()
            ]
        }


# ======== Billing (loaded before Orders so confirm_order can use it) ========

from billing import BillingManager

_billing = BillingManager()


# ======== Orders (Purchase Flow) ========

ORDERS_FILE = Path(os.getenv("LAPLACE_ORDERS_FILE", str(STATE_DIR.parent / "orders.json")))
USDT_WALLET_TRC20 = os.getenv("LAPLACE_USDT_TRC20", "").strip()
USDT_WALLET_ERC20 = os.getenv("LAPLACE_USDT_ERC20", "").strip()

_orders_lock = threading.RLock()
_orders: dict[str, dict] = {}


def _load_orders() -> None:
    with _orders_lock:
        _orders.clear()
        if ORDERS_FILE.exists():
            try:
                data = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
                _orders.update(data.get("orders", {}))
            except Exception as e:
                logger.error(f"orders load error: {e}")


def _save_orders() -> None:
    with _orders_lock:
        ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ORDERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"orders": _orders}, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ORDERS_FILE)


_load_orders()


@app.get("/api/config")
async def public_config():
    """Public endpoint: returns wallet addresses for purchase page."""
    return {
        "wallets": {
            "trc20": USDT_WALLET_TRC20,
            "erc20": USDT_WALLET_ERC20,
        },
        "plans": {
            "starter": {"name": "Starter", "price": 1000},
            "pro": {"name": "Professional", "price": 3000},
        },
    }


@app.post("/api/orders")
async def create_order(request: Request):
    body = await request.json()
    plan = body.get("plan", "").strip()
    if plan not in ("starter", "pro"):
        raise HTTPException(400, "Invalid plan")
    amount = float(body.get("amount", 0))
    plan_prices = {"starter": 1000, "pro": 3000}
    if amount < plan_prices[plan]:
        raise HTTPException(400, f"Minimum charge for {plan}: ${plan_prices[plan]}")
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "name required")
    contact = body.get("contact", "").strip()

    order_id = "ORD-" + secrets.token_hex(6).upper()
    order = {
        "order_id": order_id,
        "plan": plan,
        "amount": amount,
        "name": name,
        "contact": contact,
        "status": "pending",  # pending -> sent -> confirmed
        "created_at": datetime.utcnow().isoformat() + "Z",
        "confirmed_at": None,
    }
    with _orders_lock:
        _orders[order_id] = order
    _save_orders()
    logger.info(f"[order] NEW {order_id}: {plan} ${amount} by {name}")
    return order


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str):
    with _orders_lock:
        order = _orders.get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order_id": order["order_id"], "status": order["status"],
            "plan": order["plan"], "amount": order["amount"],
            "contact": order.get("contact", ""), "name": order.get("name", "")}


@app.post("/api/orders/{order_id}/sent")
async def mark_order_sent(order_id: str):
    with _orders_lock:
        order = _orders.get(order_id)
        if not order:
            raise HTTPException(404, "Order not found")
        if order["status"] == "pending":
            order["status"] = "sent"
    _save_orders()
    logger.info(f"[order] SENT {order_id}")
    return {"ok": True}


@app.get("/api/admin/orders")
async def list_orders(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    with _orders_lock:
        return {"orders": sorted(_orders.values(), key=lambda o: o["created_at"], reverse=True)}


@app.post("/api/admin/orders/{order_id}/confirm")
async def confirm_order(
    order_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    with _orders_lock:
        order = _orders.get(order_id)
        if not order:
            raise HTTPException(404, "Order not found")
        if order["status"] == "confirmed":
            raise HTTPException(400, "Already confirmed")
        order["status"] = "confirmed"
        order["confirmed_at"] = datetime.utcnow().isoformat() + "Z"
    _save_orders()

    # Auto-register billing if not exists
    plan_prices = {"starter": 1000, "pro": 3000}
    user_id = order["name"].lower().replace(" ", "_")
    try:
        _billing.register(
            user_id=user_id,
            bot_price=float(plan_prices.get(order["plan"], 1000)),
            profit_share_rate=0.20,
        )
    except ValueError:
        pass  # already registered
    try:
        _billing.charge(user_id, order["amount"], note=f"Order {order_id}")
    except ValueError:
        pass

    logger.info(f"[order] CONFIRMED {order_id}: {user_id} charged ${order['amount']}")
    return {"ok": True, "user_id": user_id}


# ======== User MyPage API ========


# Server-side session tokens for mypage login
_user_sessions: dict[str, str] = {}  # token -> user_id
_user_sessions_lock = threading.Lock()


@app.post("/api/user/login")
async def user_login(request: Request):
    body = await request.json()
    user_id = body.get("user_id", "").strip()
    password = body.get("password", "").strip()
    if not user_id or not password:
        raise HTTPException(400, "user_id and password required")
    if not _billing.authenticate(user_id, password):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_hex(32)
    with _user_sessions_lock:
        _user_sessions[token] = user_id
    return {"ok": True, "token": token, "user_id": user_id}


def _verify_user_token(authorization: str = Header(default="")) -> str:
    """Verify user session token. Returns user_id."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization[7:]
    with _user_sessions_lock:
        user_id = _user_sessions.get(token)
    if not user_id:
        raise HTTPException(401, "Invalid or expired token")
    return user_id


@app.get("/api/user/me")
async def user_mypage(user_id: str = Depends(_verify_user_token)):
    summary = _billing.get_summary(user_id)
    if not summary:
        raise HTTPException(404, "User not found")
    # Add session stats if available
    with _sessions_lock:
        sess = _SESSIONS.get(user_id)
        if sess:
            total = sess.total_wins + sess.total_losses
            summary["session"] = {
                "total_bets": sess.total_bets,
                "total_wins": sess.total_wins,
                "total_losses": sess.total_losses,
                "total_ties": sess.total_ties,
                "win_rate": round(sess.total_wins / total * 100, 1) if total > 0 else 0,
                "cumulative_profit_chips": sess.tracker.cumulative_profit,
                "cumulative_profit_money": round(sess.tracker.cumulative_profit * sess.chip_base, 2),
                "sets": len(sess.tracker.sets),
                "updated_at": sess.updated_at,
            }
    return summary


# ======== Billing Endpoints ========


@app.post("/api/admin/billing/register")
async def billing_register(
    request: Request,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    body = await request.json()
    uid = body.get("user_id", "").strip()
    if not uid:
        raise HTTPException(400, "user_id required")
    try:
        ub = _billing.register(
            user_id=uid,
            bot_price=float(body.get("bot_price", 0)),
            profit_share_rate=float(body.get("profit_share_rate", 0.20)),
            is_free=bool(body.get("is_free", False)),
            password=body.get("password", ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ub.to_dict()


@app.patch("/api/admin/billing/{user_id}")
async def billing_update(
    user_id: str,
    request: Request,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    body = await request.json()
    try:
        ub = _billing.update_plan(
            user_id,
            bot_price=body.get("bot_price"),
            profit_share_rate=body.get("profit_share_rate"),
            is_free=body.get("is_free"),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ub.to_dict()


@app.post("/api/admin/billing/{user_id}/charge")
async def billing_charge(
    user_id: str,
    request: Request,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    body = await request.json()
    amount = float(body.get("amount", 0))
    if amount <= 0:
        raise HTTPException(400, "amount must be > 0")
    try:
        ub = _billing.charge(user_id, amount, note=body.get("note", ""))
    except ValueError as e:
        raise HTTPException(404, str(e))
    return ub.to_dict()


@app.post("/api/admin/billing/{user_id}/daily")
async def billing_daily_settle(
    user_id: str,
    request: Request,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    body = await request.json()
    daily_profit = float(body.get("daily_profit", 0))
    rec = _billing.process_daily_profit(user_id, daily_profit)
    return {"deduction": rec.__dict__ if rec else None, "billing": _billing.get_summary(user_id)}


@app.post("/api/admin/billing/{user_id}/unsuspend")
async def billing_unsuspend(
    user_id: str,
    ctx: UserContext = Depends(verify_api_key),
):
    require_admin(ctx)
    ok = _billing.unsuspend(user_id)
    if not ok:
        raise HTTPException(404, "user not found")
    return {"ok": True}


@app.get("/api/admin/billing")
async def billing_list(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    return {"users": [_billing.get_summary(ub.user_id) for ub in _billing.list_all()]}


@app.get("/api/admin/billing/{user_id}")
async def billing_detail(user_id: str, ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    s = _billing.get_summary(user_id)
    if not s:
        raise HTTPException(404, "user not found")
    return s


# ======== Admin Stats (for dashboard) ========


@app.get("/api/admin/stats")
async def admin_stats(ctx: UserContext = Depends(verify_api_key)):
    require_admin(ctx)
    with _sessions_lock:
        for f in STATE_DIR.glob("*.json"):
            uid = f.stem
            if uid not in _SESSIONS:
                loaded = LaplaceSession.load(uid)
                if loaded:
                    _SESSIONS[uid] = loaded

        users = []
        for sid, s in _SESSIONS.items():
            total = s.total_wins + s.total_losses
            win_rate = (s.total_wins / total * 100) if total > 0 else 0
            billing_info = _billing.get_summary(sid)
            # Find max consecutive losses across all sets
            max_loss_streak = 0
            for st in s.tracker.sets:
                streak = 0
                for r in st.results:
                    if r == "x":
                        streak += 1
                        max_loss_streak = max(max_loss_streak, streak)
                    else:
                        streak = 0
            key_info = None
            for k in _api_keys.list():
                if k.user_id == sid:
                    key_info = k.to_public_dict()
                    break
            users.append({
                "user_id": sid,
                "total_bets": s.total_bets,
                "total_wins": s.total_wins,
                "total_losses": s.total_losses,
                "total_ties": s.total_ties,
                "win_rate": round(win_rate, 1),
                "cumulative_profit_chips": s.tracker.cumulative_profit,
                "cumulative_profit_money": round(s.tracker.cumulative_profit * s.chip_base, 2),
                "chip_base": s.chip_base,
                "sets": len(s.tracker.sets),
                "current_turn": len(s.tracker.current_turns),
                "max_loss_streak": max_loss_streak,
                "updated_at": s.updated_at,
                "created_at": s.created_at,
                "billing": billing_info,
                "api_key": key_info,
            })
    return {"users": users, "timestamp": datetime.utcnow().isoformat() + "Z"}


# ======== Static File Serving (Landing / Purchase / Admin) ========

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

WWW_DIR = Path(os.getenv("LAPLACE_WWW_DIR", "/opt/laplace/www"))
if WWW_DIR.is_dir():
    app.mount("/css", StaticFiles(directory=str(WWW_DIR / "css")), name="css")
    app.mount("/js", StaticFiles(directory=str(WWW_DIR / "js")), name="js")
    app.mount("/img", StaticFiles(directory=str(WWW_DIR / "img")), name="img")

    @app.get("/")
    async def landing_page():
        return FileResponse(str(WWW_DIR / "index.html"))

    @app.get("/purchase.html")
    async def purchase_page():
        return FileResponse(str(WWW_DIR / "purchase.html"))

    @app.get("/admin.html")
    async def admin_page():
        return FileResponse(str(WWW_DIR / "admin.html"))

    @app.get("/mypage.html")
    async def mypage():
        return FileResponse(str(WWW_DIR / "mypage.html"))
else:
    logger.warning(f"WWW_DIR {WWW_DIR} not found, static pages disabled")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("LAPLACE_API_HOST", "0.0.0.0")
    port = int(os.getenv("LAPLACE_API_PORT", "8000"))
    if not _api_keys.empty:
        auth_desc = f"per_user_keys ({len(_api_keys.list())} registered)"
    elif API_KEY:
        auth_desc = "legacy_single_key"
    else:
        auth_desc = "DISABLED (dev mode)"
    logger.info(f"LAPLACE API starting on {host}:{port} (auth={auth_desc})")
    if ADMIN_KEY:
        logger.info("admin key configured (LAPLACE_ADMIN_KEY)")
    logger.info(f"keys file: {KEYS_FILE}")
    _billing.load()
    uvicorn.run(app, host=host, port=port)
