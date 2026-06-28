"""BTC 5-minute market fetcher for Polymarket."""
import httpx
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional, List
from dataclasses import dataclass

logger = logging.getLogger("trading_bot")

GAMMA_API = "https://gamma-api.polymarket.com"
SERIES_SLUG = "btc-up-or-down-5m"

# Window-length config: seconds per window + Polymarket series slug, keyed by
# the short window label used throughout this module's public API.
WINDOW_SECONDS = {"5m": 300, "15m": 900}
SERIES_SLUG_BY_WINDOW = {"5m": "btc-up-or-down-5m", "15m": "btc-up-or-down-15m"}

# Strict regexes: only match real BTC window slugs (e.g. btc-updown-5m-1708531200)
_BTC_SLUG_RE_BY_WINDOW = {
    "5m": re.compile(r"^btc-updown-5m-\d{10}$"),
    "15m": re.compile(r"^btc-updown-15m-\d{10}$"),
}


def is_valid_btc_slug(slug: str, window: str = "5m") -> bool:
    """Return True only if slug matches the exact BTC window pattern."""
    pattern = _BTC_SLUG_RE_BY_WINDOW.get(window)
    return bool(pattern and pattern.match(slug))


@dataclass
class BtcMarket:
    """A single BTC Up/Down market (5-min or 15-min window)."""
    slug: str
    market_id: str
    up_price: float
    down_price: float
    window_start: datetime
    window_end: datetime
    volume: float
    closed: bool
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    price_to_beat: Optional[float] = None  # BTC price at window open (from Polymarket)

    @property
    def event_slug(self) -> str:
        return self.slug

    @property
    def spread(self) -> float:
        return abs(1.0 - self.up_price - self.down_price)

    @property
    def time_until_end(self) -> float:
        """Seconds until this window ends."""
        now = datetime.now(timezone.utc)
        return (self.window_end - now).total_seconds()

    @property
    def is_active(self) -> bool:
        """Window is currently in progress."""
        now = datetime.now(timezone.utc)
        return self.window_start <= now <= self.window_end and not self.closed

    @property
    def is_upcoming(self) -> bool:
        """Window hasn't started yet."""
        now = datetime.now(timezone.utc)
        return now < self.window_start and not self.closed

    @property
    def window_minutes(self) -> int:
        """Window length in minutes, parsed from the slug (e.g. ...-15m-... -> 15).

        Not derived from window_start/window_end: the Gamma event's startDate
        is the recurring series' creation date, not this slot's start, so the
        start/end delta is unreliable for this. The slug's window-size token
        is always accurate.
        """
        m = re.search(r"-(\d+)m-\d+$", self.slug)
        return int(m.group(1)) if m else 5


def _round_to_window(ts: float, window_seconds: int) -> int:
    """Round a unix timestamp down to the nearest window boundary."""
    return int(ts) // window_seconds * window_seconds


def _compute_window_slugs(count: int = 5, window: str = "5m") -> List[str]:
    """
    Compute event slugs for the current window and upcoming windows.

    Slug pattern: btc-updown-{window}-{unix_start_timestamp}
    The trailing number is the window START time (verified: slug 1782549300 =
    08:35 UTC start; endDate = 08:40 UTC). The old variable was named 'end_ts'
    but that was wrong — it's the start.

    We start from current_boundary (the current window's start) so we always
    include the window that is actively running right now.
    """
    window_seconds = WINDOW_SECONDS[window]
    now = time.time()
    current_boundary = _round_to_window(now, window_seconds)

    slugs = []
    for i in range(count):
        start_ts = current_boundary + (i * window_seconds)
        slugs.append(f"btc-updown-{window}-{start_ts}")

    return slugs


