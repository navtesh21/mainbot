"""Whale consensus scan — finds sports markets where multiple of Polymarket's
top traders are positioned on the same side.

Uses Polymarket's public, unauthenticated Data API (data-api.polymarket.com):
- /v1/leaderboard — top traders by PnL for a category/time period.
- /positions — a wallet's current open positions.

This is a read-only, on-demand advisory signal (triggered by the dashboard's
"Scan" button) — it does not feed into the automated entry/exit pipeline in
session_manager.py. Wiring whale consensus into actual trade triggers would
be a separate, deliberate decision since it changes what causes real (paper)
money to move.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("trading_bot")

DATA_API_BASE = "https://data-api.polymarket.com"
LEADERBOARD_LIMIT = 20
LEADERBOARD_CATEGORY = "SPORTS"
LEADERBOARD_FETCH_LIMIT = 50  # fetch more than 20 per period so filtering has room to work
LEADERBOARD_PERIODS = ["WEEK", "MONTH", "ALL"]
MIN_PERIODS_PRESENT = 2  # must rank in 2+ of the 3 periods — filters out one-week lucky spikes
MIN_CONSENSUS_TRADERS = 3
MAX_RESULTS = 15
POSITIONS_CONCURRENCY = 5


@dataclass
class TraderPosition:
    name: str
    wallet: str
    value_usd: float
    pnl_usd: float
    pnl_pct: float


@dataclass
class ConsensusTrade:
    condition_id: str
    market_title: str
    market_slug: str
    outcome: str
    trader_count: int
    traders: List[Dict[str, Any]]
    total_value_usd: float
    avg_price: float


_last_scan: Dict[str, Any] = {"trades": [], "scanned_at": None, "trader_count": 0}


def get_last_scan() -> Dict[str, Any]:
    return _last_scan


async def _fetch_leaderboard_period(client: httpx.AsyncClient, period: str) -> List[Dict[str, Any]]:
    r = await client.get(
        f"{DATA_API_BASE}/v1/leaderboard",
        params={
            "category": LEADERBOARD_CATEGORY,
            "timePeriod": period,
            "orderBy": "PNL",
            "limit": LEADERBOARD_FETCH_LIMIT,
        },
    )
    r.raise_for_status()
    return r.json()


async def _fetch_consistent_traders(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """Top sports traders, filtered for consistency rather than raw one-period PnL.

    Fetches WEEK/MONTH/ALL leaderboards in parallel, keeps only wallets that
    rank in the top LEADERBOARD_FETCH_LIMIT of at least MIN_PERIODS_PRESENT of
    the three periods (filters out a single lucky week that doesn't show up
    in their month/all-time record), then scores survivors by summed
    reciprocal rank (a standard rank-aggregation technique: appearing near
    #1 in multiple periods beats one big #1 in a single period) and returns
    the top LEADERBOARD_LIMIT.
    """
    period_results = await asyncio.gather(
        *[_fetch_leaderboard_period(client, p) for p in LEADERBOARD_PERIODS]
    )

    by_wallet: Dict[str, Dict[str, Any]] = {}
    for period, entries in zip(LEADERBOARD_PERIODS, period_results):
        for entry in entries:
            wallet = entry["proxyWallet"]
            if wallet not in by_wallet:
                by_wallet[wallet] = {
                    "proxyWallet": wallet,
                    "userName": entry.get("userName") or wallet[:10],
                    "periods": {},
                }
            by_wallet[wallet]["periods"][period] = {
                "rank": int(entry["rank"]),
                "pnl": entry.get("pnl", 0),
            }

    consistent = [t for t in by_wallet.values() if len(t["periods"]) >= MIN_PERIODS_PRESENT]

    def score(t: Dict[str, Any]) -> float:
        return sum(1.0 / p["rank"] for p in t["periods"].values())

    consistent.sort(key=score, reverse=True)
    return consistent[:LEADERBOARD_LIMIT]


async def _fetch_positions(client: httpx.AsyncClient, wallet: str, sem: asyncio.Semaphore) -> List[Dict[str, Any]]:
    async with sem:
        try:
            r = await client.get(
                f"{DATA_API_BASE}/positions",
                params={"user": wallet, "limit": 50, "sizeThreshold": 1, "sortBy": "CURRENT", "sortDirection": "DESC"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug(f"Whale scan: positions fetch failed for {wallet[:10]}: {e}")
            return []


async def find_consensus_trades(
    min_traders: int = MIN_CONSENSUS_TRADERS,
    max_results: int = MAX_RESULTS,
) -> Dict[str, Any]:
    """Scan the top sports traders' open positions and surface markets where
    several of them are on the same side — a crowd-of-whales consensus signal."""
    async with httpx.AsyncClient(timeout=15) as client:
        traders = await _fetch_consistent_traders(client)

        sem = asyncio.Semaphore(POSITIONS_CONCURRENCY)
        all_positions = await asyncio.gather(
            *[_fetch_positions(client, t["proxyWallet"], sem) for t in traders]
        )

    wallet_to_name = {
        t["proxyWallet"]: (t.get("userName") or t["proxyWallet"][:10]) for t in traders
    }

    groups: Dict[tuple, Dict[str, Any]] = {}
    for wallet_positions in all_positions:
        for pos in wallet_positions:
            key = (pos.get("conditionId"), pos.get("outcome"))
            if key not in groups:
                groups[key] = {
                    "condition_id": pos.get("conditionId", ""),
                    "market_title": pos.get("title", ""),
                    "market_slug": pos.get("eventSlug", "") or pos.get("slug", ""),
                    "outcome": pos.get("outcome", ""),
                    "traders": [],
                    "total_value_usd": 0.0,
                    "price_sum": 0.0,
                }
            g = groups[key]
            value_usd = float(pos.get("currentValue", 0) or 0)
            g["traders"].append(TraderPosition(
                name=wallet_to_name.get(pos["proxyWallet"], pos["proxyWallet"][:10]),
                wallet=pos["proxyWallet"],
                value_usd=round(value_usd, 2),
                pnl_usd=round(float(pos.get("cashPnl", 0) or 0), 2),
                pnl_pct=round(float(pos.get("percentPnl", 0) or 0), 2),
            ).__dict__)
            g["total_value_usd"] += value_usd
            g["price_sum"] += float(pos.get("curPrice", 0) or 0)

    consensus = []
    for g in groups.values():
        count = len(g["traders"])
        if count < min_traders:
            continue
        traders_sorted = sorted(g["traders"], key=lambda t: t["value_usd"], reverse=True)
        consensus.append(ConsensusTrade(
            condition_id=g["condition_id"],
            market_title=g["market_title"],
            market_slug=g["market_slug"],
            outcome=g["outcome"],
            trader_count=count,
            traders=traders_sorted,
            total_value_usd=round(g["total_value_usd"], 2),
            avg_price=round(g["price_sum"] / count, 4),
        ))

    consensus.sort(key=lambda c: (c.trader_count, c.total_value_usd), reverse=True)
    consensus = consensus[:max_results]

    result = {
        "trades": [c.__dict__ for c in consensus],
        "scanned_at": time.time(),
        "trader_count": len(traders),
    }
    _last_scan.update(result)
    return result
