"""Claude-powered BTC direction predictor for 15m Polymarket windows.

Before entering a trade, this module fetches the last 30 BTC 1m candles,
computes key indicators, and asks Claude to judge whether the current BTC
move from PTB looks like real continuation (enter) or a fake spike (skip).

This replaces the pure Brownian-motion momentum model with AI pattern
recognition. Claude can distinguish trending moves from noise spikes in a
way the simple vol model cannot.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("trading_bot")

# Rate-limit: one AI call per market per window
_last_call: dict[str, float] = {}
_AI_COOLDOWN_S = 60.0


@dataclass
class AIPrediction:
    direction: str        # "up", "down", or "skip"
    confidence: float     # 0.0 - 1.0
    reasoning: str        # brief explanation from Claude
    skip_entry: bool      # True = don't trade this signal


async def _fetch_btc_candles_1m(n: int = 30) -> list[dict]:
    """Fetch last N 1m BTC/USD candles from Coinbase (falls back to Binance)."""
    now = int(time.time())
    start = now - n * 60
    try:
        from datetime import datetime, timezone
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
        end_dt = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"granularity": 60, "start": start_dt, "end": end_dt},
            )
            r.raise_for_status()
            # [timestamp, low, high, open, close, volume]
            raw = r.json()
            raw.sort(key=lambda c: c[0])
            return [
                {"t": c[0], "o": c[3], "h": c[2], "l": c[1], "c": c[4], "v": c[5]}
                for c in raw
            ]
    except Exception:
        pass

    # Binance fallback
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": n},
            )
            r.raise_for_status()
            return [
                {"t": int(c[0]) // 1000, "o": float(c[1]), "h": float(c[2]),
                 "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
                for c in r.json()
            ]
    except Exception as e:
        logger.debug(f"AI predictor: candle fetch failed: {e}")
        return []


def _compute_indicators(candles: list[dict]) -> dict:
    """RSI(14), momentum(5), vol, VWAP deviation, consecutive directional candles."""
    if len(candles) < 5:
        return {}

    closes = [c["c"] for c in candles]
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    vols = [c["v"] for c in candles]

    # RSI(14)
    rsi = None
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, 15):
            d = closes[-15 + i] - closes[-15 + i - 1]
            (gains if d >= 0 else losses).append(abs(d))
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

    # 5-bar momentum %
    momentum_5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0

    # 1-bar momentum %
    momentum_1 = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

    # Realized vol (std of 1m returns, last 15 bars)
    if len(closes) >= 16:
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-15, 0)]
        mean_r = sum(returns) / len(returns)
        vol_1m = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns)) * 100
    else:
        vol_1m = 0

    # VWAP of last 15 bars
    recent = candles[-15:]
    tp_vol = sum(((c["h"] + c["l"] + c["c"]) / 3) * c["v"] for c in recent)
    total_vol = sum(c["v"] for c in recent)
    vwap = tp_vol / total_vol if total_vol > 0 else closes[-1]
    vwap_dev_pct = (closes[-1] - vwap) / vwap * 100

    # Consecutive directional 1m candles
    streak = 0
    for i in range(len(candles) - 1, 0, -1):
        if candles[i]["c"] > candles[i]["o"] and (streak >= 0):
            streak += 1
        elif candles[i]["c"] < candles[i]["o"] and (streak <= 0):
            streak -= 1
        else:
            break

    # Volume trend: last 5 bars vs prior 5 bars
    vol_ratio = (sum(vols[-5:]) / sum(vols[-10:-5])) if len(vols) >= 10 and sum(vols[-10:-5]) > 0 else 1.0

    return {
        "rsi": round(rsi, 1) if rsi else None,
        "momentum_5m_pct": round(momentum_5, 4),
        "momentum_1m_pct": round(momentum_1, 4),
        "vol_1m_pct": round(vol_1m, 4),
        "vwap_dev_pct": round(vwap_dev_pct, 4),
        "consecutive_directional_bars": streak,
        "vol_trend_ratio": round(vol_ratio, 2),
    }


def _format_candles_for_claude(candles: list[dict], indicators: dict) -> str:
    """Format candle data as compact text Claude can read quickly."""
    lines = ["Last 15 one-minute BTC/USD candles (oldest → newest):"]
    lines.append("  idx | open      | high      | low       | close     | vol")
    for i, c in enumerate(candles[-15:]):
        arrow = "↑" if c["c"] >= c["o"] else "↓"
        lines.append(
            f"  {i+1:3d} | {c['o']:9.2f} | {c['h']:9.2f} | {c['l']:9.2f} | {c['c']:9.2f} {arrow}| {c['v']:8.4f}"
        )

    lines.append("\nKey indicators:")
    if indicators.get("rsi"):
        lines.append(f"  RSI(14): {indicators['rsi']:.1f}")
    lines.append(f"  5m momentum: {indicators.get('momentum_5m_pct', 0):+.4f}%")
    lines.append(f"  Last 1m move: {indicators.get('momentum_1m_pct', 0):+.4f}%")
    lines.append(f"  Realized vol (15m): {indicators.get('vol_1m_pct', 0):.4f}% per bar")
    lines.append(f"  VWAP deviation: {indicators.get('vwap_dev_pct', 0):+.4f}%")
    lines.append(f"  Consecutive directional bars: {indicators.get('consecutive_directional_bars', 0)}")
    lines.append(f"  Volume trend (last 5 vs prior 5): {indicators.get('vol_trend_ratio', 1):.2f}x")

    return "\n".join(lines)


async def predict_direction(
    slug: str,
    direction: str,
    btc_price: float,
    price_to_beat: float,
    time_remaining_s: float,
    fair_prob: float,
    token_ask: float,
) -> AIPrediction:
    """Ask Claude whether to enter this trade.

    Returns AIPrediction with skip_entry=True if Claude thinks the move
    looks like noise/reversal rather than real continuation.
    """
    from backend.config import settings

    if not settings.GEMINI_API_KEY:
        return AIPrediction(
            direction=direction,
            confidence=0.5,
            reasoning="No GEMINI_API_KEY — AI filter skipped",
            skip_entry=False,
        )

    # Rate limit: one call per slug per minute
    now = time.time()
    if now - _last_call.get(slug, 0) < _AI_COOLDOWN_S:
        return AIPrediction(
            direction=direction,
            confidence=0.5,
            reasoning="AI rate-limited — using momentum signal",
            skip_entry=False,
        )
    _last_call[slug] = now

    candles = await _fetch_btc_candles_1m(30)
    if len(candles) < 10:
        return AIPrediction(
            direction=direction,
            confidence=0.5,
            reasoning="Candle fetch failed — using momentum signal",
            skip_entry=False,
        )

    indicators = _compute_indicators(candles)
    candle_text = _format_candles_for_claude(candles, indicators)

    btc_delta_pct = (btc_price - price_to_beat) / price_to_beat * 100
    btc_delta_usd = btc_price - price_to_beat

    prompt = f"""You are analyzing a Polymarket BTC Up/Down binary market window.

