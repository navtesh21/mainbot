"""Crypto scalp engine — BTC 15m Up/Down markets on Polymarket.

Entry signal: compare current BTC price vs price_to_beat (BTC price at window
open, cached on first scan). Compute implied probability using a random-walk
model (BTC realized vol ~0.12% per 5m). Enter when implied prob > live token
price + MIN_EDGE, i.e. the market hasn't priced in the BTC move yet.

Exit: CLOB mid-price every 2s (fast loop) + 5s full scan. Stop at -4c,
profit at fair_value (implied prob at entry), timeout at 800s (full exit).
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from typing import Optional

import httpx

from backend.config import settings
from backend.data.btc_markets import BtcMarket, fetch_active_btc_markets, fetch_btc_market_for_settlement
from backend.crypto.clob_trader import get_crypto_clob_trader
from backend.crypto.risk import get_portfolio_risk, get_position_registry, get_rr_guard, kelly_size
from backend.crypto.scalping import compute_stop, evaluate_exit
from backend.football.pm_client import GammaClient
from backend.models.database import SessionLocal, Trade

logger = logging.getLogger("trading_bot")

DEFAULT_TRADE_SIZE_USD = 3.0

_BTC_5M_VOL = 0.0012     # BTC realized vol per 5m window (~0.12%)
_MIN_BTC_DELTA = 0.0008  # 0.08% move (~$48 on $60k) before considering entry
_MIN_EDGE = 0.06         # 6c minimum edge after CLOB ask — primary quality filter
_MAX_TOKEN_ENTRY = 0.80  # allow up to 80c; edge check (_MIN_EDGE) does real filtering
_MIN_CONSECUTIVE_SCANS = 1   # enter on first qualifying scan — CLOB lag is only 10-20s
_MAX_ENTRIES_PER_SCAN = 1    # one entry per tick — don't pile in

_window_price_to_beat: dict[str, float] = {}
_window_ptb_confirmed: set[str] = set()
_signal_streak: dict[str, tuple[str, int]] = {}

# Persisted to disk so re-entry on stopped windows is blocked across restarts
_STOPPED_SLUGS_PATH = os.path.join(os.path.dirname(__file__), "_data", "stopped_slugs.json")
_stopped_slugs: set[str] = set()

_open_positions: dict[str, dict] = {}


def _load_stopped_slugs() -> None:
    """Load stopped slugs from disk, pruning entries older than 24h."""
    global _stopped_slugs
    try:
        if not os.path.exists(_STOPPED_SLUGS_PATH):
            return
        with open(_STOPPED_SLUGS_PATH) as f:
            data = json.load(f)
        cutoff = time.time() - 86400
        _stopped_slugs = {slug for slug, ts in data.items() if ts > cutoff}
    except Exception as e:
        logger.debug(f"Could not load stopped_slugs: {e}")


def _save_stopped_slug(slug: str) -> None:
    """Persist a newly stopped slug so re-entry is blocked after restart."""
    _stopped_slugs.add(slug)
    try:
        existing: dict = {}
        if os.path.exists(_STOPPED_SLUGS_PATH):
            with open(_STOPPED_SLUGS_PATH) as f:
                existing = json.load(f)
        existing[slug] = time.time()
        with open(_STOPPED_SLUGS_PATH, "w") as f:
            json.dump(existing, f)
    except Exception as e:
        logger.debug(f"Could not save stopped_slugs: {e}")

_engine_running: bool = settings.CRYPTO_ENABLED

_load_stopped_slugs()


def is_engine_running() -> bool:
    return _engine_running


def set_engine_running(running: bool) -> tuple[bool, str]:
    global _engine_running
    if running and not settings.CRYPTO_ENABLED:
        return False, "CRYPTO_ENABLED is false in config — cannot start"
    _engine_running = running
    return True, ""


# ---------------------------------------------------------------------------
# BTC price + implied probability
# ---------------------------------------------------------------------------

async def _fetch_btc_price() -> Optional[float]:
    """Current BTC/USD spot from Coinbase Exchange (primary source).

    Coinbase is Chainlink's primary data source for BTC/USD on Ethereum mainnet,
    so it matches Polymarket's 'Price to Beat' much more closely than Binance
    (verified: Coinbase $60,291 vs Chainlink/Polymarket $60,284 vs Binance $60,373).
    Falls back to Binance if Coinbase is unavailable.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            r.raise_for_status()
            return float(r.json()["price"])
    except Exception:
        pass
    # Binance fallback
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
            )
            r.raise_for_status()
            return float(r.json()["price"])
    except Exception as e:
        logger.debug(f"BTC price fetch failed (both sources): {e}")
        return None


