"""PortfolioRisk — adaptive, self-calibrating risk management for crypto scalping.

Copied from backend/football/risk.py (pure math/state-machine, no
football-specific content) so crypto gets its own state file
(backend/crypto/_data/portfolio_risk.json) and its own module-level
singletons — fully isolated from football's risk ledger.

One deliberate divergence from the football copy: PositionRegistry there
buckets exposure per `fixture_id` (an integer football match ID) so no
single match can hog the whole exposure cap. A BTC window has no fixture
analog — windows roll over every 5-15 minutes rather than running for a
~90+ minute match — so that per-fixture bucketing dimension is dropped here.
MAX_CONCURRENT + the overall total_exposure() cap already bound risk without
it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("trading_bot")

DATA_DIR = Path(__file__).resolve().parent / "_data"
STATE_FILE = DATA_DIR / "portfolio_risk.json"

MIN_SHARES = 5  # Polymarket CLOB minimum


@dataclass
class PortfolioState:
    initial_capital: float = 10.0
    current_capital: float = 10.0
    peak_capital: float = 10.0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    monthly_pnl: float = 0.0

    wins: int = 0
    losses: int = 0
    total_trades: int = 0
    sum_win_pct: float = 0.0
    sum_loss_pct: float = 0.0

    peak_timestamp: float = field(default_factory=time.time)
    last_drawdown_check: float = field(default_factory=time.time)
    drawdown_at_last_check: float = 0.0
    drawdown_velocity_mult: float = 1.0

    consecutive_losses: int = 0
    consecutive_wins: int = 0
    max_consecutive_losses_seen: int = 0

    is_paused: bool = False
    pause_until: float = 0.0
    pause_reason: str = ""
    permanently_halted: bool = False
    in_recovery: bool = False
    recovery_target: float = 0.0
    recovery_entry_capital: float = 0.0

    last_daily_reset: float = field(default_factory=time.time)
    month_start_time: float = field(default_factory=time.time)
    active_matches_last_cycle: int = 1


@dataclass
class RRVerdict:
    """Result of an R:R guardrail evaluation (two-gate architecture)."""
    action: str          # "pass", "block", "downsize"
    multiplier: float
    ev_pct: float
    rr_ratio: float
    p_adj: float
    reason: str


class RRGuard:
    """Two-gate R:R guardrail.

    Gate 1 — Cost Gate (model-agnostic):
        net_edge_pct = expected_gain_pct - round_trip_cost_pct
        Blocks if edge is consumed by spread + slippage.

    Gate 2 — Risk Gate (probability-weighted):
        EV = (p_adj x reward) - ((1-p_adj) x risk) - cost
        Blocks if EV <= 0 or R:R < 1.5. Downsize if R:R < 2.5.

    Model confidence is discounted by calibration_factor (default 0.55) to
    account for uncalibrated base impacts.
    """

    CF = 0.55
    P_CEIL = 0.85
    MIN_EDGE = 0.005
    RR_BLOCK = 1.5
    RR_PASS = 2.5

    def evaluate(
        self,
        entry_price: float,
        target_price: float,
        stop_price: float,
        side: str,
        best_bid: float,
        best_ask: float,
        bid_vol: float,
        ask_vol: float,
        trade_size: float,
        model_confidence: float = 1.0,
    ) -> RRVerdict:
        eps = 1e-9
        if entry_price <= 0 or best_bid <= 0 or best_ask <= best_bid:
            return RRVerdict("block", 0.0, 0.0, 0.0, 0.0,
                              f"bad book: bid={best_bid:.4f} ask={best_ask:.4f}")

        mid = max((best_bid + best_ask) / 2.0, eps)

        half_spread_pct = (best_ask - best_bid) / (2.0 * mid)
        relevant_depth = ask_vol if side.upper() == "BUY" else bid_vol
        fill_ratio = min(1.0, trade_size / max(relevant_depth, 1.0))
        impact_pct = half_spread_pct * (fill_ratio + fill_ratio ** 2)
        round_trip_cost_pct = 2.0 * (half_spread_pct + impact_pct)

        gain_pct = abs(target_price - entry_price) / mid
        risk_pct = abs(entry_price - stop_price) / mid
        rr_ratio = gain_pct / max(risk_pct, eps)

        net_edge = gain_pct - round_trip_cost_pct
        if net_edge <= self.MIN_EDGE:
            return RRVerdict("block", 0.0, net_edge - round_trip_cost_pct, rr_ratio, 0.0,
                              f"cost: net {net_edge*100:.2f}% <= {self.MIN_EDGE*100:.1f}% "
                              f"(gain={gain_pct*100:.1f}% cost={round_trip_cost_pct*100:.1f}%)")

        p_adj = self.CF * min(model_confidence * 1.5, 1.0)
        p_adj = min(p_adj, self.P_CEIL)
        ev_pct = (p_adj * gain_pct) - ((1.0 - p_adj) * risk_pct) - round_trip_cost_pct

        if ev_pct <= 0:
            return RRVerdict("block", 0.0, ev_pct, rr_ratio, round(p_adj, 3),
                              f"EV {ev_pct*100:.2f}% <= 0 (p_adj={p_adj:.2f} "
                              f"gain={gain_pct*100:.1f}% risk={risk_pct*100:.1f}% cost={round_trip_cost_pct*100:.1f}%)")

        if rr_ratio < self.RR_BLOCK:
            return RRVerdict("block", 0.0, ev_pct, rr_ratio, round(p_adj, 3),
                              f"RR {rr_ratio:.2f} < {self.RR_BLOCK}")

        if rr_ratio < self.RR_PASS:
            mult = 0.4 + 0.6 * (rr_ratio - self.RR_BLOCK) / (self.RR_PASS - self.RR_BLOCK)
            return RRVerdict("downsize", round(mult, 2), ev_pct, rr_ratio, round(p_adj, 3),
                              f"RR {rr_ratio:.2f} marginal x{mult:.2f}")

        return RRVerdict("pass", 1.0, ev_pct, rr_ratio, round(p_adj, 3),
                          f"RR {rr_ratio:.2f} healthy "
                          f"(gain={gain_pct*100:.1f}% risk={risk_pct*100:.1f}% cost={round_trip_cost_pct*100:.1f}%)")


def kelly_size(p_adj: float, reward_pct: float, risk_pct: float, capital: float,
               kelly_fraction: float = 0.25, max_position_pct: float = 0.10) -> float:
    """Quarter-Kelly sizing. Returns USD amount, capped at max_position_pct of capital.
    Formula: f* = (p x b - q) / b  where b = reward/risk.
    """
    b = reward_pct / max(risk_pct, 1e-9)
    if b <= 1e-9:
        return 0.0
    q = 1.0 - p_adj
    f_full = (p_adj * b - q) / b
    if f_full <= 0.0:
        return 0.0
    f_safe = f_full * kelly_fraction
    f_capped = min(f_safe, max_position_pct)
    return max(0.0, capital * f_capped)


def trade_priority_score(ev_pct: float, p_adj: float, expected_hold_s: float = 60.0) -> float:
    """EV per dollar per second - higher = better capital velocity."""
    turnover_speed = 1.0 / max(expected_hold_s, 1.0)
    return ev_pct * p_adj * turnover_speed


class PositionRegistry:
    """Tracks open positions to prevent long+short on same token, caps concurrency/exposure.

    No per-window exposure bucketing (see module docstring) — total_exposure()
    + MAX_CONCURRENT are the only caps.
    """
    MAX_CONCURRENT = 5
    MAX_PORTFOLIO_EXPOSURE_PCT = 0.15

    def __init__(self) -> None:
        self._positions: dict[str, list[dict]] = {}

    @staticmethod
    def _exposure_cap(capital: float) -> float:
        if capital <= 10.0:
            return 0.50
        if capital <= 25.0:
            return 0.30
        return 0.15

    def can_open(self, token_id: str, side: str, size_usd: float, capital: float) -> tuple[bool, str]:
        if self.open_count >= self.MAX_CONCURRENT:
            return False, f"max {self.MAX_CONCURRENT} concurrent trades"

        cap_pct = self._exposure_cap(capital)
        cap_dollars = capital * cap_pct if capital > 0 else 0.0
        total_new = self.total_exposure() + size_usd
        if total_new > cap_dollars:
            return False, f"exposure {total_new:.2f} > {cap_dollars:.2f} ({cap_pct*100:.0f}% cap)"

        existing = self._positions.get(token_id, [])
        for pos in existing:
            if pos["side"] != side:
                return False, f"opposite side already open on {token_id[:12]}"
            return False, f"already open on {token_id[:12]}"
        return True, ""

    def open(self, token_id: str, side: str, size_usd: float) -> None:
        if token_id not in self._positions:
            self._positions[token_id] = []
        self._positions[token_id].append({"side": side, "size": size_usd})

    def close(self, token_id: str, size_usd: float) -> None:
        pos_list = self._positions.get(token_id, [])
        self._positions[token_id] = [p for p in pos_list if abs(p["size"] - size_usd) >= 0.01]

    def total_exposure(self) -> float:
        return sum(p["size"] for pos_list in self._positions.values() for p in pos_list)

    @property
    def open_count(self) -> int:
        return sum(len(pos_list) for pos_list in self._positions.values())


class PortfolioRisk:
    """Self-calibrating risk manager - 5 layers + Kelly sizing."""

    def __init__(self, **kwargs: Any) -> None:
        self._cfg = {
            "max_per_trade_usd": float(kwargs.get("max_per_trade_usd", 3.0)),
            "daily_max_loss_pct": float(kwargs.get("daily_max_loss_pct", 0.05)),
            "monthly_max_loss_pct": float(kwargs.get("monthly_max_loss_pct", 0.15)),
            "max_drawdown_pct": float(kwargs.get("max_drawdown_pct", 0.25)),
            "total_max_loss_pct": float(kwargs.get("total_max_loss_pct", 0.40)),
            "max_consecutive_losses": int(kwargs.get("max_consecutive_losses", 6)),
            "pause_minutes": int(kwargs.get("pause_minutes", 15)),
            "initial_capital": float(kwargs.get("initial_capital", 10.0)),
        }
        self.s = self._load_state()

    def can_trade(self, active_match_count: int = 1) -> tuple[bool, str]:
        self._check_resets()
        s = self.s
        s.active_matches_last_cycle = max(active_match_count, 1)

        if s.permanently_halted:
            return False, "PERMANENT HALT - total loss limit reached"

        total_limit = self._cfg["initial_capital"] * self._cfg["total_max_loss_pct"]
        if s.total_pnl <= -total_limit:
            s.permanently_halted = True
            self._save_state()
            logger.error(f"PERMANENT HALT - total loss ${s.total_pnl:.2f}")
            return False, f"Total loss ${s.total_pnl:.2f} - halted"

        if s.is_paused and time.time() < s.pause_until:
            rem = int(s.pause_until - time.time()) // 60
            return False, f"{s.pause_reason} - {rem}m remaining"
        s.is_paused = False

        if self._effective_drawdown() >= self._cfg["max_drawdown_pct"]:
            self._pause(f"Drawdown {self._effective_drawdown():.1f}%", 7 * 24 * 60)
            return False, "drawdown limit"

        monthly = self._cfg["initial_capital"] * self._cfg["monthly_max_loss_pct"]
        if s.monthly_pnl <= -monthly:
            self._pause("Monthly loss limit", 30 * 24 * 60)
            return False, "monthly loss limit"

        daily = self._cfg["initial_capital"] * self._cfg["daily_max_loss_pct"]
        if s.daily_pnl <= -daily:
            self._pause(f"Daily loss ${s.daily_pnl:.2f}", self._cfg["pause_minutes"])
            return False, "daily loss limit"

        if s.consecutive_losses >= self._cfg["max_consecutive_losses"]:
            self._pause(f"{s.consecutive_losses} consecutive losses", self._cfg["pause_minutes"])
            return False, "consecutive losses"

        if s.in_recovery and s.total_pnl >= s.recovery_target:
            s.in_recovery = False
            self._save_state()
            logger.info("Recovery complete")

        return True, ""

    def compute_max_trade_size(
        self,
        base_size: float = 2.0,
        conviction: float = 0.5,
        token_price: float = 0.50,
    ) -> float:
        s = self.s
        hard_cap = self._cfg["max_per_trade_usd"]

        kelly_pct = self._kelly_fraction()
        kelly_dollars = s.current_capital * kelly_pct
        size = min(base_size, kelly_dollars) * (0.5 + conviction * 0.5)

        daily = self._cfg["initial_capital"] * self._cfg["daily_max_loss_pct"]
        daily_rem = max(0.1, (daily + s.daily_pnl) / daily) if daily > 0 else 1.0
        size *= daily_rem

        dd_mult = max(0.2, 1.0 - (self._effective_drawdown() / self._cfg["max_drawdown_pct"]))
        size *= dd_mult

        if s.in_recovery:
            size *= 0.4
        match_count = max(s.active_matches_last_cycle, 1)
        if match_count <= 2:
            size *= 1.0
        else:
            size *= max(0.5, 1.0 - (match_count - 2) * 0.15)
        if s.consecutive_losses > 0:
            size *= max(0.3, 1.0 - s.consecutive_losses * 0.25)

        min_dollar = max(1.0, MIN_SHARES * token_price)
        size = max(min_dollar, min(size, hard_cap))

        logger.debug(
            f"Size: kelly=${kelly_dollars:.2f} conv={conviction:.2f} dd={dd_mult:.2f} -> "
            f"${size:.2f} (min=${min_dollar:.2f} cap=${hard_cap:.2f})"
        )
        return round(size, 2)

    def record_trade(self, pnl: float, entry_price: float) -> dict[str, Any]:
        s = self.s
        s.total_trades += 1
        s.daily_pnl += pnl
        s.monthly_pnl += pnl
        s.total_pnl += pnl
        s.current_capital = s.initial_capital + s.total_pnl

        if s.current_capital > s.peak_capital:
            s.peak_capital = s.current_capital
            s.peak_timestamp = time.time()
            s.drawdown_velocity_mult = 1.0

        if pnl >= 0:
            s.wins += 1
            s.consecutive_wins += 1
            s.consecutive_losses = 0
            s.sum_win_pct += abs(pnl / (entry_price * 100)) if entry_price > 0 else 0
        else:
            s.losses += 1
            s.consecutive_losses += 1
            s.consecutive_wins = 0
            s.max_consecutive_losses_seen = max(s.max_consecutive_losses_seen, s.consecutive_losses)
            s.sum_loss_pct += abs(pnl) / (entry_price * 100) if entry_price > 0 else 0

        if s.in_recovery:
            if s.total_pnl >= s.recovery_target:
                s.in_recovery = False
                logger.info("Recovery target met")
        elif s.consecutive_losses >= self._cfg["max_consecutive_losses"] - 1:
            s.in_recovery = True
            s.recovery_entry_capital = s.current_capital
            s.recovery_target = s.total_pnl + self._cfg["initial_capital"] * 0.02
            logger.info(f"Recovery mode - target ${s.recovery_target:.2f}")

        self._save_state()
        return {
            "capital": round(s.current_capital, 2),
            "daily_pnl": round(s.daily_pnl, 4),
            "monthly_pnl": round(s.monthly_pnl, 4),
            "total_pnl": round(s.total_pnl, 4),
            "consecutive_losses": s.consecutive_losses,
            "win_rate": round(self._win_rate() * 100, 1),
        }

    def validate_order(self, price: float, size: float, token_price: float = 0.50) -> tuple[bool, str]:
        min_val = max(1.0, MIN_SHARES * token_price)
        if size < min_val:
            return False, f"${size:.2f} < minimum ${min_val:.2f} ({MIN_SHARES} shares x ${token_price:.3f})"
        if size > self._cfg["max_per_trade_usd"]:
            return False, f"${size:.2f} > max ${self._cfg['max_per_trade_usd']:.2f}"
        return True, ""

    def get_status_summary(self) -> dict[str, Any]:
        s = self.s
        dd = self._drawdown_pct()
        return {
            "capital": {
                "initial": s.initial_capital, "current": round(s.current_capital, 2),
                "peak": round(s.peak_capital, 2), "drawdown_pct": round(dd * 100, 2),
            },
            "pnl": {
                "daily": round(s.daily_pnl, 4), "monthly": round(s.monthly_pnl, 4),
                "total": round(s.total_pnl, 4),
            },
            "kelly_fraction": round(self._kelly_fraction(), 3),
            "win_rate": round(self._win_rate() * 100, 1),
            "consecutive": {"losses": s.consecutive_losses, "wins": s.consecutive_wins},
            "recovery_mode": s.in_recovery,
            "status": "halted" if s.permanently_halted else ("paused" if s.is_paused else "active"),
            "max_trade_size": self.compute_max_trade_size(),
            "total_trades": s.total_trades,
        }

    def _win_rate(self) -> float:
        tot = self.s.wins + self.s.losses
        return self.s.wins / tot if tot > 0 else 0.5

    def _avg_win_loss_ratio(self) -> float:
        s = self.s
        aw = s.sum_win_pct / s.wins if s.wins > 0 else 0.03
        al = s.sum_loss_pct / s.losses if s.losses > 0 else 0.03
        return aw / al if al > 0 else 1.0

    def _kelly_fraction(self) -> float:
        if self.s.total_trades < 5:
            return 0.15
        p = self._win_rate()
        b = self._avg_win_loss_ratio()
        if b <= 0:
            return 0.1
        k = (p * b - (1 - p)) / b
        return max(0.1, min(k * 0.5, 0.5))

    def _drawdown_pct(self) -> float:
        s = self.s
        if s.peak_capital <= 0:
            return 0.0
        return (s.peak_capital - s.current_capital) / s.peak_capital

    def _effective_drawdown(self) -> float:
        s = self.s
        dd = self._drawdown_pct()
        if dd <= 0:
            return 0.0

        hours = (time.time() - s.peak_timestamp) / 3600
        if hours < 2:
            mult = 1.5
        elif hours > 24:
            mult = 0.8
        else:
            progress = (hours - 2) / 22
            mult = 1.5 - progress * 0.5

        return min(dd * max(mult, 0.8), 0.99)

    def _pause(self, reason: str, minutes: int) -> None:
        self.s.is_paused = True
        self.s.pause_until = time.time() + minutes * 60
        self.s.pause_reason = reason
        self.s.in_recovery = True
        self.s.recovery_entry_capital = self.s.current_capital
        self.s.recovery_target = self.s.total_pnl + self._cfg["initial_capital"] * 0.02
        self._save_state()
        logger.warning(f"PAUSED: {reason} - {minutes} min")

    def _check_resets(self) -> None:
        s = self.s
        now = time.time()
        if now - s.last_daily_reset >= 86400:
            logger.info(f"Daily reset (was: ${s.daily_pnl:.4f})")
            s.daily_pnl = 0.0
            s.last_daily_reset = now
            if s.in_recovery and s.total_pnl >= 0:
                s.in_recovery = False
        if now - s.month_start_time >= 30 * 86400:
            s.monthly_pnl = 0.0
            s.month_start_time = now
        if s.in_recovery and s.total_trades == 0:
            s.in_recovery = False

    def _load_state(self) -> PortfolioState:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE) as f:
                return PortfolioState(**json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            ic = self._cfg["initial_capital"]
            logger.info(f"New crypto risk state: ${ic:.2f}")
            return PortfolioState(initial_capital=ic, current_capital=ic, peak_capital=ic)

    def _save_state(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self.s.__dict__, f, indent=2, default=str)
            tmp.rename(STATE_FILE)
        except Exception as e:
            logger.debug(f"Save state: {e}")


# Process-wide singletons by design — same single-worker assumption as
# backend/football/risk.py (see that file's comment); do not deploy with
# --workers N > 1 without moving this state into a shared datastore.
_portfolio_risk: PortfolioRisk | None = None
_position_registry: PositionRegistry | None = None
_rr_guard: RRGuard | None = None


def get_portfolio_risk() -> PortfolioRisk:
    global _portfolio_risk
    if _portfolio_risk is None:
        from backend.config import settings
        _portfolio_risk = PortfolioRisk(
            max_per_trade_usd=settings.CRYPTO_RISK_MAX_PER_TRADE_USD,
            initial_capital=settings.CRYPTO_INITIAL_CAPITAL,
        )
    return _portfolio_risk


def get_position_registry() -> PositionRegistry:
    global _position_registry
    if _position_registry is None:
        _position_registry = PositionRegistry()
    return _position_registry


def get_rr_guard() -> RRGuard:
    global _rr_guard
    if _rr_guard is None:
        _rr_guard = RRGuard()
    return _rr_guard


def reset_singletons_for_tests() -> None:
    global _portfolio_risk, _position_registry, _rr_guard
    _portfolio_risk = None
    _position_registry = None
    _rr_guard = None
