"""LAPLACE Billing System

Per-user charge-based billing with:
- Bot license fee (one-time, deducted from initial charge)
- Daily 20% profit share (JST midnight calculation)
- Carry-forward losses (net P&L before taking 20%)
- Free-tier users (no fees, no charges)
- Grace period (24h warning when balance hits zero)

Data persisted to /opt/laplace/billing.json
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("laplace.billing")

JST = timezone(timedelta(hours=9))

BILLING_FILE = Path("/opt/laplace/billing.json")


@dataclass
class ChargeRecord:
    amount: float
    date: str  # ISO date
    note: str = ""


@dataclass
class DeductionRecord:
    amount: float
    date: str  # JST date (YYYY-MM-DD)
    daily_profit: float
    carry_loss: float
    note: str = ""


@dataclass
class UserBilling:
    user_id: str
    # Bot license
    bot_price: float = 0.0       # 0 = free
    bot_paid: bool = False
    # Charge balance
    balance: float = 0.0
    total_charged: float = 0.0
    # Profit sharing
    profit_share_rate: float = 0.20  # 20%
    carry_loss: float = 0.0      # accumulated losses to offset future profits
    # Free tier
    is_free: bool = False        # True = no billing at all
    # User login
    password: str = ""           # simple password for mypage login
    # Grace period
    grace_deadline: Optional[str] = None  # ISO datetime, set when balance <= 0
    suspended: bool = False      # True = API returns 403
    # History
    charges: list[ChargeRecord] = field(default_factory=list)
    deductions: list[DeductionRecord] = field(default_factory=list)
    # Metadata
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "bot_price": self.bot_price,
            "bot_paid": self.bot_paid,
            "balance": round(self.balance, 2),
            "total_charged": round(self.total_charged, 2),
            "profit_share_rate": self.profit_share_rate,
            "carry_loss": round(self.carry_loss, 2),
            "is_free": self.is_free,
            "password": self.password,
            "grace_deadline": self.grace_deadline,
            "suspended": self.suspended,
            "charges": [{"amount": c.amount, "date": c.date, "note": c.note} for c in self.charges],
            "deductions": [
                {"amount": d.amount, "date": d.date, "daily_profit": d.daily_profit,
                 "carry_loss": d.carry_loss, "note": d.note}
                for d in self.deductions
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserBilling":
        ub = cls(
            user_id=d["user_id"],
            bot_price=float(d.get("bot_price", 0)),
            bot_paid=bool(d.get("bot_paid", False)),
            balance=float(d.get("balance", 0)),
            total_charged=float(d.get("total_charged", 0)),
            profit_share_rate=float(d.get("profit_share_rate", 0.20)),
            carry_loss=float(d.get("carry_loss", 0)),
            is_free=bool(d.get("is_free", False)),
            password=d.get("password", ""),
            grace_deadline=d.get("grace_deadline"),
            suspended=bool(d.get("suspended", False)),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )
        for c in d.get("charges", []):
            ub.charges.append(ChargeRecord(
                amount=float(c["amount"]), date=c["date"], note=c.get("note", "")
            ))
        for dd in d.get("deductions", []):
            ub.deductions.append(DeductionRecord(
                amount=float(dd["amount"]), date=dd["date"],
                daily_profit=float(dd.get("daily_profit", 0)),
                carry_loss=float(dd.get("carry_loss", 0)),
                note=dd.get("note", ""),
            ))
        return ub


class BillingManager:
    """Thread-safe billing registry."""

    def __init__(self, path: Path = BILLING_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._users: dict[str, UserBilling] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._users.clear()
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for uid, ud in data.get("users", {}).items():
                    self._users[uid] = UserBilling.from_dict(ud)
            except Exception as e:
                logger.error(f"billing load error: {e}")

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {"users": {uid: ub.to_dict() for uid, ub in self._users.items()}}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def _now_iso(self) -> str:
        return datetime.now(tz=JST).isoformat()

    def _today_jst(self) -> str:
        return datetime.now(tz=JST).strftime("%Y-%m-%d")

    def get(self, user_id: str) -> Optional[UserBilling]:
        with self._lock:
            return self._users.get(user_id)

    def list_all(self) -> list[UserBilling]:
        with self._lock:
            return list(self._users.values())

    def register(
        self,
        user_id: str,
        bot_price: float = 0.0,
        profit_share_rate: float = 0.20,
        is_free: bool = False,
        password: str = "",
    ) -> UserBilling:
        with self._lock:
            if user_id in self._users:
                raise ValueError(f"User {user_id} already registered")
            ub = UserBilling(
                user_id=user_id,
                bot_price=bot_price,
                profit_share_rate=profit_share_rate,
                is_free=is_free,
                password=password,
                created_at=self._now_iso(),
                updated_at=self._now_iso(),
            )
            self._users[user_id] = ub
        self.save()
        return ub

    def update_plan(
        self,
        user_id: str,
        bot_price: Optional[float] = None,
        profit_share_rate: Optional[float] = None,
        is_free: Optional[bool] = None,
    ) -> UserBilling:
        with self._lock:
            ub = self._users.get(user_id)
            if not ub:
                raise ValueError(f"User {user_id} not found")
            if bot_price is not None:
                ub.bot_price = bot_price
            if profit_share_rate is not None:
                ub.profit_share_rate = profit_share_rate
            if is_free is not None:
                ub.is_free = is_free
                if is_free:
                    ub.suspended = False
                    ub.grace_deadline = None
            ub.updated_at = self._now_iso()
        self.save()
        return ub

    def charge(self, user_id: str, amount: float, note: str = "") -> UserBilling:
        """Add funds. On first charge, deduct bot license fee."""
        with self._lock:
            ub = self._users.get(user_id)
            if not ub:
                raise ValueError(f"User {user_id} not found")
            ub.total_charged += amount
            ub.balance += amount
            ub.charges.append(ChargeRecord(
                amount=amount, date=self._today_jst(), note=note,
            ))
            # First charge: deduct bot price
            if not ub.bot_paid and ub.bot_price > 0:
                ub.balance -= ub.bot_price
                ub.bot_paid = True
                ub.charges.append(ChargeRecord(
                    amount=-ub.bot_price,
                    date=self._today_jst(),
                    note=f"Bot license fee (${ub.bot_price:.0f})",
                ))
            # Clear suspension if balance is now positive
            if ub.balance > 0:
                ub.suspended = False
                ub.grace_deadline = None
            ub.updated_at = self._now_iso()
        self.save()
        return ub

    def process_daily_profit(self, user_id: str, daily_profit: float) -> Optional[DeductionRecord]:
        """Called at JST midnight with the day's net P&L.

        Returns the DeductionRecord if a fee was taken, None otherwise.

        Logic:
          1. If daily_profit < 0: add to carry_loss, no deduction.
          2. If daily_profit > 0: offset carry_loss first, then take
             profit_share_rate of the remainder.
        """
        with self._lock:
            ub = self._users.get(user_id)
            if not ub or ub.is_free:
                return None

            today = self._today_jst()
            if daily_profit <= 0:
                ub.carry_loss += abs(daily_profit)
                ub.deductions.append(DeductionRecord(
                    amount=0, date=today, daily_profit=daily_profit,
                    carry_loss=ub.carry_loss, note="Loss day, carry forward",
                ))
                ub.updated_at = self._now_iso()
                self.save()
                return ub.deductions[-1]

            # Offset carry_loss
            net = daily_profit - ub.carry_loss
            if net <= 0:
                ub.carry_loss = abs(net)
                ub.deductions.append(DeductionRecord(
                    amount=0, date=today, daily_profit=daily_profit,
                    carry_loss=ub.carry_loss,
                    note=f"Profit ${daily_profit:.2f} absorbed by carry loss",
                ))
                ub.updated_at = self._now_iso()
                self.save()
                return ub.deductions[-1]

            ub.carry_loss = 0
            fee = round(net * ub.profit_share_rate, 2)
            ub.balance -= fee

            rec = DeductionRecord(
                amount=fee, date=today, daily_profit=daily_profit,
                carry_loss=0,
                note=f"{ub.profit_share_rate*100:.0f}% of net ${net:.2f}",
            )
            ub.deductions.append(rec)

            # Grace period check
            if ub.balance <= 0 and not ub.grace_deadline:
                deadline = datetime.now(tz=JST) + timedelta(hours=24)
                ub.grace_deadline = deadline.isoformat()
                logger.warning(f"[billing] {user_id} balance <= 0 (${ub.balance:.2f}), grace until {ub.grace_deadline}")

            ub.updated_at = self._now_iso()
        self.save()
        return rec

    def check_grace(self, user_id: str) -> bool:
        """Check and enforce grace period. Returns True if user is allowed."""
        with self._lock:
            ub = self._users.get(user_id)
            if not ub:
                return True  # not registered in billing = no billing enforcement
            if ub.is_free:
                return True
            if ub.suspended:
                return False
            if ub.balance > 0:
                return True
            if not ub.grace_deadline:
                return True
            deadline = datetime.fromisoformat(ub.grace_deadline)
            if datetime.now(tz=JST) > deadline:
                ub.suspended = True
                ub.updated_at = self._now_iso()
                self.save()
                logger.warning(f"[billing] {user_id} SUSPENDED (grace expired, balance=${ub.balance:.2f})")
                return False
            return True  # within grace period

    def unsuspend(self, user_id: str) -> bool:
        with self._lock:
            ub = self._users.get(user_id)
            if not ub:
                return False
            ub.suspended = False
            ub.grace_deadline = None
            ub.updated_at = self._now_iso()
        self.save()
        return True

    def authenticate(self, user_id: str, password: str) -> bool:
        with self._lock:
            ub = self._users.get(user_id)
            if not ub or not ub.password:
                return False
            return ub.password == password

    def get_summary(self, user_id: str) -> Optional[dict]:
        ub = self.get(user_id)
        if not ub:
            return None
        total_deducted = sum(d.amount for d in ub.deductions)
        summary = {
            **ub.to_dict(),
            "total_deducted": round(total_deducted, 2),
            "unpaid_balance": round(ub.balance, 2),
            "status": "free" if ub.is_free else ("suspended" if ub.suspended else ("grace" if ub.grace_deadline else "active")),
        }
        summary.pop("password", None)  # never expose password
        return summary