async def _fetch_btc_candle_open(start_unix_s: int) -> Optional[float]:
    """BTC/USD close of the 1m Coinbase candle ending at start_unix_s (prev minute).

    Uses the PREVIOUS minute's close, NOT the current minute's open, because:
    - Coinbase's first-trade-of-the-minute can spike $50-150 (stale limit fill)
      while other exchanges (Binance, Kraken, Gemini) stay flat — the spike is
      a Coinbase artifact, not reflected in Chainlink Data Streams median.
    - The previous candle's close IS the last agreed multi-exchange price and
      closely matches what Chainlink captures as PTB (verified: $10 diff vs
      $100+ diff when using spike-affected candle opens).
    - The previous candle is always complete and available immediately — no need
      to wait/retry for the new minute's candle to form.

    Falls back to Binance klines close if Coinbase is unavailable.
    """
    from datetime import datetime, timezone
    prev_start_dt = datetime.fromtimestamp(start_unix_s - 60, tz=timezone.utc)
    prev_end_dt = datetime.fromtimestamp(start_unix_s, tz=timezone.utc)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={
                    "granularity": 60,
                    "start": prev_start_dt.isoformat(),
                    "end": prev_end_dt.isoformat(),
                },
            )
            r.raise_for_status()
            candles = r.json()
            if candles:
                # format: [timestamp, low, high, open, close, volume]
                return float(candles[0][4])  # index 4 = close of previous minute
    except Exception:
        pass
    # Binance fallback: close of previous minute
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "startTime": (start_unix_s - 60) * 1000, "limit": 1},
            )
            r.raise_for_status()
            candles = r.json()
            if candles:
                return float(candles[0][4])  # index 4 = close price
    except Exception as e:
        logger.debug(f"BTC candle close fetch failed (both sources) for ts={start_unix_s}: {e}")
    return None


def _implied_prob(abs_delta_pct: float, time_remaining_s: float, window_s: float = 300.0) -> float:
    """P(BTC stays on the same side of price_to_beat at expiry).

    Uses a Brownian-motion model: the token should settle to this probability
    at expiry given the current BTC delta and remaining time uncertainty.
    Higher abs_delta or less time remaining → higher conviction → higher prob.
    """
    if time_remaining_s <= 1:
        return 0.99 if abs_delta_pct > 0.001 else 0.5
    # vol scales with sqrt(time_remaining) relative to the 5m baseline — NOT relative to
    # window_s. Using window_s caused 15m markets to use 5m vol (1.73x underestimate),
    # making fair_prob 10-13c too high and creating phantom edge.
    vol_remaining = _BTC_5M_VOL * math.sqrt(time_remaining_s / 300.0)
    if vol_remaining < 1e-7:
        return 0.99
    z = abs_delta_pct / vol_remaining
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Market fetch
# ---------------------------------------------------------------------------

async def _fetch_all_markets() -> list[BtcMarket]:
    markets: list[BtcMarket] = []
    try:
        markets.extend(await fetch_active_btc_markets(window="15m"))
    except Exception as e:
        logger.warning(f"Crypto engine: failed to fetch 15m markets: {e}")
    return markets


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

