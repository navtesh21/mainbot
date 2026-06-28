"""Polymarket Gamma API client — market search, prices, metadata.

Ported from fball_bot/pm.py's GammaClient verbatim (pure HTTP, no
dependencies on the rest of fball_bot). CLOB order execution lives in
clob_trader.py (Step E), kept separate since it requires a wallet key.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

import httpx

logger = logging.getLogger("trading_bot")

GAMMA_BASE = "https://gamma-api.polymarket.com"


class GammaClient:
    """Lightweight Polymarket Gamma API client. 10 req/s rate limit (generous)."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._base = GAMMA_BASE

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            r = await self._http.get(
                f"{self._base}/markets",
                params={"title": query, "limit": limit, "closed": "false"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug(f"Gamma search '{query}' failed: {e}")
            return []

    async def get_market(self, condition_id: str) -> Dict[str, Any] | None:
        try:
            r = await self._http.get(
                f"{self._base}/markets",
                params={"condition_ids": condition_id, "limit": "1"},
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
        except Exception as e:
            logger.debug(f"Gamma market {condition_id} failed: {e}")
            return None

    async def get_event_by_slug(self, slug: str) -> List[Dict[str, Any]]:
        """Look up a Polymarket event (and its markets) by event slug."""
        try:
            r = await self._http.get(
                f"{self._base}/events",
                params={"slug": slug},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else [data]
        except Exception as e:
            logger.debug(f"Gamma event slug '{slug}' failed: {e}")
            return []

    async def get_orderbook_depth(self, token_id: str) -> float:
        """Total USD depth (bids + asks) for a token."""
        try:
            r = await self._http.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
            )
            r.raise_for_status()
            data = r.json()
            bids = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in data.get("bids", []))
            asks = sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in data.get("asks", []))
            return bids + asks
        except Exception as e:
            logger.debug(f"Orderbook depth {token_id}: {e}")
            return 0.0

    async def get_orderbook_levels(self, token_id: str, depth: int = 3) -> Dict[str, Any]:
        """Top-of-book bid/ask price levels plus spread and total USD depth, for display/AI analysis."""
        try:
            r = await self._http.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
            )
            r.raise_for_status()
            data = r.json()
            raw_bids = sorted(data.get("bids", []), key=lambda b: float(b.get("price", 0)), reverse=True)
            raw_asks = sorted(data.get("asks", []), key=lambda a: float(a.get("price", 0)))

            bids = [{"price": float(b.get("price", 0)), "size": float(b.get("size", 0))} for b in raw_bids[:depth]]
            asks = [{"price": float(a.get("price", 0)), "size": float(a.get("size", 0))} for a in raw_asks[:depth]]
            best_bid = bids[0]["price"] if bids else 0.0
            best_ask = asks[0]["price"] if asks else 0.0
            depth_usd = (
                sum(float(b.get("size", 0)) * float(b.get("price", 0)) for b in data.get("bids", []))
                + sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in data.get("asks", []))
            )

            return {
                "bids": bids,
                "asks": asks,
                "spread": round(best_ask - best_bid, 4) if best_bid and best_ask else 0.0,
                "depth_usd": depth_usd,
            }
        except Exception as e:
            logger.debug(f"Orderbook levels {token_id}: {e}")
            return {"bids": [], "asks": [], "spread": 0.0, "depth_usd": 0.0}

    @staticmethod
    def parse_price(market: Dict[str, Any], outcome: str = "YES") -> float:
        """Extract current price for YES or NO from market data."""
        if "tokens" in market:
            for t in market["tokens"]:
                o = (t.get("outcome", "") or "").lower()
                p = t.get("price", t.get("current_price", "0.5"))
                if isinstance(p, str):
                    try:
                        p = float(p)
                    except (ValueError, TypeError):
                        p = 0.5
                if o == outcome.lower():
                    return float(p)
        elif "outcomes" in market:
            try:
                raw_outcomes = market.get("outcomes", "[]")
                raw_prices = market.get("outcomePrices", "[]")
                if isinstance(raw_outcomes, str):
                    raw_outcomes = json.loads(raw_outcomes)
                if isinstance(raw_prices, str):
                    raw_prices = json.loads(raw_prices)

                outcomes = [str(o).lower() for o in raw_outcomes]
                prices = raw_prices
                for i, o in enumerate(outcomes):
                    if o == outcome.lower():
                        p = prices[i] if i < len(prices) else 0.5
                        try:
                            return float(p)
                        except (ValueError, TypeError):
                            return 0.5
            except Exception:
                pass
        return 0.5

    @staticmethod
    def extract_tokens(market: Dict[str, Any]) -> Tuple[str, str]:
        """Extract (yes_token_id, no_token_id) from market data."""
        yes_tok = no_tok = ""
        if "tokens" in market:
            for t in market["tokens"]:
                o = (t.get("outcome", "") or "").lower()
                tid = t.get("token_id", "")
                if o == "yes":
                    yes_tok = tid
                elif o == "no":
                    no_tok = tid
        elif "outcomes" in market:
            try:
                raw_outcomes = market.get("outcomes", "[]")
                raw_token_ids = market.get("clobTokenIds", "[]")
                if isinstance(raw_outcomes, str):
                    raw_outcomes = json.loads(raw_outcomes)
                if isinstance(raw_token_ids, str):
                    raw_token_ids = json.loads(raw_token_ids)

                outcomes = [str(o).lower() for o in raw_outcomes]
                token_ids = raw_token_ids
                for i, o in enumerate(outcomes):
                    tid = token_ids[i] if i < len(token_ids) else ""
                    if o == "yes":
                        yes_tok = tid
                    elif o == "no":
                        no_tok = tid
            except Exception:
                pass
        return yes_tok, no_tok
