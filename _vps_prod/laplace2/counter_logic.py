from __future__ import annotations

import json
import os
import urllib.request
from typing import Iterable

# デフォルト値 (Supabase から取得できない場合のフォールバック)
ENTRY_WINDOW = 15
ENTRY_THRESHOLD = 0.85
EXIT_DROP3_LIMIT = 2
EXIT_DROP5_IMMEDIATE = True
FLAT_BET_AMOUNT = 1.0
SEARCH_INTERVAL = 30

_SITE_URL = os.environ.get("LAPLACE_SITE_URL", "https://bafather.uk")
_API_KEY = os.environ.get("LAPLACE_API_KEY", "")


def load_optimal_params() -> dict:
    """Supabase から最新の最適パラメータを取得。失敗時は空dictを返す。"""
    if not _API_KEY:
        return {}
    try:
        url = f"{_SITE_URL}/api/optimal-params?api_key={_API_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent": "LAPLACE/1.0"})
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data.get("params", {})
    except Exception:
        return {}


def apply_optimal_params():
    """Supabase のパラメータでモジュール定数を上書き。"""
    global ENTRY_WINDOW, ENTRY_THRESHOLD, EXIT_DROP3_LIMIT, EXIT_DROP5_IMMEDIATE
    params = load_optimal_params()
    if not params:
        return False
    if "entry_window" in params:
        ENTRY_WINDOW = int(params["entry_window"])
    if "entry_threshold" in params:
        ENTRY_THRESHOLD = float(params["entry_threshold"])
    if "exit_drop3_limit" in params:
        EXIT_DROP3_LIMIT = int(params["exit_drop3_limit"])
    if "exit_drop5_immediate" in params:
        EXIT_DROP5_IMMEDIATE = bool(params["exit_drop5_immediate"])
    return True


def compute_column_lengths(bead_road: Iterable[str]) -> list[int]:
    """bead road (P/B/T) から大路の列長リストを返す。Tie(T)は無視。"""
    columns: list[int] = []
    current_len = 0
    last_side: str | None = None
    for ch in bead_road:
        if ch == "T":
            continue
        if ch not in ("P", "B"):
            continue
        if ch == last_side:
            current_len += 1
        else:
            if last_side is not None:
                columns.append(current_len)
            current_len = 1
            last_side = ch
    if current_len > 0:
        columns.append(current_len)
    return columns


def short_rate(column_lengths: list[int], window: int = ENTRY_WINDOW) -> float:
    if len(column_lengths) < window:
        return 0.0
    recent = column_lengths[-window:]
    short_count = sum(1 for L in recent if L <= 2)
    return short_count / len(recent)


def is_tereko_state(column_lengths: list[int]) -> bool:
    if len(column_lengths) < ENTRY_WINDOW:
        return False
    return short_rate(column_lengths, ENTRY_WINDOW) >= ENTRY_THRESHOLD


def should_exit(columns_since_entry: list[int], current_column_length: int) -> str | None:
    check = list(columns_since_entry)
    if current_column_length >= 3:
        check.append(current_column_length)

    if EXIT_DROP5_IMMEDIATE:
        if any(L >= 5 for L in check) or current_column_length >= 5:
            return "streak-5"

    drop3_count = sum(1 for L in check if L >= 3)
    if drop3_count >= EXIT_DROP3_LIMIT:
        return f"streak-3x{drop3_count}"

    return None


def decide_counter_bet(last_non_tie: str | None) -> str | None:
    """逆張りBET側を返す (player/banker)。last_non_tieは 'P' or 'B'。"""
    if last_non_tie is None:
        return None
    if last_non_tie == "P":
        return "banker"
    if last_non_tie == "B":
        return "player"
    return None
