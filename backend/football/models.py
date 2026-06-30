"""Score-state probability model for football matches.

Ported verbatim from fball_bot/models.py. Inspired by Dixon-Coles, it moves
beyond flat heuristics: probability changes are driven by current score
state, time remaining, and event type. "home"/"away" are just labels for
"the side the market's YES contract refers to" vs. the other side — the
active calculation never applies an inherent home-field-advantage bias
(base_home_win_prob/base_away_win_prob below are unused), which is exactly
why this still works correctly for a neutral or co-hosted tournament.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("trading_bot")


@dataclass
class ModelUpdate:
    scalp_direction: str
    reversion_target: float
    confidence: float


class FootballReversionModel:
    """Predicts market price movements based on football events."""

    def __init__(self) -> None:
        self.base_home_win_prob = 0.45
        self.base_away_win_prob = 0.30
        self.base_draw_prob = 0.25
        self._baselines: dict[int, float] = {}
        self._drift_reference: dict[int, float] = {}

    def get_baseline(self, fixture_id: int) -> float | None:
        return self._baselines.get(fixture_id)

    def set_baseline(self, fixture_id: int, price: float) -> None:
        self._baselines[fixture_id] = price

    DRIFT_FADE_THRESHOLD = 0.04
    DRIFT_MIN_MINUTE = 20

    def update_drift_reference(self, fixture_id: int, price: float) -> None:
        """Anchor drift reference to price after a confirmed goal/card/penalty.

        Without this, a confirmed event (Japan scores → 0.115) leaves the
        reference at 0.405, so when Brazil equalizes and wins (→ 0.975) the
        drift model sees a 0.86 upward move from 0.115 and generates a SELL
        signal on a locked outcome. Call this from _handle_slow_event after
        every confirmed match event so the drift baseline stays current.
        """
        self._drift_reference[fixture_id] = price

    def check_drift(self, current: float, pre_match: float, minute: int, fixture_id: int | None = None) -> ModelUpdate:
        """Slow-path fade for sustained price drift with no goal/card/penalty event
        to explain it — distinct from update()'s event-driven reversion, which only
        fires on a discrete event. A real match can drift several cents purely from
        in-play possession/momentum with zero scoreboard change (confirmed live: a
        2026-06-23 England-Ghana session drifted 0.045->0.075 over 80 scoreless
        minutes and never traded, since nothing in update() reacts to undriven
        drift). Confidence is capped well below event-driven signals since the
        cause is unknown here — it may be real new information, not mispricing.

        Compares against a per-fixture rolling reference (reset to the current
        price every time this fires), not the fixed pre-match baseline — a
        backtest against 3 real matches caught the bug in the original
        baseline-only version: a team that's simply dominant (price drifts to
        0.99+ and stays there) made it refire every cooldown window for the
        rest of the match, since price never moves back toward the original
        baseline. Resetting the reference after each fire means only a NEW,
        further move triggers another fade — persistent one-directional drift
        fades once and then goes quiet, instead of churning the whole match.
        """
        # At min 85+, prices above 0.92 or below 0.08 reflect a nearly-locked
        # outcome (one team winning with <5 min left). Fading these is wrong —
        # the result won't revert to some mid-match equilibrium. Suppress drift
        # detection entirely for these end-game extreme prices.
        if minute >= 85 and (current > 0.92 or current < 0.08):
            return ModelUpdate("NONE", current, 0.0)

        reference = self._drift_reference.get(fixture_id, pre_match) if fixture_id is not None else pre_match
        raw_move = current - reference
        if abs(raw_move) < self.DRIFT_FADE_THRESHOLD or minute < self.DRIFT_MIN_MINUTE:
            return ModelUpdate("NONE", current, 0.0)

        reversion_rate = 0.4
        target = current - raw_move * reversion_rate
        expected_rev = abs(raw_move * reversion_rate)
        direction = "SELL" if raw_move > 0 else "BUY"
        confidence = min(0.5, 0.2 + expected_rev * 4.0)

        if fixture_id is not None:
            self._drift_reference[fixture_id] = current

        return ModelUpdate(direction, round(max(0.01, min(0.99, target)), 3), round(confidence, 3))

    def update(self, current: float, event: Any, pre_match: float) -> ModelUpdate:
        """Mean-reversion model for the fast path.

        Compares the market's raw price move to the fair-value impact of the
        event. If the market overreacted (move > fair impact), sells the
        spike expecting reversion. If it underreacted, buys the dip.
        """
        raw_move = current - pre_match
        if abs(raw_move) < 0.005:
            return ModelUpdate("NONE", current, 0.0)

        if event.type == "goal":
            impact = self.compute_event_impact(
                event_type="goal",
                team_side=event.team,
                market_home="home",
                market_away="away",
                minute=getattr(event, "minute", 45),
                home_score=getattr(event, "home_score", 0),
                away_score=getattr(event, "away_score", 0),
            )
        elif event.type in ("red_card", "var_check", "near_miss", "penalty"):
            mapping = {
                "red_card": "red_card",
                "var_check": "var_reversal",
                "near_miss": "near_miss",
                "penalty": "penalty_awarded",
            }
            impact = self.compute_event_impact(
                event_type=mapping.get(event.type, "near_miss"),
                team_side=event.team,
                market_home="home",
                market_away="away",
                minute=getattr(event, "minute", 45),
                home_score=getattr(event, "home_score", 0),
                away_score=getattr(event, "away_score", 0),
            )
        else:
            impact = -0.05 if event.team == "home" else 0.05

        justified_ratio = abs(impact) / max(abs(raw_move), 0.001)
        reversion_rate = max(0.05, min(0.75, 1.0 - justified_ratio))

        target = current - raw_move * reversion_rate
        expected_rev = abs(raw_move * reversion_rate)

        if expected_rev < 0.02:
            return ModelUpdate("NONE", current, 0.0)

        direction = "SELL" if raw_move > 0 else "BUY"
        confidence = min(1.0, 0.3 + expected_rev * 8.0)

        return ModelUpdate(direction, round(target, 3), round(confidence, 3))

    def compute_event_impact(
        self,
        event_type: str,
        team_side: str,
        market_home: str,
        market_away: str,
        minute: int,
        home_score: int,
        away_score: int,
    ) -> float:
        """Returns the expected % change in win probability for the market's target team."""
        is_positive_event = False
        if event_type in ("goal", "penalty_awarded"):
            is_positive_event = (team_side == "home" and market_home == "home") or \
                                 (team_side == "away" and market_home == "away")
        elif event_type in ("red_card", "var_reversal"):
            is_positive_event = (team_side == "away" and market_home == "home") or \
                                 (team_side == "home" and market_home == "away")
        elif event_type == "own_goal":
            is_positive_event = (team_side == "home" and market_home == "home") or \
                                 (team_side == "away" and market_home == "away")
        else:
            return 0.0

        time_factor = self._compute_time_factor(minute)
        state_factor = self._compute_state_factor(
            home_score, away_score, is_positive_event, team_side
        )

        base_impact = 0.0
        if event_type in ("goal", "own_goal"):
            base_impact = 0.20
        elif event_type == "red_card":
            base_impact = 0.12
        elif event_type == "penalty_awarded":
            base_impact = 0.15
        elif event_type == "var_reversal":
            base_impact = 0.20

        impact = base_impact * time_factor * state_factor
        return impact if is_positive_event else -impact

    def _compute_time_factor(self, minute: int) -> float:
        if minute <= 0:
            return 0.5
        if minute >= 90:
            return 1.5
        return 0.5 + (minute / 90.0)

    def _compute_state_factor(
        self, home_score: int, away_score: int, is_positive: bool, team_side: str = "home"
    ) -> float:
        pre_home = home_score - (1 if team_side == "home" else 0)
        pre_away = away_score - (1 if team_side == "away" else 0)
        pre_diff = abs(pre_home - pre_away)

        if pre_diff == 0:
            return 1.2
        elif pre_diff == 1:
            was_ahead = (team_side == "home" and pre_home > pre_away) or \
                        (team_side == "away" and pre_away > pre_home)
            return 0.8 if was_ahead else 1.2
        elif pre_diff == 2:
            return 0.4
        else:
            return 0.1

    def compute_reversion_target(self, current_price: float, expected_change: float) -> float:
        target = current_price + expected_change
        return max(0.01, min(0.99, target))
