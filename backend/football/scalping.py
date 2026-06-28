"""Active scalp exit rules — stop-loss / take-profit / timeout / partial-exit.

Constants and compute_stop() ported verbatim from fball_bot/scalping.py.
Entry detection itself does NOT use ScalpingStrategy.evaluate() here — it
already happens earlier in session_manager.py's _handle_spike/_handle_slow_event,
which is structurally different from fball_bot's single-bot poll loop (per-session
PriceTrigger + live-data polling vs. one shared evaluate() call per cycle).

evaluate_exit() reproduces the exit-check half of ScalpingStrategy.evaluate()
(the `for s in self.get_active(fixture_id)` block), adapted to operate on
session_manager._PipelineState.open_trades dicts instead of ActiveScalp
objects, since this repo needs N independent concurrent positions (one per
session) rather than fball_bot's single shared _active registry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

MIN_MOVE = 0.03
MIN_REV = 0.015
BASE_SIZE = 3.0
MAX_SIZE = 5.0

# Asymmetric risk/reward: 2:1 reward/risk
PROFIT_TARGET = 0.04
STOP_LOSS = 0.02
PARTIAL_EXIT_LEVEL = 0.025

HARD_STOP_PCT = 0.03
MAX_LOSS_USD = 0.50

TIMEOUT_DEFAULT = 300
TIMEOUT_LATE = 60

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


@dataclass
class ExitDecision:
    kind: str             # "partial", "profit", "stop", "timeout"
    exit_fraction: float  # 0.5 for partial, 1.0 otherwise
    reason: str


def evaluate_exit(position: dict, current_price: float, match_minute: int) -> Optional[ExitDecision]:
    """position is one entry of session_manager._PipelineState.open_trades:
    {token_id, side, entry_price, size, stop_price, expected_reversion,
     opened_at, partial_exited}.
    """
    side = position["side"]
    entry_price = position["entry_price"]
    pnl_per_share = (entry_price - current_price) if side == "SELL" else (current_price - entry_price)
    elapsed = time.time() - position["opened_at"]
    timeout = TIMEOUT_LATE if match_minute >= 85 else TIMEOUT_DEFAULT

    if not position.get("partial_exited") and pnl_per_share >= PARTIAL_EXIT_LEVEL:
        return ExitDecision("partial", 0.5, f"partial_profit_{pnl_per_share:.3f}")
    if pnl_per_share >= PROFIT_TARGET:
        return ExitDecision("profit", 1.0, f"profit_{pnl_per_share:.3f}")
    if pnl_per_share <= -STOP_LOSS:
        return ExitDecision("stop", 1.0, f"stop_{pnl_per_share:.3f}")
    if elapsed >= timeout:
        return ExitDecision("timeout", 0.5 if position.get("partial_exited") else 1.0, f"timeout_{elapsed:.0f}s")
    return None
