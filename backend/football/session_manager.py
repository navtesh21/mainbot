"""Per-match football session orchestration.

Not present in fball_bot (which is single-match/single-process); needed here
because the dashboard must support N independent, concurrently-running
sessions, each started by pasting a Polymarket link. Step C resolves the
link, correlates a fixture, and persists a session row. Step D attaches the
live signal-generation pipeline (PriceTrigger fast path + live-data slow
path -> FootballReversionModel -> Signal rows) — execution is wired in
Step E, active exits in Step F, match-end teardown in Step G.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from backend.config import settings
from backend.football.discovery import resolve_link, match_fixture_ref
from backend.football.models import FootballReversionModel
from backend.football.pm_client import GammaClient
from backend.football.price_trigger import PriceTrigger, PriceSpike
from backend.football.risk import get_portfolio_risk, get_position_registry, get_rr_guard, kelly_size
from backend.football.clob_trader import get_clob_trader
from backend.football.scalping import compute_stop, evaluate_exit, get_cooldown_seconds, ExitDecision
from backend.data.football_live import MatchEvent
from backend.models.database import SessionLocal, FootballSession, Signal, Trade

logger = logging.getLogger("trading_bot")

# Ported from fball_bot/bot.py::_execute_signal's entry-gating constants.
MIN_LIQUIDITY_USD = 5.0
MIN_ENTRY_CONFIDENCE = 0.15
DEFAULT_TRADE_SIZE_USD = 3.0  # RRGuard's trade_size input before Kelly sizing runs
EXIT_POLL_SECONDS = 5
LIVE_POLL_SECONDS = 5  # ESPN score/event poll cadence — matched to EXIT_POLL_SECONDS

# Ported from fball_bot/bot.py::_process_match's match-end check. The upstream
# tuple mixes ('finished','FT',...,'FINISHED',...) against an already-.lower()'d
# string, so the uppercase entries are dead — written here fully lowercase to
# actually achieve the evident intent (case-insensitive match on any of these).
MATCH_END_STATUSES = {"finished", "ft", "completed", "cancelled", "awarded"}


class SessionStartError(Exception):
    pass


@dataclass
class _PipelineState:
    session_id: int
    condition_id: str
    yes_token_id: str
    no_token_id: Optional[str]
    fixture_ref: Optional[str]
    model: FootballReversionModel
    price_trigger: PriceTrigger
    home_team: str = ""
    away_team: str = ""
    last_minute: int = 0
    last_home_score: int = 0
    last_away_score: int = 0
    last_status: str = "live"
    price_task: Optional[asyncio.Task] = None
    poll_task: Optional[asyncio.Task] = None
    exit_task: Optional[asyncio.Task] = None
    analysis_task: Optional[asyncio.Task] = None
    odds_task: Optional[asyncio.Task] = None
    stopped: bool = False
    # trade_id -> {token_id, side, entry_price, size, stop_price, expected_reversion,
    #              opened_at, partial_exited}. Populated by _execute_entry; consumed
    # by the exit-management loop (_exit_loop/_check_exits).
    open_trades: Dict[int, dict] = field(default_factory=dict)
    last_exit_time: float = 0.0
    last_exit_result: Optional[str] = None
    last_drift_check_time: float = 0.0


_pipelines: Dict[int, _PipelineState] = {}


# ── Fixture matching (Step C) ────────────────────────────────────────────

async def _build_fixture_candidates() -> list[Tuple[str, str, str]]:
    """Collect (fixture_ref, home_team, away_team) candidates from live + scheduled matches."""
    candidates: list[Tuple[str, str, str]] = []

    try:
        from backend.data.football_live import get_live_source
        live_matches = await get_live_source().get_live_matches()
        for m in live_matches:
            candidates.append((f"live:{m.fixture_id}", m.home_team, m.away_team))
    except Exception as e:
        logger.debug(f"Failed to fetch live matches for fixture matching: {e}")

    try:
        from backend.data.football_schedule import get_schedule_service
        scheduled = await get_schedule_service().get_schedule()
        for m in scheduled:
            candidates.append((f"schedule:{m.source_id}", m.home_team, m.away_team))
    except Exception as e:
        logger.debug(f"Failed to fetch schedule for fixture matching: {e}")

    return candidates


def _fixture_numeric_id(fixture_ref: Optional[str]) -> Optional[int]:
    """Live fixture refs look like 'live:12345' — only those are pollable for events."""
    if not fixture_ref or not fixture_ref.startswith("live:"):
        return None
    try:
        return int(fixture_ref.split(":", 1)[1])
    except ValueError:
        return None


async def _fetch_current_price(condition_id: str) -> float:
    client = GammaClient()
    try:
        market = await client.get_market(condition_id)
        if market:
            return GammaClient.parse_price(market, "YES")
    except Exception as e:
        logger.debug(f"Failed to fetch current price for {condition_id[:12]}: {e}")
    finally:
        await client.close()
    return 0.5


# ── Signal-agreement gate (ported from fball_bot/bot.py::_direction_agreement) ──

def _direction_agreement(spike: PriceSpike, model_side: str) -> Tuple[bool, str]:
    """Require 2+ of 3 signals (trajectory, imbalance, parity) to agree with the model direction."""
    model_dir = model_side.upper()
    if model_dir not in ("BUY", "SELL"):
        return True, ""

    votes_for = 0
    votes_against = 0
    reasons = []

    traj_up = spike.trajectory in ("step_jump", "slow_drift") and spike.direction == "up"
    traj_down = spike.trajectory in ("step_jump", "slow_drift") and spike.direction == "down"
    if traj_up and model_dir == "SELL":
        votes_for += 1
        reasons.append("traj")
    elif traj_down and model_dir == "BUY":
        votes_for += 1
        reasons.append("traj")
    else:
        votes_against += 1

    if spike.imbalance > 1.5 and model_dir == "SELL":
        votes_for += 1
        reasons.append("imb")
    elif spike.imbalance < 0.67 and model_dir == "BUY":
        votes_for += 1
        reasons.append("imb")
    else:
        votes_against += 1

    if spike.parity and spike.parity.tight_parity:
        votes_for += 1
        reasons.append("parity")
    else:
        votes_against += 1

    passed = votes_for >= 2
    detail = f"sig={votes_for}/{votes_for + votes_against} ({','.join(reasons)})"
    return passed, detail


def _map_spike_to_event(spike: PriceSpike) -> Tuple[str, str, str]:
    """Map a classified PriceSpike to (fake_team, event_kind, trade_side).

    Ported from fball_bot/bot.py::_on_price_spike's event-type mapping.
    """
    event_type_str = spike.event_type
    if event_type_str == "goal_home":
        return "home", "goal", "SELL"
    elif event_type_str == "goal_away":
        return "away", "goal", "BUY"
    elif event_type_str in ("red_card", "var_check", "near_miss", "penalty"):
        fake_team = "home" if spike.direction == "down" else "away"
        trade_side = "SELL" if spike.direction == "up" else "BUY"
        return fake_team, event_type_str, trade_side
    else:
        fake_team = "home" if spike.direction == "up" else "away"
        trade_side = "SELL" if spike.direction == "up" else "BUY"
        return fake_team, "goal", trade_side


def _persist_signal(
    session_id: int,
    condition_id: str,
    direction: str,
    model_probability: float,
    market_price: float,
    confidence: float,
    reasoning: str,
    sources: list[str],
) -> Optional[int]:
    db = SessionLocal()
    try:
        edge = abs(model_probability - market_price)
        row = Signal(
            market_ticker=condition_id,
            platform="polymarket",
            market_type="football",
            football_session_id=session_id,
            direction=direction,
            model_probability=model_probability,
            market_price=market_price,
            edge=edge,
            confidence=confidence,
            kelly_fraction=0.0,
            suggested_size=0.0,
            sources=sources,
            reasoning=reasoning,
            executed=False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        signal_id = row.id

        from backend.core.scheduler import log_event
        log_event("data", f"Football signal (session #{session_id}): {direction} edge={edge:.3f} conf={confidence:.2f} — {reasoning}", {
            "session_id": session_id,
            "direction": direction,
            "edge": edge,
            "confidence": confidence,
        })
        return signal_id
    except Exception:
        logger.exception(f"Failed to persist football signal for session {session_id}")
        return None
    finally:
        db.close()


# ── Entry execution: risk gates -> Kelly sizing -> dry-run/live order ────
# Ported from fball_bot/bot.py::_execute_signal's entry branch (lines 824-1068).
# Active exit management (stop/target/timeout) is wired in Step F; entries
# opened here sit in state.open_trades until that loop closes them.

async def _execute_entry(
    session_id: int,
    state: "_PipelineState",
    side: str,
    token_id: str,
    price: float,
    expected_reversion: float,
    confidence: float,
    reason: str,
    signal_id: Optional[int],
) -> None:
    if confidence < MIN_ENTRY_CONFIDENCE:
        return
    if not token_id or price <= 0:
        return

    # Mirrors fball_bot/scalping.py::ScalpingStrategy.evaluate()'s early-return
    # while a position is open, plus its post-exit cooldown (win/loss-aware).
    # NOTE: this "one open position per session" policy is independently
    # re-encoded in backend/football/risk.py::PositionRegistry.can_open() (which
    # blocks a second position on the same token_id). The two currently agree
    # only because each session trades exactly one token (state.yes_token_id) —
    # if a future change allows a session to hold multiple simultaneous
    # positions, both checks must be updated together or they will disagree.
    if state.open_trades:
        return
    cooldown = get_cooldown_seconds(state.last_exit_result)
    if time.time() - state.last_exit_time < cooldown:
        return

    gamma = GammaClient()
    try:
        depth = await gamma.get_orderbook_depth(token_id)
    finally:
        await gamma.close()
    if depth < MIN_LIQUIDITY_USD:
        logger.info(f"Session {session_id}: low liquidity ${depth:.0f} < ${MIN_LIQUIDITY_USD:.0f} — entry skipped")
        return

    target = price - expected_reversion if side == "SELL" else price + expected_reversion
    target = max(0.01, min(0.99, target))
    stop_price = compute_stop(price, side, expected_reversion)

    risk = get_portfolio_risk()
    can_trade, block_reason = risk.can_trade(active_match_count=max(len(_pipelines), 1))
    if not can_trade:
        logger.info(f"Session {session_id}: portfolio risk gate blocked entry — {block_reason}")
        return

    book = state.price_trigger._subscriptions.get(token_id, {})
    rr = get_rr_guard().evaluate(
        entry_price=price,
        target_price=target,
        stop_price=stop_price,
        side=side,
        best_bid=book.get("best_bid", price * 0.98),
        best_ask=book.get("best_ask", price * 1.02),
        bid_vol=book.get("bid_vol", 100.0),
        ask_vol=book.get("ask_vol", 100.0),
        trade_size=DEFAULT_TRADE_SIZE_USD,
        model_confidence=confidence,
    )
    if rr.action == "block":
        logger.info(f"Session {session_id}: R:R block — {rr.reason}")
        return

    ok, validate_reason = risk.validate_order(price, DEFAULT_TRADE_SIZE_USD, token_price=price)
    if not ok:
        logger.info(f"Session {session_id}: order size invalid — {validate_reason}")
        return

    reward_pct = abs(price - target) / max(price, 0.001)
    risk_pct = abs(price - stop_price) / max(price, 0.001)
    kelly_usd = kelly_size(p_adj=rr.p_adj, reward_pct=reward_pct, risk_pct=risk_pct, capital=risk.s.current_capital)
    adj_size = risk.compute_max_trade_size(kelly_usd, confidence, token_price=price)
    if adj_size <= 0:
        return
    final_size = min(kelly_usd, adj_size) * rr.multiplier
    if final_size <= 0:
        return

    registry = get_position_registry()
    can_open, pos_reason = registry.can_open(token_id, side, session_id, final_size, risk.s.current_capital)
    if not can_open:
        logger.info(f"Session {session_id}: position registry blocked entry — {pos_reason}")
        return

    order_id = f"dryrun-{int(time.time() * 1000)}"
    mode = "DRY-RUN"
    trader = get_clob_trader()
    if trader is not None:
        result = await trader.place_market_order(token_id=token_id, amount=final_size, side=side, price=price)
        if not result.get("success"):
            logger.error(f"Session {session_id}: live order failed — {result.get('error')}")
            return
        order_id = (result.get("order_ids") or [order_id])[0]
        mode = "LIVE"

    db = SessionLocal()
    try:
        trade = Trade(
            signal_id=signal_id or 0,
            market_ticker=state.condition_id,
            platform="polymarket",
            market_type="football",
            football_session_id=session_id,
            direction="up" if side == "BUY" else "down",
            entry_price=price,
            size=final_size,
            settled=False,
            result="pending",
            model_probability=target,
            market_price_at_entry=price,
            edge_at_entry=reward_pct,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        trade_id = trade.id

        session_row = db.query(FootballSession).filter(FootballSession.id == session_id).first()
        if session_row:
            session_row.total_trades = (session_row.total_trades or 0) + 1
            db.commit()
    except Exception:
        logger.exception(f"Session {session_id}: failed to persist trade row after {mode} order {order_id}")
        return
    finally:
        db.close()

    registry.open(token_id, side, session_id, final_size)
    state.open_trades[trade_id] = {
        "token_id": token_id,
        "side": side,
        "entry_price": price,
        "size": final_size,
        "stop_price": stop_price,
        "expected_reversion": expected_reversion,
        "opened_at": time.time(),
        "partial_exited": False,
        "reason": reason,
    }

    from backend.core.scheduler import log_event
    log_event(
        "trade",
        f"Football {mode} entry (session #{session_id}): {side} ${final_size:.2f} @ {price:.3f} "
        f"order={order_id} conf={confidence:.2f} — {reason}",
        {
            "session_id": session_id,
            "trade_id": trade_id,
            "side": side,
            "size": final_size,
            "price": price,
            "order_id": order_id,
            "mode": mode,
        },
    )


# ── Exit management: stop/target/timeout/partial-profit ──────────────────
# Ported from fball_bot/scalping.py::ScalpingStrategy.evaluate()'s exit-check
# block + bot.py::_execute_signal's exit branch (PnL formula, registry close,
# risk.record_trade). Realized PnL writes onto the same Trade row opened by
# _execute_entry — the "second settlement path" distinct from BTC's passive
# 0/1 polling in backend/core/settlement.py.

async def _execute_exit(
    session_id: int,
    state: "_PipelineState",
    trade_id: int,
    position: dict,
    decision,
    exit_price: float,
) -> None:
    side = position["side"]
    exit_side = "BUY" if side == "SELL" else "SELL"
    entry_price = position["entry_price"]
    exit_size = position["size"] * decision.exit_fraction

    qty = exit_size / entry_price if entry_price > 0 else exit_size
    pnl = qty * (entry_price - exit_price) if side == "SELL" else qty * (exit_price - entry_price)

    order_id = f"dryrun-exit-{int(time.time() * 1000)}"
    mode = "DRY-RUN"
    trader = get_clob_trader()
    if trader is not None:
        result = await trader.place_market_order(token_id=position["token_id"], amount=exit_size, side=exit_side, price=exit_price)
        if not result.get("success"):
            logger.error(f"Session {session_id}: live exit order failed for trade {trade_id} — {result.get('error')}")
            return  # leave position open; retry next cycle
        order_id = (result.get("order_ids") or [order_id])[0]
        mode = "LIVE"

    is_partial = decision.kind == "partial"

    # Mirrors fball_bot/bot.py: the registry is released using the position's
    # full original size on every exit signal, partial or final.
    get_position_registry().close(position["token_id"], session_id, position["size"])

    db = SessionLocal()
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            trade.pnl = (trade.pnl or 0.0) + pnl
            if is_partial:
                position["partial_exited"] = True
            else:
                trade.settled = True
                trade.settlement_time = datetime.utcnow()
                trade.settlement_value = exit_price
                trade.result = "win" if trade.pnl >= 0 else "loss"
            db.commit()

        session_row = db.query(FootballSession).filter(FootballSession.id == session_id).first()
        if session_row:
            session_row.realized_pnl = (session_row.realized_pnl or 0.0) + pnl
            db.commit()
    except Exception:
        logger.exception(f"Session {session_id}: failed to record exit for trade {trade_id}")
        return
    finally:
        db.close()

    risk_info = get_portfolio_risk().record_trade(pnl, entry_price)

    if not is_partial:
        state.open_trades.pop(trade_id, None)
        state.last_exit_time = time.time()
        state.last_exit_result = "win" if pnl >= 0 else "loss"

    from backend.core.scheduler import log_event
    log_event(
        "trade",
        f"Football {mode} exit (session #{session_id}): {exit_side} ${exit_size:.2f} @ {exit_price:.3f} "
        f"{decision.kind} pnl=${pnl:+.4f} cap=${risk_info['capital']:.2f} order={order_id} — {decision.reason}",
        {
            "session_id": session_id,
            "trade_id": trade_id,
            "kind": decision.kind,
            "pnl": pnl,
            "exit_price": exit_price,
            "order_id": order_id,
            "mode": mode,
        },
    )


async def _check_exits(session_id: int, state: "_PipelineState") -> None:
    if not state.open_trades:
        return

    price = await _fetch_current_price(state.condition_id)
    for trade_id, position in list(state.open_trades.items()):
        decision = evaluate_exit(position, price, state.last_minute)
        if decision is not None:
            await _execute_exit(session_id, state, trade_id, position, decision, price)


async def _exit_loop(session_id: int) -> None:
    while True:
        state = _pipelines.get(session_id)
        if not state or state.stopped:
            return
        try:
            await _check_exits(session_id, state)
        except Exception as e:
            logger.debug(f"Session {session_id}: exit check failed: {e}")
        await asyncio.sleep(EXIT_POLL_SECONDS)


# ── Fast path: PriceTrigger spike callback ───────────────────────────────

async def _handle_spike(session_id: int, spike: PriceSpike) -> None:
    state = _pipelines.get(session_id)
    if not state or state.stopped:
        return

    fake_team, event_kind, trade_side = _map_spike_to_event(spike)
    fake_event = MatchEvent(
        type=event_kind,
        minute=state.last_minute,
        team=fake_team,
        home_score=state.last_home_score,
        away_score=state.last_away_score,
        event_id=f"spike-{spike.token_id}-{int(spike.timestamp)}",
    )

    pre_match = state.model.get_baseline(session_id) or spike.price_before
    update = state.model.update(current=spike.price_now, event=fake_event, pre_match=pre_match)

    if update.scalp_direction == "NONE":
        logger.debug(f"Session {session_id}: spike produced no scalp signal")
        return

    expected_rev = spike.expected_reversion if spike.expected_reversion > 0 else abs(spike.price_now - update.reversion_target)
    if expected_rev < 0.02 and abs(spike.price_now - update.reversion_target) < 0.02:
        return

    sig_ok, sig_detail = _direction_agreement(spike, update.scalp_direction)
    if not sig_ok:
        logger.debug(f"Session {session_id}: spike blocked by signal agreement: {sig_detail}")
        return

    classifier_boost = spike.confidence if spike.confidence > 0 else 1.0
    final_confidence = min(1.0, update.confidence * classifier_boost)
    direction = "yes" if update.scalp_direction == "BUY" else "no"
    reason_tail = "|".join(spike.reasons[:2]) if spike.reasons else f"{spike.direction}_{spike.change_pct:.0f}pct"
    reasoning = f"spike_{spike.trajectory}_{event_kind}_{spike.change_pct:.0f}pct_{reason_tail} ({sig_detail})"

    signal_id = _persist_signal(
        session_id=session_id,
        condition_id=state.condition_id,
        direction=direction,
        model_probability=update.reversion_target,
        market_price=spike.price_now,
        confidence=final_confidence,
        reasoning=reasoning,
        sources=["fball_price_trigger", spike.trajectory],
    )

    # update.scalp_direction (BUY/SELL) and spike.price_now are both computed
    # relative to the YES contract — always trade YES with that side, never
    # switch tokens based on `direction`, which is only a "yes"/"no" display
    # label for Signal.direction (mirrors fball_bot/scalping.py, which always
    # trades m.yes_token_id and never touches a separate NO token for entries).
    await _execute_entry(
        session_id=session_id,
        state=state,
        side=update.scalp_direction,
        token_id=state.yes_token_id,
        price=spike.price_now,
        expected_reversion=expected_rev,
        confidence=final_confidence,
        reason=reasoning,
        signal_id=signal_id,
    )


# ── Slow path: live-data event polling ────────────────────────────────────

async def _handle_slow_event(session_id: int, event: MatchEvent) -> None:
    state = _pipelines.get(session_id)
    if not state or state.stopped:
        return

    state.last_minute = event.minute
    state.last_home_score = event.home_score
    state.last_away_score = event.away_score

    price = await _fetch_current_price(state.condition_id)

    # Anchor drift reference to post-event price so the drift model doesn't
    # try to fade the next move from a stale pre-event reference. Without this,
    # a Japan goal at 0.115 leaves reference=0.405, and when Brazil equalizes
    # (→0.60) the drift check sees a 0.49 upward move and wrongly generates
    # a SELL signal on a valid score-driven price change.
    state.model.update_drift_reference(session_id, price)

    pre_match = state.model.get_baseline(session_id) or price
    update = state.model.update(current=price, event=event, pre_match=pre_match)

    if update.scalp_direction == "NONE":
        return

    direction = "yes" if update.scalp_direction == "BUY" else "no"
    reasoning = f"slow_path_{event.type}_{event.team}_min{event.minute}"

    signal_id = _persist_signal(
        session_id=session_id,
        condition_id=state.condition_id,
        direction=direction,
        model_probability=update.reversion_target,
        market_price=price,
        confidence=update.confidence,
        reasoning=reasoning,
        sources=["fball_live_data_slow_path"],
    )

    expected_reversion = abs(update.reversion_target - price)
    # See _handle_spike's comment above — always trade YES, never switch tokens.
    await _execute_entry(
        session_id=session_id,
        state=state,
        side=update.scalp_direction,
        token_id=state.yes_token_id,
        price=price,
        expected_reversion=expected_reversion,
        confidence=update.confidence,
        reason=reasoning,
        signal_id=signal_id,
    )


DRIFT_CHECK_COOLDOWN_SECONDS = 60.0


async def _handle_drift(session_id: int, state: "_PipelineState") -> None:
    """Fade sustained price drift that no goal/card/penalty event explains —
    see FootballReversionModel.check_drift's docstring for why this exists."""
    if time.time() - state.last_drift_check_time < DRIFT_CHECK_COOLDOWN_SECONDS:
        return
    state.last_drift_check_time = time.time()

    price = await _fetch_current_price(state.condition_id)
    pre_match = state.model.get_baseline(session_id) or price
    update = state.model.check_drift(current=price, pre_match=pre_match, minute=state.last_minute, fixture_id=session_id)

    if update.scalp_direction == "NONE":
        return

    direction = "yes" if update.scalp_direction == "BUY" else "no"
    reasoning = f"drift_fade_min{state.last_minute}_{price:.3f}_vs_{pre_match:.3f}"

    signal_id = _persist_signal(
        session_id=session_id,
        condition_id=state.condition_id,
        direction=direction,
        model_probability=update.reversion_target,
        market_price=price,
        confidence=update.confidence,
        reasoning=reasoning,
        sources=["fball_drift_fade"],
    )

    expected_reversion = abs(update.reversion_target - price)
    await _execute_entry(
        session_id=session_id,
        state=state,
        side=update.scalp_direction,
        token_id=state.yes_token_id,
        price=price,
        expected_reversion=expected_reversion,
        confidence=update.confidence,
        reason=reasoning,
        signal_id=signal_id,
    )


