"""Crypto scalp engine's CLOB trader singleton.

Reuses the CLOBTrader class from backend.football.clob_trader as-is — it's a
pure py-clob-client wrapper with zero football-specific logic, so there is
nothing to fork. This module only adds an independent singleton + gate so
crypto trading can be turned on/off (CRYPTO_TRADING_ENABLED) without
touching football's FOOTBALL_TRADING_ENABLED gate.
"""
from __future__ import annotations

import logging

from backend.football.clob_trader import CLOBTrader

logger = logging.getLogger("trading_bot")

_crypto_clob_trader: CLOBTrader | None = None
_crypto_clob_trader_unavailable = False


def get_crypto_clob_trader() -> CLOBTrader | None:
    """Singleton CLOBTrader for crypto, or None if trading isn't enabled/configured.

    Returns None (never raises) when CRYPTO_TRADING_ENABLED is off or no
    wallet key is set — callers must treat that as "fall back to dry-run."
    """
    global _crypto_clob_trader, _crypto_clob_trader_unavailable
    from backend.config import settings

    if not settings.CRYPTO_TRADING_ENABLED or not settings.WALLET_PRIVATE_KEY:
        return None
    if _crypto_clob_trader_unavailable:
        return None
    if _crypto_clob_trader is None:
        try:
            _crypto_clob_trader = CLOBTrader(settings.WALLET_PRIVATE_KEY)
        except Exception:
            logger.exception("Failed to construct crypto CLOBTrader")
            _crypto_clob_trader_unavailable = True
            return None
    return _crypto_clob_trader
