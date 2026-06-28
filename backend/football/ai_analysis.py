"""Periodic Gemini-powered market analysis for active football sessions.

Every ANALYSIS_INTERVAL_SECONDS, gathers the live Polymarket order book +
match state for a session and asks Gemini for a detailed read, caching the
latest result in-memory for the dashboard to display (replaces the old
BTC-only Edge Distribution panel slot).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from backend.config import settings
from backend.football.pm_client import GammaClient

logger = logging.getLogger("trading_bot")

ANALYSIS_INTERVAL_SECONDS = 120

_latest: Dict[int, Dict[str, Any]] = {}


def get_latest_analysis(session_id: int) -> Optional[Dict[str, Any]]:
    return _latest.get(session_id)


def _budget_remaining() -> bool:
    """Cheap guard against runaway Gemini spend — skip if today's AI spend
    (across all call types) already exceeds AI_DAILY_BUDGET_USD."""
    try:
        from backend.ai.logger import get_ai_logger
        spent = get_ai_logger().get_daily_stats().get("total_cost_usd", 0.0)
        return spent < settings.AI_DAILY_BUDGET_USD
    except Exception:
        return True


async def run_analysis(session_id: int, state: "Any") -> None:
    """Gather order book + match state for one session and store a fresh Gemini read."""
    from backend.ai.gemini import get_gemini_client
    from backend.football.risk import get_portfolio_risk

    gemini = get_gemini_client()
    if gemini is None:
        return
    if not _budget_remaining():
        logger.info(f"Session {session_id}: AI analysis skipped, daily budget exhausted")
        return

    client = GammaClient()
    try:
        book = await client.get_orderbook_levels(state.yes_token_id, depth=3)
        market = await client.get_market(state.condition_id)
    finally:
        await client.close()

    yes_price = GammaClient.parse_price(market, "YES") if market else 0.5
    baseline_price = state.model.get_baseline(state.session_id) or yes_price
    risk_summary = get_portfolio_risk().get_status_summary()

    now = time.time()
    open_trades = [
        {
            "side": t["side"],
            "size": t["size"],
            "entry_price": t["entry_price"],
            "minutes_ago": (now - t["opened_at"]) / 60.0,
        }
        for t in state.open_trades.values()
    ]

    from backend.football.odds_comparison import get_latest_odds
    odds = get_latest_odds(session_id)

    data = {
        "home_team": state.home_team,
        "away_team": state.away_team,
        "minute": state.last_minute,
        "home_score": state.last_home_score,
        "away_score": state.last_away_score,
        "match_status": state.last_status,
        "yes_price": yes_price,
        "baseline_price": baseline_price,
        "price_drift": yes_price - baseline_price,
        "spread": book["spread"],
        "depth_usd": book["depth_usd"],
        "order_book": book,
        "open_trades": open_trades,
        "capital": risk_summary["capital"]["current"],
        "initial_capital": risk_summary["capital"]["initial"],
        "total_trades": risk_summary["total_trades"],
        "win_rate": risk_summary["win_rate"],
        "condition_id": state.condition_id,
        "sportsbook_odds": odds,
    }

    analysis = await gemini.analyze_match(data)

    _latest[session_id] = {
        "session_id": session_id,
        "text": analysis.reasoning,
        "model": analysis.model_used,
        "timestamp": analysis.timestamp.isoformat(),
        "latency_ms": round(analysis.latency_ms, 0),
    }

    from backend.core.scheduler import log_event
    snippet = analysis.reasoning[:140] + ("..." if len(analysis.reasoning) > 140 else "")
    log_event("data", f"AI analysis (session #{session_id}): {snippet}", {
        "session_id": session_id,
    })


async def analysis_loop(session_id: int) -> None:
    from backend.football.session_manager import _pipelines

    while True:
        state = _pipelines.get(session_id)
        if not state or state.stopped:
            return

        try:
            await run_analysis(session_id, state)
        except Exception as e:
            logger.debug(f"Session {session_id}: AI analysis failed: {e}")

        await asyncio.sleep(ANALYSIS_INTERVAL_SECONDS)