async def _refresh_match_state(state: "_PipelineState", fid_num: int) -> Optional[str]:
    """Fetch live minute/score for fid_num and update state in place. Returns
    the match's raw status string (or None if the fixture wasn't found)."""
    from backend.data.football_live import get_live_source

    source = get_live_source()
    matches = await source.get_live_matches()
    for m in matches:
        if m.fixture_id == fid_num:
            score_changed = (m.home_score, m.away_score) != (state.last_home_score, state.last_away_score)
            minute_changed = m.minute != state.last_minute

            if score_changed or minute_changed:
                from backend.core.scheduler import log_event
                log_event("data", f"ESPN live (session #{state.session_id}): {m.home_score}-{m.away_score} "
                          f"min {m.minute}' [{m.status}]", {
                    "session_id": state.session_id,
                    "home_score": m.home_score,
                    "away_score": m.away_score,
                    "minute": m.minute,
                    "status": m.status,
                })

            state.last_minute = m.minute
            state.last_home_score = m.home_score
            state.last_away_score = m.away_score
            state.last_status = m.status
            return m.status
    return None


async def _slow_path_loop(session_id: int) -> None:
    from backend.data.football_live import get_live_source

    while True:
        state = _pipelines.get(session_id)
        if not state or state.stopped:
            return

        fid_num = _fixture_numeric_id(state.fixture_ref)
        if fid_num is not None:
            try:
                match_status = await _refresh_match_state(state, fid_num)

                if match_status and match_status.lower().strip() in MATCH_END_STATUSES:
                    await _finish_session(session_id, state)
                    return

                source = get_live_source()
                events = await source.poll_new_events(fid_num)
                if events:
                    from backend.core.scheduler import log_event
                    for event in events:
                        player_suffix = f" — {event.player}" if event.player else ""
                        log_event("data", f"ESPN event (session #{session_id}): {event.type} "
                                  f"min {event.minute}' {event.team} {event.home_score}-{event.away_score}{player_suffix}", {
                            "session_id": session_id,
                            "type": event.type,
                            "minute": event.minute,
                            "team": event.team,
                            "player": event.player,
                            "home_score": event.home_score,
                            "away_score": event.away_score,
                        })
                for event in events:
                    await _handle_slow_event(session_id, event)

                # Only check for unexplained drift when nothing just explained
                # a price move this cycle — an actual event already covers it.
                if not events:
                    await _handle_drift(session_id, state)
            except Exception as e:
                logger.debug(f"Session {session_id}: slow-path poll failed: {e}")

        await asyncio.sleep(LIVE_POLL_SECONDS)


