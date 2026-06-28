"""CLOBTrader — real Polymarket CLOB order execution.

Ported verbatim from fball_bot/pm.py. Gated by FOOTBALL_TRADING_ENABLED +
WALLET_PRIVATE_KEY (both default off/unset) — see backend/config.py and the
dry-run/live branch in backend/football/session_manager.py::_execute_signal().
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("trading_bot")


class CLOBTrader:
    """Polymarket CLOB trader — places FOK market orders.

    Thin wrapper around py-clob-client. Requires a funded wallet's private key.
    """

    def __init__(self, private_key: str) -> None:
        self._key = private_key
        self._clob = None
        self._ready = False

    async def initialize(self) -> None:
        if self._ready:
            return
        try:
            from py_clob_client.client import PolyClob
            from py_clob_client.clob_types import OrderType

            self._clob = PolyClob(private_key=self._key, chain_id=137)
            self._address = self._clob.get_address()
            self._order_type = OrderType

            try:
                api_creds = self._clob.create_api_key()
                self._clob.set_api_credentials(api_creds)
            except Exception as e:
                logger.debug("API key creation (may already exist): %s", e)
                self._clob = PolyClob(private_key=self._key, chain_id=137)

            self._ready = True
            logger.info("CLOB ready for %s...%s", self._address[:6], self._address[-4:])
        except ImportError:
            logger.warning("py-clob-client not installed. Run: pip install py-clob-client")
            raise
        except Exception as e:
            logger.error("CLOB init failed: %s", e)
            raise

    async def place_market_order(
        self,
        token_id: str,
        amount: float,
        side: str,
        price: float = 0.50,
    ) -> dict[str, Any]:
        """Place a FOK market order. amount is USD; side is BUY/SELL."""
        if not self._ready:
            await self.initialize()
        if not self._clob:
            return {"success": False, "error": "CLOB not initialized"}

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            args = MarketOrderArgs(
                token_id=token_id,
                side=side.upper(),
                amount=str(amount),
                price=str(price),
                order_type=OrderType.FOK,
            )
            signed = self._clob.create_market_order(args)
            resp = self._clob.post_order(signed, OrderType.FOK)
            return {
                "success": True,
                "order_ids": resp.get("order_ids", [resp.get("id", "")]),
            }
        except Exception as e:
            logger.error("CLOB order failed: %s", e)
            return {"success": False, "error": str(e)}

    @property
    def address(self) -> str:
        return getattr(self, "_address", "unknown")

    @property
    def ready(self) -> bool:
        return self._ready


_clob_trader: CLOBTrader | None = None
_clob_trader_unavailable = False


def get_clob_trader() -> CLOBTrader | None:
    """Singleton CLOBTrader, or None if trading isn't enabled/configured.

    Returns None (never raises) when FOOTBALL_TRADING_ENABLED is off or no
    wallet key is set — callers must treat that as "fall back to dry-run."
    """
    global _clob_trader, _clob_trader_unavailable
    from backend.config import settings

    if not settings.FOOTBALL_TRADING_ENABLED or not settings.WALLET_PRIVATE_KEY:
        return None
    if _clob_trader_unavailable:
        return None
    if _clob_trader is None:
        try:
            _clob_trader = CLOBTrader(settings.WALLET_PRIVATE_KEY)
        except Exception:
            logger.exception("Failed to construct CLOBTrader")
            _clob_trader_unavailable = True
            return None
    return _clob_trader
