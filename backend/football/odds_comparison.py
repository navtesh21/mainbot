"""Sportsbook odds comparison — the-odds-api.com vs Polymarket's implied price.

Sharp sportsbooks reprice faster and more accurately than a retail-driven
prediction market; comparing Polymarket's implied probability against a
consensus sportsbook line is a well-documented edge. The free tier (~500
credits/month) covers soccer_fifa_world_cup on all plans, but the budget is
small, so this is polled on its own slow cadence (ODDS_POLL_SECONDS), not
per signal.

No-ops entirely if ODDS_API_KEY isn't set, mirroring the FOOTBALL_API_KEY/
GEMINI_API_KEY pattern elsewhere in this codebase — get a free key at
https://the-odds-api.com/ and set ODDS_API_KEY in .env to enable.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from backend.config import settings
from backend.football.discovery import normalize

logger = logging.getLogger("trading_bot")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "soccer_fifa_world_cup"

_latest: Dict[int, Dict[str, Any]] = {}


def get_latest_odds(session_id: int) -> Optional[Dict[str, Any]]:
    return _latest.get(session_id)


def is_configured() -> bool:
    return bool(settings.ODDS_API_KEY)


async def _fetch_events() -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{ODDS_API_BASE}/sports/{SPORT_KEY}/odds/",
            params={
                "apiKey": settings.ODDS_API_KEY,
                "regions": "us,uk,eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
        )
        r.raise_for_status()
        return r.json()


def _implied_probs(event: Dict[str, Any]) -> Dict[str, float]:
    """Average decimal odds across bookmakers per outcome, then devig by
    normalizing so probabilities sum to 1 (the standard 1/odds method)."""
    sums: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for bm in event.get("bookmakers", []):
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "")
                price = outcome.get("price")
                if not price or price <= 1:
                    continue
                sums[name] = sums.get(name, 0.0) + (1.0 / price)
                counts[name] = counts.get(name, 0) + 1

    raw = {name: sums[name] / counts[name] for name in sums if counts.get(name)}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {name: prob / total for name, prob in raw.items()}


async def get_odds_comparison(
    home_team: str, away_team: str, polymarket_yes_price: float
) -> Optional[Dict[str, Any]]:
    """Find this match in the odds feed and compare the sportsbook-implied
    home-win probability against Polymarket's current YES price."""
    if not is_configured():
        return None

    try:
        events = await _fetch_events()
    except Exception as e:
        logger.debug(f"Odds comparison: fetch failed: {e}")
        return None

    h_norm, a_norm = normalize(home_team), normalize(away_team)
    for event in events:
        eh, ea = normalize(event.get("home_team", "")), normalize(event.get("away_team", ""))
        if {eh, ea} != {h_norm, a_norm}:
            continue

        probs = _implied_probs(event)
        sportsbook_prob = next((p for name, p in probs.items() if normalize(name) == h_norm), None)
        if sportsbook_prob is None:
            continue

        return {
            "sportsbook_prob": round(sportsbook_prob, 4),
            "polymarket_prob": round(polymarket_yes_price, 4),
            "edge": round(sportsbook_prob - polymarket_yes_price, 4),
            "bookmaker_count": len(event.get("bookmakers", [])),
            "fetched_at": time.time(),
        }
    return None


async def update_session_odds(session_id: int, home_team: str, away_team: str, polymarket_yes_price: float) -> None:
    comparison = await get_odds_comparison(home_team, away_team, polymarket_yes_price)
    if comparison:
        _latest[session_id] = comparison


async def odds_loop(session_id: int) -> None:
    import asyncio as _asyncio
    from backend.football.session_manager import _pipelines, _fetch_current_price

    if not is_configured():
        return  # don't even start polling if no key is set

    while True:
        state = _pipelines.get(session_id)
        if not state or state.stopped:
            return

        try:
            price = await _fetch_current_price(state.condition_id)
            await update_session_odds(session_id, state.home_team, state.away_team, price)

            cached = _latest.get(session_id)
            if cached:
                from backend.core.scheduler import log_event
                log_event("data", f"Odds comparison (session #{session_id}): sportsbook {cached['sportsbook_prob']:.1%} "
                          f"vs Polymarket {cached['polymarket_prob']:.1%} (edge {cached['edge']:+.1%}, "
                          f"{cached['bookmaker_count']} books)", {"session_id": session_id})
        except Exception as e:
            logger.debug(f"Session {session_id}: odds comparison failed: {e}")

        await _asyncio.sleep(settings.ODDS_POLL_SECONDS)