# ── Match-end detection and teardown ──────────────────────────────────────
# Ported from fball_bot/bot.py::_process_match's match-over branch.

async def _force_close_all(session_id: int, state: "_PipelineState") -> None:
    """Force-close any still-open scalp via the timeout exit path."""
    if not state.open_trades:
        return
    price = await _fetch_current_price(state.condition_id)
    for trade_id, position in list(state.open_trades.items()):
        fraction = 0.5 if position.get("partial_exited") else 1.0
        decision = ExitDecision("timeout", fraction, "match_end_force_close")
        await _execute_exit(session_id, state, trade_id, position, decision, price)


async def _finish_session(session_id: int, state: "_PipelineState") -> None:
    await _force_close_all(session_id, state)
    await _teardown_pipeline(session_id)

    db = SessionLocal()
    try:
        session_row = db.query(FootballSession).filter(FootballSession.id == session_id).first()
        if session_row:
            session_row.status = "finished"
            session_row.ended_at = datetime.utcnow()
            db.commit()

        from backend.core.scheduler import log_event
        log_event("info", f"Football session #{session_id} finished — match ended, position(s) force-closed", {
            "session_id": session_id,
        })
    finally:
        db.close()


# ── Pipeline lifecycle ─────────────────────────────────────────────────────

async def _attach_pipeline(session: FootballSession) -> None:
    if not session.condition_id or not session.yes_token_id:
        logger.warning(f"Session {session.id}: missing condition_id/yes_token_id, no pipeline attached")
        return

    current_price = await _fetch_current_price(session.condition_id)
    model = FootballReversionModel()
    model.set_baseline(session.id, current_price)

    price_trigger = PriceTrigger()
    await price_trigger.subscribe(
        session.yes_token_id,
        fixture_id=session.id,
        baseline_price=current_price,
        no_token_id=session.no_token_id or "",
    )

    async def _on_spike(spike: PriceSpike) -> None:
        await _handle_spike(session.id, spike)

    price_trigger.on_spike(_on_spike)

    state = _PipelineState(
        session_id=session.id,
        condition_id=session.condition_id,
        yes_token_id=session.yes_token_id,
        no_token_id=session.no_token_id,
        fixture_ref=session.fixture_ref,
        model=model,
        price_trigger=price_trigger,
        home_team=session.home_team or "",
        away_team=session.away_team or "",
    )
    _pipelines[session.id] = state

    # Populate real minute/score synchronously before the spike/slow-path
    # loops launch, so a spike firing before _slow_path_loop's first 15s tick
    # doesn't compute model impact against stale minute=0/score=0/0 defaults.
    fid_num = _fixture_numeric_id(session.fixture_ref)
    if fid_num is not None:
        try:
            await _refresh_match_state(state, fid_num)
        except Exception as e:
            logger.debug(f"Session {session.id}: initial match-state refresh failed: {e}")

    state.price_task = asyncio.create_task(price_trigger.start())
    state.poll_task = asyncio.create_task(_slow_path_loop(session.id))
    state.exit_task = asyncio.create_task(_exit_loop(session.id))

    from backend.football.ai_analysis import analysis_loop
    state.analysis_task = asyncio.create_task(analysis_loop(session.id))

    from backend.football.odds_comparison import odds_loop
    state.odds_task = asyncio.create_task(odds_loop(session.id))

    logger.info(f"Session {session.id}: pipeline attached (baseline={current_price:.3f})")