MARKET CONTEXT:
- Window type: 15-minute binary (BTC vs price-to-beat at window start)
- Price to beat (PTB): ${price_to_beat:,.2f}
- Current BTC price: ${btc_price:,.2f}
- BTC delta from PTB: {btc_delta_pct:+.3f}% (${btc_delta_usd:+.0f})
- Direction we want to enter: {direction.upper()}
- Time remaining in window: {time_remaining_s:.0f}s ({time_remaining_s/60:.1f} min)
- Momentum model fair probability: {fair_prob:.3f} ({fair_prob*100:.1f}%)
- Current CLOB ask for {direction.upper()} token: {token_ask:.3f}c
- Model edge: {fair_prob - token_ask:+.3f}

{candle_text}

QUESTION:
Given the BTC candle data and indicators above, does this {direction.upper()} trade look like:
A) ENTER — the move from PTB looks like real momentum that will continue or hold for the next {time_remaining_s/60:.0f} minutes
B) SKIP — the move looks like a spike about to reverse, or the setup is too uncertain

Consider: is the move sustained across multiple candles, or just one sharp spike? Is volume confirming? Is BTC near VWAP or extended? Is momentum building or fading?

Respond in exactly this format (3 lines, nothing else):
DECISION: ENTER or SKIP
CONFIDENCE: 0.0 to 1.0
REASONING: one sentence max"""

    try:
        from google import genai as google_genai
        gclient = google_genai.Client(api_key=settings.GEMINI_API_KEY)
        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: gclient.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                ),
            ),
            timeout=12.0,
        )
        text = response.text.strip()
        lines = [l.strip() for l in text.split("\n") if l.strip()]

        decision = "ENTER"
        confidence = 0.5
        reasoning = "no response"

        for line in lines:
            if line.startswith("DECISION:"):
                decision = "ENTER" if "ENTER" in line.upper() else "SKIP"
            elif line.startswith("CONFIDENCE:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("REASONING:"):
                reasoning = line.split(":", 1)[1].strip()

        skip_entry = decision == "SKIP" or confidence < 0.55

        logger.info(
            f"AI predictor [{slug}] {direction.upper()}: {decision} "
            f"conf={confidence:.2f} — {reasoning}"
        )

        return AIPrediction(
            direction=direction,
            confidence=confidence,
            reasoning=reasoning,
            skip_entry=skip_entry,
        )

    except asyncio.TimeoutError:
        logger.debug("AI predictor: Claude API timeout — passing through")
    except Exception as e:
        logger.debug(f"AI predictor: Claude API error — {e}")

    return AIPrediction(
        direction=direction,
        confidence=0.5,
        reasoning="AI unavailable — using momentum signal",
        skip_entry=False,
    )