async def scan_and_scalp() -> None:
    if not settings.CRYPTO_ENABLED or not _engine_running:
        return

    from backend.core.scheduler import log_event

    try:
        markets = await _fetch_all_markets()
    except Exception as e:
        log_event("error", f"Crypto scan: failed to fetch markets — {e}")
        return

    markets_by_slug = {m.slug: m for m in markets}

    # 1) Exit checks on open positions (full: with market fallback)
    if _open_positions:
        gamma = GammaClient()
        try:
            for slug, position in list(_open_positions.items()):
                market = markets_by_slug.get(slug)
                if market is None:
                    market = await fetch_btc_market_for_settlement(slug)
                if market is None:
                    continue
                token_id = position.get("token_id")
                current_price = market.up_price if position["direction"] == "up" else market.down_price
                if token_id:
                    try:
                        book = await gamma.get_orderbook_levels(token_id)
                        bid = book["bids"][0]["price"] if book["bids"] else None
                        ask = book["asks"][0]["price"] if book["asks"] else None
                        if bid is not None and ask is not None:
                            current_price = (bid + ask) / 2
                        elif bid is not None:
                            current_price = bid
                        elif ask is not None:
                            current_price = ask
                    except Exception as clob_err:
                        logger.debug(f"CLOB price fetch failed for {slug}: {clob_err}")
                decision = evaluate_exit(position, current_price)
                if decision is not None:
                    await _execute_exit(slug, position, decision, current_price)
        finally:
            await gamma.close()

    # 2) Fetch current BTC price (one call for all entry decisions this tick)
    current_btc = await _fetch_btc_price()
    if current_btc is None:
        log_event("warning", "Crypto scan: Binance BTC price unavailable, skipping entries")
        return

    # 3) Cache price_to_beat for new windows using an accurate Binance kline.
    #
    # Polymarket slug format: btc-updown-5m-{UNIX_START} — the trailing timestamp
    # is the window START (verified: slug 1782549000 = 08:30 UTC, endDate 08:35 UTC).
    # Polymarket's startDate field is the series creation date, not the window start —
    # ignore it. endDate is accurate but we derive start = slug_ts directly.
    #
    # We only fetch the kline once the window has actually started: before that the
    # Binance candle at window_start doesn't exist yet and we'd always fall back to
    # the approximate current price, which would be permanently wrong.
    for market in markets:
        # Skip if we already have a confirmed kline PTB
        if market.slug in _window_ptb_confirmed or market.closed:
            continue
        try:
            slug_start_ts = int(market.slug.rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            continue
        if time.time() < slug_start_ts:
            continue  # window hasn't started yet; skip until it does
        kline_ptb = await _fetch_btc_candle_open(slug_start_ts)
        if kline_ptb is not None:
            ptb = kline_ptb
            _window_price_to_beat[market.slug] = ptb
            _window_ptb_confirmed.add(market.slug)
            # Also update any open position that was entered on an approx PTB
            if market.slug in _open_positions:
                _open_positions[market.slug]["price_to_beat"] = ptb
            log_event(
                "info",
                f"PTB (kline): ${ptb:,.2f} [BTC now ${current_btc:,.2f}] — {market.slug}",
                {"slug": market.slug, "ptb": ptb, "current_btc": current_btc, "source": "kline"},
            )
        elif market.slug not in _window_price_to_beat:
            # Kline not available yet (candle still forming) — use current price as
            # temporary fallback but DO NOT mark confirmed so we retry next scan.
            _window_price_to_beat[market.slug] = current_btc
            log_event(
                "info",
                f"PTB (approx, will retry): ${current_btc:,.2f} — {market.slug}",
                {"slug": market.slug, "ptb": current_btc, "current_btc": current_btc, "source": "approx"},
            )

    # 4) Score all candidate markets by edge = implied_prob - live_token_ask
    active_markets = [m for m in markets if m.is_active]
    log_event(
        "data",
        f"Crypto scan: {len(active_markets)} active markets, BTC ${current_btc:,.0f}",
        {"btc": current_btc, "active": len(active_markets), "open_positions": len(_open_positions)},
    )

    candidates: list[tuple[float, float, str, BtcMarket]] = []
    for market in markets:
        if market.slug in _open_positions or not market.is_active:
            continue
        if market.slug in _stopped_slugs:
            continue  # stop was hit on this window — don't re-enter

        ptb = _window_price_to_beat.get(market.slug)
        if ptb is None:
            continue

        btc_delta_pct = (current_btc - ptb) / ptb
        abs_delta = abs(btc_delta_pct)

        if abs_delta < _MIN_BTC_DELTA:
            logger.debug(f"[scan] {market.slug}: delta {abs_delta:.4%} < {_MIN_BTC_DELTA:.4%} threshold — skip")
            continue  # BTC hasn't moved enough from window open

        time_remaining = market.time_until_end
        min_remaining = (
            settings.CRYPTO_MIN_TIME_REMAINING_15M if market.window_minutes >= 15
            else settings.CRYPTO_MIN_TIME_REMAINING_5M
        )
        if time_remaining < min_remaining:
            continue

        direction = "down" if btc_delta_pct < 0 else "up"

        # Momentum confirmation: require the same BTC direction for
        # _MIN_CONSECUTIVE_SCANS ticks (24s) before entering. A spike that
        # reverses on the next scan won't satisfy this, so we avoid entering
        # and immediately seeing our thesis invalidated.
        prev_dir, streak = _signal_streak.get(market.slug, (direction, 0))
        if prev_dir == direction:
            new_streak = streak + 1
        else:
            new_streak = 1
        _signal_streak[market.slug] = (direction, new_streak)
        if new_streak < _MIN_CONSECUTIVE_SCANS:
            logger.debug(f"Streak {market.slug} {direction} {new_streak}/{_MIN_CONSECUTIVE_SCANS} — waiting")
            continue

        window_s = float(market.window_minutes * 60)
        fair_prob = _implied_prob(abs_delta, time_remaining, window_s)

        # Live token mid-price from Gamma (bestBid/bestAsk already parsed)
        token_mid = market.up_price if direction == "up" else market.down_price
        edge = fair_prob - token_mid

        if edge < _MIN_EDGE:
            continue
        if token_mid > _MAX_TOKEN_ENTRY:
            continue  # too late — most of move already priced in

        candidates.append((edge, fair_prob, direction, market))

    # 5) Enter the best opportunities this tick (sorted by edge, capped)
    candidates.sort(key=lambda x: x[0], reverse=True)
    entries_this_scan = 0
    for edge, fair_prob, direction, market in candidates:
        if entries_this_scan >= _MAX_ENTRIES_PER_SCAN:
            break
        try:
            entered = await _try_entry(market, direction, fair_prob, current_btc)
            if entered:
                entries_this_scan += 1
        except Exception as e:
            log_event("warning", f"Crypto entry exception ({market.slug}): {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Entry execution
# ---------------------------------------------------------------------------

async def _try_entry(
    market: BtcMarket,
    direction: str,
    fair_prob: float,
    btc_price: float,
) -> bool:
    """Place a dry-run/live entry. Returns True if an order was placed."""
    from backend.core.scheduler import log_event

    unrestricted = settings.CRYPTO_UNRESTRICTED and not settings.CRYPTO_TRADING_ENABLED

    risk = get_portfolio_risk()
    if not unrestricted:
        can_trade, block_reason = risk.can_trade(active_match_count=max(len(_open_positions), 1))
        if not can_trade:
            log_event("info", f"Crypto entry blocked ({market.slug}): {block_reason}")
            return False

    token_id = market.up_token_id if direction == "up" else market.down_token_id
    if not token_id:
        log_event("warning", f"Entry blocked ({market.slug}): {direction} token_id is empty — check Gamma clobTokenIds parsing")
        return False

    # Fetch CLOB book for the token we're buying (ask = actual cost to enter)
    gamma = GammaClient()
    try:
        book = await gamma.get_orderbook_levels(token_id)
    finally:
        await gamma.close()

    if book["depth_usd"] < settings.CRYPTO_MIN_LIQUIDITY_USD:
        log_event("info", f"Entry blocked ({market.slug}): low liquidity ${book['depth_usd']:.0f} < ${settings.CRYPTO_MIN_LIQUIDITY_USD:.0f}")
        return False

    best_bid = book["bids"][0]["price"] if book["bids"] else None
    best_ask = book["asks"][0]["price"] if book["asks"] else None
    bid_vol = book["bids"][0]["size"] if book["bids"] else 100.0
    ask_vol = book["asks"][0]["size"] if book["asks"] else 100.0

    if best_ask is None:
        log_event("info", f"Entry blocked ({market.slug}): no ask in CLOB orderbook")
        return False

    entry_price = best_ask  # real price to beat — what we actually pay

    if entry_price > settings.CRYPTO_MAX_ENTRY_PRICE:
        log_event("info", f"Entry blocked ({market.slug}): entry_price {entry_price:.3f} > max {settings.CRYPTO_MAX_ENTRY_PRICE:.3f}")
        return False

    # Re-check edge against actual ask (not Gamma mid) — always enforce, even in unrestricted mode
    actual_edge = fair_prob - entry_price
    if actual_edge < _MIN_EDGE:
        log_event("info", f"Entry blocked ({market.slug}): insufficient CLOB edge={actual_edge:.3f} (entry={entry_price:.3f} fair={fair_prob:.3f})")
        return False

    expected_reversion = max(0.01, actual_edge)
    target_price = min(0.99, fair_prob)
    stop_price = compute_stop(entry_price, "BUY", expected_reversion)

    rr = get_rr_guard().evaluate(
        entry_price=entry_price,
        target_price=target_price,
        stop_price=stop_price,
        side="BUY",
        best_bid=best_bid or entry_price * 0.98,
        best_ask=best_ask,
        bid_vol=bid_vol,
        ask_vol=ask_vol,
        trade_size=DEFAULT_TRADE_SIZE_USD,
        model_confidence=min(0.95, fair_prob),
    )
    if rr.action == "block" and not unrestricted:
        log_event("info", f"Crypto entry blocked ({market.slug}): R:R — {rr.reason}")
        return False

    # AI gate: ask Claude whether this candle pattern supports the momentum signal
    try:
        from backend.crypto.ai_predictor import predict_direction
        ai = await predict_direction(
            slug=market.slug,
            direction=direction,
            btc_price=btc_price,
            price_to_beat=_window_price_to_beat.get(market.slug, btc_price),
            time_remaining_s=market.time_until_end,
            fair_prob=fair_prob,
            token_ask=entry_price,
        )
        if ai.skip_entry:
            log_event("info", f"AI skipped {market.slug} {direction.upper()}: {ai.reasoning} (conf={ai.confidence:.2f})")
            return False
        log_event("info", f"AI confirmed {market.slug} {direction.upper()}: {ai.reasoning} (conf={ai.confidence:.2f})")
    except Exception as ai_err:
        logger.debug(f"AI gate error ({market.slug}): {ai_err} — proceeding without AI")

    ok, validate_reason = risk.validate_order(entry_price, DEFAULT_TRADE_SIZE_USD, token_price=entry_price)
    if not ok:
        return False

    reward_pct = abs(target_price - entry_price) / max(entry_price, 0.001)
    risk_pct = abs(entry_price - stop_price) / max(entry_price, 0.001)
    p_for_kelly = rr.p_adj if not unrestricted else max(rr.p_adj, fair_prob)
    kelly_usd = kelly_size(p_adj=p_for_kelly, reward_pct=reward_pct, risk_pct=risk_pct, capital=risk.s.current_capital)
    adj_size = risk.compute_max_trade_size(kelly_usd, fair_prob, token_price=entry_price)
    multiplier = 1.0 if unrestricted else rr.multiplier
    final_size = min(kelly_usd, adj_size) * multiplier
    if final_size <= 0:
        if unrestricted:
            final_size = min(settings.CRYPTO_RISK_MAX_PER_TRADE_USD * 0.3, risk.s.current_capital * 0.05)
        if final_size <= 0:
            return False

    registry = get_position_registry()
    can_open, pos_reason = registry.can_open(token_id, "BUY", final_size, risk.s.current_capital)
    if not can_open:
        log_event("info", f"Crypto entry blocked ({market.slug}): {pos_reason}")
        return False

    order_id = f"dryrun-{int(time.time() * 1000)}"
    mode = "DRY-RUN"
    trader = get_crypto_clob_trader()
    if trader is not None:
        result = await trader.place_market_order(token_id=token_id, amount=final_size, side="BUY", price=entry_price)
        if not result.get("success"):
            log_event("error", f"Crypto live entry failed ({market.slug}): {result.get('error')}")
            return False
        order_id = (result.get("order_ids") or [order_id])[0]
        mode = "LIVE"

    db = SessionLocal()
    try:
        trade = Trade(
            market_ticker=market.market_id,
            platform="polymarket",
            event_slug=market.slug,
            market_type="crypto_scalp",
            direction=direction,
            entry_price=entry_price,
            size=final_size,
            settled=False,
            result="pending",
            model_probability=fair_prob,
            market_price_at_entry=entry_price,
            edge_at_entry=actual_edge,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        trade_id = trade.id
    except Exception:
        logger.exception(f"Crypto: failed to persist trade row after {mode} order {order_id}")
        return False
    finally:
        db.close()

    ptb = _window_price_to_beat.get(market.slug, btc_price)
    btc_delta_usd = btc_price - ptb

    registry.open(token_id, "BUY", final_size)
    _open_positions[market.slug] = {
        "trade_id": trade_id,
        "token_id": token_id,
        "complement_token_id": market.down_token_id if direction == "up" else market.up_token_id,
        "side": "BUY",
        "direction": direction,
        "entry_price": entry_price,
        "size": final_size,
        "original_size": final_size,
        "stop_price": stop_price,
        "expected_reversion": expected_reversion,
        "opened_at": time.time(),
        "partial_exited": False,
        "window_minutes": market.window_minutes,
        "window_end_ts": market.window_end.timestamp(),
        "fair_value": fair_prob,
        "price_to_beat": ptb,
        "btc_price_at_entry": btc_price,
    }

    log_event(
        "trade",
        f"Crypto {mode} entry: BUY ${final_size:.2f} {direction.upper()} @ {entry_price:.3f} "
        f"fair={fair_prob:.3f} edge={actual_edge:+.3f} btc_delta=${btc_delta_usd:+.0f} — {market.slug}",
        {
            "slug": market.slug,
            "trade_id": trade_id,
            "direction": direction,
            "size": final_size,
            "price": entry_price,
            "fair_value": fair_prob,
            "btc_delta_usd": btc_delta_usd,
            "order_id": order_id,
            "mode": mode,
        },
    )
    return True


# ---------------------------------------------------------------------------
# Exit execution
# ---------------------------------------------------------------------------

async def _execute_exit(slug: str, position: dict, decision, exit_price: float) -> None:
    from backend.core.scheduler import log_event

    trade_id = position["trade_id"]
    entry_price = position["entry_price"]
    exit_size = position["size"] * decision.exit_fraction

    qty = exit_size / entry_price if entry_price > 0 else exit_size
    pnl = qty * (exit_price - entry_price)

    order_id = f"dryrun-exit-{int(time.time() * 1000)}"
    mode = "DRY-RUN"
    trader = get_crypto_clob_trader()
    if trader is not None:
        result = await trader.place_market_order(token_id=position["token_id"], amount=exit_size, side="SELL", price=exit_price)
        if not result.get("success"):
            log_event("error", f"Crypto live exit failed ({slug}, trade {trade_id}): {result.get('error')}")
            return
        order_id = (result.get("order_ids") or [order_id])[0]
        mode = "LIVE"

    is_partial = decision.kind == "partial"
    get_position_registry().close(position["token_id"], position["original_size"])

    db = SessionLocal()
    try:
        trade = db.query(Trade).filter(Trade.id == trade_id).first()
        if trade:
            trade.pnl = (trade.pnl or 0.0) + pnl
            if is_partial:
                position["size"] -= exit_size
                position["partial_exited"] = True
                # Move stop to entry after partial profit — locks in gain, remaining half is a free ride
                position["stop_price"] = position["entry_price"]
            else:
                trade.settled = True
                trade.settlement_time = datetime.utcnow()
                trade.settlement_value = exit_price
                trade.result = "win" if trade.pnl >= 0 else "loss"
            trade.exit_reason = decision.reason
            db.commit()
    except Exception:
        logger.exception(f"Crypto: failed to record exit for trade {trade_id}")
        return
    finally:
        db.close()

    risk_info = get_portfolio_risk().record_trade(pnl, entry_price)

    if not is_partial:
        _open_positions.pop(slug, None)
        # After a stop-loss, block re-entry on this window — repeated entries
        # against a market that disagrees with our PTB just compounds losses.
        if decision.kind == "stop":
            _save_stopped_slug(slug)

    log_event(
        "trade",
        f"Crypto {mode} exit: SELL ${exit_size:.2f} @ {exit_price:.3f} {decision.kind} "
        f"pnl=${pnl:+.4f} cap=${risk_info['capital']:.2f} — {decision.reason} — {slug}",
        {
            "slug": slug,
            "trade_id": trade_id,
            "kind": decision.kind,
            "pnl": pnl,
            "exit_price": exit_price,
            "order_id": order_id,
            "mode": mode,
        },
    )


# ---------------------------------------------------------------------------
# Fast exit loop (2s) — catches stop/profit between 12s scan ticks
# ---------------------------------------------------------------------------

async def _get_clob_mid(gamma: GammaClient, token_id: str) -> Optional[float]:
    try:
        book = await gamma.get_orderbook_levels(token_id)
        bid = book["bids"][0]["price"] if book["bids"] else None
        ask = book["asks"][0]["price"] if book["asks"] else None
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        if bid is not None:
            return bid
        if ask is not None:
            return ask
    except Exception:
        pass
    return None


async def _check_exits_fast() -> None:
    """CLOB-only exit check every 2s. Falls back to complement token when book is empty.

    Also dynamically updates fair_value based on current BTC vs price_to_beat:
    when BTC keeps moving our way, we raise the target so we capture more profit
    instead of exiting at the implied_prob computed at entry time. We only ever
    raise fair_value (never lower it) so that a short-term tick against us doesn't
    prematurely tighten our target — the stop at -3c handles the downside.
    """
    if not _open_positions:
        return

    current_btc = await _fetch_btc_price()

    gamma = GammaClient()
    try:
        for slug, position in list(_open_positions.items()):
            token_id = position.get("token_id")
            if not token_id:
                continue

            # Dynamic fair_value update — only when BTC is still on our side
            if current_btc is not None:
                ptb = position.get("price_to_beat")
                if ptb:
                    btc_delta = (current_btc - ptb) / ptb
                    direction = position["direction"]
                    directional_delta = btc_delta if direction == "up" else -btc_delta

                    if directional_delta > 0:
                        window_end_ts = position.get("window_end_ts")
                        if window_end_ts:
                            time_remaining = max(1.0, window_end_ts - time.time())
                        else:
                            elapsed = time.time() - position["opened_at"]
                            time_remaining = max(1.0, position.get("window_minutes", 5) * 60 - elapsed)

                        window_s = float(position.get("window_minutes", 5) * 60)
                        new_fv = _implied_prob(abs(btc_delta), time_remaining, window_s)
                        if new_fv > position.get("fair_value", 0.0):
                            position["fair_value"] = new_fv

            current_price = await _get_clob_mid(gamma, token_id)

            if current_price is None:
                comp_id = position.get("complement_token_id")
                if comp_id:
                    comp_mid = await _get_clob_mid(gamma, comp_id)
                    if comp_mid is not None:
                        current_price = 1.0 - comp_mid

            if current_price is None:
                continue

            decision = evaluate_exit(position, current_price)
            if decision is not None:
                await _execute_exit(slug, position, decision, current_price)
    finally:
        await gamma.close()


async def crypto_exit_job() -> None:
    if not settings.CRYPTO_ENABLED or not _engine_running:
        return
    try:
        await _check_exits_fast()
    except Exception as e:
        logger.debug(f"Crypto fast exit error: {e}")


async def settle_orphaned_trades() -> None:
    """Settle crypto_scalp trades from expired windows that were orphaned by a restart.

    On restart, _open_positions is empty so the exit loop never fires for old
    positions. This job runs once at startup and uses binary outcome (the window's
    actual UP/DOWN result from Polymarket) to settle them.
    """
    from backend.core.scheduler import log_event
    from backend.data.btc_markets import fetch_btc_market_for_settlement

    db = SessionLocal()
    try:
        now_ts = time.time()
        orphans = db.query(Trade).filter(
            Trade.market_type == "crypto_scalp",
            Trade.settled == False,
        ).all()

        if not orphans:
            return

        # Only settle orphans from the last 4 hours — older ones are history
        # and making 50+ Gamma API calls on startup blocks the event loop.
        cutoff_ts = now_ts - 4 * 3600
        recent_orphans = [
            t for t in orphans
            if t.timestamp and t.timestamp.timestamp() > cutoff_ts
        ]
        if not recent_orphans:
            return

        log_event("info", f"Startup: checking {len(recent_orphans)} recent orphaned crypto scalp trades")
        settled_count = 0

        for trade in recent_orphans:
            try:
                slug_ts = int(trade.event_slug.rsplit("-", 1)[-1])
                win_min = 15 if "-15m-" in trade.event_slug else 5
                window_end_ts = slug_ts + win_min * 60
            except (ValueError, IndexError):
                continue

            if now_ts < window_end_ts + 30:
                continue  # window hasn't fully resolved yet

            market = await fetch_btc_market_for_settlement(trade.event_slug)
            if market is None or not market.closed:
                continue

            # Infer binary result: UP token settles to 1.0 if UP won, 0.0 if DOWN won
            # market.up_price post-resolution ≈ 1.0 or ≈ 0.0
            up_won = market.up_price >= 0.95
            down_won = market.down_price >= 0.95

            if not (up_won or down_won):
                continue  # ambiguous

            trade_won = (trade.direction == "up" and up_won) or (trade.direction == "down" and down_won)
            entry_price = trade.entry_price or 0.5
            qty = (trade.size or 1.0) / max(entry_price, 0.01)
            settlement_price = 1.0 if trade_won else 0.0
            pnl = qty * (settlement_price - entry_price)

            trade.pnl = pnl
            trade.settled = True
            trade.result = "win" if trade_won else "loss"
            trade.settlement_value = settlement_price
            trade.settlement_time = datetime.utcnow()
            trade.exit_reason = "binary_settlement"
            settled_count += 1

        if settled_count:
            db.commit()
            log_event("info", f"Startup: settled {settled_count} orphaned crypto scalp trades")

    except Exception as e:
        logger.warning(f"settle_orphaned_trades failed: {e}")
    finally:
        db.close()


async def crypto_scan_job() -> None:
    from backend.core.scheduler import log_event
    if not settings.CRYPTO_ENABLED or not _engine_running:
        return
    try:
        await scan_and_scalp()
    except Exception as e:
        log_event("error", f"Crypto scan error: {e}")
        logger.exception("Error in crypto_scan_job")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def get_engine_status() -> dict:
    risk = get_portfolio_risk()
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    db = SessionLocal()
    try:
        today_pnl = db.query(Trade).filter(
            Trade.market_type == "crypto_scalp",
            Trade.settled == True,
            Trade.settlement_time >= today_start,
        ).all()
        today_pnl_sum = sum(t.pnl or 0.0 for t in today_pnl)
        total_trades = db.query(Trade).filter(Trade.market_type == "crypto_scalp").count()
    finally:
        db.close()

    return {
        "enabled": settings.CRYPTO_ENABLED,
        "running": _engine_running,
        "trading_live": bool(settings.CRYPTO_TRADING_ENABLED and settings.WALLET_PRIVATE_KEY),
        "open_positions": [
            {"slug": slug, **{k: v for k, v in pos.items() if k not in ("token_id",)}}
            for slug, pos in _open_positions.items()
        ],
        "today_pnl": round(today_pnl_sum, 4),
        "total_trades": total_trades,
        "risk": risk.get_status_summary(),
    }