async def _teardown_pipeline(session_id: int) -> None:
    state = _pipelines.pop(session_id, None)
    if not state:
        return

    state.stopped = True
    try:
        await state.price_trigger.stop()
    except Exception:
        pass
    if state.price_task:
        state.price_task.cancel()
    if state.poll_task:
        state.poll_task.cancel()
    if state.exit_task:
        state.exit_task.cancel()
    if state.analysis_task:
        state.analysis_task.cancel()
    if state.odds_task:
        state.odds_task.cancel()
    logger.info(f"Session {session_id}: pipeline torn down")


# ── Public API ──────────────────────────────────────────────────────────

async def start_session(link: str) -> FootballSession:
    """Resolve a pasted Polymarket link to a market, correlate it to a fixture,
    persist a new FootballSession row, and attach the live signal pipeline."""
    db = SessionLocal()
    try:
        resolved = await resolve_link(link)
        if not resolved:
            raise SessionStartError(f"Could not resolve a market from link: {link}")

        candidates = await _build_fixture_candidates()
        fixture_ref = match_fixture_ref(resolved.home_team, resolved.away_team, candidates)

        session = FootballSession(
            polymarket_link=link,
            polymarket_slug=resolved.polymarket_slug,
            condition_id=resolved.condition_id,
            yes_token_id=resolved.yes_token_id or None,
            no_token_id=resolved.no_token_id or None,
            home_team=resolved.home_team or None,
            away_team=resolved.away_team or None,
            fixture_ref=fixture_ref,
            status="starting",
            created_at=datetime.utcnow(),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        session_id = session.id
        home_team, away_team = resolved.home_team, resolved.away_team

        # Only flip to "running" once the pipeline actually attaches (or
        # immediately if football trading is globally disabled, since then
        # no pipeline is ever attempted by design). A failed attach must not
        # leave the row stuck at "running" with no pipeline behind it.
        if settings.FOOTBALL_ENABLED:
            try:
                await _attach_pipeline(session)
            except Exception as attach_exc:
                logger.exception(f"Session {session_id}: pipeline attach failed")
                session.status = "error"
                session.error_message = str(attach_exc)
                db.commit()
                raise SessionStartError(f"Pipeline attach failed: {attach_exc}")

        session.status = "running"
        db.commit()
        db.refresh(session)

        from backend.core.scheduler import log_event
        log_event("success", f"Football session #{session_id} started: {home_team} vs {away_team}", {
            "session_id": session_id,
            "condition_id": resolved.condition_id,
            "fixture_ref": fixture_ref,
        })

        return session
    except SessionStartError:
        raise
    except Exception as e:
        logger.exception("Failed to start football session")
        raise SessionStartError(str(e))
    finally:
        db.close()


async def stop_session(session_id: int) -> Optional[FootballSession]:
    await _teardown_pipeline(session_id)

    db = SessionLocal()
    try:
        session = db.query(FootballSession).filter(FootballSession.id == session_id).first()
        if not session:
            return None
        session.status = "stopped"
        session.ended_at = datetime.utcnow()
        db.commit()
        db.refresh(session)

        from backend.core.scheduler import log_event
        log_event("info", f"Football session #{session.id} stopped")

        return session
    finally:
        db.close()


def list_sessions() -> List[FootballSession]:
    db = SessionLocal()
    try:
        return db.query(FootballSession).order_by(FootballSession.created_at.desc()).all()
    finally:
        db.close()
