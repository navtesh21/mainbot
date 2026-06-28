"""Signal classifier — consumes L2 orderbook data and price trajectories
to produce typed event predictions with confidence scores.

Ported verbatim from fball_bot/signals.py.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

TRAJECTORY_STEP_JUMP = "step_jump"
TRAJECTORY_SLOW_DRIFT = "slow_drift"
TRAJECTORY_SPIKE_RETRACE = "spike_retrace"
TRAJECTORY_VOLATILITY_BURST = "volatility_burst"
TRAJECTORY_NOISE = "noise"

EVENT_GOAL_HOME = "goal_home"
EVENT_GOAL_AWAY = "goal_away"
EVENT_RED_CARD = "red_card"
EVENT_VAR_CHECK = "var_check"
EVENT_NEAR_MISS = "near_miss"
EVENT_PENALTY = "penalty"
EVENT_UNCERTAIN = "uncertain"


@dataclass
class OrderbookLevel:
    price: float
    size: float


@dataclass
class L2Snapshot:
    token_id: str
    timestamp: float
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    mid_price: float = 0.0
    spread: float = 0.0
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    imbalance: float = 1.0
    cancel_rate: float = 0.0


@dataclass
class ParityInfo:
    yes_price: float = 0.0
    no_price: float = 0.0
    sum_prices: float = 0.0
    deviation: float = 0.0
    below_parity: bool = False
    tight_parity: bool = False


@dataclass
class ClassifiedSignal:
    event_type: str = EVENT_UNCERTAIN
    confidence: float = 0.0
    trajectory_type: str = TRAJECTORY_NOISE
    direction: str = "up"
    expected_reversion: float = 0.0
    reasons: list = field(default_factory=list)


class SignalClassifier:
    """Consumes orderbook + price history data to produce typed event predictions."""

    STEP_JUMP_MIN_PCT = 5.0
    STEP_JUMP_MAX_DURATION = 5.0
    SPIKE_RETRACE_MIN_PCT = 3.0
    SPIKE_RETRACE_RATIO = 0.4
    SLOW_DRIFT_MIN_PCT = 2.0
    SLOW_DRIFT_MIN_DURATION = 10.0
    VOLATILITY_BURST_SPREAD_MULT = 2.0

    CONFIDENCE_HIGH = 0.85
    CONFIDENCE_MEDIUM = 0.60
    CONFIDENCE_LOW = 0.35

    def __init__(self) -> None:
        self._baseline_spreads: dict[str, float] = {}

    def analyze(
        self,
        *,
        trajectory: str,
        imbalance: float,
        spread: float,
        cancel_rate: float,
        parity: Optional[ParityInfo] = None,
        price_change_pct: float,
        direction: str,
        token_id: str = "",
    ) -> ClassifiedSignal:
        reasons: list[str] = []
        confidence = self.CONFIDENCE_LOW
        event_type = EVENT_UNCERTAIN
        expected_reversion = 0.0
        trajectory_type = trajectory

        if trajectory == TRAJECTORY_STEP_JUMP:
            confidence = self.CONFIDENCE_HIGH
            side = "home" if direction == "up" else "away"
            event_type = EVENT_GOAL_HOME if direction == "up" else EVENT_GOAL_AWAY
            expected_reversion = 0.04
            reasons.append(f"step_jump_{price_change_pct:.0f}pct_{side}")

        elif trajectory == TRAJECTORY_SLOW_DRIFT:
            confidence = self.CONFIDENCE_MEDIUM
            event_type = EVENT_RED_CARD
            expected_reversion = 0.03
            side = "home" if direction == "up" else "away"
            reasons.append(f"slow_drift_{price_change_pct:.0f}pct_{side}")

        elif trajectory == TRAJECTORY_SPIKE_RETRACE:
            confidence = self.CONFIDENCE_MEDIUM
            event_type = EVENT_VAR_CHECK
            expected_reversion = 0.02
            reasons.append(f"spike_retrace_{price_change_pct:.0f}pct")

        elif trajectory == TRAJECTORY_VOLATILITY_BURST:
            confidence = self.CONFIDENCE_LOW
            event_type = EVENT_VAR_CHECK
            expected_reversion = 0.015
            reasons.append("volatility_burst")

        else:
            return ClassifiedSignal(
                event_type=EVENT_UNCERTAIN,
                confidence=0.0,
                trajectory_type=trajectory,
                direction=direction,
                reasons=["noise"],
            )

        if imbalance > 2.0 and direction == "up":
            confidence = min(1.0, confidence * 1.15)
            reasons.append(f"buy_pressure_{imbalance:.1f}x")
        elif imbalance < 0.5 and direction == "down":
            confidence = min(1.0, confidence * 1.15)
            reasons.append(f"sell_pressure_{imbalance:.1f}x")

        if cancel_rate > 0.3:
            confidence = min(1.0, confidence * 1.1)
            reasons.append(f"cancel_rate_{cancel_rate:.0%}")

        if parity is not None and parity.yes_price > 0:
            if parity.below_parity:
                if event_type in (EVENT_GOAL_HOME, EVENT_GOAL_AWAY):
                    confidence *= 0.7
                    event_type = EVENT_RED_CARD
                    reasons.append("below_parity_uncertainty")
                else:
                    confidence *= 1.1
                    reasons.append("below_parity_confirms")
            elif parity.tight_parity and abs(parity.deviation) < 0.05:
                if event_type in (EVENT_GOAL_HOME, EVENT_GOAL_AWAY):
                    confidence = min(1.0, confidence * 1.2)
                    reasons.append(f"tight_parity_{parity.deviation:.3f}")
            elif abs(parity.deviation) > 0.10:
                confidence *= 0.85
                reasons.append(f"wide_parity_{parity.deviation:.3f}")

        return ClassifiedSignal(
            event_type=event_type,
            confidence=round(confidence, 3),
            trajectory_type=trajectory_type,
            direction=direction,
            expected_reversion=round(expected_reversion, 3),
            reasons=reasons,
        )

    def classify_trajectory(self, price_history: deque, current_mid: float, spread: float) -> str:
        if len(price_history) < 5:
            return TRAJECTORY_NOISE

        now = time.time()
        recent = [(ts, p) for ts, p in price_history if now - ts <= 30]
        if len(recent) < 3:
            return TRAJECTORY_NOISE

        prices = [p for _, p in recent]
        timestamps = [ts for ts, _ in recent]
        first_price = prices[0]
        last_price = prices[-1]

        if first_price <= 0:
            return TRAJECTORY_NOISE

        total_change_pct = ((last_price - first_price) / first_price) * 100
        total_duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0

        retrace_pct = self._detect_retrace(prices)
        if retrace_pct >= self.SPIKE_RETRACE_RATIO:
            return TRAJECTORY_SPIKE_RETRACE

        max_jump = self._max_window_jump(prices, timestamps, self.STEP_JUMP_MAX_DURATION)
        if max_jump >= self.STEP_JUMP_MIN_PCT:
            return TRAJECTORY_STEP_JUMP

        if self._detect_spread_burst(spread, current_mid, len(recent)):
            return TRAJECTORY_VOLATILITY_BURST

        if total_duration >= self.SLOW_DRIFT_MIN_DURATION and \
           abs(total_change_pct) >= self.SLOW_DRIFT_MIN_PCT:
            return TRAJECTORY_SLOW_DRIFT

        return TRAJECTORY_NOISE

    def _detect_retrace(self, prices: list) -> float:
        if len(prices) < 3:
            return 0.0

        start = prices[0]
        end = prices[-1]
        peak = max(prices)
        trough = min(prices)

        up_from_start = peak - start
        down_from_start = start - trough

        if up_from_start >= down_from_start:
            if up_from_start <= 0:
                return 0.0
            retrace = peak - end
            if retrace <= 0:
                return 0.0
            return min(1.0, retrace / up_from_start)
        else:
            if down_from_start <= 0:
                return 0.0
            bounce = end - trough
            if bounce <= 0:
                return 0.0
            return min(1.0, bounce / down_from_start)

    def _max_window_jump(self, prices: list, timestamps: list, window: float) -> float:
        best = 0.0
        n = len(prices)
        for i in range(n):
            for j in range(i + 1, n):
                if timestamps[j] - timestamps[i] > window:
                    break
                if prices[i] > 0:
                    change = abs((prices[j] - prices[i]) / prices[i]) * 100
                    if change > best:
                        best = change
        return best

    def _detect_spread_burst(self, spread: float, mid: float, sample_count: int) -> bool:
        if mid <= 0 or spread <= 0:
            return False
        baseline = mid * 0.005
        return spread >= baseline * self.VOLATILITY_BURST_SPREAD_MULT

    def compute_micro_structure(self, book: dict) -> dict:
        bids_raw = book.get("bids", [])
        asks_raw = book.get("asks", [])

        def _parse(raw: list) -> list:
            levels = []
            for r in raw:
                if isinstance(r, dict):
                    p = float(r.get("price", 0))
                    s = float(r.get("size", 0))
                elif isinstance(r, (list, tuple)) and len(r) >= 2:
                    p = float(r[0])
                    s = float(r[1])
                else:
                    continue
                if p > 0:
                    levels.append(OrderbookLevel(price=p, size=s))
            return levels

        bids = _parse(bids_raw)
        asks = _parse(asks_raw)

        if not bids or not asks:
            return {
                "imbalance": 1.0, "spread": 0.0,
                "bid_vol": 0.0, "ask_vol": 0.0,
                "cancel_rate": 0.0, "bid_density": 0.0, "ask_density": 0.0,
            }

        best_bid = bids[0].price
        best_ask = asks[0].price
        mid = (best_bid + best_ask) / 2
        spread = (best_ask - best_bid) / mid if mid > 0 else 0

        bid_vol = sum(b.price * b.size for b in bids[:10])
        ask_vol = sum(a.price * a.size for a in asks[:10])
        imbalance = bid_vol / max(ask_vol, 0.01)

        bid_near = sum(b.size for b in bids if b.price >= mid * 0.98)
        ask_near = sum(a.size for a in asks if a.price <= mid * 1.02)
        total_bid = sum(b.size for b in bids[:10])
        total_ask = sum(a.size for a in asks[:10])
        bid_density = bid_near / max(total_bid, 0.01)
        ask_density = ask_near / max(total_ask, 0.01)

        cancel_rate = min(1.0, max(0.0, 1.0 - (bid_density + ask_density) / 2))

        return {
            "imbalance": round(imbalance, 3),
            "spread": round(spread, 5),
            "bid_vol": round(bid_vol, 2),
            "ask_vol": round(ask_vol, 2),
            "cancel_rate": round(cancel_rate, 3),
            "bid_density": round(bid_density, 3),
            "ask_density": round(ask_density, 3),
        }

    def analyze_parity(self, yes_price: float, no_price: float) -> ParityInfo:
        sum_prices = yes_price + no_price
        deviation = abs(1.0 - sum_prices)

        below_parity = False
        tight_parity = False

        if 0.80 <= sum_prices <= 1.20:
            if deviation < 0.05:
                tight_parity = True
            elif sum_prices < 0.95:
                below_parity = True

        return ParityInfo(
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            sum_prices=round(sum_prices, 4),
            deviation=round(deviation, 4),
            below_parity=below_parity,
            tight_parity=tight_parity,
        )


def parse_l2_snapshot(token_id: str, raw: dict, existing: Optional[L2Snapshot] = None) -> L2Snapshot:
    bids_raw = raw.get("bids", [])
    asks_raw = raw.get("asks", [])

    def _parse_levels(raw_levels: list) -> list:
        levels = []
        for r in raw_levels:
            if isinstance(r, dict):
                p = float(r.get("price", 0))
                s = float(r.get("size", 0))
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                p = float(r[0])
                s = float(r[1])
            else:
                continue
            if p > 0:
                levels.append(OrderbookLevel(price=p, size=s))
        return levels

    bids = _parse_levels(bids_raw)
    asks = _parse_levels(asks_raw)

    best_bid = bids[0].price if bids else 0.0
    best_ask = asks[0].price if asks else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
    spread = (best_ask - best_bid) / mid if mid > 0 else 0.0

    bid_vol = sum(b.price * b.size for b in bids[:10])
    ask_vol = sum(a.price * a.size for a in asks[:10])
    imbalance = bid_vol / max(ask_vol, 0.01)

    return L2Snapshot(
        token_id=token_id,
        timestamp=time.time(),
        bids=bids,
        asks=asks,
        mid_price=round(mid, 4),
        spread=round(spread, 5),
        bid_volume=round(bid_vol, 2),
        ask_volume=round(ask_vol, 2),
        imbalance=round(imbalance, 3),
    )