def _parse_event_to_btc_market(event: dict) -> Optional[BtcMarket]:
    """Parse a Polymarket event into a BtcMarket."""
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]

    # Live CLOB prices — Gamma embeds bestBid/bestAsk for the UP token
    # directly in the event response, updated continuously. Use these instead
    # of outcomePrices which are stale aggregate snapshots.
    best_bid = market.get("bestBid")   # UP token bid (what buyers pay)
    best_ask = market.get("bestAsk")   # UP token ask (what sellers receive)
    up_price = 0.5
    down_price = 0.5
    if best_bid is not None and best_ask is not None:
        try:
            up_price = (float(best_bid) + float(best_ask)) / 2  # UP mid
            down_price = 1.0 - up_price                          # DOWN mid (binary complement)
        except (ValueError, TypeError):
            pass
    else:
        # Fall back to outcomePrices if CLOB fields are absent
        outcome_prices = market.get("outcomePrices", "")
        if outcome_prices:
            try:
                prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                if isinstance(prices, list) and len(prices) >= 2:
                    up_price = float(prices[0])
                    down_price = float(prices[1])
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    # Parse timestamps
    slug = event.get("slug", "")
    start_str = event.get("startDate") or market.get("startDate")
    end_str = event.get("endDate") or market.get("endDate")

    window_start = datetime.now(timezone.utc)
    window_end = datetime.now(timezone.utc)

    if start_str:
        try:
            window_start = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    if end_str:
        try:
            window_end = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    # CLOB token ids — needed to place/inspect orders for this market's Up/Down
    # tokens; order matches outcomePrices ([Up, Down]).
    up_token_id = ""
    down_token_id = ""
    clob_token_ids = market.get("clobTokenIds", "")
    if clob_token_ids:
        try:
            ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
            if isinstance(ids, list) and len(ids) >= 2:
                up_token_id = str(ids[0])
                down_token_id = str(ids[1])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Price to beat: BTC's reference price at window open, shown by Polymarket
    # as "Price to Beat". Parse from the market question or event description
    # (e.g. "Will Bitcoin be above $59,199.19 at 9:00 AM ET?").
    price_to_beat: Optional[float] = None
    for text in [
        market.get("question", "") or "",
        market.get("title", "") or "",
        event.get("description", "") or "",
        event.get("title", "") or "",
    ]:
        match = re.search(r'\$([0-9,]+(?:\.\d{1,2})?)', text)
        if match:
            try:
                price_to_beat = float(match.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    return BtcMarket(
        slug=slug,
        market_id=str(market.get("id", "")),
        up_price=up_price,
        down_price=down_price,
        window_start=window_start,
        window_end=window_end,
        volume=float(market.get("volume", 0) or 0),
        closed=bool(market.get("closed", False) or event.get("closed", False)),
        condition_id=str(market.get("conditionId", "")),
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        price_to_beat=price_to_beat,
    )


async def fetch_btc_market_by_slug(slug: str, window: str = "5m") -> Optional[BtcMarket]:
    """Fetch a single BTC market by its event slug."""
    if not is_valid_btc_slug(slug, window):
        logger.debug(f"Rejected invalid BTC slug: {slug}")
        return None

    url = f"{GAMMA_API}/events"
    params = {"slug": slug}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            events = response.json()

            if not events:
                return None

            event = events[0] if isinstance(events, list) else events
            return _parse_event_to_btc_market(event)

        except Exception as e:
            logger.debug(f"Failed to fetch BTC market {slug}: {e}")
            return None


async def fetch_active_btc_markets(window: str = "5m") -> List[BtcMarket]:
    """
    Fetch current and upcoming BTC markets of the given window size from Polymarket.

    Strategy: compute expected slugs from current time and fetch them,
    plus do a series search as fallback.
    """
    markets: List[BtcMarket] = []
    seen_slugs = set()
    slug_prefix = f"btc-updown-{window}"

    # Method 1: Compute expected slugs and fetch directly
    expected_slugs = _compute_window_slugs(count=6, window=window)
    for slug in expected_slugs:
        market = await fetch_btc_market_by_slug(slug, window=window)
        if market and market.slug not in seen_slugs:
            seen_slugs.add(market.slug)
            markets.append(market)

    # Method 2 removed: the Gamma API ignores the slug_contains param and returns
    # unrelated events. Method 1 now starts from current_boundary so the active
    # window is always included in the slug list.

    # Sort by window end time (soonest first)
    markets.sort(key=lambda m: m.window_end)

    # Filter out already-closed markets
    markets = [m for m in markets if not m.closed]

    logger.info(f"Fetched {len(markets)} active BTC {window} markets")
    return markets


async def fetch_btc_market_for_settlement(slug: str) -> Optional[BtcMarket]:
    """
    Fetch a BTC market for settlement purposes (includes closed markets).
    """
    url = f"{GAMMA_API}/events"
    params = {"slug": slug}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            events = response.json()

            if not events:
                return None

            event = events[0] if isinstance(events, list) else events
            return _parse_event_to_btc_market(event)

        except Exception as e:
            logger.warning(f"Failed to fetch BTC market for settlement {slug}: {e}")
            return None


if __name__ == "__main__":
    import asyncio

    async def test():
        for window in ("5m", "15m"):
            print(f"\nFetching active BTC {window} markets...")
            markets = await fetch_active_btc_markets(window=window)
            print(f"Found {len(markets)} markets")

            for m in markets:
                print(f"\n  {m.slug}")
                print(f"  Up: {m.up_price:.2%} | Down: {m.down_price:.2%}")
                print(f"  Window: {m.window_start} -> {m.window_end} ({m.window_minutes}m)")
                print(f"  Volume: ${m.volume:,.0f}")
                print(f"  Active: {m.is_active} | Upcoming: {m.is_upcoming}")

    asyncio.run(test())
