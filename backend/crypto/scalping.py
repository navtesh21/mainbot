"""Active scalp exit rules — stop-loss / take-profit / timeout / partial-exit.

Copied from backend/football/scalping.py (compute_stop/get_cooldown_seconds/
evaluate_exit are pure math, no football-specific content) and adapted for
BTC Up/Down windows: football's evaluate_exit() takes a `match_minute` and
switches to a shorter TIMEOUT_LATE when the match is past minute 85 ("late
game" compression). A BTC window has no such concept — instead the timeout
is chosen directly from the window's length (5m vs 15m), since a 5-minute
window needs a much tighter timeout than a 15-minute one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from backend.config import settings

MIN_MOVE = 0.03
MIN_REV = 0.015
BASE_SIZE = 3.0
MAX_SIZE = 5.0

# Asymmetric risk/reward: 2:1 reward/risk — same token price scale (0-1) as
# football's Polymarket tokens, so these are kept identical.
PROFIT_TARGET = 0.06    # 6c — binary convergence can produce 10-30c swings
STOP_LOSS = 0.04        # 4c — wider than 3c to reduce gap-through slippage (2s loop lags real moves)
PARTIAL_EXIT_LEVEL = 0.04  # 4c partial lock-in

HARD_STOP_PCT = 0.03
MAX_LOSS_USD = 0.50

COOLDOWN_WIN = 45
COOLDOWN_LOSS = 120
COOLDOWN_DEFAULT = 60


def compute_stop(entry_price: float, side: str, expected_rev: float) -> float:
    """Structural stop: if price moves against thesis this far, we're wrong.
    For SELL (expect reversion DOWN): stop is ABOVE entry.
    For BUY  (expect reversion UP):  stop is BELOW entry.
    """
    stop_dist = max(HARD_STOP_PCT * entry_price, expected_rev * 0.4)
    if side == "SELL":
        return entry_price + stop_dist
    return entry_price - stop_dist


def get_cooldown_seconds(last_result: Optional[str]) -> float:
    if last_result == "win":
        return COOLDOWN_WIN
    if last_result == "loss":
        return COOLDOWN_LOSS
    return COOLDOWN_DEFAULT


def _timeout_seconds(window_minutes: int) -> int:
    if window_minutes >= 15:
        return settings.CRYPTO_TIMEOUT_15M_SECONDS
    return settings.CRYPTO_TIMEOUT_5M_SECONDS


@dataclass
class ExitDecision:
    kind: str             # "partial", "profit", "stop", "timeout"
    exit_fraction: float  # 0.5 for partial, 1.0 otherwise
    reason: str


def evaluate_exit(position: dict, current_price: float) -> Optional[ExitDecision]:
    """position is one entry of engine._open_positions:
    {token_id, side, entry_price, size, stop_price, expected_reversion,
     opened_at, partial_exited, window_minutes, fair_value}.

    fair_value: model's predicted probability for the token we bought (e.g. 0.65
    means model says 65% chance BTC goes our direction). We hold until the market
    prices in that fair value rather than exiting at a fixed +6c regardless of
    how large the model's edge was. Falls back to fixed PROFIT_TARGET when
    fair_value is missing or below entry (e.g. unrestricted mode / tiny edge).
    """
    side = position["side"]
    entry_price = position["entry_price"]
    fair_value = position.get("fair_value")
    pnl_per_share = (entry_price - current_price) if side == "SELL" else (current_price - entry_price)
    elapsed = time.time() - position["opened_at"]
    timeout = _timeout_seconds(position.get("window_minutes", 5))

    # Profit target: use model fair value if it gives a meaningful edge above
    # entry; otherwise fall back to the fixed PROFIT_TARGET constant.
    if fair_value is not None and fair_value > entry_price + 0.01:
        profit_hit = current_price >= fair_value
    else:
        profit_hit = pnl_per_share >= PROFIT_TARGET

    if not position.get("partial_exited") and pnl_per_share >= PARTIAL_EXIT_LEVEL:
        return ExitDecision("partial", 0.5, f"partial_profit_{pnl_per_share:.3f}")
    if profit_hit:
        return ExitDecision("profit", 1.0, f"profit_{pnl_per_share:.3f}")
    if pnl_per_share <= -STOP_LOSS:
        return ExitDecision("stop", 1.0, f"stop_{pnl_per_share:.3f}")
    if elapsed >= timeout:
        # Always exit 100% at timeout — never hold half a position to binary settlement.
        # Binary settlement losses (-$0.73 to -$1.05) dwarf any upside from riding to expiry.
        return ExitDecision("timeout", 1.0, f"timeout_{elapsed:.0f}s")
    return None
